#!/usr/bin/env python3
"""Check that every newly imported file actually plays.

Why: Radarr and Sonarr import on name and size alone. A zero-filled or truncated
"episode" can sit in the library for weeks before anyone presses play. This job
reads both apps' import history, runs ffprobe on each new file and alerts when
one is unreadable. It never deletes anything.

A failed probe is tried once more after a minute: a probe can race the import
while the file is still settling on a network share, and a healthy file must not
be reported as corrupt.

Optional self-heal (VERIFY_IMPORTS_HEAL=true, off by default): on some network
shares a freshly hardlinked library path cannot be opened while the same data
reads fine through its download-side name. When the library file is unreadable,
the download copy recorded in the history has the same size, reads and probes
clean, and there is room for a copy, the library file is rebuilt from the
download copy (written next to it, verified, then swapped in), the app rescans,
and the alert says "self-healed". On any doubt it does nothing and the normal
alert fires.

Runs at :07 and :37 every hour from cron. Flags: --dry-run.
Exit codes: 0 all new imports play, 1 a new import does not play,
2 neither Radarr nor Sonarr could be reached.
"""
from __future__ import annotations

import argparse
import html
import http.client
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "verify_imports"
P = "[verify_imports]"

STATE_FILE = ".verify_imports_state.json"
LOCK_FILE = ".verify_imports.lock"     # runs can be long (one minute per suspicious file); never overlap
HISTORY_PAGE_SIZE = 200               # newest history records per app and run; imports come in bursts during backfills
SEEN_KEEP = 5000                      # remembered history ids (far more than one history page)
MAX_PROBES = 100                      # per run; the rest are picked up by the next run
RECHECK_WAIT_S = 60                   # second opinion after a failed probe (file may still be settling)
IMPORT_EVENTS = frozenset({"downloadFolderImported", "episodeFileImported",
                           "movieFolderImported", "seriesFolderImported"})
ALERT_LINES = 6
HEAL_TMP_SUFFIX = ".heal-tmp"

