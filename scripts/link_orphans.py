#!/usr/bin/env python3
"""File movies stranded in the downloads folder into the right library folder.

Why: a collection or pack download that Radarr imported only partly, or a movie
downloaded by hand, sits in the downloads folder where no media server looks.
This job finds those movies and hardlinks them into the library (the download
keeps seeding and no extra space is used).

What counts as a stranded movie: a video file of more than 300 MB under the
downloads folder, older than two hours, not part of an active Radarr download,
whose name carries a year and no episode number, and that is not already in a
library folder. "Already in the library" means the same file name, the same
title and year as a library folder that holds a video, or the very same file
(hardlinked earlier). Never the byte size alone: two different movies can have
exactly the same size.

Routing, safest first:
  1. Radarr already has the movie (title words + exact year, or the lookup says so):
     link into Radarr's own folder for it, then RescanMovie.
  2. Otherwise Radarr's movie lookup by title and year: link into
     "<Title> (Year)" in the library for the title's original language
     (MOVIE_LIBRARIES, else the default movies folder).
  3. No confident match: do nothing and say so. A wrong folder is worse than none.

It only ever creates folders and hardlinks: nothing is copied, moved or deleted.
A hardlink needs the downloads and the library on one filesystem; when they are
not, the job says so instead of copying.

Runs every 4 hours from cron. Flags: --dry-run.
Exit codes: 0 ran, 1 hardlinks are impossible in this setup (different filesystems),
2 Radarr could not be reached.
"""
from __future__ import annotations

import argparse
import errno
import html
import http.client
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "link_orphans"
P = "[link_orphans]"

MIN_MOVIE_BYTES = 300 * 1024 ** 2   # smaller files are samples, extras or fakes, never the movie itself
MIN_AGE_S = 2 * 3600                # newer files may still be downloading or waiting for the app's own import
LOOKUP_CANDIDATES = 4               # only the first few lookup results are close enough to trust
QUEUE_PAGE_SIZE = 500
MAX_QUEUE_PAGES = 40
PLEX_TIMEOUT_S = 15
FILED_LINES = 12                    # alert length caps
PROBLEM_LINES = 6

STOPWORDS = frozenset({"the", "a", "an", "of", "and", "part", "in"})
YEAR_RE = re.compile(r"(?<![0-9A-Za-z])((?:19|20)\d\d)(?![0-9A-Za-z])")
EPISODE_RE = re.compile(r"[sS]\d{1,2}[ ._-]*[eE]\d{1,3}|(?<![0-9])(?<![0-9]\.)\d{1,2}[xX]\d{2}(?![0-9])")
SAMPLE_RE = re.compile(r"(?i)(?:^|[ ._-])sample(?:[ ._-]|$)")
SKIP_DIR_NAMES = frozenset({"sample", "samples", "extras", "featurettes"})

