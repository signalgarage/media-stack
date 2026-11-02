#!/usr/bin/env python3
"""Re-download guard: cancel grabs of things the library already has.

Why: Radarr and Sonarr decide what is missing from their own database, not from
the disk. A file they cannot match (a release-named folder, a different episode
numbering, a library imported by hand) looks missing, so they download it again,
and again. This guard looks at what is really in the library folders and, when an
app is downloading something that is already there, cancels that download,
blocklists the release and rescans so the app adopts the copy you have.

Rules it never breaks:
  * It never deletes or moves a library file; it only removes the redundant download.
  * "Owned" means a matching video file in a library folder (settings.library_roots()),
    never in the downloads folder: a half-finished download is not a copy you have.
  * A season pack is cancelled only when EVERY episode it carries is owned: removing one
    queue row removes the whole torrent, and a mixed pack still delivers episodes you need.
  * The only exemption is the episode ids in .shrink_pending.json (the optional space-saver
    replacing a file with a smaller copy on purpose).
  * With ALLOW_UPGRADES=true, a grab that upgrades a file the app itself rates below the
    profile cutoff is left alone; cancelling it loops (cancel, blocklist, search again,
    grab the next release, ...) and burns good releases.
  * A missing or unmounted media folder stops the run: an absent share must never look
    like an empty library.

It also clears two dead ends that are safe to resolve automatically:
  * Radarr "owned but missing": rescan, and when the file sits in another folder inside a
    Radarr root folder, point the movie at that folder (moveFiles=false: nothing on disk moves).
  * Imports blocked only because the release name differs from the title the app grabbed it
    for: finished with the id the app itself recorded, and only when the download holds
    exactly one main video file. Packs stay with a human.

The library index (.libindex.json in the state folder) is rebuilt at most about once an
hour, because walking a big library over a network share every run is slow; a match is
always re-checked on disk before anything is cancelled.

Runs every 10 minutes from cron.
Flags: --dry-run (report, change nothing), --rebuild-index (ignore the cached index).
Exit codes: 0 ran (fixes and open issues go to Telegram), 2 could not reach Radarr or Sonarr.
"""
from __future__ import annotations

import argparse
import html
import http.client
import os
import re
import socket
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "regrab_guard"
P = "[regrab_guard]"

INDEX_FILE = ".libindex.json"
INDEX_VERSION = 1
INDEX_MAX_AGE_S = 3300        # just under hourly: a full walk of a big share every 10 minutes is too slow
STATE_FILE = ".regrab_guard_state.json"
SHRINK_PENDING_FILE = ".shrink_pending.json"   # written by shrink_oversized.py
QUEUE_PAGE_SIZE = 500         # read the WHOLE queue; a single fixed page silently missed rows once it grew
MAX_QUEUE_PAGES = 40
DELETE_TIMEOUT_S = 120        # removing a big torrent is slow; the app usually applies it even when the call times out
SETTLE_S = 75                 # Radarr needs about a minute to finish a rescan before a re-check means anything
RESCAN_RETRY_S = 6 * 3600     # do not rescan the same "owned but missing" movie every 10 minutes
MIN_MOVIE_BYTES = 300 * 1024 ** 2   # smaller videos in a download are samples or extras, never the movie
ACTED_LINES = 10              # alert length caps: enough to understand, short enough to read on a phone
ISSUE_LINES = 8

STOPWORDS = frozenset({"the", "a", "an", "of", "and", "part", "in"})
EPISODE_RE = re.compile(r"[sS](\d{1,2})[ ._-]*[eE](\d{1,3})((?:[ ._-]*[eE]\d{1,3})*)")
ALT_EPISODE_RE = re.compile(r"(?<![0-9])(?<![0-9]\.)(\d{1,2})[xX](\d{2})(?![0-9])")   # 1x02; not 5.1x264 audio tags
SAMPLE_RE = re.compile(r"(?i)(?:^|[ ._-])sample(?:[ ._-]|$)")
SKIP_DIR_NAMES = frozenset({"sample", "samples"})

