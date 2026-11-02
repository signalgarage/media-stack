#!/usr/bin/env python3
"""Weekly naming check of the library (read-only).

Why: a file whose name the apps cannot read is how content you already own gets
downloaded again: Sonarr or Radarr think it is missing and fetch it, or the media
server never shows it. Typical causes: a streaming service's "part" numbering where
Sonarr uses another episode order, episodes named "Part 1..10" or "Season 1
Episode 1", an episode saved under a hash-like name, a movie folder without a year,
a show matched to the wrong series in Plex.

What it flags:
  * re-download risk: a file in a monitored series folder that Sonarr does not know
    (no episode number, a number Sonarr's episode list does not have, or an episode
    Sonarr thinks is missing), and monitored movies Radarr thinks are missing while
    their folder holds a video;
  * TV files without an SxxEyy episode number (when Sonarr cannot be asked: all of them);
  * hash-like or random file names;
  * movie files outside a movie folder, and movie folders without a year in the name;
  * only when PLEX_URL and PLEX_TOKEN are set: files Plex does not list (older than a
    day, so Plex had time to scan them) and series folders Plex matched to another show.

It never renames, moves or deletes anything. Only problems that are NEW since the last
run are sent (alertstate.filter_new), so a known issue is not repeated every week.

Runs weekly from cron.
Flags: --dry-run (print, record and send nothing), --seed (record today's problems as
known without sending anything; use it once after installing on an existing library).
Exit codes: 0 ran.
"""
from __future__ import annotations

import argparse
import html
import http.client
import json
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

JOB = "naming_guard"
P = "[naming_guard]"

SKIP_DIRS = frozenset({"downloads", ".downloads", "sample", "samples", "extras", "featurettes", "_duplicates",
                       "@eadir", "#recycle", "$recycle.bin"})
FRESH_S = 86400            # Plex may not have scanned a file this new yet
REPORT_LINES = 15          # per category in one message
PLEX_TIMEOUT_S = 300       # listing a big library can take a while
SXE_RE = re.compile(r"[sS](\d{1,2})[ ._-]*[eE](\d{1,3})|(?<![0-9])(?<![0-9]\.)(\d{1,2})[xX](\d{2})(?![0-9])")
YEAR_RE = re.compile(r"(?<![0-9])(19|20)\d\d(?![0-9])")
SAMPLE_RE = re.compile(r"(?i)(?:^|[ ._-])sample(?:[ ._-]|$)")
HASH_HEX_RE = re.compile(r"[0-9a-f]{16,}")
HASH_ALNUM_RE = re.compile(r"[a-z0-9]{24,}")
HASH_MIN_DIGITS = 6        # a long run of letters without digits is more likely a squashed title

LABELS = {
    "redownload_risk": "Could be downloaded again (the app cannot match a file you already have)",
    "no_episode_number": "TV files without an episode number (SxxEyy) in the name",
    "hash_name": "Files with a random or hash-like name",
    "movie_not_in_folder": "Movie files lying loose in a library folder (Radarr expects one folder per movie)",
    "movie_folder_no_year": "Movie folders without a year in the name (Radarr expects \"Title (Year)\")",
    "plex_cannot_see": "On disk but Plex does not show it",
    "plex_wrong_show": "Plex matched the folder to a different show",
}

