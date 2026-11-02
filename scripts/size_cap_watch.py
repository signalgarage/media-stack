#!/usr/bin/env python3
"""Ask on Telegram before downloading an episode that only Sonarr's size cap blocks.

Why: Sonarr's size limits are stored per minute of runtime, so a longer than usual
episode (a premiere, a finale) can have every good copy rejected as too large while
the rest of the season downloads fine, and Sonarr says nothing. The cap stays; this
job notices when an episode is missed for that reason alone and asks you whether to
download it anyway.

Every run (cron, every 10 minutes):
  1. Answers: for each question asked earlier, read the Yes/No the bot recorded.
     Yes -> search that episode again and grab the over-cap copy it offered (a manual
     grab is not subject to the cap). No -> remember it and never ask about it again.
  2. Detection, at most once every SEARCH_EVERY_MIN: take a few recently aired,
     monitored, missing episodes that are not downloading, run Sonarr's interactive
     search, and flag an episode only when the best copy Sonarr turned down was turned
     down for SIZE ALONE, is not above EPISODE_CAP_GB, has a non-negative custom-format
     score (the language rules score other languages far below zero) and beats anything
     Sonarr would accept. Findings become one question per show and season.

Design constraints:
  * Interactive searches hit every indexer (with a slow Cloudflare solve for some), so
    each pass spends a small BUDGET and every episode has a COOLDOWN.
  * Only single-episode copies in PREFERRED_LANGUAGE or the show's original language are
    offered; never season packs.
  * Questions go through the bot's file-based Yes/No prompts (tg.post_ask), so this job
    never competes with the bot for Telegram's single update stream.
  * Nothing downloads without a Yes.

Flags: --dry-run (search and print, ask and grab nothing), --probe <episodeId> (classify
one episode and print the verdict; ignores queue, cooldown and state).
Exit codes: 0 ran, 2 Sonarr could not be reached.
"""
from __future__ import annotations

import argparse
import datetime
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
import tg  # noqa: E402

JOB = "size_cap_watch"
P = "[size_cap_watch]"

STATE_FILE = ".size_cap_state.json"
LOCK_FILE = ".size_cap_watch.lock"
ASK_PREFIX = "sz"
SEARCH_EVERY_MIN = 120     # detection pass at most this often
BUDGET = 3                 # interactive episode searches per detection pass
COOLDOWN_H = 24            # search the same episode at most this often
GRACE_H = 4                # give Sonarr's own feed-based grab this long after airing
WINDOW_DAYS = 14           # only episodes that aired within this many days
ASK_EXPIRE_DAYS = 7        # unanswered questions are dropped after this (and not asked again)
FORGET_DAYS = 60           # closed questions are forgotten after this
GAP_S = 15                 # seconds between searches, to be polite to the indexers
SEARCH_TIMEOUT_S = 600     # an interactive search waits for every indexer
SIZE_REASON = "larger than maximum allowed"
GIB = 1024 ** 3            # the apps show sizes in units of 1024^3 bytes
PAGE_SIZE = 1000
MAX_PAGES = 50
CLOSED = ("pending", "declined", "grabbed", "expired")   # episodes in these states are never searched again

API_ERRORS = (settings.ApiError, OSError, ValueError, KeyError, TypeError, http.client.HTTPException)


# ---------------------------------------------------------------- pure helpers (tested)

def ep_numbers(rel: dict) -> list:
    return rel.get("mappedEpisodeNumbers") or rel.get("episodeNumbers") or []


def season_number(rel: dict):
    m = rel.get("mappedSeasonNumber")
    return m if m is not None else rel.get("seasonNumber")


def language_ok(rel: dict, wanted: set) -> bool:
    names = {str(x.get("name")) for x in rel.get("languages") or [] if isinstance(x, dict)}
    return bool(names & wanted)


def rank(rel: dict) -> tuple:
    return (rel.get("qualityWeight") or 0, rel.get("customFormatScore") or 0)