API_ERRORS = (settings.ApiError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


# ---------------------------------------------------------------- pure helpers (tested)

def tokens(s: str) -> set:
    """Title words: lower case, no years, no stopwords; sequel digits are kept."""
    out = set()
    for w in re.findall(r"[a-z0-9]+", (s or "").lower()):
        if re.fullmatch(r"(19|20)\d\d", w) or w in STOPWORDS:
            continue
        if len(w) >= 3 or w.isdigit():
            out.add(w)
    return out


def clean_title(t: str) -> str:
    t = re.sub(r"(?i)^\s*www\.\S+?\.[a-z]{2,6}\s*[-.]*", "", t)   # leading web-site name prefixes
    t = re.sub(r"\[[^\]]*\]", "", t)                              # [tags]
    t = re.sub(r"[._]", " ", t)
    return re.sub(r"\s+", " ", t).strip(" -.[](){}")


def parse_title_year(name: str, is_file: bool = True):
    """(title, year) from a release or folder name; (None, None) when it has no usable year."""
    stem = os.path.splitext(name)[0] if is_file else name
    for m in YEAR_RE.finditer(stem):
        title = clean_title(stem[:m.start()])
        if len(title) >= 2:
            return title, m.group(1)
    return None, None


def title_year_key(title: str, year) -> str:
    t = re.sub(r"^the\s+", "", (title or "").lower().strip())
    return re.sub(r"[^a-z0-9]", "", t) + str(year or "")


def is_episode_name(name: str) -> bool:
    return bool(EPISODE_RE.search(name or ""))


def sanitize(s: str) -> str:
    """A safe folder / file name: no characters that Windows shares or Plex choke on."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", s or "")
    return re.sub(r"\s+", " ", s).strip(" .")


def pick_radarr_movie(movies: list, title_tokens: set, year: str):
    """(movie, ambiguous) for the Radarr movie whose title words are all in the download's
    title and whose year is exact; the most specific title wins, a tie is ambiguous."""
    hits = []
    for m in movies:
        mt = tokens(m.get("title") or "")
        if mt and mt.issubset(title_tokens) and str(m.get("year") or "") == year:
            hits.append((len(mt), m))
    if not hits:
        return None, False
    hits.sort(key=lambda x: -x[0])
    if len(hits) > 1 and hits[0][0] == hits[1][0] and hits[0][1].get("id") != hits[1][1].get("id"):
        return None, True
    return hits[0][1], False


def pick_lookup_result(results: list, title_tokens: set, year: str):
    """(result, ambiguous) among the first lookup results: exact year, and one title's
    words contain the other's. More than one distinct movie is ambiguous."""
    found = {}
    for c in (results or [])[:LOOKUP_CANDIDATES]:
        if not isinstance(c, dict) or str(c.get("year") or "") != year:
            continue
        ct = tokens(c.get("title") or "")
        if ct and (ct.issubset(title_tokens) or title_tokens.issubset(ct)):
            found.setdefault(c.get("tmdbId") or c.get("title"), c)
    if len(found) > 1:
        return None, True
    return (next(iter(found.values())) if found else None), False


def is_within(path: str, roots: list) -> bool:
    p = os.path.normpath(path)
    return any(p.startswith(os.path.normpath(r) + os.sep) for r in roots)


# ---------------------------------------------------------------- utilities

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


def scan_videos(root: str, skip: set):
    """Yield os.DirEntry objects for the video files under root, never entering `skip` folders."""
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = list(it)
        except OSError as e:
            log("cannot read %s (%s)" % (d, e.strerror or type(e).__name__))
            continue
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if e.name.lower() not in SKIP_DIR_NAMES and os.path.normpath(e.path) not in skip:
                        stack.append(e.path)
                elif e.is_file(follow_symlinks=False) and e.name.lower().endswith(settings.VIDEO_EXT):
                    yield e
            except OSError:
                continue


def folder_has_video(folder: str) -> bool:
    try:
        return any(n.lower().endswith(settings.VIDEO_EXT) and not SAMPLE_RE.search(n) for n in os.listdir(folder))
    except OSError:
        return False


def device_of(path: str):
    """st_dev of path, or of its nearest existing parent (the folder may not exist yet)."""
    p = path
    while p and not os.path.exists(p):
        parent = os.path.dirname(p)
        if parent == p:
            break
        p = parent
    try:
        return os.stat(p).st_dev
    except OSError:
        return None


def read_queue(app) -> list:
    records, page = [], 1
    while page <= MAX_QUEUE_PAGES:
        data = settings.api(app, "queue", params={"page": page, "pageSize": QUEUE_PAGE_SIZE})
        if not isinstance(data, dict):
            raise ValueError("unexpected queue reply")
        recs = data.get("records") or []
        records.extend(recs)
        if not recs or len(records) >= int(data.get("totalRecords") or 0):
            break
        page += 1
    return records


def plex_refresh(dry: bool) -> None:
    url, token = settings.plex_url(), settings.get("PLEX_TOKEN")
    if not url or not token:
        return
    if dry:
        log("[dry-run] would ask Plex to scan its libraries")
        return
    req = urllib.request.Request(url + "/library/sections/all/refresh", headers={"X-Plex-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=PLEX_TIMEOUT_S) as r:
            r.read()
        log("asked Plex to scan its libraries")
    except (OSError, http.client.HTTPException) as e:
        log("Plex scan request failed (%s)" % why(e))


# ---------------------------------------------------------------- main

class LibraryView:
    """File names, title+year folder keys and file identities already in the library."""

    def __init__(self) -> None:
        self.names: set = set()
        self.folder_keys: set = set()
        self.inodes: set = set()
        downloads = os.path.normpath(settings.DOWNLOADS)
        dl_dev = device_of(downloads)
        movie_roots = [os.path.normpath(r) for r in settings.movie_roots()]
        for root in sorted(set(os.path.normpath(r) for r in settings.library_roots())):
            same_fs = dl_dev is not None and device_of(root) == dl_dev
            for e in scan_videos(root, {downloads}):
                if SAMPLE_RE.search(e.name):
                    continue
                self.names.add(e.name.lower())
                if same_fs:
                    try:
                        self.inodes.add(e.inode())   # hardlinks share it; only meaningful on one filesystem
                    except OSError:
                        pass
                parent = os.path.dirname(e.path)
                if root in movie_roots and os.path.normpath(parent) != root:
                    t, y = parse_title_year(os.path.basename(parent), is_file=False)
                    if t:
                        self.folder_keys.add(title_year_key(t, y))

    def has(self, entry, title: str, year: str) -> str:
        """Why this download is already in the library ('' when it is not)."""
        if entry.name.lower() in self.names:
            return "same file name"
        if title_year_key(title, year) in self.folder_keys:
            return "a library folder with the same title and year"
        try:
            if entry.inode() in self.inodes:
                return "already hardlinked into the library"
        except OSError:
            pass
        return ""


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="link_orphans.py", description="File stranded movie downloads into the library.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="report only, change nothing")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()

    if not dry:
        settings.heartbeat(JOB)
        lock = alertstate.acquire_lock()   # never race the other guards on the downloads tree
        if lock is None:
            log("another guard holds the lock - skipping this run")
            return 0
    if not alertstate.mount_canary(JOB, settings.movie_roots() + [settings.DOWNLOADS], dry_run=dry):
        return 0

    radarr = settings.radarr()
    try:
        movies = [m for m in settings.api(radarr, "movie") if isinstance(m, dict) and "id" in m]
        queue = read_queue(radarr)
    except API_ERRORS as e:
        log("cannot reach radarr at %s (%s); nothing filed this run" % (radarr.url, why(e)))
        return 2
    by_id = {m["id"]: m for m in movies}
    active = {os.path.normpath(settings.to_host(r["outputPath"])) for r in queue
              if isinstance(r, dict) and r.get("outputPath")}

    lib = LibraryView()
    movie_roots = settings.movie_roots()
    now = time.time()
    linked, failed, unsure = [], [], []
    cross_device = False

    for e in scan_videos(settings.DOWNLOADS, set()):
        try:
            st = e.stat(follow_symlinks=False)
        except OSError:
            continue
        name = e.name
        if st.st_size < MIN_MOVIE_BYTES or is_episode_name(name) or SAMPLE_RE.search(name):
            continue
        if now - st.st_mtime < MIN_AGE_S:
            continue
        p = os.path.normpath(e.path)
        if any(p == a or p.startswith(a + os.sep) for a in active):
            continue   # Radarr is still handling this download; its own import comes first
        title, year = parse_title_year(name)
        if not title:
            continue
        reason = lib.has(e, title, year)
        if reason:
            continue
        tt = tokens(title)

        # 1) already in Radarr? then use Radarr's own folder for it
        hit, ambiguous = pick_radarr_movie(movies, tt, year)
        route = ""
        if ambiguous:
            unsure.append("%s (several Radarr movies match)" % name[:60])
            continue
        if not hit:
            # 2) Radarr's lookup by title and year
            try:
                results = settings.api(radarr, "movie/lookup", params={"term": "%s %s" % (title, year)})
            except API_ERRORS as ex:
                log("lookup failed for %s (%s)" % (name, why(ex)))
                results = []
            best, ambiguous = pick_lookup_result(results if isinstance(results, list) else [], tt, year)
            if not best:
                unsure.append("%s%s" % (name[:60], " (several movies match)" if ambiguous else ""))
                continue
            if best.get("id") in by_id:
                hit = by_id[best["id"]]   # the lookup says Radarr already has it under another title
        if hit:
            if hit.get("hasFile"):
                log("skip %s: Radarr already has a file for %s (%s)" % (name, hit.get("title"), hit.get("year")))
                continue
            folder = settings.to_host(hit.get("path") or "")
            if not is_within(folder, movie_roots):
                unsure.append("%s (Radarr's folder for it is outside the movie libraries)" % name[:60])
                continue
            base = os.path.basename(folder.rstrip(os.sep))
            label = "%s (%s)" % (hit.get("title"), hit.get("year"))
            route = "Radarr's folder"
            movie_id = hit["id"]
        else:
            lang = (best.get("originalLanguage") or {}).get("name") or ""
            root = settings.library_for_language(lang)
            base = sanitize("%s (%s)" % (best.get("title") or title, best.get("year") or year))
            folder = os.path.join(root, base)
            label = base
            route = alertstate.describe_path(root)
            movie_id = None
        dest = os.path.join(folder, base + os.path.splitext(name)[1].lower())
        if os.path.exists(dest) or folder_has_video(folder):
            log("skip %s: %s already holds a video" % (name, folder))
            continue
        if device_of(folder) != st.st_dev:
            cross_device = True
            failed.append("%s: the downloads and the library are on different filesystems, "
                          "so it cannot be hardlinked" % label)
            continue
        if dry:
            linked.append("WOULD LINK %s -> %s" % (label, route))
            log("[dry-run] would hardlink %s -> %s" % (e.path, dest))
            continue
        try:
            os.makedirs(folder, exist_ok=True)
            os.link(e.path, dest)
        except OSError as ex:
            if ex.errno == errno.EXDEV:
                cross_device = True
                failed.append("%s: the downloads and the library are on different filesystems, "
                              "so it cannot be hardlinked" % label)
            elif ex.errno in (errno.EACCES, errno.EPERM):
                failed.append("%s: no permission to write into the library folder" % label)
            else:
                failed.append("%s: %s" % (label, ex.strerror or type(ex).__name__))
            continue
        log("linked %s -> %s" % (e.path, dest))
        linked.append("%s -> %s" % (label, route))
        if movie_id:
            try:
                settings.api(radarr, "command", "POST", data={"name": "RescanMovie", "movieId": movie_id}, timeout=60)
            except API_ERRORS as ex:
                log("rescan of %s failed (%s)" % (label, why(ex)))

    if dry:
        log("[dry-run] movies that would be filed: %d | left alone (uncertain): %d | problems: %d"
            % (len(linked), len(unsure), len(failed)))
        for x in linked + ["uncertain: " + u for u in unsure] + ["problem: " + f for f in failed]:
            log("   " + x)
        return 1 if cross_device else 0
    if linked:
        plex_refresh(dry)

    new_f, old_f = alertstate.filter_new("lo-failed", failed)
    new_u, old_u = alertstate.filter_new("lo-uncertain", unsure)
    parts = []
    if linked:
        parts.append("Filed into the library:\n" + bullets([x.replace(" -> ", " → ") for x in linked], FILED_LINES))
    if new_f:
        parts.append("Could not file these:\n" + bullets(new_f, PROBLEM_LINES))
    if new_u:
        parts.append("Not sure where these belong (left alone):\n" + bullets(new_u, PROBLEM_LINES))
    if (old_f or old_u) and alertstate.digest_due("lo-digest"):
        parts.append("Still open from before: %d filing problem(s), %d uncertain." % (len(old_f), len(old_u)))
    elif new_f or new_u:
        alertstate.digest_due("lo-digest")   # today's summary is this alert itself
    if parts:
        msg = "Movie filing report\n\n" + "\n\n".join(parts)
        settings.notify(msg)
        print(msg, flush=True)
    elif old_f or old_u:
        log("nothing new to report (%d known problem(s), %d known uncertain)" % (len(old_f), len(old_u)))
    else:
        log("no stranded movies to file")
    return 1 if cross_device else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
