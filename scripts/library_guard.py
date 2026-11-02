#!/usr/bin/env python3
"""Library completeness and search-plumbing watchdog.

Why: when searching breaks, nothing new arrives and the library silently stops
filling; when the back-catalogue sweep stops running, the gaps silently stay.
Neither shows up anywhere unless something watches for it. This is the detection
half of missing_sweep.py:

  1. Indexer health: alerts on indexers that are switched off or failing, and on a
     blind stack (no indexer working at all). Indexers behind Cloudflare reach their
     site only through FlareSolverr, so when any indexer uses it, FlareSolverr is
     probed the same way Prowlarr reaches it: from inside the prowlarr container
     (its port is not published on the host).
  2. Sweep heartbeat: missing_sweep writes a heartbeat on every run, including the
     runs that correctly stand down. More than 48 hours without one means the
     self-healing has stopped. (Its state file only changes when it dispatches
     something, so it would false-alarm whenever there was nothing to do.)
  3. Weekly completeness report (Sundays): series with missing aired episodes and
     released movies that are still missing, in plain language.

It also flags Python files in the scripts folder that share a name with a
standard-library module: Python puts a script's own folder first on the import
path, so such a file breaks every guard with errors that name the symptom, never
the cause.

Alerts go through alertstate.filter_new, so a lasting problem is reported once,
not every day.

Runs daily from cron. Flags: --dry-run.
Exit codes: 0 healthy, 1 a serious problem (no working indexer, FlareSolverr down,
Prowlarr unreachable, sweep not running, an app scan failing).
"""
from __future__ import annotations

import argparse
import datetime
import html
import http.client
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "library_guard"
P = "[library_guard]"

STATE_FILE = ".library_guard_state.json"   # remembers its first run (grace period for the sweep check)
SWEEP_JOB = "missing_sweep"
SWEEP_STALE_HOURS = 48       # the sweep runs daily; two missed days is a dead cron job, not a quiet one
REPORT_WEEKDAY = 6           # Sunday (Monday = 0)
REPORT_SERIES_LINES = 15
FLARESOLVERR_URL = "http://flaresolverr:8191/v1"   # the address Prowlarr uses on the stack's network
PROBE_TIMEOUT_S = 90         # a FlareSolverr that is busy solving can be slow to answer
PAGE_SIZE = 1000
MAX_PAGES = 50
_STDLIB_FALLBACK = frozenset({   # Python 3.9 has no sys.stdlib_module_names
    "abc", "argparse", "array", "ast", "asyncio", "base64", "bisect", "calendar", "cmd", "code", "collections",
    "copy", "csv", "datetime", "decimal", "difflib", "email", "enum", "errno", "fcntl", "fractions", "functools",
    "gc", "getopt", "glob", "gzip", "hashlib", "heapq", "hmac", "html", "http", "inspect", "io", "ipaddress",
    "itertools", "json", "logging", "math", "mimetypes", "operator", "os", "pathlib", "pickle", "platform",
    "pprint", "queue", "random", "re", "secrets", "select", "shlex", "shutil", "signal", "socket", "sqlite3",
    "ssl", "stat", "statistics", "string", "struct", "subprocess", "sys", "tempfile", "textwrap", "threading",
    "time", "token", "tokenize", "traceback", "types", "typing", "unittest", "urllib", "uuid", "warnings",
    "weakref", "xml", "zipfile", "zlib"})

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


