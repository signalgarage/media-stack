#!/usr/bin/env python3
"""Back-catalogue sweep: search for monitored episodes and movies that are still missing.

Why: Sonarr and Radarr only watch the release feeds for things that come out from
the moment you add them. Anything that was already released (the earlier
episodes of a show added mid-season, an older movie whose first search found
nothing) stays missing forever unless someone searches by hand. This sweep does
that searching on a schedule.

Design constraints that matter:
  * Hundreds of searches in a burst get the server rate-limited or banned, and an
    indexer behind Cloudflare costs a slow FlareSolverr solve per query, so each run
    spends a small BUDGET. Prowlarr's per-indexer query limits are the hard backstop;
    this budget is the polite one.
  * Plenty of missing titles are simply not available. Searching them every day is
    indexer abuse for nothing, so every item gets a COOLDOWN before it is retried.
  * A fully missing season is fetched with one season search (one query, and packs are
    usually better quality). A partly owned season uses episode searches: a pack there
    would download episodes the library already has.
  * When the download queue is already deep the sweep stands down, unless nothing has
    been dispatched for several days (a queue full of dead downloads must not stop the
    sweep forever).
  * When no indexer is enabled it stands down; when some are failing it spends a third
    of the budget instead of making things worse.
  * It records a heartbeat on EVERY run, including runs that correctly stand down, so
    library_guard can tell a dead cron job from a quiet one.

Runs daily from cron. Flags: --dry-run (list what it would search, search nothing).
Exit codes: 0 ran (including standing down), 1 no enabled indexer, 2 Sonarr/Radarr unreachable.
"""
from __future__ import annotations

import argparse
import datetime
import html
import http.client
import os
import socket
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

JOB = "missing_sweep"
P = "[missing_sweep]"

STATE_FILE = ".missing_sweep_state.json"
COOLDOWN_DAYS = 21          # do not search the same item more often than this
BUDGET_SEASONS = 6          # season searches per run
BUDGET_EPISODES = 10        # episode-batch searches per run (one batch = one season's gaps)
BUDGET_MOVIES = 8           # movie searches per run
DEGRADED_FLOOR = (1, 2, 1)  # minimum budgets when indexers are failing (a third of the normal budget otherwise)
FORCED_BUDGET = (1, 2, 1)   # the minimal batch pushed through a permanently busy queue
QUEUE_STANDDOWN = 40        # with this many items already queued, add nothing...
FORCE_AFTER_DAYS = 3        # ...unless nothing has been dispatched for this long
GAP_S = 20                  # seconds between searches, to be polite to the indexers
PAGE_SIZE = 1000
MAX_PAGES = 50
STATE_KEEP_DAYS = 2 * COOLDOWN_DAYS   # older cooldown entries no longer matter

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


def parse_utc(s):
    """'YYYY-MM-DDTHH:MM:SSZ' (any fraction) -> aware UTC datetime, or None."""
    if not s:
        return None
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def aired(rec: dict, now: datetime.datetime) -> bool:
    t = parse_utc(rec.get("airDateUtc"))
    return t is not None and t <= now


def fresh(state: dict, key: str, now: float) -> bool:
    """True when this item was searched recently enough to skip."""
    ts = state.get(key)
    return isinstance(ts, (int, float)) and (now - ts) < COOLDOWN_DAYS * 86400