API_ERRORS = (settings.ApiError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


# ---------------------------------------------------------------- pure helpers (tested)

def episode_of(name: str):
    """(season, episode) from a file name, or None."""
    m = SXE_RE.search(name or "")
    if not m:
        return None
    if m.group(1):
        return int(m.group(1)), int(m.group(2))
    return int(m.group(3)), int(m.group(4))


def hash_like(name: str) -> bool:
    stem = os.path.splitext(name or "")[0].lower()
    if HASH_HEX_RE.fullmatch(stem):
        return True
    return bool(HASH_ALNUM_RE.fullmatch(stem)) and sum(c.isdigit() for c in stem) >= HASH_MIN_DIGITS


def same_title(a: str, b: str) -> bool:
    """Loose title comparison: no years in brackets, letters and digits only, either contains the other."""
    a = re.sub(r"[^a-z0-9]", "", re.sub(r"\(\d{4}\)", "", (a or "").lower()))
    b = re.sub(r"[^a-z0-9]", "", re.sub(r"\(\d{4}\)", "", (b or "").lower()))
    return bool(a and b) and (a == b or a in b or b in a)


def pair_key(path: str) -> tuple:
    """(parent folder, file name), lower case: how a local file and a Plex path are matched,
    whatever the mount prefix or path style on the Plex side."""
    parts = [x for x in re.split(r"[\\/]+", path or "") if x]
    if not parts:
        return ("", "")
    return ((parts[-2] if len(parts) > 1 else "").lower(), parts[-1].lower())


def short(path: str) -> str:
    """'folder/file' for messages: readable, no full system paths."""
    return "/".join(path.replace("\\", "/").split("/")[-2:])


# ---------------------------------------------------------------- utilities

def log(msg: str) -> None:
    settings.log(P, msg)


def why(e: Exception) -> str:
    if isinstance(e, settings.ApiError):
        return "HTTP %d%s" % (e.status, " (API key missing or wrong)" if e.status in (401, 403) else "")
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d" % e.code
    if isinstance(e, urllib.error.URLError):
        return "not reachable: %s" % (e.reason,)
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timed out"
    return type(e).__name__


def walk(root: str, skip_paths: set) -> list:
    out = []
    for d, dirs, files in os.walk(root):
        dirs[:] = [x for x in dirs if x.lower() not in SKIP_DIRS and not x.startswith(".")
                   and os.path.normpath(os.path.join(d, x)) not in skip_paths]
        out += [os.path.join(d, f) for f in files
                if f.lower().endswith(settings.VIDEO_EXT) and not SAMPLE_RE.search(f)]
    return out


def plex_get(path: str) -> dict:
    url = settings.plex_url() + path
    req = urllib.request.Request(url, headers={"X-Plex-Token": settings.get("PLEX_TOKEN"),
                                               "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=PLEX_TIMEOUT_S) as r:
        return (json.loads(r.read()) or {}).get("MediaContainer") or {}


def plex_files():
    """[(pair_key, show title or '')] for every file Plex lists, or None when Plex is off or unreachable."""
    if not settings.plex_url() or not settings.get("PLEX_TOKEN"):
        return None
    out = []
    try:
        for sec in plex_get("/library/sections").get("Directory") or []:
            if sec.get("type") not in ("show", "movie"):
                continue
            typ = 4 if sec["type"] == "show" else 1     # 4 = episodes, 1 = movies
            for it in plex_get("/library/sections/%s/all?type=%d" % (sec["key"], typ)).get("Metadata") or []:
                show = (it.get("grandparentTitle") or "") if sec["type"] == "show" else ""
                for media in it.get("Media") or []:
                    for part in media.get("Part") or []:
                        if part.get("file"):
                            out.append((pair_key(part["file"]), show))
    except (OSError, ValueError, KeyError, http.client.HTTPException) as e:
        log("cannot read the Plex library (%s); Plex checks skipped" % why(e))
        return None
    return out


# ---------------------------------------------------------------- main

def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="naming_guard.py", description="Weekly read-only naming check.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="print only; record and send nothing")
    ap.add_argument("--seed", action="store_true", help="record today's problems as known, send nothing")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()

    if not dry:
        settings.heartbeat(JOB)
    tv_root = os.path.normpath(settings.TV)
    movie_roots = sorted(set(os.path.normpath(r) for r in settings.movie_roots()))
    if not alertstate.mount_canary(JOB, movie_roots + [tv_root], dry_run=dry):
        return 0
    skip = {os.path.normpath(settings.DOWNLOADS)}
    found = {k: [] for k in LABELS}
    now = time.time()

    tv_files = walk(tv_root, skip)
    movie_files = {root: walk(root, skip) for root in movie_roots if root != tv_root}

    # ---- hash-like names, anywhere
    for f in tv_files + [f for files in movie_files.values() for f in files]:
        if hash_like(os.path.basename(f)):
            found["hash_name"].append(short(f))

    # ---- movie folders
    for root, files in movie_files.items():
        flagged = set()
        for f in files:
            parent = os.path.dirname(f)
            if os.path.normpath(parent) == root:
                found["movie_not_in_folder"].append(os.path.basename(f))
            elif parent not in flagged and not YEAR_RE.search(os.path.basename(parent)):
                flagged.add(parent)
                found["movie_folder_no_year"].append(os.path.relpath(parent, root))

    # ---- TV against Sonarr
    sonarr = settings.sonarr()
    try:
        series = [s for s in settings.api(sonarr, "series") if isinstance(s, dict)]
    except API_ERRORS as e:
        log("cannot reach sonarr (%s); TV checked by name only" % why(e))
        series = None
    by_folder = {}
    if series is not None:
        known = set()
        for s in series:
            host = os.path.normpath(settings.to_host(s.get("path") or ""))
            by_folder[host.lower()] = s
            try:
                for ef in settings.api(sonarr, "episodefile", params={"seriesId": s["id"]}) or []:
                    known.add(os.path.normpath(settings.to_host(ef.get("path") or "")).lower())
            except API_ERRORS as e:
                log("cannot read the files of %s (%s)" % (s.get("title"), why(e)))
        episodes: dict = {}
        for f in tv_files:
            rel = os.path.relpath(f, tv_root).split(os.sep)
            s = by_folder.get(os.path.join(tv_root, rel[0]).lower()) if len(rel) > 1 else None
            if os.path.normpath(f).lower() in known:
                continue
            ep = episode_of(os.path.basename(f))
            if not s or not s.get("monitored"):
                if not ep:
                    found["no_episode_number"].append(short(f))
                continue
            title = s.get("title") or "?"
            if not ep:
                found["redownload_risk"].append("%s: Sonarr cannot read the episode number in %s"
                                                % (title, os.path.basename(f)))
                continue
            if s["id"] not in episodes:
                try:
                    episodes[s["id"]] = {(e.get("seasonNumber"), e.get("episodeNumber")): e for e in
                                         settings.api(sonarr, "episode", params={"seriesId": s["id"]}) or []}
                except API_ERRORS:
                    episodes[s["id"]] = None
            eps = episodes[s["id"]]
            if eps is None:
                continue
            e = eps.get(ep)
            if e is None:
                found["redownload_risk"].append("%s: a file is numbered S%02dE%02d, which Sonarr's episode list does "
                                                "not have (different numbering)" % ((title,) + ep))
            elif not e.get("hasFile"):
                found["redownload_risk"].append("%s S%02dE%02d: on disk but Sonarr thinks it is missing"
                                                % ((title,) + ep))
    else:
        found["no_episode_number"] += [short(f) for f in tv_files if not episode_of(os.path.basename(f))]

    # ---- movies against Radarr
    radarr = settings.radarr()
    try:
        folders_with_video = {os.path.dirname(f).lower() for files in movie_files.values() for f in files}
        for m in settings.api(radarr, "movie") or []:
            if not isinstance(m, dict) or m.get("hasFile") or not m.get("monitored"):
                continue
            if os.path.normpath(settings.to_host(m.get("path") or "")).lower() in folders_with_video:
                found["redownload_risk"].append("%s (%s): on disk but Radarr thinks it is missing"
                                                % (m.get("title"), m.get("year")))
    except API_ERRORS as e:
        log("cannot reach radarr (%s); movie cross-check skipped" % why(e))

    # ---- Plex (optional)
    plex = plex_files()
    if plex is not None:
        if not plex:
            log("Plex lists no files; Plex checks skipped")
        else:
            seen_pairs = {k for k, _ in plex}
            for f in tv_files + [f for files in movie_files.values() for f in files]:
                try:
                    old_enough = now - os.path.getmtime(f) > FRESH_S
                except OSError:
                    continue
                if old_enough and pair_key(f) not in seen_pairs:
                    found["plex_cannot_see"].append(short(f))
            local = {pair_key(f): f for f in tv_files}
            reported = set()
            for k, show in plex:
                f = local.get(k)
                if not f or not show:
                    continue
                folder = os.path.relpath(f, tv_root).split(os.sep)[0]
                s = by_folder.get(os.path.join(tv_root, folder).lower())
                expected = (s or {}).get("title") or folder
                if (folder, show) not in reported and not same_title(expected, show):
                    reported.add((folder, show))
                    found["plex_wrong_show"].append("the folder of %s shows in Plex as '%s'" % (expected, show))

    # ---- report what is new
    body = ""
    total_new = 0
    for cat, items in found.items():
        items = sorted(set(items))
        log("%s: %d" % (cat, len(items)))
        for x in items:
            log("    " + x)
        new, _ = alertstate.filter_new("naming_guard_" + cat, items, dry_run=dry)
        total_new += len(new)
        if new:
            body += "\n%s:\n" % LABELS[cat]
            body += "".join("\u2022 %s\n" % html.escape(x, quote=False) for x in new[:REPORT_LINES])
            if len(new) > REPORT_LINES:
                body += "\u2022 ...and %d more\n" % (len(new) - REPORT_LINES)
    if not body:
        log("no new naming problems")
        return 0
    msg = ("Weekly naming check: new problems\n" + body + "\nNothing was changed. Renaming these files the way "
           "the app expects (or a manual import in Sonarr / Radarr) fixes them.")
    if dry:
        print("[dry-run] would send:\n" + msg, flush=True)
    elif args.seed:
        log("seed run: %d problem(s) recorded as known, nothing sent" % total_new)
    else:
        settings.notify(msg)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