API_ERRORS = (settings.ApiError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


def log(msg: str) -> None:
    settings.log(P, msg)


def why(e: Exception) -> str:
    if isinstance(e, settings.ApiError):
        return "HTTP %d%s" % (e.status, " (API key missing or wrong)" if e.status in (401, 403) else "")
    if isinstance(e, urllib.error.URLError):
        return "not reachable: %s" % (e.reason,)
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    return type(e).__name__


def esc(s) -> str:
    return html.escape(str(s), quote=False)


def is_within(path: str, roots: list) -> bool:
    p = os.path.normpath(path)
    return any(p.startswith(os.path.normpath(r) + os.sep) for r in roots)


class ProbeUnavailable(Exception):
    """ffprobe itself cannot run (no ffprobe on the host and no way to run it in a container)."""


def probe(path: str) -> tuple:
    """settings.ffprobe, but a probe that cannot RUN is never reported as a broken file."""
    try:
        ok, detail = settings.ffprobe(path)
    except (OSError, subprocess.SubprocessError) as e:
        raise ProbeUnavailable(why(e)) from None
    if not ok and str(detail).startswith("no ffprobe available"):
        raise ProbeUnavailable(str(detail))
    return ok, detail


def readable(path: str) -> bool:
    try:
        with open(path, "rb") as fh:
            fh.read(8)
        return True
    except OSError:
        return False


def load_seen(path: str) -> dict:
    """{history id: first seen (epoch)}; also reads the older {"ids": [...]} layout."""
    st = settings.load_json(path, {})
    if isinstance(st.get("seen"), dict):
        return {str(k): int(v) for k, v in st["seen"].items() if isinstance(v, (int, float))}
    if isinstance(st.get("ids"), list):
        return {str(k): 0 for k in st["ids"]}
    return {}


def prune_seen(seen: dict, keep: int = SEEN_KEEP) -> dict:
    """Keep the most recently seen ids (by when they were first seen, not by their spelling)."""
    if len(seen) <= keep:
        return seen
    return dict(sorted(seen.items(), key=lambda kv: kv[1])[-keep:])


def heal_enabled() -> bool:
    return settings.get_bool("VERIFY_IMPORTS_HEAL", False)


def try_heal(app, rec: dict, imported_c: str, dry: bool):
    """Rebuild an unreadable library file from its readable download copy. Returns the
    probe detail of the rebuilt file, or None when the preconditions do not all hold."""
    dropped_c = (rec.get("data") or {}).get("droppedPath") or ""
    if not dropped_c:
        return None
    ih, dh = settings.to_host(imported_c), settings.to_host(dropped_c)
    if not is_within(ih, settings.library_roots()) or not is_within(dh, [settings.DOWNLOADS]):
        return None   # only ever rewrite inside a library folder, only ever from the downloads folder
    try:
        dsize = os.path.getsize(dh)
        isize = os.stat(ih).st_size
    except OSError:
        return None
    if dsize != isize or dsize == 0:
        return None
    if readable(ih) or not readable(dh):
        return None   # the library file reads (a different problem) or the download does not
    try:
        ok, _ = probe(dh)
    except ProbeUnavailable:
        return None
    if not ok:
        return None
    min_free = settings.get_int("MIN_FREE_SPACE_MB", 25000) * 1024 * 1024
    try:
        free = shutil.disk_usage(os.path.dirname(ih)).free
    except OSError:
        return None
    if free < dsize + min_free:
        log("not self-healing %s: not enough free space for a copy" % os.path.basename(ih))
        return None
    if dry:
        log("[dry-run] would rebuild %s from its download copy" % ih)
        return None
    tmp = os.path.join(os.path.dirname(ih), "." + os.path.basename(ih) + HEAL_TMP_SUFFIX)
    try:
        shutil.copyfile(dh, tmp)
        ok, _ = probe(tmp)
        if not ok:
            raise OSError("the copy does not probe")
        os.replace(tmp, ih)   # the library name only changes once a verified copy exists
    except (OSError, ProbeUnavailable) as e:
        log("self-heal of %s failed (%s)" % (os.path.basename(ih), e))
        try:
            os.remove(tmp)    # our own temporary file, never a library file
        except OSError:
            pass
        return None
    try:
        ok, detail = probe(ih)
    except ProbeUnavailable:
        return None
    if not ok:
        return None
    body = None
    if app.name == "radarr" and rec.get("movieId"):
        body = {"name": "RescanMovie", "movieId": rec["movieId"]}
    elif app.name == "sonarr" and rec.get("seriesId"):
        body = {"name": "RescanSeries", "seriesId": rec["seriesId"]}
    if body:
        try:
            settings.api(app, "command", "POST", data=body, timeout=60)
        except API_ERRORS as e:
            log("rescan after self-heal failed (%s)" % why(e))
    return detail


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="verify_imports.py", description="ffprobe every newly imported file.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="probe and report, change nothing")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()

    if not dry:
        settings.heartbeat(JOB)
        lock = alertstate.acquire_lock(LOCK_FILE)
        if lock is None:
            log("the previous run is still going - skipping this run")
            return 0
    # a missing share would make every probe fail and look like corrupt imports
    if not alertstate.mount_canary(JOB, settings.library_roots(), dry_run=dry):
        return 0

    if not settings.ffprobe_available():
        log("cannot run ffprobe: install the ffmpeg package on this machine (recommended), enable the subtitles "
            "profile, or pull %s once by hand; imports stay unchecked until then" % settings.FFMPEG_IMAGE)
        return 2

    state_path = settings.state_path(STATE_FILE)
    seen = load_seen(state_path)
    bad, healed = [], []
    checked = 0
    reached = 0
    heal = heal_enabled()
    cannot_probe = ""
    for app in (settings.radarr(), settings.sonarr()):
        if cannot_probe:
            break
        label = app.name.capitalize()
        try:
            hist = settings.api(app, "history", params={"page": 1, "pageSize": HISTORY_PAGE_SIZE,
                                                        "sortKey": "date", "sortDirection": "descending"})
            records = (hist.get("records") or []) if isinstance(hist, dict) else []
        except API_ERRORS as e:
            log("cannot read %s history at %s (%s)" % (app.name, app.url, why(e)))
            continue
        reached += 1
        for rec in reversed(records):   # oldest first, so a capped run leaves the newest for next time
            if not isinstance(rec, dict) or rec.get("eventType") not in IMPORT_EVENTS:
                continue
            rid = "%s-%s" % (label, rec.get("id"))
            if rid in seen:
                continue
            if checked >= MAX_PROBES:
                break
            path = (rec.get("data") or {}).get("importedPath") or ""
            if not path:
                seen[rid] = int(time.time())
                continue
            host = settings.to_host(path)
            name = os.path.basename(host)[:70]
            try:
                os.stat(host)
            except FileNotFoundError:
                log("GONE  %s (replaced or removed since the import)" % name)
                seen[rid] = int(time.time())
                continue
            except OSError:
                pass   # exists but cannot be stat-ed: probe it, that is exactly what we are here for
            try:
                ok, detail = probe(host)
                if not ok:
                    if dry:
                        log("[dry-run] probe failed for %s (%s); a real run re-probes after %ds"
                            % (name, detail, RECHECK_WAIT_S))
                    else:
                        time.sleep(RECHECK_WAIT_S)
                        ok, detail = probe(host)
            except ProbeUnavailable as e:
                cannot_probe = str(e)
                break
            checked += 1
            seen[rid] = int(time.time())
            if ok:
                log("OK    %s %s" % (name, detail))
                continue
            fixed = try_heal(app, rec, path, dry) if heal else None
            if fixed:
                log("HEALED %s (rebuilt from the download copy, %s)" % (name, fixed))
                healed.append("%s (plays, %s)" % (name, fixed))
            else:
                log("BAD   %s %s" % (name, detail))
                bad.append("%s: %s (%s)" % (label, name, detail))

    if not dry:
        try:
            settings.save_json(state_path, {"seen": prune_seen(seen)})
        except OSError as e:
            log("could not save state (%s)" % e)
    if reached == 0:
        return 2
    if cannot_probe:
        log("cannot run ffprobe (%s): install ffprobe on the server or keep the stack's containers running; "
            "the unchecked imports are checked on the next run" % cannot_probe)

    new_bad, _ = alertstate.filter_new("verify-imports", bad, dry_run=dry)
    messages = []
    if new_bad:
        messages.append("Corrupt import found\n\nThese files were imported but do not play:\n"
                        + "\n".join("• " + esc(b) for b in new_bad[:ALERT_LINES])
                        + ("\n• ...and %d more" % (len(new_bad) - ALERT_LINES) if len(new_bad) > ALERT_LINES else "")
                        + "\n\nNothing was deleted. In the app, open History, mark the download as failed "
                          "so it is fetched again, or check the disk.")
    if healed:
        messages.append("Self-healed an unreadable import\n\n"
                        + "\n".join("• " + esc(h) for h in healed[:ALERT_LINES])
                        + "\n\nThe library copy could not be read while the download copy (same size) played "
                          "fine. The library file was rebuilt from the download copy and verified. No action needed.")
    for msg in messages:
        if dry:
            print("[dry-run] would send:\n" + msg, flush=True)
        else:
            settings.notify(msg)
    if checked or not cannot_probe:
        log("checked %d new import(s): %d bad, %d self-healed" % (checked, len(bad), len(healed))
            if checked else "no new imports")
    if cannot_probe:
        return 2
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