def classify(ep: dict, releases: list, wanted: set, cap_bytes: float):
    """(verdict, offer, limit_text). verdict: 'size_miss' (offer = best over-cap copy),
    'grabbable' (Sonarr would take a copy at least as good) or 'nothing'."""
    mine = [r for r in releases or [] if isinstance(r, dict) and not r.get("fullSeason")
            and season_number(r) == ep.get("seasonNumber") and ep_numbers(r) == [ep.get("episodeNumber")]]
    accepted = [r for r in mine if not r.get("rejected")]
    size_only = [r for r in mine if r.get("rejected") and r.get("rejections")
                 and all(SIZE_REASON in str(x).lower() for x in r["rejections"])
                 and language_ok(r, wanted)
                 and (r.get("customFormatScore") or 0) >= 0
                 and 0 < (r.get("size") or 0) <= cap_bytes]
    if not size_only:
        return ("grabbable" if accepted else "nothing"), None, None
    offer = max(size_only, key=lambda r: (rank(r), r.get("seeders") or 0, -(r.get("size") or 0)))
    if accepted and max(rank(r) for r in accepted) >= rank(offer):
        return "grabbable", None, None
    m = re.search(r"maximum allowed ([\d.]+ ?[KMGT]i?B)", " ".join(map(str, offer["rejections"])))
    return "size_miss", offer, (m.group(1) if m else None)


def describe(rel: dict) -> str:
    """Plain description of a release, e.g. '1080p web copy'."""
    q = (rel.get("quality") or {}).get("quality") or {}
    res = q.get("resolution")
    src = str(q.get("source") or "").lower()
    kind = {"web": "web", "webrip": "web", "webdl": "web", "television": "TV", "bluray": "Blu-ray"}.get(src, src)
    parts = ["%sp" % res if res else "", (kind + " copy") if kind else "copy"]
    return " ".join(p for p in parts if p)