def parse_utc(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def aired(rec: dict, now: datetime.datetime) -> bool:
    t = parse_utc(rec.get("airDateUtc"))
    return t is not None and t <= now


def all_records(app, endpoint: str, params: dict) -> list:
    out, page = [], 1
    while page <= MAX_PAGES:
        data = settings.api(app, endpoint, params=dict(params, page=page, pageSize=PAGE_SIZE))
        if not isinstance(data, dict):
            raise ValueError("unexpected reply from %s" % endpoint)
        recs = data.get("records") or []
        out.extend(recs)
        if not recs or len(out) >= int(data.get("totalRecords") or 0):
            break
        page += 1
    return out


def flaresolverr_users(indexers: list, proxies) -> list:
    """Enabled indexers that go through a FlareSolverr proxy (matched by tag).

    proxies None (unknown): fall back to "any tagged indexer", the way the tags are normally used.
    """
    enabled = [i for i in indexers if isinstance(i, dict) and i.get("enable")]
    if proxies is None:
        return [i for i in enabled if i.get("tags")]
    fs = [p for p in proxies if isinstance(p, dict) and str(p.get("implementation") or "").lower() == "flaresolverr"]
    if not fs:
        return []
    tags = set()
    for p in fs:
        if not p.get("tags"):
            return enabled          # an untagged proxy is treated as applying to every indexer
        tags |= set(p["tags"])
    return [i for i in enabled if tags & set(i.get("tags") or [])]


def flaresolverr_reply_ok(stdout: str) -> bool:
    try:
        return str((json.loads(stdout) or {}).get("status", "")).lower() == "ok"
    except (ValueError, AttributeError):
        return '"status":"ok"' in stdout.replace(" ", "").lower()


def flaresolverr_problem():
    """None when FlareSolverr answers; otherwise a short reason."""
    try:
        if not settings.container_running("flaresolverr"):
            return "the container is not running"
        r = settings.docker("exec", "prowlarr", "wget", "-qO-", "-T", "25", "--post-data", '{"cmd":"sessions.list"}',
                            "--header=Content-Type: application/json", FLARESOLVERR_URL, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        return "the check itself failed (%s)" % type(e).__name__
    if r.returncode != 0 or not flaresolverr_reply_ok(r.stdout):
        return "it is running but not answering requests"
    return None


def shadowing_files() -> list:
    here = os.path.dirname(os.path.abspath(__file__))
    std = set(getattr(sys, "stdlib_module_names", ())) or set(_STDLIB_FALLBACK)
    try:
        return sorted(f for f in os.listdir(here) if f.endswith(".py") and f[:-3] in std)
    except OSError:
        return []


def check_indexers(problems: list) -> None:
    prowlarr = settings.prowlarr()
    try:
        idx = settings.api(prowlarr, "indexer")
        status = settings.api(prowlarr, "indexerstatus")
        if not isinstance(idx, list) or not isinstance(status, list):
            raise ValueError("unexpected reply")
    except API_ERRORS as e:
        problems.append(("prowlarr_down", "Cannot reach Prowlarr to check the indexers (%s)." % why(e), True))
        return
    try:
        proxies = settings.api(prowlarr, "indexerproxy")
        proxies = proxies if isinstance(proxies, list) else None
    except API_ERRORS:
        proxies = None
    now = datetime.datetime.now(datetime.timezone.utc)
    blocked = set()
    for s in status:
        until = parse_utc((s or {}).get("disabledTill"))
        if until and until > now:
            blocked.add(s.get("indexerId"))
    usable = 0
    for i in idx:
        if not isinstance(i, dict):
            continue
        name = i.get("name") or "#%s" % i.get("id")
        if not i.get("enable"):
            problems.append(("idx_disabled:%s" % i.get("id"), "Indexer %s is switched off in Prowlarr." % name, False))
        elif i.get("id") in blocked:
            problems.append(("idx_failing:%s" % i.get("id"),
                             "Searches are failing on indexer %s. Usually a Cloudflare block or FlareSolverr "
                             "trouble; it often clears by itself, but if it keeps coming back the indexer needs "
                             "attention." % name, False))
        else:
            usable += 1
    if not idx:
        problems.append(("idx_none", "Prowlarr has no indexers yet, so nothing can be searched. Add at least one "
                         "in Prowlarr (Indexers, Add Indexer).", True))
    elif usable == 0:
        problems.append(("idx_blind", "No indexer is working (every one is switched off or failing): nothing can be "
                         "found or downloaded until one is back.", True))
    users = flaresolverr_users(idx, proxies)
    if users:
        err = flaresolverr_problem()
        if err:
            names = ", ".join(sorted(str(i.get("name") or i.get("id")) for i in users))
            problems.append(("flaresolverr_down",
                             "FlareSolverr is not working: %s. %d indexer%s on it to get past Cloudflare (%s) "
                             "and will find nothing until it is back. Restarting the flaresolverr container usually "
                             "fixes it." % (err, len(users), " relies" if len(users) == 1 else "s rely", names), True))


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="library_guard.py", description="Library completeness and indexer watchdog.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="report only, send nothing")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()

    if not dry:
        settings.heartbeat(JOB)
    state_path = settings.state_path(STATE_FILE)
    state = settings.load_json(state_path, {})
    if not dry and not state.get("first_run"):
        state["first_run"] = int(time.time())
        try:
            settings.save_json(state_path, state)
        except OSError:
            pass
    problems = []   # (key, text, serious)

    # 1. indexer health (+ FlareSolverr)
    check_indexers(problems)

    # 1c. a file in the scripts folder that hides a standard-library module
    shadows = shadowing_files()
    if shadows:
        problems.append(("stdlib_shadow:" + ",".join(shadows),
                         "%d file%s in the scripts folder share a name with a Python standard-library module (%s). "
                         "Every guard imports these instead of the real module and fails in confusing ways; rename "
                         "or remove them." % (len(shadows), "" if len(shadows) == 1 else "s", ", ".join(shadows)),
                         True))

    # 2. the sweep heartbeat
    age = settings.heartbeat_age(SWEEP_JOB)
    if age >= 1e11:
        since = time.time() - float(state.get("first_run") or time.time())
        if since > SWEEP_STALE_HOURS * 3600:
            problems.append(("sweep_never", "The daily back-catalogue sweep has never recorded a run, so missing "
                             "episodes and movies are not being searched for automatically.", True))
    elif age > SWEEP_STALE_HOURS * 3600:
        problems.append(("sweep_stale", "The daily back-catalogue sweep has not run for %d hours, so missing "
                         "episodes and movies are no longer searched for automatically." % (age // 3600), True))

    # 3. completeness
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    gaps, movie_gap = [], 0
    sonarr, radarr = settings.sonarr(), settings.radarr()
    try:
        series = {s["id"]: s for s in settings.api(sonarr, "series") if isinstance(s, dict) and s.get("monitored")}
        per: dict = {}
        for r in all_records(sonarr, "wanted/missing", {"monitored": "true"}):
            if not isinstance(r, dict) or r.get("seasonNumber", 0) == 0 or r.get("seriesId") not in series:
                continue
            if aired(r, now_dt):
                per[r["seriesId"]] = per.get(r["seriesId"], 0) + 1
        gaps = sorted(((series[k].get("title") or "?", v) for k, v in per.items()), key=lambda x: -x[1])
    except API_ERRORS as e:
        problems.append(("sonarr_scan", "Could not read Sonarr's missing episodes (%s)." % why(e), True))
    try:
        movie_gap = len([m for m in all_records(radarr, "wanted/missing", {"monitored": "true"})
                         if isinstance(m, dict) and m.get("isAvailable")])
    except API_ERRORS as e:
        problems.append(("radarr_scan", "Could not read Radarr's missing movies (%s)." % why(e), True))

    # alerts: only what is new
    text = {k: t for k, t, _ in problems}
    new, _persisting = alertstate.filter_new("library_guard", [k for k, _, _ in problems], dry_run=dry)
    messages = []
    if new:
        messages.append("Library watchdog\n\nSomething needs looking at:\n\n"
                        + "\n".join("• " + esc(text[k]) for k in new))

    # weekly report
    if datetime.datetime.now().weekday() == REPORT_WEEKDAY and alertstate.digest_due("library_weekly", dry_run=dry):
        ep_gap = sum(n for _, n in gaps)
        if not gaps and not movie_gap:
            body = ("Weekly library check\n\nEverything is complete: every monitored series has all of its aired "
                    "episodes, and every released movie you follow is downloaded.")
        else:
            body = "Weekly library check\n\n"
            if ep_gap:
                body += "%d episode%s still missing across %d series:\n\n" % (
                    ep_gap, "" if ep_gap == 1 else "s", len(gaps))
                for t, n in gaps[:REPORT_SERIES_LINES]:
                    body += "• %s - %d episode%s\n" % (esc(t), n, "" if n == 1 else "s")
                if len(gaps) > REPORT_SERIES_LINES:
                    body += "• ...and %d more series\n" % (len(gaps) - REPORT_SERIES_LINES)
            if movie_gap:
                body += "\n%d released movie%s still missing.\n" % (movie_gap, "" if movie_gap == 1 else "s")
            body += ("\nThese are searched for automatically every day, a few at a time. Anything listed here for "
                     "weeks is probably not available on your indexers.")
        messages.append(body)

    for msg in messages:
        if dry:
            print("[dry-run] would send:\n" + msg, flush=True)
        else:
            settings.notify(msg)
    serious = [k for k, _, s in problems if s]
    log("problems=%d (new=%d, serious=%d) episode-gaps=%d movie-gaps=%d"
        % (len(problems), len(new), len(serious), sum(n for _, n in gaps), movie_gap))
    for k, t, _ in problems:
        log("  %s: %s" % (k, t))
    return 1 if serious else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