API_ERRORS = (settings.ApiError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


# ---------------------------------------------------------------- pure helpers (tested)

def tokens(s: str) -> set:
    """Title words for matching: lower case, no years, no stopwords; sequel digits are kept."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
        if re.fullmatch(r"(19|20)\d\d", w) or w in STOPWORDS:
            continue
        if len(w) >= 3 or w.isdigit():
            out.add(w)
    return out


def episodes_in_name(name: str) -> list:
    """[(season, episode), ...] named in a file name (S01E02, S01E02E03, S01E02-E03, 1x02)."""
    m = EPISODE_RE.search(name or "")
    if m:
        season = int(m.group(1))
        eps = [int(m.group(2))] + [int(x) for x in re.findall(r"[eE](\d{1,3})", m.group(3) or "")]
        return [(season, e) for e in eps]
    m = ALT_EPISODE_RE.search(name or "")
    if m:
        return [(int(m.group(1)), int(m.group(2)))]
    return []


def is_episode_name(name: str) -> bool:
    return bool(episodes_in_name(name))


def episode_key(season: int, episode: int) -> str:
    return "S%02dE%02d" % (int(season), int(episode))


def series_key(name: str) -> str:
    """Folder / title normalisation for series: lower case, letters and digits only.

    A leading "The" is kept on purpose: two different shows can differ only by it, and
    treating them as one would cancel a legitimate grab.
    """
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def movie_name_matches(title: str, year, file_name: str) -> bool:
    """True when every title word is in the file name and the file name carries the year."""
    tt = tokens(title)
    if not tt:
        return False
    yr = str(year or "")
    if yr and yr not in file_name:
        return False
    return tt.issubset(tokens(file_name))


def pack_cancellable(rows: list, cancellable: dict) -> bool:
    """A download may be removed only when every queue row it carries is cancellable."""
    return bool(rows) and all(cancellable.get(r.get("id")) for r in rows)


# ---------------------------------------------------------------- small utilities

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


def bullets(items: list, limit: int) -> str:
    out = "\n".join("• " + esc(x) for x in items[:limit])
    if len(items) > limit:
        out += "\n• ...and %d more" % (len(items) - limit)
    return out


def read_queue(app, **params) -> list:
    """Every queue record of an app (all pages)."""
    records, page = [], 1
    while page <= MAX_QUEUE_PAGES:
        q = dict(params, page=page, pageSize=QUEUE_PAGE_SIZE)
        data = settings.api(app, "queue", params=q)
        if not isinstance(data, dict):
            raise ValueError("unexpected queue reply")
        recs = data.get("records") or []
        records.extend(recs)
        total = int(data.get("totalRecords") or 0)
        if not recs or len(records) >= total:
            break
        page += 1
    return records


def command(app, body: dict, dry: bool) -> None:
    if dry:
        log("[dry-run] would run %s %s" % (app.name, body))
        return
    try:
        settings.api(app, "command", "POST", data=body, timeout=60)
    except API_ERRORS as e:
        log("%s command %s failed (%s)" % (app.name, body.get("name"), why(e)))


def delete_queue_row(app, row_id: int, dry: bool, remove: bool, blocklist: bool, skip_redownload: bool) -> bool:
    params = {"removeFromClient": str(remove).lower(), "blocklist": str(blocklist).lower()}
    if skip_redownload:
        params["skipRedownload"] = "true"   # the library already has it: never start a replacement search
    if dry:
        log("[dry-run] would remove %s queue row %d %s" % (app.name, row_id, params))
        return True
    try:
        settings.api(app, "queue/%d" % row_id, "DELETE", params=params, timeout=DELETE_TIMEOUT_S)
    except settings.ApiError as e:
        if e.status == 404:
            return True   # already gone
        log("%s refused to remove queue row %d (%s)" % (app.name, row_id, why(e)))
        return False
    except (OSError, http.client.HTTPException) as e:
        log("%s did not answer the removal of queue row %d in time (%s); it usually applies it anyway"
            % (app.name, row_id, why(e)))
    return True


# ---------------------------------------------------------------- library index

def _index_roots() -> list:
    return sorted(set(os.path.normpath(r) for r in settings.library_roots())) + [os.path.normpath(settings.DOWNLOADS)]


def _walk_videos(root: str, errors: list):
    skip = os.path.normpath(settings.DOWNLOADS)
    for d, dirs, files in os.walk(root, onerror=errors.append):
        # never descend into the downloads folder, even when it sits inside a library root
        dirs[:] = [x for x in dirs
                   if x.lower() not in SKIP_DIR_NAMES and os.path.normpath(os.path.join(d, x)) != skip]
        for f in files:
            if f.lower().endswith(settings.VIDEO_EXT) and not SAMPLE_RE.search(f):
                yield os.path.join(d, f)


def build_index():
    """Walk the library roots. Returns (index, number of folders that could not be read)."""
    errors: list = []
    movies = []
    seen_roots = set()
    for root in settings.movie_roots():
        root = os.path.normpath(root)
        if root in seen_roots:
            continue
        seen_roots.add(root)
        for path in _walk_videos(root, errors):
            if not is_episode_name(os.path.basename(path)):
                movies.append(path)
    tv: dict = {}
    tv_root = os.path.normpath(settings.TV)
    for path in _walk_videos(tv_root, errors):
        parts = os.path.relpath(path, tv_root).split(os.sep)
        if len(parts) < 2:
            continue   # a loose file in the TV root belongs to no series folder
        for season, episode in episodes_in_name(parts[-1]):
            tv.setdefault(parts[0], {}).setdefault(episode_key(season, episode), path)
    index = {"version": INDEX_VERSION, "built": int(time.time()), "roots": _index_roots(),
             "movies": sorted(movies), "tv": tv}
    return index, len(errors)


def load_index(force: bool, dry: bool) -> dict:
    path = settings.state_path(INDEX_FILE)
    idx = settings.load_json(path, {})
    age = time.time() - float((idx or {}).get("built") or 0) if isinstance(idx, dict) else -1
    if (not force and isinstance(idx, dict) and idx.get("version") == INDEX_VERSION
            and idx.get("roots") == _index_roots() and 0 <= age < INDEX_MAX_AGE_S):
        log("library index: cached %d min ago (%d movie files, %d series folders)"
            % (age // 60, len(idx.get("movies") or []), len(idx.get("tv") or {})))
        return idx
    t0 = time.time()
    idx, nerr = build_index()
    log("library index: rebuilt in %.0fs (%d movie files, %d series folders)"
        % (time.time() - t0, len(idx["movies"]), len(idx["tv"])))
    if nerr:
        log("%d folder(s) could not be read; this index is used for this run only" % nerr)
    elif not dry:
        try:
            settings.save_json(path, idx)
        except OSError as e:
            log("could not cache the library index (%s)" % e)
    return idx


class Library:
    """What is on disk in the library folders, from the index."""

    def __init__(self, idx: dict) -> None:
        self.movies = [(p, os.path.basename(p)) for p in (idx.get("movies") or [])]
        self.tv = idx.get("tv") or {}
        self.tv_by_key: dict = {}
        for folder in self.tv:
            self.tv_by_key.setdefault(series_key(folder), []).append(folder)

    def movie_files(self, title, year) -> list:
        """Library files of this movie that still exist, largest first."""
        if not tokens(title):
            return []
        hits = []
        for path, name in self.movies:
            if movie_name_matches(title, year, name):
                try:
                    hits.append((os.path.getsize(path), path))
                except OSError:
                    continue   # gone since the index was built
        return [p for _, p in sorted(hits, reverse=True)]

    def episode_file(self, series: dict, season, episode):
        """Library file of this episode that still exists, or None."""
        key = episode_key(season, episode)
        folders = []
        own = settings.to_host(series.get("path") or "")
        if own and os.path.normpath(os.path.dirname(own)) == os.path.normpath(settings.TV):
            folders.append(os.path.basename(own))
        for f in self.tv_by_key.get(series_key(series.get("title") or ""), []):
            if f not in folders:
                folders.append(f)
        for f in folders:
            path = (self.tv.get(f) or {}).get(key)
            if path and os.path.exists(path):
                return path
        return None


# ---------------------------------------------------------------- Radarr

def movie_label(m: dict) -> str:
    return "%s (%s)" % (m.get("title") or "?", m.get("year") or "?")


def resolve_blocked_movie(app, row: dict, movies: dict, dry: bool, acted: list) -> bool:
    """Finish an import Radarr blocked only because the release name differs from the movie.

    Radarr's own grab already tied the download to a movie id; that id is authoritative.
    Acts only when Radarr supplied the id and the download holds exactly one main video.
    """
    mid, folder = row.get("movieId"), row.get("outputPath")
    if not mid or not folder:
        return False
    mv = movies.get(mid) or {}
    if mv.get("hasFile"):
        # an earlier pass already imported it: clear the stale row, keep the torrent seeding
        if delete_queue_row(app, row["id"], dry, remove=False, blocklist=False, skip_redownload=False):
            acted.append("Cleared a finished import that was still listed: " + movie_label(mv))
            return True
        return False
    try:
        cands = settings.api(app, "manualimport", params={"folder": folder, "filterExistingFiles": "false"})
    except API_ERRORS as e:
        log("manual-import lookup failed for %s (%s)" % (movie_label(mv), why(e)))
        return False
    vids = [c for c in (cands if isinstance(cands, list) else [])
            if isinstance(c, dict) and (c.get("path") or "").lower().endswith(settings.VIDEO_EXT)
            and (c.get("size") or 0) >= MIN_MOVIE_BYTES and not SAMPLE_RE.search(os.path.basename(c["path"]))]
    if len(vids) != 1:
        return False   # nothing usable, or a pack with several movies: a human decides
    f = vids[0]
    body = {"name": "ManualImport", "importMode": "auto", "files": [{
        "path": f["path"], "movieId": mid, "quality": f.get("quality"), "languages": f.get("languages"),
        "releaseGroup": f.get("releaseGroup"), "downloadId": row.get("downloadId")}]}
    command(app, body, dry)
    acted.append("Finished an import Radarr had blocked (release name differed): " + movie_label(mv))
    return True


def repoint_movie(app, m: dict, file_path: str, roots: list, dry: bool, acted: list) -> None:
    """Point Radarr at the folder that holds the movie, when that folder is inside a Radarr root folder.

    Radarr identifies a movie by its configured folder; a rescan of the wrong folder can
    never find the file. moveFiles=false: nothing on disk is moved or renamed.
    """
    folder = settings.to_container(os.path.dirname(file_path))
    if not any(folder.startswith(r.rstrip("/") + "/") for r in roots):
        return   # only ever a folder strictly INSIDE a root folder, never a root itself
    if folder.rstrip("/") == (m.get("path") or "").rstrip("/"):
        return
    if dry:
        log("[dry-run] would point Radarr's %s at %s" % (movie_label(m), folder))
        return
    try:
        full = settings.api(app, "movie/%d" % m["id"])
        full["path"] = folder
        settings.api(app, "movie/%d" % m["id"], "PUT", data=full, params={"moveFiles": "false"})
        acted.append("Pointed Radarr at the folder that holds %s (no files moved)" % movie_label(m))
    except API_ERRORS as e:
        log("could not repoint %s (%s)" % (movie_label(m), why(e)))


def radarr_pass(lib: Library, state: dict, dry: bool, acted: list, residual: list) -> bool:
    app = settings.radarr()
    try:
        movies = {m["id"]: m for m in settings.api(app, "movie") if isinstance(m, dict) and "id" in m}
        queue = read_queue(app)
    except API_ERRORS as e:
        log("cannot reach radarr at %s (%s); movies skipped this run" % (app.url, why(e)))
        return False

    # 1) the queue: cancel re-downloads of owned movies; finish imports blocked on a name mismatch
    for r in queue:
        m = movies.get(r.get("movieId"))
        if m and settings.ALLOW_UPGRADES and m.get("hasFile") and (m.get("movieFile") or {}).get("qualityCutoffNotMet"):
            log("leaving %s alone: an upgrade of a file below the profile cutoff" % movie_label(m))
            continue
        if m and lib.movie_files(m.get("title"), m.get("year")):
            if delete_queue_row(app, r["id"], dry, remove=True, blocklist=True, skip_redownload=True):
                command(app, {"name": "RescanMovie", "movieId": m["id"]}, dry)
                acted.append("Cancelled a re-download of %s: already in the library" % movie_label(m))
        elif r.get("trackedDownloadState") == "importBlocked":
            if not resolve_blocked_movie(app, r, movies, dry, acted):
                residual.append("Radarr import needs a hand: " + (r.get("title") or "?")[:60])

    # 2) owned but flagged missing: repoint when needed, rescan, re-check after Radarr settles
    try:
        roots = [x.get("path") for x in settings.api(app, "rootfolder") if isinstance(x, dict) and x.get("path")]
    except API_ERRORS:
        roots = []
    false_missing = []
    for m in movies.values():
        if m.get("hasFile") or not m.get("monitored"):
            continue
        files = lib.movie_files(m.get("title"), m.get("year"))
        if files:
            false_missing.append((m, files[0]))
    tried = state.setdefault("rescanned", {})
    now = time.time()
    rescanned = set()
    for m, path in false_missing:
        if now - float(tried.get(str(m["id"]), 0)) < RESCAN_RETRY_S:
            continue
        repoint_movie(app, m, path, roots, dry, acted)
        command(app, {"name": "RescanMovie", "movieId": m["id"]}, dry)
        tried[str(m["id"])] = int(now)
        rescanned.add(m["id"])
    if rescanned and not dry:
        time.sleep(SETTLE_S)
    for m, _path in false_missing:
        if m["id"] in rescanned and not dry:
            try:
                if (settings.api(app, "movie/%d" % m["id"]) or {}).get("hasFile"):
                    continue
            except API_ERRORS:
                pass
        residual.append("Radarr does not see a movie that is in the library: " + movie_label(m))
    still = {str(m["id"]) for m, _ in false_missing}
    for k in [k for k in tried if k not in still]:
        del tried[k]
    return True


# ---------------------------------------------------------------- Sonarr

def shrink_pending() -> set:
    """Episode ids the space-saver is deliberately replacing (the only exemption)."""
    data = settings.load_json(settings.state_path(SHRINK_PENDING_FILE), {})
    ids = data.keys() if isinstance(data, dict) else (data if isinstance(data, list) else [])
    out = set()
    for k in ids:
        try:
            out.add(int(k))
        except (TypeError, ValueError):
            continue
    return out


def resolve_blocked_episode(app, row: dict, dry: bool, acted: list) -> bool:
    """Sonarr twin of resolve_blocked_movie: only when Sonarr identified the episode and the
    download is a single file (packs stay with a human: file-to-episode mapping is ambiguous)."""
    sid, epid, folder = row.get("seriesId"), row.get("episodeId"), row.get("outputPath")
    if not sid or not epid or not folder:
        return False
    try:
        ep = settings.api(app, "episode/%d" % epid) or {}
    except API_ERRORS:
        return False
    label = (row.get("title") or "?")[:60]
    if ep.get("hasFile"):
        if delete_queue_row(app, row["id"], dry, remove=False, blocklist=False, skip_redownload=False):
            acted.append("Cleared a finished import that was still listed: " + label)
            return True
        return False
    try:
        cands = settings.api(app, "manualimport", params={"folder": folder, "filterExistingFiles": "false"})
    except API_ERRORS:
        return False
    vids = [c for c in (cands if isinstance(cands, list) else [])
            if isinstance(c, dict) and (c.get("path") or "").lower().endswith(settings.VIDEO_EXT)
            and not SAMPLE_RE.search(os.path.basename(c["path"]))]
    if len(vids) != 1:
        return False
    f = vids[0]
    body = {"name": "ManualImport", "importMode": "auto", "files": [{
        "path": f["path"], "seriesId": sid, "episodeIds": [epid], "quality": f.get("quality"),
        "languages": f.get("languages"), "releaseGroup": f.get("releaseGroup"), "downloadId": row.get("downloadId")}]}
    command(app, body, dry)
    acted.append("Finished an episode import Sonarr had blocked (release name differed): " + label)
    return True


def sonarr_pass(lib: Library, dry: bool, acted: list, residual: list) -> bool:
    app = settings.sonarr()
    try:
        rows = read_queue(app, includeSeries="true", includeEpisode="true")
    except API_ERRORS as e:
        log("cannot reach sonarr at %s (%s); episodes skipped this run" % (app.url, why(e)))
        return False
    pending = shrink_pending()
    upgrade_cache: dict = {}

    def is_upgrade(ep: dict) -> bool:
        fid = ep.get("episodeFileId")
        if not ep.get("hasFile") or not fid:
            return False
        if fid not in upgrade_cache:
            try:
                upgrade_cache[fid] = bool((settings.api(app, "episodefile/%d" % fid) or {}).get("qualityCutoffNotMet"))
            except API_ERRORS:
                upgrade_cache[fid] = True   # cannot tell: do not cancel
        return upgrade_cache[fid]

    info, cancellable = {}, {}
    for r in rows:
        series = r.get("series") or {}
        ep = r.get("episode") or {}
        sn = r.get("seasonNumber") if r.get("seasonNumber") is not None else ep.get("seasonNumber")
        en = ep.get("episodeNumber") if ep.get("episodeNumber") is not None else r.get("episodeNumber")
        owned = None
        if series and sn is not None and en is not None:
            owned = lib.episode_file(series, sn, en)
        exempt = ""
        if owned and r.get("episodeId") in pending:
            exempt = "a deliberate smaller-copy replacement"
        elif owned and settings.ALLOW_UPGRADES and is_upgrade(ep):
            exempt = "an upgrade of a file below the profile cutoff"
        info[r["id"]] = (owned, exempt, series.get("title") or "?", sn, en)
        cancellable[r["id"]] = bool(owned) and not exempt

    groups: dict = {}
    for r in rows:
        groups.setdefault(r.get("downloadId") or "row%d" % r["id"], []).append(r)

    killed = set()
    for r in rows:
        owned, exempt, title, sn, en = info[r["id"]]
        if owned:
            if exempt:
                log("leaving %s S%02dE%02d alone: %s" % (title, sn, en, exempt))
                continue
            key = r.get("downloadId") or "row%d" % r["id"]
            sibs = groups.get(key, [r])
            if key in killed or not pack_cancellable(sibs, cancellable):
                continue   # already removed with an earlier row, or a mixed pack that still brings episodes we need
            killed.add(key)
            if delete_queue_row(app, r["id"], dry, remove=True, blocklist=True, skip_redownload=True):
                if r.get("seriesId"):
                    command(app, {"name": "RescanSeries", "seriesId": r["seriesId"]}, dry)
                extra = "" if len(sibs) == 1 else " (+%d more in the same pack, all already owned)" % (len(sibs) - 1)
                acted.append("Cancelled a re-download of %s S%02dE%02d%s" % (title, sn, en, extra))
        elif r.get("trackedDownloadState") == "importBlocked":
            if not resolve_blocked_episode(app, r, dry, acted):
                residual.append("Sonarr import needs a hand: " + (r.get("title") or "?")[:60])
    return True


# ---------------------------------------------------------------- main

def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="regrab_guard.py", description="Cancel grabs of things the library already has.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="report only, change nothing")
    ap.add_argument("--rebuild-index", action="store_true", help="ignore the cached library index")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()

    if not dry:
        settings.heartbeat(JOB)
        lock = alertstate.acquire_lock()
        if lock is None:
            log("another guard holds the lock - skipping this run")
            return 0
    if not alertstate.mount_canary(JOB, settings.library_roots(), dry_run=dry):
        return 0

    state_path = settings.state_path(STATE_FILE)
    state = settings.load_json(state_path, {})
    lib = Library(load_index(args.rebuild_index, dry))
    acted: list = []
    residual: list = []
    ok_r = radarr_pass(lib, state, dry, acted, residual)
    ok_s = sonarr_pass(lib, dry, acted, residual)
    if not dry:
        try:
            settings.save_json(state_path, state)
        except OSError as e:
            log("could not save state (%s)" % e)
    if not ok_r and not ok_s:
        return 2

    new_r, old_r = alertstate.filter_new("regrab-residual", residual, dry_run=dry)
    digest = bool(old_r) and alertstate.digest_due("regrab-digest", dry_run=dry)
    if new_r and not old_r:
        alertstate.digest_due("regrab-digest", dry_run=dry)   # today's summary is this alert itself
    if acted or new_r or digest:
        parts = []
        if acted:
            parts.append("Fixed automatically:\n" + bullets(acted, ACTED_LINES))
        if new_r:
            parts.append("Needs your attention:\n" + bullets(new_r, ISSUE_LINES))
        if digest:
            parts.append("Still open: %d issue(s) reported earlier." % len(old_r))
        msg = "Re-download guard\n\n" + "\n\n".join(parts)
        if dry:
            print("[dry-run] would send:\n" + msg, flush=True)
        else:
            settings.notify(msg)
            print(msg, flush=True)
    else:
        log("all clear" + (" (%d known issue(s) still open)" % len(old_r) if old_r else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