def parse_utc(s):
    if not s:
        return None
    try:
        return datetime.datetime.strptime(str(s)[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc)
    except ValueError:
        return None


def question_text(group: list) -> str:
    series, season = group[0]["series"], group[0]["season"]
    lines = ["\U0001F4FA <b>Episode skipped: over the size limit</b>", "",
             "<b>%s</b> · Season %s" % (html.escape(series), season)]
    for it in group:
        lim = " (limit %s)" % it["limit"] if it.get("limit") else ""
        lines.append("• Episode %s “%s”: best copy is %.2f GB%s, %s"
                     % (it["episode"], html.escape(it["ep_title"]), it["size_gb"], lim, html.escape(it["desc"])))
    lines += ["", "Sonarr keeps the size limit, so it will not download "
              + ("this" if len(group) == 1 else "these") + " by itself. Download anyway?"]
    return "\n".join(lines)


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


def sonarr_api(endpoint: str, method: str = "GET", data=None, params=None, timeout: int = 90):
    return settings.api(settings.sonarr(), endpoint, method=method, data=data, params=params, timeout=timeout)


def all_records(endpoint: str, params: dict) -> list:
    out, page = [], 1
    while page <= MAX_PAGES:
        data = sonarr_api(endpoint, params=dict(params, page=page, pageSize=PAGE_SIZE))
        if not isinstance(data, dict):
            raise ValueError("unexpected reply from %s" % endpoint)
        recs = data.get("records") or []
        out.extend(recs)
        if not recs or len(out) >= int(data.get("totalRecords") or 0):
            break
        page += 1
    return out


def wanted_languages(series: dict) -> set:
    out = {settings.PREFERRED_LANGUAGE}
    orig = (series or {}).get("originalLanguage") or {}
    if orig.get("name"):
        out.add(str(orig["name"]))
    return out


def series_of(episode: dict) -> dict:
    """The episode's series (for its original language); {} when it cannot be read."""
    if not episode.get("seriesId"):
        return {}
    try:
        return sonarr_api("series/%d" % episode["seriesId"]) or {}
    except API_ERRORS:
        return {}


def load_state(path: str) -> dict:
    st = settings.load_json(path, {})
    st.setdefault("last_search", 0)
    st.setdefault("episodes", {})   # episode id -> {"checked": ts, "status": ...}
    st.setdefault("asks", {})       # question id -> {"created": ts, "status": ..., "items": [...]}
    return st


def save_state(path: str, st: dict) -> None:
    try:
        settings.save_json(path, st)
    except OSError as e:
        log("could not save state (%s)" % e)


# ---------------------------------------------------------------- the two phases

def grab(it: dict) -> str:
    """Search the episode again and grab the approved copy (or the best over-cap copy now)."""
    e = sonarr_api("episode/%d" % it["episodeId"]) or {}
    if e.get("hasFile"):
        return "already downloaded"
    if any(r.get("episodeId") == it["episodeId"] for r in all_records("queue", {})):
        return "already downloading"
    releases = sonarr_api("release", params={"episodeId": it["episodeId"]}, timeout=SEARCH_TIMEOUT_S) or []
    same = [r for r in releases if isinstance(r, dict) and r.get("title") == it["release_title"]]
    if same:
        rel = max(same, key=lambda r: r.get("seeders") or 0)
    else:
        verdict, rel, _ = classify(e, releases, wanted_languages(series_of(e)), settings.EPISODE_CAP_GB * GIB)
        if verdict != "size_miss":
            return "the copy is no longer available"
    sonarr_api("release", "POST", data={"guid": rel["guid"], "indexerId": rel["indexerId"]}, timeout=120)
    return "downloading"


def process_replies(st: dict, state_path: str) -> None:
    now = time.time()
    for cid, a in list(st["asks"].items()):
        if a.get("status") != "pending":
            continue
        answer = tg.read_reply(cid, consume=False)
        if answer == "yes":
            results = []
            for it in a["items"]:
                try:
                    res = grab(it)
                except API_ERRORS as ex:
                    res = "failed (%s)" % why(ex)
                done = res == "downloading" or res.startswith("already")
                st["episodes"][str(it["episodeId"])] = {"checked": int(now), "status": "grabbed" if done else "failed"}
                results.append("• Episode %s: %s" % (it["episode"], res))
                log("approved %s: %s S%02dE%02d -> %s" % (cid, it["series"], it["season"], it["episode"], res))
            a["status"] = "approved"
            first = a["items"][0]
            settings.notify("\U0001F4E5 <b>%s</b> · Season %s\n\n%s"
                            % (html.escape(first["series"]), first["season"], "\n".join(results)))
        elif answer == "no":
            a["status"] = "declined"
            for it in a["items"]:
                st["episodes"][str(it["episodeId"])] = {"checked": int(now), "status": "declined"}
            log("declined %s" % cid)
        elif now - a.get("created", now) > ASK_EXPIRE_DAYS * 86400:
            a["status"] = "expired"
            for it in a["items"]:
                st["episodes"][str(it["episodeId"])] = {"checked": int(now), "status": "expired"}
            log("expired unanswered %s" % cid)
        else:
            continue
        save_state(state_path, st)   # record the outcome before the reply file goes
        tg.drop_ask(cid)
    for cid, a in list(st["asks"].items()):
        if a.get("status") != "pending" and now - a.get("created", now) > FORGET_DAYS * 86400:
            del st["asks"][cid]
    try:
        for fn in os.listdir(settings.CONFIRM_DIR):   # a second tap on a closed question leaves a stray reply
            if fn.startswith(ASK_PREFIX) and fn.endswith(".reply") and \
                    st["asks"].get(fn[:-6], {}).get("status") != "pending":
                tg.drop_ask(fn[:-6])
    except OSError:
        pass


def candidates(st: dict) -> list:
    now = datetime.datetime.now(datetime.timezone.utc)
    queued = {r.get("episodeId") for r in all_records("queue", {})}
    out = []
    for e in all_records("wanted/missing", {"monitored": "true", "includeSeries": "true"}):
        if not isinstance(e, dict) or e.get("hasFile") or not e.get("monitored") or e.get("id") in queued:
            continue
        series = e.get("series") or {}
        if series and not series.get("monitored", True):
            continue
        air = parse_utc(e.get("airDateUtc"))
        if not air or not (datetime.timedelta(hours=GRACE_H) <= now - air <= datetime.timedelta(days=WINDOW_DAYS)):
            continue
        rec = st["episodes"].get(str(e["id"]), {})
        if rec.get("status") in CLOSED or time.time() - rec.get("checked", 0) < COOLDOWN_H * 3600:
            continue
        e["_series"] = series.get("title") or "?"
        e["_langs"] = wanted_languages(series)
        out.append(e)
    out.sort(key=lambda e: e.get("airDateUtc") or "", reverse=True)   # newest first
    return out


def ask(st: dict, group: list, dry: bool) -> None:
    text = question_text(group)
    if dry:
        log("[dry-run] would ask:\n" + text)
        return
    cid = tg.post_ask(text, prefix=ASK_PREFIX)
    now = int(time.time())
    if not cid:   # Telegram not set up: nobody can answer, so just keep the normal cooldown
        for it in group:
            st["episodes"][str(it["episodeId"])] = {"checked": now, "status": "none"}
        return
    st["asks"][cid] = {"created": now, "status": "pending", "items": group}
    for it in group:
        st["episodes"][str(it["episodeId"])] = {"checked": now, "status": "pending", "cid": cid}
    log("asked (%s): %s S%02d episodes %s" % (cid, group[0]["series"], group[0]["season"],
                                             [it["episode"] for it in group]))


def detect(st: dict, dry: bool) -> None:
    if not dry and time.time() - st["last_search"] < SEARCH_EVERY_MIN * 60:
        return
    if not dry:
        st["last_search"] = int(time.time())
    cands = candidates(st)
    log("%d candidate episode(s) (missing, monitored, aired %dh-%dd ago, not downloading)"
        % (len(cands), GRACE_H, WINDOW_DAYS))
    found = []
    for i, e in enumerate(cands[:BUDGET]):
        if i:
            time.sleep(GAP_S)
        label = "%s S%02dE%02d" % (e["_series"], e.get("seasonNumber", 0), e.get("episodeNumber", 0))
        try:
            releases = sonarr_api("release", params={"episodeId": e["id"]}, timeout=SEARCH_TIMEOUT_S) or []
        except API_ERRORS as ex:
            log("search failed for %s (%s)" % (label, why(ex)))
            continue
        verdict, offer, limit = classify(e, releases, e["_langs"], settings.EPISODE_CAP_GB * GIB)
        log("%s: %s%s" % (label, verdict,
                          " -> %s (%.2f GB)" % (offer.get("title"), (offer.get("size") or 0) / GIB) if offer else ""))
        if not dry:
            st["episodes"][str(e["id"])] = {"checked": int(time.time()), "status": "none"}
        if verdict == "size_miss":
            found.append({"episodeId": e["id"], "series": e["_series"], "season": e.get("seasonNumber"),
                          "episode": e.get("episodeNumber"), "ep_title": e.get("title") or "",
                          "release_title": offer.get("title"), "size_gb": (offer.get("size") or 0) / GIB,
                          "limit": limit, "desc": describe(offer)})
    groups: dict = {}
    for it in found:
        groups.setdefault((it["series"], it["season"]), []).append(it)
    for g in groups.values():
        ask(st, sorted(g, key=lambda it: it["episode"]), dry)


def probe(episode_id: int) -> int:
    try:
        e = sonarr_api("episode/%d" % episode_id) or {}
        series = series_of(e)
        releases = sonarr_api("release", params={"episodeId": episode_id}, timeout=SEARCH_TIMEOUT_S) or []
    except API_ERRORS as ex:
        log("cannot probe episode %d (%s)" % (episode_id, why(ex)))
        return 2
    verdict, offer, limit = classify(e, releases, wanted_languages(series), settings.EPISODE_CAP_GB * GIB)
    print(verdict, "|", offer and offer.get("title"), "|", offer and "%.2f GB" % ((offer.get("size") or 0) / GIB),
          "| limit", limit, "|", offer and describe(offer))
    return 0


def main(argv) -> int:
    ap = argparse.ArgumentParser(prog="size_cap_watch.py", description="Ask before grabbing an over-cap episode.")
    ap.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="search and print only")
    ap.add_argument("--probe", type=int, metavar="EPISODE_ID", help="classify one episode and print the verdict")
    args = ap.parse_args(argv)
    dry = args.dry or settings.dry_run()
    if args.probe is not None:
        return probe(args.probe)

    lock = alertstate.acquire_lock(LOCK_FILE)
    if lock is None:
        log("the previous run is still going - skipping this run")
        return 0
    if not dry:
        settings.heartbeat(JOB)
    try:
        sonarr_api("system/status", timeout=20)
    except API_ERRORS as e:
        log("cannot reach sonarr at %s (%s)" % (settings.sonarr().url, why(e)))
        return 2
    state_path = settings.state_path(STATE_FILE)
    st = load_state(state_path)
    try:
        if not dry:
            process_replies(st, state_path)
            save_state(state_path, st)
        detect(st, dry)
    except API_ERRORS as e:
        log("sonarr stopped answering (%s)" % why(e))
        if not dry:
            save_state(state_path, st)
        return 2
    if not dry:
        save_state(state_path, st)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