def degraded(budget: tuple) -> tuple:
    return tuple(max(floor, b // 3) for b, floor in zip(budget, DEGRADED_FLOOR))


def plan_series(series: dict, missing: list, episodes_of, state: dict, now_ts: float, now_dt) -> tuple:
    """(season_jobs, episode_jobs) for the missing, aired, monitored episodes.

    season_jobs: (key, seriesId, season, title, missing count) for fully missing seasons.
    episode_jobs: (key, seriesId, season, title, [episode ids]) for partly owned seasons.
    episodes_of(seriesId) returns that series' episode list.
    """
    by_season: dict = {}
    for r in missing:
        if not isinstance(r, dict) or r.get("seasonNumber", 0) == 0:   # specials are never chased
            continue
        if r.get("seriesId") not in series or not aired(r, now_dt):
            continue
        by_season.setdefault((r["seriesId"], r["seasonNumber"]), []).append(r)
    season_jobs, episode_jobs = [], []
    for (sid, sn), miss in by_season.items():
        title = series[sid].get("title") or "?"
        try:
            all_eps = [e for e in episodes_of(sid)
                       if e.get("seasonNumber") == sn and e.get("monitored") and aired(e, now_dt)]
        except API_ERRORS as e:
            log("cannot read the episodes of %s (%s)" % (title, why(e)))
            continue
        if not all_eps:
            continue
        full = len(miss) >= len(all_eps)
        key = "s:%d:%d:%s" % (sid, sn, "season" if full else "eps")
        if fresh(state, key, now_ts):
            continue
        if full:
            season_jobs.append((key, sid, sn, title, len(miss)))
        else:
            episode_jobs.append((key, sid, sn, title, [m["id"] for m in miss]))
    # smallest gaps first: cheap, likely wins go out ahead of bulk packs
    episode_jobs.sort(key=lambda j: len(j[4]))
    season_jobs.sort(key=lambda j: j[4])
    return season_jobs, episode_jobs


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


def indexer_health():
    """(enabled count, failing count) from Prowlarr; raises when Prowlarr cannot be read."""
    prowlarr = settings.prowlarr()
    idx = settings.api(prowlarr, "indexer")
    status = settings.api(prowlarr, "indexerstatus")
    now = datetime.datetime.now(datetime.timezone.utc)
    blocked = set()
    for s in status if isinstance(status, list) else []:
        until = parse_utc((s or {}).get("disabledTill"))
        if until and until > now:
            blocked.add(s.get("indexerId"))
    enabled = [i for i in (idx if isinstance(idx, list) else []) if isinstance(i, dict) and i.get("enable")]
    return len(enabled), len([i for i in enabled if i.get("id") in blocked])


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="missing_sweep.py", description="Search for missing episodes and movies.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="list what would be searched")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()

    if not dry:
        settings.heartbeat(JOB)   # every run, including stand-downs: library_guard watches this
    state_path = settings.state_path(STATE_FILE)
    state = settings.load_json(state_path, {})
    now_ts = time.time()
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    sonarr, radarr = settings.sonarr(), settings.radarr()

    # --- stand down when the download client is already busy ----------------
    try:
        sq = int((settings.api(sonarr, "queue", params={"pageSize": 1}) or {}).get("totalRecords") or 0)
        rq = int((settings.api(radarr, "queue", params={"pageSize": 1}) or {}).get("totalRecords") or 0)
    except (AttributeError,) + API_ERRORS as e:
        log("cannot read the Sonarr/Radarr queues (%s); standing down" % why(e))
        return 2
    budget = (BUDGET_SEASONS, BUDGET_EPISODES, BUDGET_MOVIES)
    forced = False
    if sq + rq >= QUEUE_STANDDOWN:
        last = state.get("_last_dispatch", 0) or 0
        if now_ts - last < FORCE_AFTER_DAYS * 86400:
            log("queue busy (%d items); standing down" % (sq + rq))
            return 0
        forced = True
        log("queue busy (%d items) but nothing dispatched for %d days; pushing a minimal batch"
            % (sq + rq, FORCE_AFTER_DAYS))

    # --- indexer sanity: never pile searches onto broken plumbing ----------
    try:
        enabled, failing = indexer_health()
        if not enabled:
            log("no enabled indexer in Prowlarr; standing down (nothing can be found)")
            return 1
        if failing:
            budget = degraded(budget)
            log("%d indexer(s) failing; budget reduced to %d/%d/%d" % ((failing,) + budget))
    except API_ERRORS as e:
        log("indexer check failed (%s); continuing with the normal budget" % why(e))
    if forced:
        budget = FORCED_BUDGET
    b_seasons, b_episodes, b_movies = budget

    acted = []
    searches = 0

    def dispatch(app, body: dict, key: str, line: str) -> None:
        nonlocal searches
        if dry:
            log("[dry-run] would search: " + line)
            acted.append(line)
            return
        if searches:
            time.sleep(GAP_S)
        searches += 1
        try:
            settings.api(app, "command", "POST", data=body, timeout=60)
        except API_ERRORS as e:
            log("search failed for %s (%s)" % (line, why(e)))
            return
        state[key] = now_ts
        acted.append(line)

    # --- Sonarr: missing episodes, grouped by season ------------------------
    try:
        series = {s["id"]: s for s in settings.api(sonarr, "series") if isinstance(s, dict) and s.get("monitored")}
        missing = all_records(sonarr, "wanted/missing", {"monitored": "true"})
    except API_ERRORS as e:
        log("cannot read Sonarr's missing list (%s)" % why(e))
        series, missing = {}, []
    cache: dict = {}

    def episodes_of(sid: int) -> list:
        if sid not in cache:   # one fetch per series, not per season
            cache[sid] = settings.api(sonarr, "episode", params={"seriesId": sid}) or []
        return cache[sid]

    season_jobs, episode_jobs = plan_series(series, missing, episodes_of, state, now_ts, now_dt)
    for key, _sid, sn, title, ids in episode_jobs[:b_episodes]:
        n = len(ids)
        dispatch(sonarr, {"name": "EpisodeSearch", "episodeIds": ids}, key,
                 "%s S%02d - searched %d missing episode%s" % (title, sn, n, "" if n == 1 else "s"))
    for key, sid, sn, title, n in season_jobs[:b_seasons]:
        dispatch(sonarr, {"name": "SeasonSearch", "seriesId": sid, "seasonNumber": sn}, key,
                 "%s S%02d - searched the whole season (%d episodes missing)" % (title, sn, n))

    # --- Radarr: released movies without a file -----------------------------
    try:
        movies = all_records(radarr, "wanted/missing", {"monitored": "true"})
    except API_ERRORS as e:
        log("cannot read Radarr's missing list (%s)" % why(e))
        movies = []
    picked = 0
    for m in movies:
        if picked >= b_movies:
            break
        if not isinstance(m, dict) or not m.get("isAvailable"):   # not released yet: nothing to find
            continue
        key = "m:%d" % m["id"]
        if fresh(state, key, now_ts):
            continue
        picked += 1
        dispatch(radarr, {"name": "MoviesSearch", "movieIds": [m["id"]]}, key,
                 "%s (%s) - searched for this movie" % (m.get("title") or "?", m.get("year") or "?"))

    if dry:
        log("[dry-run] %d search(es) would be started" % len(acted))
        return 0
    if acted:
        state["_last_dispatch"] = now_ts   # feeds the anti-starvation check above
    cutoff = now_ts - STATE_KEEP_DAYS * 86400
    state = {k: v for k, v in state.items() if k.startswith("_") or (isinstance(v, (int, float)) and v >= cutoff)}
    try:
        settings.save_json(state_path, state)
    except OSError as e:
        log("could not save state (%s)" % e)
    if not acted:
        log("nothing due this run")
        return 0
    log("dispatched %d search(es)" % len(acted))
    n = len(acted)
    body = ("Back-catalogue sweep\n\nStarted %d search%s for things the library is missing:\n\n"
            % (n, "" if n == 1 else "es"))
    body += "\n".join("• " + html.escape(a, quote=False) for a in acted)
    body += ("\n\nAnything that turns up downloads and imports by itself. Titles that cannot be found "
             "are retried every few weeks rather than every day, so the indexers do not block the server.")
    settings.notify(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
