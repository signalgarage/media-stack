#!/usr/bin/env python3
"""integrity_scan: rolling read-only health check of the movie and TV libraries.

Why it exists: files rot, imports go wrong, and network shares occasionally hand back
garbage. Nothing checks a library on an ongoing basis, so a broken file is usually found when
someone sits down to watch it. Without parity or a backup of the media, the only real defence
is finding damage EARLY, while the title can still be downloaded again.

Each run ffprobes every file that is due and records the result; every file is re-checked
weekly, and at once when its size or modification time changes (an upgrade or re-import).
ffprobe reads headers rather than whole files, so the I/O cost is small (about a quarter of a
second per file over a network share), which matters because saturating a share is what
makes playback stutter. The first run covers the whole library; later runs handle roughly a
seventh of it in a few minutes. A tiny pause between files lets playback win any contention.

Scope: settings.library_roots() (the movie roots, the extra movie libraries and TV), minus
INTEGRITY_EXCLUDE (comma separated: absolute paths, paths relative to MEDIA_ROOT, or bare
folder names matched anywhere). The downloads and backup folders, hidden folders, NAS system
folders and extras/sample folders are never scanned: half-written downloads and short
trailers are not corruption. Everything here is READ-ONLY: it probes and reports, and never
moves, renames or deletes anything.

Runs daily from cron. Broken files are announced once each (alertstate), not every day.
Flags: --dry-run  scan and print; send nothing, keep the state file unchanged.
Exit:  0 normal (broken files are reported by message), 2 the guard itself failed.
"""
from __future__ import annotations

import argparse
import html
import os
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "integrity_scan"
TAG = "[integrity]"
STATE_NAME = ".integrity_state.json"

BUDGET = 4000            # files per run: effectively "everything currently due"
RECHECK_DAYS = 7         # the whole library is verified every week
GAP_S = 0.05             # token pause between files so playback always wins contention
TIME_BUDGET_S = 2400     # hard stop after 40 minutes, whatever happens; the rest rolls over
MAX_LIST = 10            # files listed per alert

# Only real feature/episode containers. Disc structures (.vob/.m2ts menus) and transport-stream
# clips are full of legitimately tiny files that would read as "truncated".
SCAN_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv")
# NAS/OS system folders and media-server extras folders (short clips by design).
SKIP_DIR_NAMES = {"@eadir", "#recycle", "#snapshot", ".snapshot", "lost+found", "$recycle.bin",
                  "sample", "samples", "extras", "featurettes", "trailers", "behind the scenes",
                  "deleted scenes", "interviews", "scenes", "shorts", "other"}


class Excluder:
    """INTEGRITY_EXCLUDE entries plus the folders that are never part of the library."""

    def __init__(self, entries):
        self.prefixes = [os.path.normpath(settings.DOWNLOADS), os.path.normpath(settings.BACKUP_DIR)]
        self.names = set()
        for e in entries:
            e = e.strip()
            if not e:
                continue
            if os.path.isabs(e):
                self.prefixes.append(os.path.normpath(e))
            else:
                self.prefixes.append(os.path.normpath(os.path.join(settings.MEDIA_ROOT, e.strip("/"))))
                if "/" not in e.strip("/"):
                    self.names.add(e.strip("/").casefold())

    def path(self, p: str) -> bool:
        p = os.path.normpath(p)
        return any(p == x or p.startswith(x.rstrip(os.sep) + os.sep) for x in self.prefixes)

    def dirname(self, parent: str, d: str) -> bool:
        low = d.casefold()
        return (d.startswith(".") or low in SKIP_DIR_NAMES or low in self.names
                or self.path(os.path.join(parent, d)))


def is_sample(name: str) -> bool:
    stem = os.path.splitext(name)[0].casefold()
    return stem == "sample" or stem.endswith(("-sample", ".sample", "_sample"))


def human(detail: str) -> str:
    """settings.ffprobe's detail in plain words, without any file path."""
    d = (detail or "").strip()
    if d == "ffprobe timed out":
        return "timed out reading the file"
    if d == "no duration readable":
        return "no duration in the file header"
    if d.startswith("suspicious duration"):
        return "only %s long; likely truncated" % d.replace("suspicious duration", "").strip()
    if ": " in d:
        d = d.rsplit(": ", 1)[1]            # ffprobe prefixes errors with the file path
    return d[:100] or "unreadable"


def _media_missing() -> str:
    for p in [settings.MEDIA_ROOT] + settings.get_list("MOUNT_PATHS"):
        if not os.path.isdir(p):
            return p
    return ""


def _send(text: str, dry: bool) -> None:
    if dry:
        settings.log(TAG, "[dry-run] would send: " + text.replace("\n", " | "))
    else:
        settings.notify(text)


def collect(roots: list, ex: Excluder):
    """(files, walked): video files in scope, and the roots that were walked without errors."""
    files, walked = [], []
    for root in roots:
        root = os.path.normpath(root)
        if ex.path(root) or os.path.basename(root).casefold() in ex.names:
            settings.log(TAG, "excluded: %s" % root)
            continue
        if not os.path.isdir(root):
            settings.log(TAG, "library folder not found, skipped this run: %s" % root)
            continue
        errs = []
        for dp, dirs, names in os.walk(root, onerror=errs.append):
            dirs[:] = sorted(d for d in dirs if not ex.dirname(dp, d))
            for n in names:
                if n.lower().endswith(SCAN_EXT) and not is_sample(n):
                    p = os.path.join(dp, n)
                    if not ex.path(p):
                        files.append(p)
        if errs:
            settings.log(TAG, "%d folder(s) under %s could not be read this run" % (len(errs), root))
        else:
            walked.append(root)
    return files, walked


