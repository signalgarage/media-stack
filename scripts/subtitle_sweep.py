#!/usr/bin/env python3
"""Chase missing subtitles the way missing_sweep chases missing episodes.

Why: Bazarr knows exactly which subtitles are missing, but nothing drives the
retries. Its own scheduled search is conservative, and a title that had no
subtitle on release day is rarely revisited once one appears weeks later.

Same design constraints as missing_sweep, for the same reasons:
  * a BUDGET per run: every search hits the subtitle providers, and some accounts
    have hard daily download limits; one run must not burn them and leave nothing
    for a genuinely new episode;
  * a COOLDOWN per item: plenty of titles simply have no subtitle in the wanted
    languages, and asking every day is abuse for nothing;
  * it stands down when Bazarr is unreachable rather than guessing.

Media stays untouched: Bazarr writes the subtitle file next to the video, exactly as
it does normally. Nothing is moved, renamed or deleted.

Only meaningful with the "subtitles" compose profile (Bazarr); without it the job
logs that and exits 0. Runs daily from cron. Flags: --dry-run.
Exit codes: 0 ran (or nothing to do), 1 Bazarr runs but no API key can be found.
"""
from __future__ import annotations

import argparse
import html
import http.client
import os
import socket
import sys
import time
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

JOB = "subtitle_sweep"
P = "[subtitle_sweep]"

STATE_FILE = ".subtitle_sweep_state.json"
BUDGET_EPISODES = 12        # episode subtitle searches per run
BUDGET_MOVIES = 4           # movie subtitle searches per run
COOLDOWN_DAYS = 14          # search the same item at most this often
GAP_S = 6                   # seconds between searches, to be polite to the providers
WANTED_EPISODES_PAGE = 500  # rows read per run (the budget only ever uses a few)
WANTED_MOVIES_PAGE = 200
SEARCH_TIMEOUT_S = 120      # a search asks every provider in turn
REPORT_LINES = 12
STATE_KEEP_DAYS = 2 * COOLDOWN_DAYS

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


def fresh(state: dict, key: str, now: float) -> bool:
    ts = state.get(key)
    return isinstance(ts, (int, float)) and (now - ts) < COOLDOWN_DAYS * 86400


def search_params(missing: dict) -> dict:
    """The language and flags Bazarr expects, taken from the wanted record itself
    (it lists exactly which language is missing, forced or hearing-impaired)."""
    return {"language": missing.get("code2") or "en",
            "forced": str(bool(missing.get("forced"))).lower(),
            "hi": str(bool(missing.get("hi"))).lower()}


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="subtitle_sweep.py", description="Search for missing subtitles through Bazarr.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="list what would be searched")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()

    if not dry:
        settings.heartbeat(JOB)
    if not settings.profile_enabled("subtitles"):
        log("the subtitles profile is off (COMPOSE_PROFILES), so there is no Bazarr to ask; nothing to do")
        return 0
    bazarr = settings.bazarr()
    if not bazarr.key:
        log("no Bazarr API key (set BAZARR_API_KEY or let Bazarr create config/bazarr/config/config.yaml); "
            "standing down")
        return 1
    try:
        want = settings.api(bazarr, "episodes/wanted", params={"start": 0, "length": WANTED_EPISODES_PAGE})
        rows = (want or {}).get("data") or []
    except (AttributeError,) + API_ERRORS as e:
        log("cannot reach bazarr at %s (%s); standing down" % (bazarr.url, why(e)))
        return 0

    state_path = settings.state_path(STATE_FILE)
    state = settings.load_json(state_path, {})
    now = time.time()
    acted = []
    searches = 0

    def search(endpoint: str, params: dict, key: str, line: str) -> bool:
        nonlocal searches
        if dry:
            log("[dry-run] would search: " + line)
            acted.append(line)
            return True
        if searches:
            time.sleep(GAP_S)
        searches += 1
        try:
            settings.api(bazarr, endpoint, "PATCH", params=params, timeout=SEARCH_TIMEOUT_S)
        except API_ERRORS as e:
            log("search failed for %s (%s)" % (line, why(e)))
            return False
        state[key] = now
        acted.append(line)
        return True

    # ---------- episodes
    picked = 0
    for r in rows:
        if picked >= BUDGET_EPISODES:
            break
        if not isinstance(r, dict):
            continue
        sid, eid = r.get("sonarrSeriesId"), r.get("sonarrEpisodeId")
        miss = [m for m in r.get("missing_subtitles") or [] if isinstance(m, dict)]
        if not sid or not eid or not miss:
            continue
        key = "e:%s:%s" % (sid, eid)
        if fresh(state, key, now):
            continue
        picked += 1
        params = dict(search_params(miss[0]), seriesid=sid, episodeid=eid)
        search("episodes/subtitles", params, key, "%s %s (%s)" % (
            r.get("seriesTitle") or "?", r.get("episode_number") or "", miss[0].get("name") or "?"))

    # ---------- movies
    try:
        mrows = (settings.api(bazarr, "movies/wanted", params={"start": 0, "length": WANTED_MOVIES_PAGE})
                 or {}).get("data") or []
    except (AttributeError,) + API_ERRORS as e:
        log("cannot read the wanted movies (%s)" % why(e))
        mrows = []
    picked = 0
    for r in mrows:
        if picked >= BUDGET_MOVIES:
            break
        if not isinstance(r, dict) or not r.get("radarrId"):
            continue
        miss = [m for m in r.get("missing_subtitles") or [] if isinstance(m, dict)]
        key = "m:%s" % r["radarrId"]
        if not miss or fresh(state, key, now):
            continue
        picked += 1
        params = dict(search_params(miss[0]), radarrid=r["radarrId"])
        search("movies/subtitles", params, key, "%s (%s)" % (r.get("title") or "?", miss[0].get("name") or "?"))

    if not dry:
        cutoff = now - STATE_KEEP_DAYS * 86400
        state = {k: v for k, v in state.items() if isinstance(v, (int, float)) and v >= cutoff}
        try:
            settings.save_json(state_path, state)
        except OSError as e:
            log("could not save state (%s)" % e)

    # how much is still outstanding, for the log and the message
    try:
        rem_e = (settings.api(bazarr, "episodes/wanted", params={"start": 0, "length": 1}) or {}).get("total", "?")
        rem_m = (settings.api(bazarr, "movies/wanted", params={"start": 0, "length": 1}) or {}).get("total", "?")
    except (AttributeError,) + API_ERRORS:
        rem_e = rem_m = "?"
    log("%s %d item(s); still wanted: %s episodes, %s movies"
        % ("would search" if dry else "searched", len(acted), rem_e, rem_m))
    if acted and not dry:
        n = len(acted)
        body = ("Subtitle sweep\n\nSearched for subtitles that are still missing on %d title%s:\n\n"
                % (n, "" if n == 1 else "s"))
        body += "\n".join("\u2022 " + html.escape(a, quote=False) for a in acted[:REPORT_LINES])
        if n > REPORT_LINES:
            body += "\n\u2022 ...and %d more" % (n - REPORT_LINES)
        body += ("\n\nStill waiting on subtitles: %s episodes and %s movies. Anything that cannot be found is "
                 "retried every couple of weeks rather than every day, so provider limits are not used up."
                 % (rem_e, rem_m))
        settings.notify(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
