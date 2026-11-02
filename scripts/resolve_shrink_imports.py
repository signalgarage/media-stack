#!/usr/bin/env python3
"""resolve_shrink_imports: land the smaller replacements Sonarr refuses as "Not an upgrade".

WARNING: this belongs to the space-saver, which RE-DOWNLOADS episodes you already own. It is
OFF by default (SHRINK_ENABLED=false) and most people should leave it off. Enable it only
together with shrink_oversized.py.

Why it exists: a replacement grabbed by shrink_oversized is smaller and often scores lower than
the file it replaces, so Sonarr parks it at importPending with "Not an upgrade" (or "Not a
Custom Format upgrade" / "Not a quality revision upgrade"). This script decides each such
queue row on the FILES, never on any state file:
    downloaded file meaningfully SMALLER than the library's -> a shrink win -> force-import it
    downloaded file NOT smaller                               -> redundant -> remove + blocklist
    either size unknown or ambiguous                          -> DO NOTHING, and say so
Uncertainty must never resolve to deletion. (An earlier design decided by .shrink_pending.json:
"not in pending -> unwanted -> delete". That list is pruned as swaps land, so good completed
replacements were read as unwanted and thrown away.)

ManualImport is what gets past the "Not an upgrade" refusal. It is safe here because the
replacement is already downloaded and on disk: nothing is removed before its successor exists.

Further rules
 - A row whose episode is on .shrink_keep.json is never imported, removed or blocklisted, only
   logged. If that file exists but cannot be read as a JSON object, the script exits 1 BEFORE
   any Sonarr or qBittorrent call (fail closed).
 - A release with a negative custom-format score, or one tagged with another language than
   PREFERRED_LANGUAGE (or the show's own), is removed with blocklist.
 - For a show in the preferred language, the FIRST audio track of the downloaded file must be
   in that language (read from the file with ffprobe: a release name can hide a multi-language
   file whose default track is another language). Unreadable or "und" -> left for a human.
 - Season packs are never removed for one episode: that would delete data other episodes
   still need. They are left for a human.
 - An import MOVES the file, so its torrent can never seed again and sits in qBittorrent as
   "missingFiles". Only torrents THIS script moved (recorded in .shrink_moved.json) and that
   Sonarr's history shows as imported are tidied, and only the torrent ENTRY is removed, never
   data. Sonarr imports everything else by hardlink, so after a share outage dozens of healthy
   torrents can show missingFiles at once: when 5 or more are missing together the tidy-up
   stands down (a share is down, not a finished swap).

Runs hourly from cron when enabled; holds the shared downloads lock.
Flags: --dry-run  decide and print; import nothing, remove nothing, write nothing.
Exit:  0 normal (also when disabled), 1 a Sonarr call failed or the keep-list is unreadable,
       2 the script itself failed.
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "resolve_shrink_imports"
TAG = "[shrink_resolve]"
DISABLED_WARNING = ("resolve_shrink_imports is OFF (SHRINK_ENABLED is not true). It belongs to the "
                    "space-saver that re-downloads episodes you already own; most people should leave "
                    "it off. Nothing was done.")

MAX_ROWS = 30            # queue rows decided per run
SMALLER_THAN = 0.85      # at least 15 % smaller to count as a shrink win (same as shrink_oversized)
MOVED_TTL_DAYS = 30      # forget moved-download records after a month
MISSING_FILES_OUTAGE = 5 # this many missingFiles torrents at once = a share outage (as arr_health_guard)
IMPORT_GAP_S = 4         # let Sonarr finish one import before the next
HISTORY_PAGE = 2000
API_TIMEOUT = 180
PROBE_TIMEOUT = 180
VIDEO_SUFFIXES = (".mkv", ".mp4", ".avi", ".m4v")
NOT_UPGRADE = re.compile(r"not an? (custom format |quality revision )?upgrade", re.I)
LANG_TAGS = {"italian": ("ITA", "iTALiAN"), "french": ("TRUEFRENCH", "FRENCH"), "german": ("GERMAN",),
             "spanish": ("SPANISH", "LATINO"), "russian": ("RUS",), "hindi": ("HINDI",)}
ALWAYS_FOREIGN_TAGS = ("VOSTFR", "DUAL")   # subtitled original / dual audio: either may default wrong
# ISO 639-2 codes (bibliographic and terminology forms) for the ISO 639-1 codes settings knows.
ISO639_2 = {
    "en": ("eng",), "fr": ("fre", "fra"), "es": ("spa",), "de": ("ger", "deu"), "it": ("ita",),
    "pt": ("por",), "nl": ("dut", "nld"), "sv": ("swe",), "da": ("dan",), "no": ("nor", "nob", "nno"),
    "fi": ("fin",), "pl": ("pol",), "cs": ("cze", "ces"), "hu": ("hun",), "ro": ("rum", "ron"),
    "el": ("gre", "ell"), "tr": ("tur",), "ru": ("rus",), "uk": ("ukr",), "ar": ("ara",), "he": ("heb",),
    "fa": ("per", "fas"), "hi": ("hin",), "pa": ("pan",), "bn": ("ben",), "ta": ("tam",), "te": ("tel",),
    "ml": ("mal",), "mr": ("mar",), "ur": ("urd",), "th": ("tha",), "vi": ("vie",), "id": ("ind",),
    "ms": ("may", "msa"), "tl": ("tgl", "fil"), "zh": ("chi", "zho"), "ja": ("jpn",), "ko": ("kor",),
    "bg": ("bul",), "hr": ("hrv",), "sr": ("srp",), "sk": ("slo", "slk"), "sl": ("slv",), "lt": ("lit",),
    "lv": ("lav",), "et": ("est",), "is": ("ice", "isl"), "ca": ("cat",),
}


def preferred() -> str:
    return settings.PREFERRED_LANGUAGE or "English"


def preferred_audio_codes():
    """Tags a first audio track may carry for the preferred language ("" = untagged, which is
    usually a single track in the release's original language). None when the preferred
    language is not in settings.LANGUAGE_NAMES: then the file cannot be judged."""
    code = settings.LANGUAGE_CODES.get(preferred().lower(), "")
    return ({"", code} | set(ISO639_2.get(code, ()))) if code else None


def foreign_tag_rx():
    pref = preferred().lower()
    tags = [t for lang, ts in LANG_TAGS.items() if lang != pref for t in ts] + list(ALWAYS_FOREIGN_TAGS)
    return re.compile(r"\b(%s)\b" % "|".join(tags), re.I)


def foreign_langs(langs, original=None) -> list:
    """Languages in a release that are neither the preferred one, Unknown, nor the show's own."""
    ok = {preferred().lower(), "unknown", "original", (original or "").lower(), ""}
    names = {((x or {}).get("name") or "") for x in (langs or [])}
    return sorted(n for n in names if n.lower() not in ok)


def is_preferred(language) -> bool:
    """True for a show in the preferred language (or one whose language Sonarr does not know)."""
    return (language or "").lower() in ("", preferred().lower())


def load_keep(path: str):
    """Same rule as shrink_oversized: no file -> empty set; a file that is not a JSON object ->
    None (the caller must refuse to run); otherwise the set of str(episodeId)."""
    if not os.path.exists(path):
        return set()
    keep = settings.load_json(path, None)
    if not isinstance(keep, dict):
        return None
    return {str(k) for k in keep}


def _media_missing() -> str:
    for p in [settings.MEDIA_ROOT] + settings.get_list("MOUNT_PATHS"):
        if not os.path.isdir(p):
            return p
    return ""


def first_audio_lang(cpath: str):
    """Language tag of the FIRST audio track of a downloaded file (container path), read from
    the file itself; None when it cannot be read, "" when the track has no language tag.
    Same probe order as settings.ffprobe: host ffprobe, Bazarr's, then a local ffmpeg image
    (never pulled)."""
    args = ["-v", "error", "-select_streams", "a", "-show_entries", "stream_tags=language", "-of", "csv=p=0"]
    try:
        if shutil.which("ffprobe"):
            cmd = ["ffprobe"] + args + [settings.to_host(cpath)]
        elif settings.container_running("bazarr"):
            cmd = settings.docker_cmd() + ["exec", "bazarr", "ffprobe"] + args + [cpath]
        elif settings.image_present(settings.FFMPEG_IMAGE):
            cmd = settings.docker_cmd() + ["run", "--rm", "--pull", "never", "-v",
                                           "%s:%s:ro" % (settings.MEDIA_ROOT, settings.CONTAINER_DATA),
                                           "--entrypoint", "ffprobe", settings.FFMPEG_IMAGE] + args + [cpath]
        else:
            return None
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=PROBE_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0:
        return None
    langs = [x.strip().lower() for x in r.stdout.splitlines()]
    return langs[0] if langs else None


class Resolver:
    def __init__(self, dry: bool):
        self.dry = dry
        self.app = settings.sonarr()
        self.moved_path = settings.state_path(".shrink_moved.json")

    def api(self, endpoint: str, method: str = "GET", data=None, params=None):
        return settings.api(self.app, endpoint, method=method, data=data, params=params, timeout=API_TIMEOUT)

    def remove(self, row: dict) -> None:
        # blocklist: otherwise the same release can be grabbed again tomorrow
        if not self.dry:
            self.api("queue/%d" % row["id"], method="DELETE", params={"removeFromClient": "true", "blocklist": "true"})

    def load_moved(self) -> dict:
        return settings.load_json(self.moved_path, {})

    def save_moved(self, moved: dict) -> None:
        if not self.dry:
            settings.save_json(self.moved_path, moved)

    def tidy_moved_torrents(self) -> int:
        """Remove the dead torrent ENTRIES of downloads this script moved (never their data)."""
        try:
            cutoff = time.time() - MOVED_TTL_DAYS * 86400
            moved = {h: t for h, t in self.load_moved().items() if t > cutoff}
            tors = settings.qbit("torrents/info?filter=errored", timeout=60)
            miss = [t for t in (tors if isinstance(tors, list) else []) if t.get("state") == "missingFiles"]
            if len(miss) >= MISSING_FILES_OUTAGE:
                settings.log(TAG, "tidy skipped: %d torrents are missing files at once; a share is "
                                  "probably down" % len(miss))
                return 0
            gone = [t for t in miss if t["hash"].lower() in moved]
            if gone:
                hist = self.api("history", params={"pageSize": HISTORY_PAGE, "sortKey": "date",
                                                   "sortDirection": "descending", "eventType": 3}) or {}
                imported = {(h.get("downloadId") or "").lower() for h in hist.get("records", [])}
                gone = [t for t in gone if t["hash"].lower() in imported]
            if gone and not self.dry:
                settings.qbit("torrents/delete", {"hashes": "|".join(t["hash"] for t in gone), "deleteFiles": "false"},
                              timeout=60)
                for t in gone:
                    moved.pop(t["hash"].lower(), None)
            for t in gone[:10]:
                settings.log(TAG, "%stidy: removed the finished torrent entry (data kept): %s"
                             % ("[dry-run] would " if self.dry else "", t.get("name", "")[:60]))
            self.save_moved(moved)
            return len(gone)
        except Exception as e:
            settings.log(TAG, "tidy skipped: %s" % e)
            return 0

    def decide(self, rows: list, keep: set) -> int:
        per_dl = {}
        for r in rows:
            per_dl[r.get("downloadId")] = per_dl.get(r.get("downloadId"), 0) + 1
        imported, removed, skipped, failed, kept = [], [], [], [], []
        rx = foreign_tag_rx()
        audio_ok = preferred_audio_codes()

        for r in rows[:MAX_ROWS]:
            title = (r.get("title") or "")[:48]
            epid, sid, folder = r.get("episodeId"), r.get("seriesId"), r.get("outputPath")
            is_pack = per_dl.get(r.get("downloadId"), 1) > 1
            if epid is not None and str(epid) in keep:
                kept.append((title, epid))     # a human decides; never touched
                continue
            msgs = [m for sm in (r.get("statusMessages") or []) for m in (sm.get("messages") or [])]
            # Sonarr words the refusal several ways ("Not an upgrade", "Not a Custom Format
            # upgrade", "Not a quality revision upgrade"); matching only one strands the rest.
            if not any(NOT_UPGRADE.search(m) for m in msgs):
                continue                       # some other import problem: not this script's business
            if not (epid and sid and folder):
                skipped.append((title, "incomplete queue row"))
                continue
            try:
                ep = self.api("episode/%d" % epid, params={"includeEpisodeFile": "true"}) or {}
                cur = (ep.get("episodeFile") or {}).get("size") or 0
            except Exception as e:
                skipped.append((title, "could not read the current file (%s)" % e))
                continue
            new = r.get("size") or 0
            try:
                orig = ((self.api("series/%d" % sid) or {}).get("originalLanguage") or {}).get("name")
            except Exception:
                orig = None

            # Never land a release the owner's own custom formats call unwanted, or a foreign or
            # multi-language one (its default audio may not be the preferred language). The
            # title tags only count for shows in the preferred language: a release tagged with
            # a show's own language is right for that show.
            if (r.get("customFormatScore") or 0) < 0 \
                    or (is_preferred(orig) and rx.search(r.get("title") or "")) \
                    or foreign_langs(r.get("languages"), orig):
                if is_pack:
                    skipped.append((title, "unwanted language/format but part of a pack; left for a human"))
                    continue
                try:
                    self.remove(r)
                    removed.append((title + " [unwanted language/format]", cur, new))
                except Exception as e:
                    failed.append("remove %s: %s" % (epid, e))
                continue

            if not cur or not new:
                skipped.append((title, "size unknown (have=%s new=%s)" % (cur, new)))
                continue                       # unknown sizes: never a reason to delete

            if new > cur * SMALLER_THAN:
                # Not smaller: genuinely redundant, Sonarr will never take it.
                if is_pack:
                    skipped.append((title, "not smaller, but part of a pack; left alone"))
                    continue
                try:
                    self.remove(r)
                    removed.append((title, cur, new))
                except Exception as e:
                    failed.append("remove %s: %s" % (epid, e))
                continue

            # Smaller: a shrink win. Force it past the upgrade check.
            try:
                cands = self.api("manualimport", params={"folder": folder, "filterExistingFiles": "false"}) or []
                vids = [c for c in cands if (c.get("path") or "").lower().endswith(VIDEO_SUFFIXES)]
                if len(vids) != 1:
                    skipped.append((title, "%d video candidates" % len(vids)))
                    continue
                f = vids[0]
                if is_preferred(orig):
                    if audio_ok is None:
                        skipped.append((title, "PREFERRED_LANGUAGE is not a known language name; left for a human"))
                        continue
                    a0 = first_audio_lang(f["path"])
                    if a0 is None or a0 == "und":
                        skipped.append((title, "audio language unreadable or untagged (%s); left for a human" % a0))
                        continue
                    if a0 not in audio_ok:
                        if is_pack:
                            skipped.append((title, "first audio track is %s, part of a pack; left for a human" % a0))
                            continue
                        self.remove(r)
                        removed.append((title + " [first audio %s]" % a0, cur, new))
                        continue
                if self.dry:
                    imported.append((title, cur, new))
                    continue
                self.api("command", method="POST", data={
                    "name": "ManualImport", "importMode": "move",
                    "files": [{"path": f["path"], "seriesId": sid, "episodeIds": [epid],
                               "quality": f.get("quality"), "languages": f.get("languages"),
                               "releaseGroup": f.get("releaseGroup"), "downloadId": r.get("downloadId")}],
                })
                imported.append((title, cur, new))
                # Remember it so tidy_moved_torrents may later remove its dead torrent entry.
                # Never a pack: moving one file breaks the torrent the other episodes still need.
                if r.get("downloadId") and not is_pack:
                    moved = self.load_moved()
                    moved[r["downloadId"].lower()] = time.time()
                    self.save_moved(moved)
                time.sleep(IMPORT_GAP_S)
            except Exception as e:
                failed.append("import %s: %s" % (epid, e))

        saved = sum(c - n for _t, c, n in imported)
        pre = "[dry-run] would have " if self.dry else ""
        settings.log(TAG, "%simported %d (~%.1f GB to reclaim) | removed %d | skipped %d | failed %d"
                     % (pre, len(imported), saved / 1e9, len(removed), len(skipped), len(failed)))
        for t, c, n in imported[:10]:
            settings.log(TAG, "  import  %-48s %.2f -> %.2f GB" % (t, c / 1e9, n / 1e9))
        for t, c, n in removed[:6]:
            settings.log(TAG, "  remove  %-48s have %.2f GB, offered %.2f GB" % (t, c / 1e9, n / 1e9))
        for t, why in skipped[:6]:
            settings.log(TAG, "  skip    %-48s %s" % (t, why))
        for x in failed[:5]:
            settings.log(TAG, "  FAIL    %s" % x)
        for t, e in kept[:20]:
            settings.log(TAG, "  keep    %-48s episode %s is on .shrink_keep.json; not touched" % (t, e))
        return 1 if failed else 0


def run(dry: bool) -> int:
    keep = load_keep(settings.state_path(".shrink_keep.json"))
    if keep is None:
        settings.log(TAG, "cannot read .shrink_keep.json; refusing to run rather than touch kept episodes")
        return 1                               # fail closed: nothing imported or removed, monitor turns red
    lock = alertstate.acquire_lock()
    if lock is None:
        settings.log(TAG, "another guard is working on the downloads; trying again next run")
        return 0
    try:
        missing = _media_missing()
        if missing:
            settings.log(TAG, "media folder missing or not mounted (%s); standing down" % missing)
            return 0
        rs = Resolver(dry)
        if not rs.app.key:
            settings.log(TAG, "no Sonarr API key found; standing down")
            return 1
        try:
            q = rs.api("queue", params={"pageSize": 500}) or {}
            rows = [r for r in q.get("records", []) if r.get("trackedDownloadState") == "importPending"]
        except Exception as e:
            settings.log(TAG, "cannot read the Sonarr queue: %s" % e)
            return 1                           # a genuine failure must not report success
        settings.log(TAG, "importPending rows: %d" % len(rows))
        rs.tidy_moved_torrents()
        return rs.decide(rows, keep)
    finally:
        if hasattr(lock, "close"):
            lock.close()


def main(argv) -> int:
    if not settings.get_bool("SHRINK_ENABLED"):
        print(DISABLED_WARNING, flush=True)
        return 0
    p = argparse.ArgumentParser(description="Land smaller shrink replacements that Sonarr calls 'Not an upgrade'.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="decide and print only; import nothing, remove nothing")
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