def run(dry: bool) -> int:
    missing = _media_missing()
    if missing:
        # A missing share looks exactly like an empty library: never read that as "all clean".
        settings.log(TAG, "media folder missing or not mounted (%s); standing down" % missing)
        if not dry and alertstate.digest_due("media-root-missing"):
            settings.notify("Media folder missing\n\nThe media folder is not available, so the guard "
                            "scripts that read or change media are standing down until it is back. "
                            "mount_guard reports the details.")
        return 0

    ex = Excluder(settings.get_list("INTEGRITY_EXCLUDE"))
    files, walked = collect(settings.library_roots(), ex)

    def in_walked(p: str) -> bool:
        return any(p.startswith(r.rstrip(os.sep) + os.sep) for r in walked)

    st_path = settings.state_path(STATE_NAME)
    st = settings.load_json(st_path, {})
    known = set(files)
    # Forget files that are gone, so the state cannot grow forever; but only under a root that
    # was walked completely (an unreadable folder must not wipe its history).
    for gone in [k for k in st if k not in known and in_walked(k)]:
        del st[gone]

    now = time.time()
    due = []
    for p in files:
        try:
            s = os.stat(p)
        except OSError:
            continue
        rec = st.get(p) or {}
        changed = rec and (rec.get("s") != s.st_size or rec.get("m") != int(s.st_mtime))
        if not rec or changed or now - rec.get("t", 0) > RECHECK_DAYS * 86400:
            due.append((0 if (not rec or changed) else rec.get("t", 0), p, s.st_size, int(s.st_mtime)))
    due.sort()                                  # never-checked and changed first, then oldest
    batch = due[:BUDGET]
    settings.log(TAG, "%d files in scope, %d due, checking %d" % (len(files), len(due), len(batch)))

    if batch and not settings.ffprobe_available():
        settings.log(TAG, "no ffprobe available: install ffmpeg on the host (recommended), enable the "
                          "subtitles profile (Bazarr ships ffprobe), or pull %s once by hand. Nothing checked."
                     % settings.FFMPEG_IMAGE)
        if not dry:
            new, _ = alertstate.filter_new("integrity_scan_noprobe", ["noprobe"])
            if new:
                settings.notify("Library check cannot run\n\nNo ffprobe is available on the server. Install "
                                "ffmpeg on the host (recommended), or enable the subtitles profile, whose "
                                "Bazarr container includes it. Nothing is ever pulled automatically.")
        return 0
    if not dry:
        alertstate.filter_new("integrity_scan_noprobe", [])

    checked, started = 0, time.time()
    for _t, p, size, mtime in batch:
        if time.time() - started > TIME_BUDGET_S:
            settings.log(TAG, "time budget reached; the remaining files roll into the next run")
            break
        ok, detail = settings.ffprobe(p)
        checked += 1
        st[p] = {"t": now, "ok": bool(ok), "d": detail, "s": size, "m": mtime}
        if not ok:
            settings.log(TAG, "BROKEN: %s (%s)" % (p, detail))
        time.sleep(GAP_S)

    if not dry:
        try:
            settings.save_json(st_path, st)
        except OSError as e:
            settings.log(TAG, "could not save state: %s" % e)

    good = sum(1 for p in known if (st.get(p) or {}).get("ok"))
    broken = sorted(p for p, rec in st.items() if not rec.get("ok") and (p in known or not in_walked(p)))
    settings.log(TAG, "checked %d this run; %d/%d verified good overall; %d broken"
                 % (checked, good, len(files), len(broken)))

    # Every currently-broken file is passed in, so a known one is announced once, not weekly.
    keys = ["integrity:" + p for p in broken]
    new = set(keys if dry else alertstate.filter_new("integrity_scan", keys)[0])
    fresh = [p for p in broken if "integrity:" + p in new]
    if fresh:
        body = ("Damaged files found\n\nThe library check could not read %d file%s:\n\n"
                % (len(fresh), "" if len(fresh) == 1 else "s"))
        for p in fresh[:MAX_LIST]:
            rec = st.get(p) or {}
            body += "• %s\n   %s (%.2f GB)\n" % (html.escape(os.path.basename(p), quote=False),
                                                 html.escape(human(rec.get("d", "")), quote=False),
                                                 (rec.get("s") or 0) / 1e9)
        if len(fresh) > MAX_LIST:
            body += "• ...and %d more\n" % (len(fresh) - MAX_LIST)
        body += ("\nNothing has been changed or deleted; this is a read-only check. A damaged file can "
                 "usually be fixed by deleting it and letting it download again, which is worth doing "
                 "while it is still available.")
        _send(body, dry)
    return 0


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Rolling read-only ffprobe check of the libraries.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="scan and print only; send nothing, keep the state file")
    args = p.parse_args(argv)
    dry = args.dry_run or settings.get_bool("DRY_RUN")
    if not dry:
        settings.heartbeat(JOB)
    try:
        return run(dry)
    except Exception as e:
        settings.log(TAG, "internal error: %s: %s" % (type(e).__name__, e))
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
