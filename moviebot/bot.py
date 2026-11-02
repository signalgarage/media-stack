#!/usr/bin/env python3
"""moviebot: the Telegram assistant of media-stack.

Send it a title and it searches Radarr (movies) and Sonarr (shows); tap a number to add one.
Radarr and Sonarr report back through webhooks, so the bot can announce, check and cancel
downloads and say when something is ready to watch. Background jobs clean up dead and fake
downloads, send a daily report, suggest new releases (through Jellyseerr), fill missing
episodes, watch disk space, and show the Yes/No questions the host-side guard scripts ask
through files in /state/confirms.

Access: only the Telegram user ids in ALLOWED_TELEGRAM_USER_IDS can use anything, and only in a
private chat with the bot. Everyone else gets no answer at all; their id is logged so the owner
can add it. The webhooks only accept calls on the secret paths /radarr/<WEBHOOK_SECRET> and
/sonarr/<WEBHOOK_SECRET>.

Settings come from the environment (see .env.example); docs/BOT.md describes every command.
"""
from __future__ import annotations

import asyncio
import functools
import hmac
import json
import logging
import os
import re
import shutil
import signal
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import requests
from aiohttp import web
from botlib import langs
from botlib.releases import (
    GIB,
    ForcePicks,
    cf_flag,
    cf_negative,
    choose_season_pack,
    closest_clean_first,
    covered_seasons,
    ep_list,
    fake_reason,
    fill_pick,
    grab_problems,
    is_cam,
    lang_note,
    pick_profile,
    queue_fake_reason,
    rel_quality_name,
    season_complete,
    torrent_dead,
    torrent_finished,
)
from botlib.text import (
    CAPTION_LIMIT,
    bar,
    clean_service,
    critics_line,
    ep_codes,
    esc,
    fmt_bytes,
    fmt_dur,
    parse_iso,
    plain_len,
    trim_caption,
)
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("moviebot")
# httpx logs every Telegram API URL at INFO and those URLs contain the bot token; aiohttp's access
# log would print the webhook path, which contains the secret. Both stay at WARNING (access log off).
for _noisy in ("httpx", "httpcore", "telegram", "apscheduler", "aiohttp.access"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ================================================================== settings
def _env(name, default=""):
    val = os.environ.get(name, "").strip()
    return val if val else default


def _env_int(name, default):
    try:
        return int(_env(name, str(default)))
    except ValueError:
        log.warning("%s is not a whole number; using %s", name, default)
        return default


def _env_float(name, default):
    try:
        return float(_env(name, str(default)))
    except ValueError:
        log.warning("%s is not a number; using %s", name, default)
        return default


def _parse_ids(raw):
    """'123, 456' -> ([123, 456], number of entries that are not integer ids)."""
    ids, bad = [], 0
    for part in (raw or "").replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            ids.append(int(part))
        except ValueError:
            bad += 1
    return list(dict.fromkeys(ids)), bad


def _parse_lang_caps(raw):
    """'en:5:3,fr:2:1' -> {'en': (5, 3), 'fr': (2, 1)}: new-release picks per day (movies, shows)."""
    caps = {}
    for part in (raw or "").split(","):
        bits = [b.strip() for b in part.split(":")]
        if len(bits) != 3 or not bits[0]:
            continue
        try:
            caps[langs.lang_code(bits[0]) or bits[0].lower()] = (int(bits[1]), int(bits[2]))
        except ValueError:
            continue
    return caps


TOKEN = _env("TELEGRAM_BOT_TOKEN")
ALLOWED, _BAD_IDS = _parse_ids(_env("ALLOWED_TELEGRAM_USER_IDS") or _env("ALLOWED_CHAT_IDS"))
ALLOWED_SET = frozenset(ALLOWED)
SECRET = _env("WEBHOOK_SECRET")
PORT = _env_int("WEBHOOK_PORT", 8090)

RADARR = _env("RADARR_URL", "http://radarr:7878").rstrip("/")
RKEY = _env("RADARR_API_KEY")
SONARR = _env("SONARR_URL", "http://sonarr:8989").rstrip("/")
SKEY = _env("SONARR_API_KEY")
QBIT = _env("QBIT_URL", "http://gluetun:8080").rstrip("/")
QBIT_USER = _env("QBIT_USERNAME", "admin")
QBIT_PASS = _env("QBIT_PASSWORD")
JSEERR = _env("JELLYSEERR_URL", "http://jellyseerr:5055").rstrip("/")
JKEY = _env("JELLYSEERR_API_KEY")

try:
    LOCAL_TZ = ZoneInfo(_env("TZ", "Etc/UTC"))
except Exception:
    log.warning("TZ is not a known time zone; using UTC")
    LOCAL_TZ = timezone.utc

REPORT_HOUR = _env_int("REPORT_HOUR", 9)
DEAD_MIN = _env_int("DEAD_MINUTES", 30)
GRACE = _env_int("FALLBACK_GRACE_SEC", 300)          # wait after an add before deciding nothing matched
DISK_ALERT_PCT = _env_int("DISK_ALERT_PCT", 80)
PREFERRED_LANGUAGE = _env("PREFERRED_LANGUAGE", "English")
EPISODE_CAP_GB = _env_float("EPISODE_CAP_GB", 2.5)
EP_CAP = EPISODE_CAP_GB * GIB      # the apps display sizes in units of 1024^3, as does size_cap_watch

# MEDIA_ROOT is mounted at /data in every container; the folders below it come from .env.
DATA = "/data"
DOWNLOADS_DIR = _env("DOWNLOADS_DIR", "downloads").strip("/")
MOVIES_DIR = _env("MOVIES_DIR", "movies").strip("/")
TV_DIR = _env("TV_DIR", "tv").strip("/")
LIBRARIES, _BAD_LIBS = langs.parse_libraries(_env("MOVIE_LIBRARIES"))   # language code -> folder

STATE_DIR = _env("STATE_DIR", "/state")
CONFIRM_DIR = os.path.join(STATE_DIR, "confirms")
NEW_SEEN = os.path.join(STATE_DIR, "new_releases_seen.json")
FILL_STATE = os.path.join(STATE_DIR, "missing_fill.json")
DISK_STATE = os.path.join(STATE_DIR, "disk_alerts.json")

# New releases (Jellyseerr): picks per original language and day, the countries whose streaming
# services count, and how far back a release may be (digital releases land weeks after cinemas).
NEW_HOUR = _env_int("NEW_RELEASES_HOUR", 18)
NEW_REGIONS = [x.strip().upper() for x in _env("NEW_RELEASES_REGIONS", "US").split(",") if x.strip()]
NEW_LANG_CAPS = _parse_lang_caps(_env("NEW_RELEASES_LANG_CAPS", "en:5:3"))
NEW_DAYS_MOVIES = _env_int("NEW_RELEASES_DAYS_MOVIES", 90)
NEW_DAYS_TV = _env_int("NEW_RELEASES_DAYS_TV", 45)
# "all" follows every service and store in those countries; otherwise a comma-separated list of
# provider names as TMDB spells them (case does not matter).
_SERVICES = _env("NEW_RELEASES_SERVICES", "all")
NEW_ALL = _SERVICES.lower() == "all"
NEW_FOLLOW = {" ".join(x.lower().split()) for x in _SERVICES.split(",") if x.strip()}
# The first language in NEW_RELEASES_LANG_CAPS is usually the big one: a TMDB vote floor keeps
# obscure filler out of its picks. Extra languages have far fewer TMDB votes, so a floor would
# hide nearly all of their titles; they get none.
NEW_MAIN_VOTE_FLOOR = 10
REGION_LABEL = {"GB": "UK"}

FILL_HOUR = _env_int("FILL_HOUR", 7)
FILL_SEASONS = _env_int("FILL_SEASONS_PER_DAY", 4)

FORCE_TTL = 6 * 3600         # a user pick whose Grab webhook never arrives stops counting after this
RETRY_PAUSE = 5.0            # seconds between the two Radarr remove attempts
QBIT_SETTLE = 1.0            # seconds qBittorrent gets to process a delete before it is checked
BIG_MOVIE_GB = 3.2           # 'bigger than usual' note when the quality has no preferred size
CONFIRM_POLL_SEC = 4         # how often /state/confirms is checked for new questions
DEAD_SCAN_SEC = 600          # dead-torrent cleanup interval
FAKE_FIRST_SEC = 180         # first fake-download check after start
FAKE_SCAN_SEC = 900          # fake-download cleanup interval
DISK_FIRST_SEC = 300
DISK_SCAN_SEC = 6 * 3600
DISK_REPEAT_SEC = 86400      # at most one alert per disk per day
SONARR_BATCH_SEC = 45        # a season pack imports one file at a time; one message per show
FILL_COOLDOWN = 21 * 86400   # a season the filler tried is left alone this long
FILL_LEAVE_SEC = 90 * 86400  # 'Leave it' on a 720p offer: ask again after about three months
FILL_GAP_SEC = 10            # pause between season searches (indexers rate-limit)
ARR_CATEGORIES = {"radarr", "sonarr"}   # qBittorrent categories that belong to the apps
HASH_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")   # never pass 'all' or junk to qBittorrent
CONFIRM_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")       # becomes callback data and a file name

# ================================================================== runtime state
QPROFILE = None                      # Radarr quality profile id for new movies
MOVIE_ROOT = f"{DATA}/{MOVIES_DIR}"  # default movie library (as Radarr knows it)
MOVIE_LIBS = {}                      # language code -> /data/<folder> for MOVIE_LIBRARIES entries
SPROFILE = None
SROOT = f"{DATA}/{TV_DIR}"
FORCE_IDS = ForcePicks(ttl=FORCE_TTL)   # user picks pass the Grab check unjudged
REL_CACHE = {}       # movieId -> releases of the last force-grab list
CANCEL_MSGS = {}     # torrent hash -> [(chat_id, message_id)] of messages carrying its Cancel button
CANCEL_BUSY = set()  # hashes whose Cancel tap is being handled (double taps are ignored)
FB_CACHE = {}        # token -> {"kind", "rels", ids...} behind the closest-release buttons
FB_SEQ = 0
CONFIRM_MSGS = {}    # confirmation id -> [(chat_id, message_id)]
ANSWERED = {}        # confirmation id -> "yes"/"no"; a second tap cannot overwrite the first answer
SHOWN_ASKS = set()   # confirmation ids already shown whose .ask file could not be removed
EP_READY = {}        # Sonarr series id -> imports waiting to be announced together
BUSY = set()         # long jobs running right now ("fill", "new")
_TASKS = set()
_STRANGERS = {}
_WEBHOOK_REJECTS = {}
_LOG_ONCE = set()


def log_once(key, msg, *args):
    if key not in _LOG_ONCE:
        _LOG_ONCE.add(key)
        log.warning(msg, *args)


# ================================================================== blocking helpers
# THE ASYNC RULE: every function in this section blocks (network calls, sleeps, slow disks).
# Never call one of them, or anything that calls them, directly inside an `async def`; always
# `await asyncio.to_thread(fn, ...)`. Radarr and Sonarr deliver their webhooks synchronously from
# inside their own API calls (a POST /api/v3/release waits until the Grab webhook was delivered),
# so one blocking call on the event loop leaves the bot waiting for Radarr while Radarr waits for
# the bot. tools/block_scan.py enforces this rule in CI.

def _short(url):
    return url.split("?", 1)[0].rsplit("/", 1)[-1]


def _http(method, url, *, headers=None, params=None, json_body=None, timeout=45, tries=8):
    """requests.request that rides out timeouts and refused connections with growing pauses.

    Radarr and Sonarr answer slowly or not at all while their database is busy (a library scan,
    a backup). HTTP error statuses are returned to the caller, not retried.
    """
    last = None
    for attempt in range(max(1, tries)):
        try:
            return requests.request(method, url, headers=headers, params=params, json=json_body, timeout=timeout)
        except (requests.Timeout, requests.ConnectionError) as e:
            last = e
            log.warning("%s %s failed (try %d/%d): %s", method, _short(url), attempt + 1, tries, type(e).__name__)
            if attempt + 1 < tries:
                time.sleep(min(2 ** attempt, 20))
    raise last


def _json(r):
    return r.json() if r.text.strip() else None


def _body(r):
    """Response body for error messages: JSON when it parses, else the start of the text."""
    try:
        return _json(r)
    except ValueError:
        return r.text[:300]


# ---------- Radarr
def rget(path, **params):
    r = _http("GET", f"{RADARR}/api/v3/{path}", headers={"X-Api-Key": RKEY}, params=params)
    r.raise_for_status()
    return _json(r)


def rpost(path, body):
    r = _http("POST", f"{RADARR}/api/v3/{path}", headers={"X-Api-Key": RKEY}, json_body=body)
    return r.status_code, _body(r)


def rput(path, body):
    r = _http("PUT", f"{RADARR}/api/v3/{path}", headers={"X-Api-Key": RKEY}, json_body=body)
    return r.status_code, _body(r)


def rdelete(path, **params):
    return _http("DELETE", f"{RADARR}/api/v3/{path}", headers={"X-Api-Key": RKEY}, params=params).status_code


def rsearch_releases(mid):
    """Interactive search of every indexer for one movie (slow: up to two minutes)."""
    r = _http("GET", f"{RADARR}/api/v3/release", headers={"X-Api-Key": RKEY}, params={"movieId": mid}, timeout=120)
    r.raise_for_status()
    return _json(r) or []


def queue_id_for(h):
    try:
        recs = (rget("queue", pageSize=1000, includeMovie=False) or {}).get("records", [])
        return next((x["id"] for x in recs if (x.get("downloadId") or "").lower() == h.lower()), None)
    except Exception as e:
        log.warning("radarr queue lookup failed: %s", type(e).__name__)
        return None


# ---------- Sonarr
def sget(path, **params):
    r = _http("GET", f"{SONARR}/api/v3/{path}", headers={"X-Api-Key": SKEY}, params=params)
    r.raise_for_status()
    return _json(r)


def spost(path, body):
    r = _http("POST", f"{SONARR}/api/v3/{path}", headers={"X-Api-Key": SKEY}, json_body=body)
    return r.status_code, _body(r)


def sdelete(path, **params):
    return _http("DELETE", f"{SONARR}/api/v3/{path}", headers={"X-Api-Key": SKEY}, params=params).status_code


def squeue_id_for(h):
    if not SKEY:
        return None
    try:
        recs = (sget("queue", pageSize=1000, includeUnknownSeriesItems=True) or {}).get("records", [])
        return next((x["id"] for x in recs if (x.get("downloadId") or "").lower() == h.lower()), None)
    except Exception as e:
        log.warning("sonarr queue lookup failed: %s", type(e).__name__)
        return None


def ssearch_releases(sid, season):
    r = _http("GET", f"{SONARR}/api/v3/release", headers={"X-Api-Key": SKEY},
              params={"seriesId": sid, "seasonNumber": season}, timeout=120)
    r.raise_for_status()
    return _json(r) or []


def season_releases(sid, sn):
    """Season search for the filler. It asks every indexer (some through FlareSolverr) and can take
    minutes, so it gets one long try instead of short timeouts with retries."""
    r = _http("GET", f"{SONARR}/api/v3/release", headers={"X-Api-Key": SKEY},
              params={"seriesId": sid, "seasonNumber": sn}, timeout=600, tries=1)
    r.raise_for_status()
    return _json(r) or []


# ---------- Jellyseerr
def jget(path, **params):
    r = _http("GET", f"{JSEERR}/api/v1/{path}", headers={"X-Api-Key": JKEY}, params=params, tries=3)
    if r.status_code == 404:
        return {}
    r.raise_for_status()
    return _json(r) or {}


# ---------- qBittorrent (WebUI API v2, logged in with QBIT_USERNAME / QBIT_PASSWORD)
_QS = requests.Session()
_QLOCK = threading.Lock()
_QSTATE = {"ok": False, "retry_at": 0.0}
QBIT_LOGIN_BACKOFF = 300     # qBittorrent bans an address after a few failed logins: never hammer it
_QBIT_V4_NAMES = {"stop": "pause", "start": "resume"}   # qBittorrent 4.x names of the v5 endpoints


def _qbit_login():
    with _QLOCK:
        if _QSTATE["ok"] or time.time() < _QSTATE["retry_at"]:
            return _QSTATE["ok"]
        try:
            r = _QS.post(f"{QBIT}/api/v2/auth/login", data={"username": QBIT_USER, "password": QBIT_PASS},
                         timeout=15)
        except requests.RequestException as e:
            log.warning("qBittorrent login: no answer (%s)", type(e).__name__)
            return False
        # older versions answer 200 "Ok." (or "Fails."); 5.x answers 204, and 401 when refused
        if r.status_code == 204 or (r.status_code == 200 and r.text.strip().lower().startswith("ok")):
            _QSTATE["ok"] = True
            return True
        _QSTATE["retry_at"] = time.time() + QBIT_LOGIN_BACKOFF
        log_once("qbit-login", "qBittorrent refused the login (HTTP %s): check QBIT_USERNAME and QBIT_PASSWORD",
                 r.status_code)
        return False


def _qreq(method, path, *, params=None, data=None, timeout=15):
    """One WebUI call with the session cookie; after a 403 (expired session) it logs in once more."""
    r = None
    for attempt in (1, 2):
        if not _QSTATE["ok"]:
            _qbit_login()
        r = _QS.request(method, f"{QBIT}/api/v2/{path}", params=params, data=data, timeout=timeout)
        if r.status_code == 403 and attempt == 1:
            with _QLOCK:
                _QSTATE["ok"] = False
            continue
        break
    return r


def qpost(action, **data):
    """POST torrents/<action>. Returns the HTTP status, or None when qBittorrent did not answer."""
    try:
        r = _qreq("POST", f"torrents/{action}", data=data)
        if r.status_code == 404 and action in _QBIT_V4_NAMES:
            r = _qreq("POST", f"torrents/{_QBIT_V4_NAMES[action]}", data=data)
        return r.status_code
    except requests.RequestException as e:
        log.warning("qbit %s failed: %s", action, type(e).__name__)
        return None


def qinfo(h=None):
    """Torrent list ([] when qBittorrent does not answer)."""
    got = qinfo_checked(h)
    return got if got is not None else []


def qinfo_checked(h=None):
    """Like qinfo but tells 'not in qBittorrent' ([]) apart from 'qBittorrent did not answer' (None).
    The API can stall for minutes while qBittorrent moves a big finished torrent between disks."""
    try:
        r = _qreq("GET", "torrents/info", params=({"hashes": h} if h else {}))
        if r.status_code != 200:
            log.warning("qbit info -> HTTP %s", r.status_code)
            return None
        body = r.json() if r.text.strip() else []
        return body if isinstance(body, list) else None
    except (requests.RequestException, ValueError) as e:
        log.warning("qbit info failed: %s", type(e).__name__)
        return None


def qfiles(h):
    """Files of one torrent, or None when qBittorrent did not answer."""
    try:
        r = _qreq("GET", "torrents/files", params={"hash": h})
        if r.status_code != 200:
            return None
        body = r.json() if r.text.strip() else []
        return body if isinstance(body, list) else None
    except (requests.RequestException, ValueError) as e:
        log.warning("qbit files failed: %s", type(e).__name__)
        return None


def qbit_delete_verified(h):
    """Delete THIS ONE torrent (and its files) and confirm qBittorrent no longer lists it."""
    if not HASH_RE.match(h or ""):
        return False
    for attempt in (1, 2):
        qpost("delete", hashes=h, deleteFiles="true")
        for _ in range(3):
            time.sleep(QBIT_SETTLE)
            if qinfo_checked(h) == []:
                return True
        log.warning("qbit delete %s not confirmed (try %d/2)", h[:10], attempt)
    return False


# ---------- removing downloads
def radarr_queue_remove(qid, h):
    """DELETE queue/<qid> (remove from client, blocklist, search again), retried once.
    Returns (True, '') on success, else (False, 'HTTP 500' / 'no answer')."""
    detail = ""
    for attempt in (1, 2):
        try:
            sc = rdelete(f"queue/{qid}", removeFromClient="true", blocklist="true", skipRedownload="false")
        except Exception as e:
            sc = None
            log.warning("radarr queue delete %s failed: %s", qid, type(e).__name__)
        if isinstance(sc, int) and 200 <= sc < 300:
            return True, ""
        detail = f"HTTP {sc}" if sc is not None else "no answer"
        log.warning("radarr queue delete %s for %s -> %s (try %d/2)", qid, h[:10], detail, attempt)
        if attempt == 1:
            time.sleep(RETRY_PAUSE)
    return False, detail


def remove_refused(h, qid):
    """Remove a grab that breaks the settings and report what REALLY happened.
    -> ('radarr' | 'qbit' | 'failed', detail). The qBittorrent fallback deletes only this hash."""
    detail = ""
    if qid:
        ok, detail = radarr_queue_remove(qid, h)
        if ok:
            return "radarr", ""
    return ("qbit" if qbit_delete_verified(h) else "failed"), detail


def cancel_download(h):
    """Cancel button: returns (HTML suffix for the message, keep the button for another try).
    A download that already finished or was imported is never deleted."""
    qid = queue_id_for(h)
    detail = ""
    if qid:
        ok, detail = radarr_queue_remove(qid, h)
        if ok:
            return "\n\n❌ <b>Cancelled.</b> Removed and blocklisted; Radarr will look for a different release.", False
    info = qinfo_checked(h)
    why = f"Radarr: {esc(detail)}; " if detail else ""
    if info is None:
        return (f"\n\n⚠️ <b>Couldn't cancel</b> ({why}qBittorrent didn't answer). Nothing was changed; "
                "try again in a minute."), True
    if not info or torrent_finished(info[0]):
        return "\n\nℹ️ <b>Already finished or imported: nothing cancelled.</b>", False
    if qbit_delete_verified(h):
        how = f"Radarr wouldn't remove it ({esc(detail)})" if detail else "Radarr didn't list it (or didn't answer)"
        return (f"\n\n❌ <b>Cancelled</b>: deleted from the download client. {how}, so it is NOT "
                "blocklisted and Radarr won't look for another release on its own."), False
    return (f"\n\n⚠️ <b>Couldn't cancel</b> ({why}qBittorrent didn't confirm the delete). "
            "It may still be downloading; try again, or delete it by hand in qBittorrent."), True


# ---------- app defaults
def radarr_defaults():
    """Quality profile and library roots for new movies, checked against Radarr's root folders."""
    global QPROFILE, MOVIE_ROOT, MOVIE_LIBS
    if not QPROFILE:
        QPROFILE = pick_profile(rget("qualityprofile"))
    roots = [str(r.get("path") or "").rstrip("/") for r in (rget("rootfolder") or [])]
    want = f"{DATA}/{MOVIES_DIR}"
    MOVIE_ROOT = want if (want in roots or not roots) else roots[0]
    if MOVIE_ROOT != want:
        log_once("movie-root", "radarr has no root folder %s; new movies go to %s", want, MOVIE_ROOT)
    libs = {}
    for code, folder in LIBRARIES.items():
        path = f"{DATA}/{folder}"
        if roots and path not in roots:
            log_once("lib-" + code, "MOVIE_LIBRARIES: %s is not a root folder in Radarr; %s titles go to %s",
                     path, langs.lang_name(code), MOVIE_ROOT)
            continue
        libs[code] = path
    MOVIE_LIBS = libs
    log.info("radarr: qualityProfileId=%s root=%s libraries=%s", QPROFILE, MOVIE_ROOT, MOVIE_LIBS or "none")


def sonarr_defaults():
    global SPROFILE, SROOT
    if not SKEY:
        return
    if not SPROFILE:
        SPROFILE = pick_profile(sget("qualityprofile"))
    roots = [str(r.get("path") or "").rstrip("/") for r in (sget("rootfolder") or [])]
    want = f"{DATA}/{TV_DIR}"
    SROOT = want if (want in roots or not roots) else roots[0]
    log.info("sonarr: qualityProfileId=%s root=%s", SPROFILE, SROOT)


def movie_root_for(mv):
    """Library root by the movie's original language (Radarr gives a name, Jellyseerr a code)."""
    return langs.library_root((mv or {}).get("originalLanguage"), MOVIE_LIBS, MOVIE_ROOT)


def movie_grabbed(mid):
    """True if the movie already has a file or something is in the download queue for it."""
    try:
        if (rget(f"movie/{mid}") or {}).get("hasFile"):
            return True
        recs = (rget("queue", pageSize=1000) or {}).get("records", [])
        return any(x.get("movieId") == mid for x in recs)
    except Exception:
        return True    # on error assume it is handled, so the user is not nagged


def season_has_activity(sid, season):
    """True if a season already has a file, or a queued or downloading release."""
    try:
        recs = (sget("queue", pageSize=1000, includeEpisode=True) or {}).get("records", [])
        for x in recs:
            if x.get("seriesId") != sid:
                continue
            if (x.get("episode") or {}).get("seasonNumber") == season or x.get("seasonNumber") == season:
                return True
        eps = sget("episode", seriesId=sid) or []
        return any(e.get("seasonNumber") == season and e.get("hasFile") for e in eps)
    except Exception:
        return True


_CRIT = {"t": 0.0, "profiles": {}, "qdefs": {}}


def load_criteria():
    """Radarr's quality profiles and quality definitions, cached for ten minutes."""
    if time.time() - _CRIT["t"] > 600 or not _CRIT["profiles"]:
        _CRIT["profiles"] = {p["id"]: p for p in (rget("qualityprofile") or [])}
        _CRIT["qdefs"] = {d["quality"]["name"]: d for d in (rget("qualitydefinition") or [])}
        _CRIT["t"] = time.time()
    return _CRIT["profiles"], _CRIT["qdefs"]


# ---------- state files (small JSON files in /state)
def load_json(path):
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_json(path, obj):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh)
    os.replace(tmp, path)


def state_writable():
    try:
        os.makedirs(CONFIRM_DIR, exist_ok=True)
        probe = os.path.join(STATE_DIR, ".moviebot_write_test")
        with open(probe, "w", encoding="utf-8") as fh:
            fh.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


# ---------- Yes/No questions from the host scripts (files in /state/confirms)
def read_asks():
    """[(id, text)] for every new <id>.ask file. A file holds JSON {"text": "..."} or the bare
    text (HTML). Each file is removed once read; when that is not allowed (the folder belongs to
    another user) the id is remembered so the question is still shown only once."""
    try:
        names = sorted(os.listdir(CONFIRM_DIR))
    except FileNotFoundError:
        try:
            os.makedirs(CONFIRM_DIR, exist_ok=True)
        except OSError as e:
            log_once("ask-mkdir", "cannot create %s (%s); maintenance questions are off", CONFIRM_DIR,
                     type(e).__name__)
        return []
    except OSError as e:
        log_once("ask-list", "cannot read %s (%s); maintenance questions are off", CONFIRM_DIR, type(e).__name__)
        return []
    out = []
    for fn in names:
        if not fn.endswith(".ask"):
            continue
        cid = fn[:-4]
        path = os.path.join(CONFIRM_DIR, fn)
        if not CONFIRM_ID_RE.match(cid) or cid in SHOWN_ASKS:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                raw = fh.read().strip()
        except OSError:
            continue
        try:
            os.remove(path)
        except OSError as e:
            log_once("ask-rm", "cannot remove question files in %s (%s); check the folder's owner",
                     CONFIRM_DIR, type(e).__name__)
            if len(SHOWN_ASKS) > 1000:
                SHOWN_ASKS.clear()
            SHOWN_ASKS.add(cid)
        text = raw
        try:
            obj = json.loads(raw)
            if isinstance(obj, dict) and obj.get("text"):
                text = str(obj["text"])
        except ValueError:
            pass
        if text:
            out.append((cid, text))
    return out


def write_reply(cid, answer):
    """<id>.reply with 'yes' or 'no', written atomically so the asking script never reads half a file."""
    os.makedirs(CONFIRM_DIR, exist_ok=True)
    final = os.path.join(CONFIRM_DIR, cid + ".reply")
    tmp = final + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(answer)
    os.replace(tmp, final)


# ---------- disk space
def disk_targets():
    """[(label, path)]: the media folders under /data (downloads, libraries)."""
    folders = [DOWNLOADS_DIR, MOVIES_DIR, TV_DIR] + list(LIBRARIES.values())
    return [(f, f"{DATA}/{f}") for f in dict.fromkeys(folders)]


def disk_usage():
    """[(name, used %, free bytes)] per filesystem. Folders on the same disk are reported once;
    '/' is the container's own disk, i.e. Docker's storage on the server. Blocking: a dead
    network mount can hang right here."""
    groups = {}
    for label, path in disk_targets():
        try:
            dev = os.stat(path).st_dev
        except OSError:
            continue                                   # folder missing (not created yet, share absent)
        groups.setdefault(dev, ([], path))[0].append(label)
    try:
        if os.stat("/").st_dev not in groups:
            groups[os.stat("/").st_dev] = (["system disk"], "/")
    except OSError:
        pass
    out = []
    for labels, path in groups.values():
        try:
            u = shutil.disk_usage(path)
        except OSError as e:
            log.warning("disk space %s: %s", path, type(e).__name__)
            continue
        name = "System disk" if labels == ["system disk"] else "Media disk (" + ", ".join(labels) + ")"
        out.append((name, round(100 * u.used / u.total) if u.total else 0, u.free))
    return out


# ---------- new releases (Jellyseerr)
def _followed(name):
    if NEW_ALL:
        return True
    n = " ".join(str(name or "").lower().split())
    return n in NEW_FOLLOW or clean_service(name).lower() in NEW_FOLLOW


def _service_ids(kind):
    """{region: "8|9|..."}: ids of the followed services, looked up per country (ids differ by country)."""
    out = {}
    for reg in NEW_REGIONS:
        ps = jget(f"watchproviders/{'movies' if kind == 'movie' else 'tv'}", watchRegion=reg) or []
        ids = [str(x["id"]) for x in ps if isinstance(x, dict) and x.get("id") and _followed(x.get("name"))]
        if ids:
            out[reg] = "|".join(ids)
    return out


def _fmt_where(d):
    best = sorted(d.items(), key=lambda kv: -len(kv[1]))[:4]
    return " · ".join(f"{k} ({', '.join(v)})" for k, v in best)


def _where(det):
    """('Service A (XX, YY) · Service B (ZZ)', rent/buy line) from the title's providers in our regions."""
    subs, rent = {}, {}
    for w in det.get("watchProviders") or []:
        reg = w.get("iso_3166_1")
        if reg not in NEW_REGIONS:
            continue
        known = [x.get("name") for k in ("flatrate", "ads", "free", "rent", "buy") for x in (w.get(k) or [])]
        for kind, bucket in (("flatrate", subs), ("ads", subs), ("free", subs), ("rent", rent), ("buy", rent)):
            for x in w.get(kind) or []:
                name = (x.get("name") or "").strip()
                if not name or not _followed(name):
                    continue
                regs = bucket.setdefault(clean_service(name, known), [])
                tag = REGION_LABEL.get(reg, reg)
                if tag not in regs:
                    regs.append(tag)
    return _fmt_where(subs), _fmt_where(rent)


def new_card(kind, tmdb, det):
    """(caption HTML, photo URL or None, keyboard) for one new release."""
    title = det.get("title") or det.get("name") or "?"
    year = (det.get("releaseDate") or det.get("firstAirDate") or "")[:4]
    genres = ", ".join(g["name"] for g in (det.get("genres") or [])[:3] if g.get("name"))
    if kind == "movie":
        rr = jget(f"movie/{tmdb}/ratingscombined") or {}
        rt, imdb = rr.get("rt"), rr.get("imdb")
        rtime = det.get("runtime") or 0
        length = f"{rtime // 60}h {rtime % 60:02d}m" if rtime else ""
    else:
        rt, imdb = jget(f"tv/{tmdb}/ratings") or None, None
        n = det.get("numberOfSeasons")
        length = f"{n} season{'' if n == 1 else 's'}" if n else ""
    word, scores = critics_line(rt, imdb)
    subs, rent = _where(det)
    cast = ", ".join(c["name"] for c in ((det.get("credits") or {}).get("cast") or [])[:5] if c.get("name"))
    head = [f"🆕{'🎬' if kind == 'movie' else '📺'} <b>{esc(title)}</b> ({esc(year)})",
            esc(" · ".join(x for x in (genres, length) if x))]
    if subs:
        head.append(f"📺 {esc(subs)}")
    if rent:
        head.append(f"💳 Rent/buy: {esc(rent)}")
    if cast:
        head.append(f"🎭 <b>Starring:</b> {esc(cast)}")
    tail = f"⭐ <b>{esc(word)}</b>" + (f": {esc(scores)}" if scores else "")
    text = trim_caption(head, det.get("overview") or "", tail)
    img = det.get("backdropPath") or det.get("posterPath")
    photo = f"https://image.tmdb.org/t/p/{'w1280' if det.get('backdropPath') else 'w780'}{img}" if img else None
    if kind == "movie":
        add = InlineKeyboardButton("✅ Add & download", callback_data=f"addm:{tmdb}")
    else:
        tvdb = (det.get("externalIds") or {}).get("tvdbId")
        add = InlineKeyboardButton("✅ Add (all seasons)", callback_data=f"adds:{tvdb}")
    kb = InlineKeyboardMarkup([[add, InlineKeyboardButton("🙈 Not interested", callback_data="nrskip")]])
    return text, photo, kb


def find_new_releases(seen):
    """Newest titles on the followed services, most popular first, each language with its own slots."""
    today = datetime.now(LOCAL_TZ).date()
    have_m = {m.get("tmdbId") for m in (rget("movie") or [])}
    have_t = {x.get("tvdbId") for x in (sget("series") or [])} if SKEY else set()
    main_lang = next(iter(NEW_LANG_CAPS), "")
    cards = []
    for kind, path, d_from, d_to, days in (
            ("movie", "discover/movies", "primaryReleaseDateGte", "primaryReleaseDateLte", NEW_DAYS_MOVIES),
            ("tv", "discover/tv", "firstAirDateGte", "firstAirDateLte", NEW_DAYS_TV)):
        since = today - timedelta(days=days)
        services = _service_ids(kind)
        for lang, caps in NEW_LANG_CAPS.items():
            cap = caps[0] if kind == "movie" else caps[1]
            if cap <= 0:
                continue
            pool = {}
            for reg, ids in services.items():
                for page in (1, 2):
                    res = jget(path, page=page, sortBy="popularity.desc", watchRegion=reg, watchProviders=ids,
                               language=lang, voteCountGte=NEW_MAIN_VOTE_FLOOR if lang == main_lang else 0,
                               **{d_from: str(since), d_to: str(today)}) or {}
                    for r in res.get("results") or []:
                        pool.setdefault(r["id"], r)
                    if page >= (res.get("totalPages") or 1):
                        break
            got = 0
            for r in sorted(pool.values(), key=lambda r: -(r.get("popularity") or 0)):
                if got >= cap:
                    break
                key = f"{kind}:{r['id']}"
                if key in seen or r.get("originalLanguage") != lang:
                    continue
                if ((r.get("mediaInfo") or {}).get("status") or 1) >= 2 or (kind == "movie" and r["id"] in have_m):
                    seen[key] = time.time()                 # already requested or in the library
                    continue
                det = jget(f"{kind}/{r['id']}") or {}
                if kind == "tv":
                    tvdb = (det.get("externalIds") or {}).get("tvdbId")
                    if not tvdb or tvdb in have_t:
                        seen[key] = time.time()
                        continue
                cards.append((key,) + new_card(kind, r["id"], det))
                got += 1
    return cards


def load_seen():
    return load_json(NEW_SEEN)


def save_seen(seen):
    cutoff = time.time() - 365 * 86400                 # a title is never suggested twice within a year
    save_json(NEW_SEEN, {k: v for k, v in seen.items() if isinstance(v, (int, float)) and v > cutoff})


# ---------- fake downloads
def clean_fakes():
    """Remove queue items whose download can never be imported (archive or program instead of video):
    blocklist them so the app searches for a real copy. Returns [(title, reason)]."""
    removed = []
    for name, get, delete in (("sonarr", sget, sdelete), ("radarr", rget, rdelete)):
        if name == "sonarr" and not SKEY:
            continue
        done = set()
        for q in (get("queue", pageSize=1000) or {}).get("records", []):
            h = (q.get("downloadId") or "").lower()
            if not h or h in done:
                continue
            why = fake_reason(qfiles(h)) or queue_fake_reason(q.get("statusMessages"))
            if not why:
                continue
            done.add(h)
            delete(f"queue/{q['id']}", removeFromClient="true", blocklist="true", skipRedownload="false")
            removed.append(((q.get("title") or "?")[:70], why))
            log.info("fake download removed (%s): %s", why, q.get("title"))
    return removed


# ---------- dead torrents
def reap_dead():
    """Remove torrents that are stalled with no seeders and no peers at all. Through Radarr/Sonarr
    (blocklisted, no instant re-search) when they track it; directly in qBittorrent only for the
    apps' own categories, so a torrent someone added by hand is never touched. Returns names."""
    now, removed = time.time(), []
    for t in qinfo():
        if not torrent_dead(t, now, DEAD_MIN):
            continue
        h = (t.get("hash") or "").lower()
        if not HASH_RE.match(h):
            continue
        qid = queue_id_for(h)
        sqid = None if qid else squeue_id_for(h)
        if qid:
            rdelete(f"queue/{qid}", removeFromClient="true", blocklist="true", skipRedownload="true")
        elif sqid:
            sdelete(f"queue/{sqid}", removeFromClient="true", blocklist="true", skipRedownload="true")
        elif (t.get("category") or "").lower() in ARR_CATEGORIES:
            qpost("delete", hashes=h, deleteFiles="true")
        else:
            continue
        removed.append((t.get("name") or "?")[:55])
    return removed


# ---------- missing-episode filler
def missing_seasons():
    """[(series id, title, season, wanted episode numbers, aired episodes in the season)] for monitored
    seasons with aired, missing episodes that are not already downloading."""
    now = datetime.now(timezone.utc)
    queued = set()
    for x in (sget("queue", pageSize=1000, includeEpisode=True) or {}).get("records", []):
        e = x.get("episode") or {}
        queued.add((x.get("seriesId"), e.get("seasonNumber"), e.get("episodeNumber")))
    out = []
    for srs in sget("series") or []:
        st = srs.get("statistics") or {}
        if not srs.get("monitored") or (st.get("episodeFileCount") or 0) >= (st.get("episodeCount") or 0):
            continue
        by = {}
        for e in sget("episode", seriesId=srs["id"]) or []:
            if not e.get("seasonNumber") or not e.get("airDateUtc"):
                continue
            try:
                if parse_iso(e["airDateUtc"]) > now - timedelta(days=1):
                    continue                           # a fresh episode gets a day of normal RSS first
            except ValueError:
                continue
            b = by.setdefault(e["seasonNumber"], {"n": 0, "want": set()})
            b["n"] += 1
            if (e.get("monitored") and not e.get("hasFile")
                    and (srs["id"], e["seasonNumber"], e["episodeNumber"]) not in queued):
                b["want"].add(e["episodeNumber"])
        out += [(srs["id"], srs.get("title") or "?", sn, b["want"], b["n"]) for sn, b in by.items() if b["want"]]
    return out


def grab_series_release(r):
    sc, _ = spost("release", {"guid": r["guid"], "indexerId": r["indexerId"]})
    return sc in (200, 201)


def fill_run(state, budget=None):
    """One filler pass over at most `budget` seasons. Returns (grabbed lines, 720p offers)."""
    now = time.time()
    todo = [x for x in missing_seasons() if now - state.get(f"{x[0]}:{x[2]}", 0) > FILL_COOLDOWN]
    todo.sort(key=lambda x: state.get(f"{x[0]}:{x[2]}", 0))          # never-tried seasons first
    grabbed, offers = [], []
    for sid, title, sn, want, n in todo[:budget or FILL_SEASONS]:
        state[f"{sid}:{sn}"] = now
        try:
            rels = season_releases(sid, sn)
        except Exception as e:
            log.warning("fill lookup %s S%s failed: %s", sid, sn, type(e).__name__)
            continue
        picks, _ = fill_pick(rels, want, n, 1080, EP_CAP)
        got = set()
        for r in picks:
            if grab_series_release(r):
                got |= (set(want) if r.get("fullSeason") else set(ep_list(r)))
        if got:
            grabbed.append(f"{title} S{sn:02d}: {len(got)} episode{'' if len(got) == 1 else 's'}")
        left = set(want) - got
        if left:
            p720, _ = fill_pick(rels, left, n, 720, EP_CAP)
            if p720:
                best = max(p720, key=lambda r: r.get("seeders") or 0)
                offers.append((sid, title, sn, sorted(left), len(p720), best))
        time.sleep(FILL_GAP_SEC)
    return grabbed, offers


def grab_720(sid, sn):
    """'Get 720p' tap: grab the 720p copies for the season's missing episodes. Returns HTML."""
    eps = sget("episode", seriesId=sid, seasonNumber=sn) or []
    now = datetime.now(timezone.utc)
    aired = []
    for e in eps:
        try:
            if e.get("airDateUtc") and parse_iso(e["airDateUtc"]) <= now:
                aired.append(e)
        except ValueError:
            continue
    want = {e["episodeNumber"] for e in aired if e.get("monitored") and not e.get("hasFile")}
    if not want:
        return "✅ Nothing is missing any more."
    picks, covered = fill_pick(season_releases(sid, sn), want, len(aired), 720, EP_CAP)
    got = set()
    for r in picks:
        if grab_series_release(r):
            got |= covered if r.get("fullSeason") else set(ep_list(r))
    n = len(got & want)
    if not n:
        return "⚠️ The 720p copies are gone now; I'll look again in a few weeks."
    return f"✅ <b>Downloading 720p</b> for {n} episode{'' if n == 1 else 's'}."


# ---------- daily report
def build_report():
    since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
    hist = rget("history/since", date=since) or []
    movies = {m["id"]: m for m in (rget("movie") or [])}
    grabbed, imported = {}, []
    for h in hist:
        did = h.get("downloadId") or ""
        if h.get("eventType") == "grabbed" and did:
            grabbed.setdefault(did, h.get("date"))
        elif h.get("eventType") == "downloadFolderImported":
            imported.append(h)
    items, mtotal = [], 0
    for imp in imported:
        mv = movies.get(imp.get("movieId"), {})
        title = f"{mv.get('title')} ({mv.get('year')})" if mv.get("title") else (imp.get("sourceTitle") or "?")
        size = int((imp.get("data") or {}).get("size", 0) or 0)
        mtotal += size
        dur, g = None, grabbed.get(imp.get("downloadId") or "")
        if g:
            try:
                dur = (parse_iso(imp["date"]) - parse_iso(g)).total_seconds()
                if dur <= 0:
                    dur = None
            except (KeyError, ValueError):
                dur = None
        items.append({"title": title, "gb": size / GIB, "dur": dur})
    tv_n, tv_gb, tv_shows = 0, 0.0, {}
    if SKEY:
        try:
            for h in sget("history/since", date=since, includeSeries=True) or []:
                if h.get("eventType") == "downloadFolderImported":
                    tv_n += 1
                    tv_gb += int((h.get("data") or {}).get("size", 0) or 0) / GIB
                    show = (h.get("series") or {}).get("title") or "?"
                    tv_shows[show] = tv_shows.get(show, 0) + 1
        except Exception as e:
            log.warning("sonarr history: %s", type(e).__name__)
    active = [t for t in qinfo() if not torrent_finished(t)]
    still = f"\n⬇️ Still downloading: <b>{len(active)}</b>" if active else ""
    disks = " · ".join(f"{esc(n)} {p}%" for n, p, _ in disk_usage())
    disk_line = f"\n💾 {disks}" if disks else ""
    if not items and not tv_n:
        return "📊 <b>Daily report</b>: last 24 hours\n\nNothing finished downloading." + still + disk_line
    lines = ["📊 <b>Daily report</b>: last 24 hours\n",
             f"🎬 Movies: <b>{len(items)}</b> ({mtotal / GIB:.1f} GB)"]
    for it in items[:10]:
        lines.append(f"     • {esc(it['title'])}")
    if len(items) > 10:
        lines.append(f"     • …and {len(items) - 10} more")
    if tv_n:
        lines.append(f"📺 TV episodes: <b>{tv_n}</b> ({tv_gb:.1f} GB)")
        top = sorted(tv_shows.items(), key=lambda kv: -kv[1])
        for show, n in top[:12]:
            lines.append(f"     • {esc(show)}: {n} episode{'' if n == 1 else 's'}")
        if len(top) > 12:
            lines.append(f"     • …and {len(top) - 12} more shows")
    lines.append(f"💾 Total: <b>{mtotal / GIB + tv_gb:.1f} GB</b>")
    timed = [i for i in items if i["dur"]]
    if timed:
        slow = max(timed, key=lambda x: x["dur"])
        fast = min(timed, key=lambda x: x["dur"])
        lines.append(f"\n🐢 Longest movie: {esc(slow['title'])}\n     {fmt_dur(slow['dur'])} · {slow['gb']:.1f} GB")
        lines.append(f"⚡ Fastest movie: {esc(fast['title'])}\n     {fmt_dur(fast['dur'])} · {fast['gb']:.1f} GB")
    return "\n".join(lines) + still + disk_line


# ================================================================== Telegram helpers
def spawn(coro):
    """Start a background task and keep a reference to it, so it is not garbage-collected mid-run."""
    task = asyncio.get_running_loop().create_task(coro)
    _TASKS.add(task)
    task.add_done_callback(_task_done)
    return task


def _task_done(task):
    _TASKS.discard(task)
    if not task.cancelled() and task.exception() is not None:
        log.warning("background task failed: %s", task.exception())


async def broadcast(bot, text, **kw):
    """Send a message to every allowed user. Returns [(chat_id, message_id)] of what went out."""
    out = []
    for cid in ALLOWED:
        try:
            m = await bot.send_message(chat_id=cid, text=text, **kw)
            if getattr(m, "message_id", None) is not None:
                out.append((cid, m.message_id))
        except Exception as e:
            log.warning("send to an allowed user failed: %s", e)
    return out


async def drop_buttons(bot, refs):
    """Remove the inline buttons from the given messages (best effort)."""
    for cid, mid in refs or []:
        try:
            await bot.edit_message_reply_markup(chat_id=cid, message_id=mid, reply_markup=None)
        except Exception as e:
            log.info("buttons on %s not removed: %s", mid, e)


def remember_cancel_msgs(h, sent):
    """Keep where each Cancel button went, so the import can take it away again."""
    if not h or not sent:
        return
    if len(CANCEL_MSGS) > 300:                        # bounded; a lost entry only means the button stays
        CANCEL_MSGS.pop(next(iter(CANCEL_MSGS)))
    CANCEL_MSGS[h] = list(sent)


def _msg_html(q):
    """HTML of the tapped message: plain text messages and photo cards alike."""
    m = q.message
    try:
        return (m.text_html if m.text else m.caption_html) or ""
    except AttributeError:                           # message too old for Telegram to show it
        return ""


async def _edit_msg(q, text, **kw):
    """edit_message_text that also works on photo cards. A caption holds at most 1024 characters,
    so a longer text goes out as a new message and the card just loses its buttons."""
    m = q.message
    if not getattr(m, "photo", None):
        return await q.edit_message_text(text, **kw)
    kw.pop("link_preview_options", None)
    if plain_len(text) <= CAPTION_LIMIT:
        return await q.edit_message_caption(caption=text, **kw)
    try:
        await q.edit_message_reply_markup(None)
    except Exception:
        pass
    return await m.reply_text(text, **kw)


async def finish(q, suffix):
    """Append a result to the tapped message and remove its buttons."""
    try:
        await _edit_msg(q, _msg_html(q) + suffix, parse_mode=ParseMode.HTML)
    except Exception:
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        try:
            await q.message.reply_text(suffix.strip(), parse_mode=ParseMode.HTML)
        except Exception:
            pass


def _reason(sc, res):
    """A Radarr/Sonarr error status and body -> a readable reason (already HTML-escaped)."""
    msgs = []
    if isinstance(res, list):
        msgs = [e["errorMessage"] for e in res if isinstance(e, dict) and e.get("errorMessage")]
    elif isinstance(res, dict):
        msgs = [res[k] for k in ("message", "errorMessage") if res.get(k)]
    elif isinstance(res, str) and res.strip():
        msgs = [res.strip()]
    text = "; ".join(str(m) for m in msgs)[:220]
    return esc(text) or {400: "bad request", 401: "not authorised (API key issue)", 403: "forbidden",
                         404: "not found", 409: "already exists", 500: "the server had an internal error",
                         502: "server unreachable", 503: "server busy, try again"}.get(sc, f"error {sc}")


def _folder(path):
    return (path or "").rstrip("/").rsplit("/", 1)[-1]


def refile_rows(mid, current_root):
    """One button per movie library (MOVIE_LIBRARIES plus the default) except the current one."""
    choices = [("default", MOVIE_ROOT)] + sorted(MOVIE_LIBS.items())
    row = [InlineKeyboardButton("📁 " + _folder(path), callback_data=f"mvroot:{mid}:{code}")
           for code, path in choices if path and path.rstrip("/") != (current_root or "").rstrip("/")]
    return [row[i:i + 3] for i in range(0, len(row), 3)]


def _rel_line(i, r, flag=""):
    gb = (r.get("size") or 0) / GIB
    sd = r.get("seeders")
    return (f"<b>{i + 1}.</b> {flag}{gb:.2f} GB · {esc(rel_quality_name(r))} · 🌱{sd if sd is not None else '?'}"
            f"{lang_note(r, (PREFERRED_LANGUAGE,))}\n"
            f"     <code>{esc((r.get('title') or '')[:44])}</code>{cf_flag(r)}")


CF_LEGEND = ("\n🚫 = your custom formats score it below 0 (wrong language or low quality); "
             "grab it only if you really want it.")


# ================================================================== "closest release" prompts
def _fb_token():
    global FB_SEQ
    FB_SEQ += 1
    if len(FB_CACHE) > 200:                          # keep the cache from growing forever
        FB_CACHE.clear()
    return str(FB_SEQ)


async def send_fallback_prompt(app, kind, heading, meta, rels, note=""):
    """Up to three closest releases with tap-to-grab buttons and a Skip button."""
    rels = rels[:3]
    if not rels:
        return
    tok = _fb_token()
    FB_CACHE[tok] = {"kind": kind, "rels": rels, **meta}
    intro = note or "Couldn't find a release that meets your settings."
    lines = [f"🤔 <b>{esc(heading)}</b>",
             f"{intro} Closest I found (a bigger size or a different quality is fine; tap one to grab it):\n"]
    btns = []
    for i, r in enumerate(rels):
        lines.append(_rel_line(i, r))
        label = str(i + 1) + (" 🚫" if cf_negative(r) else "")
        btns.append(InlineKeyboardButton(label, callback_data=f"fba:{tok}:{i}"))
    if any(cf_negative(r) for r in rels):
        lines.append(CF_LEGEND)
    kb = InlineKeyboardMarkup([btns, [InlineKeyboardButton("❌ Skip", callback_data=f"fbx:{tok}")]])
    await broadcast(app.bot, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=kb)


async def watch_movie_fallback(app, mid, title, year):
    """GRACE seconds after an add: when nothing was grabbed, offer the closest releases."""
    try:
        await asyncio.sleep(GRACE)
        if await asyncio.to_thread(movie_grabbed, mid):
            return                                   # a release was grabbed; the Grab webhook handled it
        rels = await asyncio.to_thread(rsearch_releases, mid)
        real = [r for r in rels if not is_cam(r)]
        name = f"<b>{esc(title)}</b> ({esc(year)})"
        if rels and not real:
            await broadcast(app.bot, f"🎬 {name}: only cinema recordings (CAM/TS) are out so far, so nothing was "
                                     "grabbed. Radarr will get a proper copy once it is released.",
                            parse_mode=ParseMode.HTML)
            return
        cand = [r for r in real if (r.get("seeders") or 0) > 0] or real
        if not cand:
            await broadcast(app.bot, f"🤷 {name}: no releases on your indexers yet. Still watching.",
                            parse_mode=ParseMode.HTML)
            return
        await send_fallback_prompt(app, "movie", f"{title} ({year})", {"mid": mid}, closest_clean_first(cand))
    except Exception as e:
        log.warning("movie fallback failed: %s", e)


async def grab_full_seasons(app, sid, title, seasons):
    """First add of a series: a COMPLETE season pack for each finished season (never loose episodes).
    A season still airing is searched episode by episode now, and Sonarr's monitoring grabs the
    rest week by week."""
    try:
        eps = []
        for _ in range(12):                          # Sonarr needs up to a minute to fill the episode table
            eps = await asyncio.to_thread(sget, "episode", seriesId=sid) or []
            if eps:
                break
            await asyncio.sleep(5)
        now = datetime.now(timezone.utc)
        covered, prompted, searched = set(), 0, 0
        for sn in seasons:
            if await asyncio.to_thread(season_has_activity, sid, sn):
                continue
            if not season_complete(eps, sn, now):
                await asyncio.to_thread(spost, "command", {"name": "SeasonSearch", "seriesId": sid, "seasonNumber": sn})
                continue
            if sn in covered:
                continue                             # already inside a grabbed multi-season pack
            if searched >= 20:                       # cap live pack searches per add; Sonarr searches the rest
                await asyncio.to_thread(spost, "command", {"name": "SeasonSearch", "seriesId": sid, "seasonNumber": sn})
                continue
            searched += 1
            try:
                rels = await asyncio.to_thread(ssearch_releases, sid, sn)
            except Exception as e:
                log.warning("season search %s S%s failed: %s", sid, sn, type(e).__name__)
                continue
            # the healthiest non-overlapping pack, never one Sonarr scored below 0
            # (POST /release does not re-check custom formats, so a rejected pack would be grabbed as-is)
            best, packs, dropped = choose_season_pack(rels, sn, seasons, covered)
            if dropped:
                log.info("series %s S%s: skipped %d healthy pack(s) with a negative custom-format score",
                         sid, sn, dropped)
            if best:
                log.info("series %s S%s: auto-grab pack cf=%s seeders=%s", sid, sn, best.get("customFormatScore"),
                         best.get("seeders"))
                sc, _ = await asyncio.to_thread(spost, "release",
                                                {"guid": best["guid"], "indexerId": best["indexerId"]})
                if sc in (200, 201):
                    covered |= covered_seasons(best.get("title"), sn, seasons)
                    continue
            if prompted >= 4:
                continue                             # anti-spam; the rest can be requested again
            prompted += 1
            weak = [r for r in packs if (r.get("seeders") or 0) > 0]
            offer = weak or [r for r in rels if (r.get("seeders") or 0) > 0] or rels
            if not offer:
                await broadcast(app.bot, f"🤷 <b>{esc(title)}</b>, season {sn}: no releases found yet; "
                                         "Sonarr keeps monitoring.", parse_mode=ParseMode.HTML)
                continue
            note = ("No complete season pack meets your settings. A full pack is preferred, but the options "
                    "below may be a different quality, weaker seeds or, as a last resort, loose episodes.")
            await send_fallback_prompt(app, "series", f"{title}, season {sn}", {"sid": sid, "season": sn},
                                       closest_clean_first(offer), note=note)
    except Exception as e:
        log.warning("grab_full_seasons failed: %s", e)


# ================================================================== Radarr / Sonarr webhooks
# Radarr's Grab webhook: a grab that meets the criteria set in Radarr itself (the movie's quality
# profile: allowed qualities and the minimum custom-format score; the quality definitions' size
# limits per minute of runtime) downloads straight away, and the user is told, with a Cancel
# button. Radarr applies those rules before an AUTOMATIC grab, so those always pass; the re-check
# catches grabs that bypassed them (a manual override in Radarr's own UI). The rules are read live
# from Radarr: there is no second copy of them in the bot. Picks made in this bot are not judged.
async def hold_and_ask(app, payload):
    try:
        if str(payload.get("downloadClientType") or "").lower() != "qbittorrent":
            return
        h = str(payload.get("downloadId") or "").lower()
        if not HASH_RE.match(h):
            return
        movie = payload.get("movie") or payload.get("remoteMovie") or {}
        rel = payload.get("release") or {}
        mid = movie.get("id")
        # only the release the user picked passes unjudged; an automatic grab of the same movie is
        # judged normally and leaves the user's pick in place
        if mid is not None and FORCE_IDS.take(mid, rel.get("releaseTitle"), rel.get("size")):
            await asyncio.to_thread(qpost, "start", hashes=h)
            log.info("grab %s: picked by the user in the bot -> downloading", h[:10])
            return
        if mid is not None and mid in FORCE_IDS:
            log.info("grab %s: movie %s has a pending pick, but this is another release -> checked normally",
                     h[:10], mid)
        title, year = movie.get("title") or "a movie", movie.get("year") or ""
        mv = {}
        if mid:
            try:
                mv = await asyncio.to_thread(rget, f"movie/{mid}") or {}
            except Exception as e:
                log.warning("movie lookup for the grab check failed: %s", type(e).__name__)
        checked, qdefs = bool(mv), {}
        try:
            profiles, qdefs = await asyncio.to_thread(load_criteria)
            probs = grab_problems(rel, mv, profiles.get(mv.get("qualityProfileId")), qdefs)
        except Exception as e:                       # Radarr already applied its rules before grabbing
            log.warning("criteria check failed (%s); letting the grab download", type(e).__name__)
            probs, checked = [], False
        try:
            size_gb = float(rel.get("size") or 0) / GIB
        except (TypeError, ValueError):
            size_gb = 0.0
        try:
            runtime = int(mv.get("runtime") or movie.get("runtime") or 0)
        except (TypeError, ValueError):
            runtime = 0
        log.info("grab %s: movie %s | %s | cf=%s | %.2f GB -> %s", h[:10], mid, rel.get("quality"),
                 rel.get("customFormatScore"), size_gb, ("REFUSED: " + "; ".join(probs)) if probs else "download")
        rt = f" · ⏱️ {runtime} min" if runtime else ""
        head = (f"🎬 <b>{esc(title)}</b> ({esc(year)}){rt}\n"
                f"📦 <code>{esc((rel.get('releaseTitle') or '?')[:70])}</code>\n"
                f"💾 {size_gb:.1f} GB · {esc(rel.get('quality') or '?')} · {esc(rel.get('releaseGroup') or 'N/A')}")
        if not probs:
            pref = ((qdefs or {}).get(rel.get("quality")) or {}).get("preferredSize")
            usual = pref * runtime / 1024 if isinstance(pref, (int, float)) and pref and runtime else BIG_MOVIE_GB
            warn = ""
            if size_gb > usual:                      # information only
                warn = (f"\n⚠️ <b>Bigger than usual</b>: a {runtime}-min movie at {size_gb:.1f} GB."
                        if runtime else f"\n⚠️ <b>Bigger than usual: {size_gb:.1f} GB.</b>")
            note = "" if checked else "\n(couldn't re-check the settings just now; Radarr had already checked them)"
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("❌ Cancel this download", callback_data=f"cancel:{h}")]])
            sent = await broadcast(app.bot, f"⬇️ <b>Downloading</b>\n{head}{warn}{note}\n\n"
                                            "It meets your settings, so there is nothing to approve.",
                                   parse_mode=ParseMode.HTML, reply_markup=kb)
            remember_cancel_msgs(h, sent)
            return
        # breaks a criterion: stop it at once, then remove it through Radarr (blocklisted, new search)
        for _ in range(20):                          # ~10 s stop loop to win the race against the download
            await asyncio.to_thread(qpost, "stop", hashes=h)
            info = await asyncio.to_thread(qinfo, h)
            if info and str(info[0].get("state") or "").lower().startswith(("stopped", "paused")):
                break
            await asyncio.sleep(0.5)
        qid = None
        for _ in range(60):                          # a new grab shows in Radarr's queue only after its next
            qid = await asyncio.to_thread(queue_id_for, h)   # refresh (about a minute); wait up to 10 min
            if qid:
                break
            await asyncio.sleep(10)
        # the message says what REALLY happened: Radarr's DELETE is checked and retried once; when
        # Radarr still won't remove it, this one hash is deleted in qBittorrent and verified gone
        how, detail = await asyncio.to_thread(remove_refused, h, qid)
        top = "⛔ <b>Not downloading</b>: it breaks your settings"
        if how == "radarr":
            outcome = "Removed and blocklisted; Radarr is looking for a release that meets your settings."
        elif how == "qbit" and qid:
            outcome = (f"Radarr wouldn't remove it ({esc(detail)}), so I deleted it from the download client myself. "
                       "It is NOT blocklisted and Radarr won't look for another release on its own; "
                       "tap ⚡ to pick one.")
        elif how == "qbit":
            outcome = "Removed from the download client (Radarr never listed it, so it could not be blocklisted)."
        else:
            top = "⚠️ <b>Breaks your settings, but I could NOT remove it</b>"
            why = f"Radarr: {esc(detail)}; " if qid else "Radarr never listed it; "
            outcome = (f"({why}qBittorrent didn't confirm the delete.) It is stopped for now, but anything that "
                       "resumes all torrents would start it again: delete it by hand in qBittorrent.")
        log.info("grab %s refused -> %s", h[:10], {"radarr": "radarr queue %s removed and blocklisted" % qid,
                                                    "qbit": "qbit delete (verified)",
                                                    "failed": "NOT removed (%s)" % (detail or "no queue id")}[how])
        reasons = "\n".join(f"• {esc(p)}" for p in probs)
        rows = [[InlineKeyboardButton("⚡ Pick a release myself", callback_data=f"force:{mid}")]] if mid else []
        await broadcast(app.bot, f"{top}\n{head}\n{reasons}\n\n{outcome}", parse_mode=ParseMode.HTML,
                        reply_markup=(InlineKeyboardMarkup(rows) if rows else None))
    except Exception as e:
        log.warning("hold_and_ask failed: %s", e)


async def notify_ready(app, payload):
    """Radarr import -> the movie is ready to watch (and its Cancel button has no job left)."""
    try:
        h = str(payload.get("downloadId") or "").lower()
        if h:
            await drop_buttons(app.bot, CANCEL_MSGS.pop(h, None))
        movie = payload.get("movie") or {}
        name = f"<b>{esc(movie.get('title') or 'A movie')}</b> ({esc(movie.get('year') or '')})"
        if payload.get("isUpgrade"):
            msg = f"⬆️ {name} was upgraded to a better copy."
        else:
            msg = f"🍿 {name} is ready to watch!"
        await broadcast(app.bot, msg, parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("notify_ready failed: %s", e)


async def import_list_added(app, payload):
    """A movie came in from a Radarr import list (a watchlist, for example), which saves everything to
    one root folder. File it by language exactly like a bot add (no files yet, so nothing moves on
    disk) and offer the library buttons, because TMDB's language is not always how a film is filed."""
    mid = (payload.get("movie") or {}).get("id")
    if not mid:
        return
    try:
        mv = await asyncio.to_thread(rget, f"movie/{mid}") or {}
        title, year = mv.get("title") or "?", mv.get("year") or ""
        root = movie_root_for(mv)
        if root and not (mv.get("path") or "").startswith(root.rstrip("/") + "/") and not mv.get("hasFile"):
            await asyncio.to_thread(rput, "movie/editor",
                                    {"movieIds": [mid], "rootFolderPath": root, "moveFiles": False})
        rows = refile_rows(mid, root)
        rows.append([InlineKeyboardButton("⚡ Not downloading? Force-grab a release", callback_data=f"force:{mid}")])
        await broadcast(app.bot, f"📋 <b>{esc(title)}</b> ({esc(year)}) was added from an import list.\n"
                                 f"📁 Saving to <b>{esc(_folder(root))}</b>. Searching now; I'll tell you when it "
                                 "starts downloading.",
                        parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))
        spawn(watch_movie_fallback(app, mid, title, year))
    except Exception as e:
        log.warning("import-list add %s failed: %s", mid, e)


def sonarr_ready(app, payload):
    """Buffer one Sonarr import. The first import of a show starts a short timer, so a season pack
    that imports file by file becomes one message instead of one per episode."""
    series = payload.get("series") or {}
    key = series.get("id") or series.get("title") or "?"
    files = payload.get("episodeFiles") or ([payload["episodeFile"]] if payload.get("episodeFile") else [])
    ent = EP_READY.get(key)
    if ent is None:
        ent = EP_READY[key] = {"title": series.get("title") or "A show", "eps": set(), "quality": [],
                               "all_upgrades": True}
        spawn(_flush_ready(app, key))
    for e in payload.get("episodes") or []:
        s, n = e.get("seasonNumber"), e.get("episodeNumber")
        if isinstance(s, int) and isinstance(n, int):
            ent["eps"].add((s, n))
    for f in files:
        qn = str((f or {}).get("quality") or "") if isinstance(f, dict) else ""
        if qn and qn not in ent["quality"]:
            ent["quality"].append(qn)
    ent["all_upgrades"] = ent["all_upgrades"] and bool(payload.get("isUpgrade"))


async def _flush_ready(app, key):
    await asyncio.sleep(SONARR_BATCH_SEC)
    ent = EP_READY.pop(key, None)
    if not ent:
        return
    codes = ep_codes(ent["eps"]) or "new episodes"
    quality = f" ({esc(', '.join(ent['quality'][:2]))})" if ent["quality"] else ""
    if ent["all_upgrades"]:
        msg = f"⬆️ <b>{esc(ent['title'])}</b> {esc(codes)} upgraded{quality}."
    else:
        msg = f"📺 <b>{esc(ent['title'])}</b> {esc(codes)} ready to watch{quality}."
    await broadcast(app.bot, msg, parse_mode=ParseMode.HTML)


def _webhook_rejected(app_name):
    now = time.time()
    if now - _WEBHOOK_REJECTS.get(app_name, 0) > 600:
        _WEBHOOK_REJECTS[app_name] = now
        log.warning("webhook /%s: wrong or missing secret (403); check the URL in %s's Connect settings",
                    app_name, app_name.capitalize())


def make_web(app):
    """The webhook server: POST /radarr/<secret> and /sonarr/<secret>; everything else is refused.
    Handlers only read the payload and start background tasks, so they answer at once."""
    def authorised(request):
        token = request.match_info.get("token", "")
        return bool(SECRET) and hmac.compare_digest(token.encode(), SECRET.encode())

    async def payload_of(request):
        try:
            data = await request.json()
        except Exception:
            return None
        return data if isinstance(data, dict) else None

    async def radarr_hook(request):
        if not authorised(request):
            _webhook_rejected("radarr")
            return web.json_response({"ok": False}, status=403)
        payload = await payload_of(request)
        if payload is None:
            return web.json_response({"ok": True})
        et = payload.get("eventType")
        if et == "Test":
            spawn(broadcast(app.bot, "🔔 Radarr is connected to the bot."))
        elif et == "Grab":
            spawn(hold_and_ask(app, payload))
        elif et in ("Download", "MovieFileImported"):
            spawn(notify_ready(app, payload))
        elif et == "MovieAdded" and str(payload.get("addMethod") or "").lower() == "list":
            spawn(import_list_added(app, payload))
        return web.json_response({"ok": True})

    async def sonarr_hook(request):
        if not authorised(request):
            _webhook_rejected("sonarr")
            return web.json_response({"ok": False}, status=403)
        payload = await payload_of(request)
        if payload is None:
            return web.json_response({"ok": True})
        et = payload.get("eventType")
        if et == "Test":
            spawn(broadcast(app.bot, "🔔 Sonarr is connected to the bot."))
        elif et in ("Download", "EpisodeFileImported"):
            sonarr_ready(app, payload)
        # Grab: nothing here; Sonarr grabs are checked host-side by the on-grab guard
        return web.json_response({"ok": True})

    async def refused(request):
        _webhook_rejected(request.path.strip("/").split("/", 1)[0] or "?")
        return web.json_response({"ok": False}, status=403)

    w = web.Application(client_max_size=2 * 1024 * 1024)
    w.router.add_post("/radarr/{token}", radarr_hook)
    w.router.add_post("/sonarr/{token}", sonarr_hook)
    w.router.add_post("/radarr", refused)
    w.router.add_post("/sonarr", refused)
    return w


# ================================================================== background jobs
async def sleep_until(hour, minute=0):
    """Sleep until the next local hour:minute (at least a minute). The difference is taken in UTC so
    a daylight-saving change does not shift the run by an hour."""
    now = datetime.now(LOCAL_TZ)
    target = now.replace(hour=hour % 24, minute=minute % 60, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    delay = (target.astimezone(timezone.utc) - now.astimezone(timezone.utc)).total_seconds()
    await asyncio.sleep(max(60, delay))


async def daily_loop(hour, minute, job, what):
    while True:
        try:
            await sleep_until(hour, minute)
            await job()
        except Exception as e:
            log.warning("%s loop error: %s", what, e)
            await asyncio.sleep(3600)


async def dead_cleanup(app):
    """Every 10 minutes: torrents with no seeders and no peers, older than DEAD_MINUTES, are removed."""
    while True:
        await asyncio.sleep(DEAD_SCAN_SEC)
        try:
            removed = await asyncio.to_thread(reap_dead)
            if removed:
                await broadcast(app.bot, "🧹 <b>Cleaned up dead torrents</b> (no seeders):\n"
                                + "\n".join(f"• <code>{esc(n)}</code>" for n in removed[:15]),
                                parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("dead cleanup error: %s", e)


async def fake_cleanup(app):
    """Every 15 minutes: downloads that are archives or programs instead of video are removed."""
    await asyncio.sleep(FAKE_FIRST_SEC)
    while True:
        try:
            removed = await asyncio.to_thread(clean_fakes)
            if removed:
                await broadcast(app.bot, "🧹 <b>Removed fake downloads</b>: blocked them and searching for a "
                                "real copy:\n"
                                + "\n".join(f"• <code>{esc(t)}</code>: {esc(w)}" for t, w in removed[:15]),
                                parse_mode=ParseMode.HTML)
        except Exception as e:
            log.warning("fake cleanup error: %s", e)
        await asyncio.sleep(FAKE_SCAN_SEC)


async def disk_watch(app):
    """Every 6 hours: a disk at or above DISK_ALERT_PCT gets one alert a day."""
    await asyncio.sleep(DISK_FIRST_SEC)
    while True:
        try:
            state, now = await asyncio.to_thread(load_json, DISK_STATE), time.time()
            for name, pct, free in await asyncio.to_thread(disk_usage):
                if pct >= DISK_ALERT_PCT and now - state.get(name, 0) > DISK_REPEAT_SEC:
                    await broadcast(app.bot, f"💾 <b>{esc(name)} is {pct}% full</b>: {fmt_bytes(free)} left.\n"
                                             "Delete what nobody watches any more, or add space: downloads stop "
                                             "when the disk is full.", parse_mode=ParseMode.HTML)
                    state[name] = now
            await asyncio.to_thread(save_json, DISK_STATE, state)
        except Exception as e:
            log.warning("disk watch error: %s", e)
        await asyncio.sleep(DISK_SCAN_SEC)


async def confirm_watcher(app):
    """Show the Yes/No questions of the host scripts (<id>.ask in /state/confirms) to every allowed
    user; the first answer is written back as <id>.reply. Polling the folder lets the scripts ask
    without a second Telegram poller (getUpdates allows only one consumer: this bot)."""
    while True:
        try:
            for cid, text in await asyncio.to_thread(read_asks):
                kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Yes", callback_data=f"cok:{cid}"),
                                            InlineKeyboardButton("❌ No", callback_data=f"cno:{cid}")]])
                head = "❓ <b>A maintenance job asks</b>\n\n"
                sent = await broadcast(app.bot, head + text, parse_mode=ParseMode.HTML, reply_markup=kb)
                if not sent:                         # not valid Telegram HTML: send it as plain text instead
                    sent = await broadcast(app.bot, head + esc(text), parse_mode=ParseMode.HTML, reply_markup=kb)
                if len(CONFIRM_MSGS) > 300:
                    CONFIRM_MSGS.pop(next(iter(CONFIRM_MSGS)))
                CONFIRM_MSGS[cid] = sent
        except Exception as e:
            log.warning("confirm watcher: %s", e)
        await asyncio.sleep(CONFIRM_POLL_SEC)


async def init_arr_defaults():
    """Load the Radarr/Sonarr defaults in the background, so the bot answers at once even while the
    apps are still starting; movie and TV features work as soon as the apps answer."""
    while True:
        try:
            await asyncio.to_thread(radarr_defaults)
            break
        except Exception as e:
            log.info("waiting for Radarr (%s)", type(e).__name__)
            await asyncio.sleep(20)
    try:
        await asyncio.to_thread(sonarr_defaults)
    except Exception as e:
        log.warning("sonarr defaults failed: %s", type(e).__name__)


async def send_card(bot, text, photo, kb):
    sent = 0
    for cid in ALLOWED:
        try:
            if photo:
                await bot.send_photo(chat_id=cid, photo=photo, caption=text, parse_mode=ParseMode.HTML, reply_markup=kb)
            else:
                await bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                       link_preview_options=LinkPreviewOptions(is_disabled=True))
            sent += 1
        except Exception as e:
            log.warning("new-release card failed (%s); sending it as text", e)
            try:
                await bot.send_message(chat_id=cid, text=text, parse_mode=ParseMode.HTML, reply_markup=kb,
                                       link_preview_options=LinkPreviewOptions(is_disabled=True))
                sent += 1
            except Exception as e2:
                log.warning("new-release card failed again: %s", e2)
    return sent


async def send_new_releases(app, manual=False):
    if not JKEY:
        if manual:
            await broadcast(app.bot, "🆕 New releases are off: JELLYSEERR_API_KEY is not set.")
        return
    if "new" in BUSY:
        if manual:
            await broadcast(app.bot, "🆕 Already checking for new releases; the cards are on their way.")
        return
    BUSY.add("new")
    try:
        seen = await asyncio.to_thread(load_seen)
        try:
            cards = await asyncio.to_thread(find_new_releases, seen)
        except Exception as e:
            log.warning("new releases lookup failed: %s", e)
            if manual:
                await broadcast(app.bot, "⚠️ Couldn't check for new releases right now; "
                                         "try /new again in a few minutes.")
            return
        if not cards:
            await asyncio.to_thread(save_seen, seen)
            if manual:
                await broadcast(app.bot, "🆕 Nothing new on the streaming services since the last check.")
            return
        regions = ", ".join(REGION_LABEL.get(r, r) for r in NEW_REGIONS)
        await broadcast(app.bot, f"🆕 <b>New on the streaming services</b> ({esc(regions)}): {len(cards)} pick"
                                 f"{'' if len(cards) == 1 else 's'}.\nTap ✅ on anything you want and it downloads "
                                 "automatically. Each title is only suggested once.", parse_mode=ParseMode.HTML)
        for key, text, photo, kb in cards:
            if await send_card(app.bot, text, photo, kb):
                seen[key] = time.time()
        await asyncio.to_thread(save_seen, seen)
        log.info("new releases: sent %d cards", len(cards))
    finally:
        BUSY.discard("new")


async def run_missing_fill(app, budget=None, manual=False):
    if not SKEY:
        if manual:
            await broadcast(app.bot, "📺 The episode filler is off: SONARR_API_KEY is not set.")
        return
    if "fill" in BUSY:
        if manual:
            await broadcast(app.bot, "📺 The missing-episode check is already running.")
        return
    BUSY.add("fill")
    try:
        state = await asyncio.to_thread(load_json, FILL_STATE)
        try:
            grabbed, offers = await asyncio.to_thread(fill_run, state, budget)
        except Exception as e:
            log.warning("missing-episode fill failed: %s", e)
            if manual:
                await broadcast(app.bot, "⚠️ Couldn't check the missing episodes: Sonarr didn't answer.")
            return
        finally:
            await asyncio.to_thread(save_json, FILL_STATE, state)
        cap = f"{EPISODE_CAP_GB:g}"
        if grabbed:
            await broadcast(app.bot, f"📺 <b>Filled missing episodes</b> (healthy copies under {cap} GB):\n"
                            + "\n".join(f"• {esc(g)}" for g in grabbed), parse_mode=ParseMode.HTML)
        for sid, title, sn, left, n_rel, best in offers:
            copies = f"{n_rel} single-episode cop{'y' if n_rel == 1 else 'ies'}"
            what = "a season pack" if best.get("fullSeason") else copies
            eps = ", E".join(str(e) for e in left[:12]) + ("…" if len(left) > 12 else "")
            kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ Get 720p", callback_data=f"g720:{sid}:{sn}"),
                                        InlineKeyboardButton("🙈 Leave it", callback_data=f"g720no:{sid}:{sn}")]])
            await broadcast(app.bot,
                            f"📺 <b>{esc(title)}</b> season {sn}: {len(left)} episode{'' if len(left) == 1 else 's'} "
                            f"missing (E{eps}).\nNo working 1080p copy exists. A <b>720p</b> one does: {what}, "
                            "the best "
                            f"has {best.get('seeders')} seeders ({(best.get('size') or 0) / 1e9:.1f} GB).\n"
                            "Get the 720p copy?", parse_mode=ParseMode.HTML, reply_markup=kb)
        if manual and not grabbed and not offers:
            await broadcast(app.bot, "📺 Checked the missing episodes: nothing new could be filled today.")
    finally:
        BUSY.discard("fill")


async def send_report(app):
    try:
        await broadcast(app.bot, await asyncio.to_thread(build_report), parse_mode=ParseMode.HTML)
    except Exception as e:
        log.warning("report failed: %s", e)


# ================================================================== commands
def help_text():
    cap = f"{EPISODE_CAP_GB:g}"
    new_line = (f"• 🆕 Every day at {NEW_HOUR % 24:02d}:00 I send new titles from the streaming services, with "
                "picture, cast, story and what critics say; tap ✅ to download. <b>/new</b> checks now."
                if JKEY else "• 🆕 <b>/new</b>: new releases (off: Jellyseerr is not connected).")
    fill_line = (f"• 📺 Every day at {FILL_HOUR % 24:02d}:30 I fill episodes Sonarr skipped (copies under {cap} GB); "
                 "if only 720p exists I ask you first. <b>/fill</b> runs it now."
                 if SKEY else "• 📺 <b>/fill</b>: missing-episode filler (off: Sonarr is not connected).")
    return "\n".join([
        "🎬📺 <b>Media bot ready.</b>\n",
        "• Send a title: I search 🎬 movies and 📺 shows; tap a number to add it.",
        "• 🎬 Movies: a release that meets your Radarr settings downloads straight away and I tell you, with a "
        "❌ Cancel button. One that breaks them is removed and Radarr looks again.",
        "• 📺 Shows: adding one monitors <b>every season</b>; finished seasons come as complete season packs.",
        "• ⚡ <b>Force-grab</b>: when a title won't download, tap ⚡ and pick a release yourself "
        "(it skips every filter).",
        "• 🤔 When nothing meets your settings, I send you the <b>closest releases</b> to approve.",
        "• 🍿 I tell you when a movie or an episode <b>is ready</b> to watch.",
        "• 🧹 Dead torrents (no seeders) and fake downloads (archives or programs instead of video) are cleaned up.",
        new_line,
        fill_line,
        f"• 📊 <b>/report</b>: what finished in the last 24 hours (also sent daily at {REPORT_HOUR % 24:02d}:00).",
        "• 📥 <b>/downloading</b> (or /status, /dl): live progress of every download.",
        "• ❓ Maintenance jobs ask here before risky steps; tap Yes or No.",
        "• <b>/help</b> shows this list again.",
    ])


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(help_text(), parse_mode=ParseMode.HTML)


async def cmd_report(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    try:
        text = await asyncio.to_thread(build_report)
    except Exception as e:
        log.warning("report failed: %s", e)
        text = "⚠️ Couldn't build the report: Radarr didn't answer. Try again in a minute."
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def cmd_downloading(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    ts = await asyncio.to_thread(qinfo_checked)
    if ts is None:
        await update.message.reply_text("⚠️ qBittorrent didn't answer. Try again in a minute.")
        return
    live = ("downloading", "stalledDL", "metaDL", "forcedMetaDL", "queuedDL", "forcedDL", "checkingDL", "allocating")
    act = [t for t in ts if (t.get("state") or "") in live]
    if not act:
        await update.message.reply_text("💤 Nothing downloading right now.")
        return
    lines = ["📥 <b>Downloading now:</b>\n"]
    for t in sorted(act, key=lambda x: -(x.get("progress") or 0))[:12]:
        pct = (t.get("progress") or 0) * 100
        spd = (t.get("dlspeed") or 0) / 1048576
        gb = (t.get("total_size") or 0) / GIB
        eta = t.get("eta") or 0
        eta_s = fmt_dur(eta) if 0 < eta < 8640000 else "?"
        lines.append(f"<b>{esc((t.get('name') or '?')[:40])}</b>\n"
                     f"{bar(pct)} {pct:.0f}% · {gb:.1f} GB · {spd:.1f} MB/s · ⏳ {eta_s}")
    await update.message.reply_text("\n\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_new(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Checking the streaming services for new releases…")
    spawn(send_new_releases(ctx.application, manual=True))


async def cmd_fill(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔎 Looking for copies of missing episodes (this takes a few minutes)…")
    spawn(run_missing_fill(ctx.application, manual=True))


async def on_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """A plain message is a title search in Radarr and Sonarr."""
    term = " ".join((update.message.text or "").split())[:100]
    if not term:
        return
    try:
        movies = [m for m in (await asyncio.to_thread(rget, "movie/lookup", term=term) or []) if m.get("tmdbId")][:4]
    except Exception as e:
        movies = []
        log.warning("movie lookup: %s", type(e).__name__)
    shows = []
    if SKEY:
        try:
            found = await asyncio.to_thread(sget, "series/lookup", term=term) or []
            shows = [s for s in found if s.get("tvdbId")][:4]
        except Exception as e:
            log.warning("series lookup: %s", type(e).__name__)
    if not movies and not shows:
        await update.message.reply_text(f"No matches for “{term}”. Try the exact title.")
        return
    lines, btns, i = [f"🔎 Results for <b>{esc(term)}</b>:\n"], [], 0
    for m in movies:
        i += 1
        name = f"{esc(m.get('title') or '?')} ({esc(m.get('year') or '')})"
        if m.get("id"):                              # already in the library: offer a force-grab
            lines.append(f"<b>{i}.</b> 🎬 {name}  ✅ in library")
            btns.append(InlineKeyboardButton(f"{i} ⚡", callback_data=f"force:{m['id']}"))
        else:
            lines.append(f"<b>{i}.</b> 🎬 {name}")
            btns.append(InlineKeyboardButton(str(i), callback_data=f"addm:{m['tmdbId']}"))
    for s in shows:
        i += 1
        tag = "  ✅" if s.get("id") else ""
        lines.append(f"<b>{i}.</b> 📺 {esc(s.get('title') or '?')} ({esc(s.get('year') or '')}){tag}")
        btns.append(InlineKeyboardButton(str(i), callback_data=f"adds:{s['tvdbId']}"))
    lines.append("\n👇 Tap a number to add it.  📺 = all seasons · ⚡ = already added, force-grab a release.")
    rows = [btns[j:j + 5] for j in range(0, len(btns), 5)]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML,
                                    reply_markup=InlineKeyboardMarkup(rows))


# ================================================================== buttons
def _ints(rest, n):
    """'12:3' -> (12, 3) when there are exactly n integer parts, else None."""
    parts = rest.split(":")
    if len(parts) != n:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


async def btn_new_skip(q, ctx, prefix, rest):
    await q.answer("Skipped")
    await finish(q, "\n\n🙈 <b>Skipped</b>: I won't suggest it again.")


async def btn_720(q, ctx, prefix, rest):
    ids = _ints(rest, 2)
    if not ids:
        await q.answer()
        return
    sid, sn = ids
    if prefix == "g720no":
        await q.answer("OK, leaving it")
        st = await asyncio.to_thread(load_json, FILL_STATE)
        st[f"{sid}:{sn}"] = time.time() + FILL_LEAVE_SEC - FILL_COOLDOWN
        await asyncio.to_thread(save_json, FILL_STATE, st)
        await finish(q, "\n\n🙈 <b>Left as is</b>: I'll look for 1080p again in about three months.")
        return
    await q.answer("Searching… (up to a few minutes)")
    try:
        res = await asyncio.to_thread(grab_720, sid, sn)
    except Exception as e:
        log.warning("720p grab %s S%s failed: %s", sid, sn, type(e).__name__)
        await q.message.reply_text("⚠️ Couldn't search right now; tap again in a few minutes.")
        return
    await finish(q, "\n\n" + res)


async def btn_add_movie(q, ctx, prefix, rest):
    await q.answer("Adding…")
    if not rest.isdigit():
        return
    try:
        look = await asyncio.to_thread(rget, "movie/lookup", term=f"tmdb:{rest}") or []
    except Exception as e:
        log.warning("movie lookup failed: %s", type(e).__name__)
        await finish(q, "\n\n⚠️ Couldn't look that up right now: Radarr didn't answer. Try again in a moment.")
        return
    if not look:
        await finish(q, "\n\n⚠️ Couldn't fetch that movie.")
        return
    mv = look[0]
    title, year = mv.get("title") or "?", mv.get("year") or ""
    name = f"<b>{esc(title)}</b> ({esc(year)})"
    if mv.get("id"):
        await finish(q, f"\n\n✅ {name} is already in your library.")
        return
    if not QPROFILE:
        try:
            await asyncio.to_thread(radarr_defaults)
        except Exception as e:
            log.warning("radarr defaults: %s", type(e).__name__)
    if not QPROFILE:
        await finish(q, "\n\n⚠️ Radarr isn't ready yet (no quality profile). Try again in a minute.")
        return
    root = movie_root_for(mv)
    mv.update({"qualityProfileId": QPROFILE, "rootFolderPath": root, "monitored": True,
               "minimumAvailability": "released", "addOptions": {"searchForMovie": True}})
    try:
        sc, res = await asyncio.to_thread(rpost, "movie", mv)
    except Exception as e:
        log.warning("movie add failed: %s", type(e).__name__)
        await finish(q, f"\n\n⚠️ <b>Couldn't add {esc(title)}</b>: Radarr didn't answer.")
        return
    if sc not in (200, 201):
        reason = _reason(sc, res)
        if sc == 400 and "already" in reason.lower():
            await finish(q, f"\n\n✅ {name} is already in your library.")
        else:
            await finish(q, f"\n\n⚠️ <b>Couldn't add {esc(title)}</b>\n{reason}")
        return
    mid = (res or {}).get("id") if isinstance(res, dict) else None
    body = (_msg_html(q) + f"\n\n✅ <b>Got it: {esc(title)} ({esc(year)}) is added.</b>\n"
            f"📁 Saving to <b>{esc(_folder(root))}</b>. Searching now; "
            "I'll tell you when a release starts downloading.")
    rows = refile_rows(mid, root) if mid else []     # one tap to file it in another library
    if mid:
        rows.append([InlineKeyboardButton("⚡ Not downloading? Force-grab a release", callback_data=f"force:{mid}")])
    try:
        await _edit_msg(q, body, parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows) if rows else None)
    except Exception:
        await finish(q, f"\n\n✅ Added; saving to {esc(_folder(root))}.")
    if mid:                                          # if nothing matches the settings, offer the closest later
        spawn(watch_movie_fallback(ctx.application, mid, title, year))


async def btn_add_series(q, ctx, prefix, rest):
    await q.answer("Adding all seasons…")
    if not SKEY:
        await finish(q, "\n\n⚠️ TV is not set up: SONARR_API_KEY is empty.")
        return
    if not rest.isdigit():
        await finish(q, "\n\n⚠️ Couldn't fetch that show.")
        return
    try:
        look = await asyncio.to_thread(sget, "series/lookup", term=f"tvdb:{rest}") or []
    except Exception as e:
        log.warning("series lookup failed: %s", type(e).__name__)
        await finish(q, "\n\n⚠️ Couldn't look that up right now: Sonarr didn't answer. Try again in a moment.")
        return
    if not look:
        await finish(q, "\n\n⚠️ Couldn't fetch that show.")
        return
    s = look[0]
    title, year = s.get("title") or "?", s.get("year") or ""
    if s.get("id"):
        await finish(q, f"\n\n✅ <b>{esc(title)}</b> is already in your library.")
        return
    if not SPROFILE:
        try:
            await asyncio.to_thread(sonarr_defaults)
        except Exception as e:
            log.warning("sonarr defaults: %s", type(e).__name__)
    if not SPROFILE:
        await finish(q, "\n\n⚠️ Sonarr isn't ready yet (no quality profile). Try again in a minute.")
        return
    for season in s.get("seasons", []):
        season["monitored"] = True
    s.update({"qualityProfileId": SPROFILE, "rootFolderPath": SROOT, "monitored": True, "seasonFolder": True,
              # no per-episode search on add: the bot searches per SEASON (packs) below
              "addOptions": {"monitor": "all", "searchForMissingEpisodes": False,
                             "searchForCutoffUnmetEpisodes": False}})
    try:
        sc, res = await asyncio.to_thread(spost, "series", s)
    except Exception as e:
        log.warning("series add failed: %s", type(e).__name__)
        await finish(q, f"\n\n⚠️ <b>Couldn't add {esc(title)}</b>: Sonarr didn't answer.")
        return
    if sc not in (200, 201):
        reason = _reason(sc, res)
        if sc == 400 and "already" in reason.lower():
            await finish(q, f"\n\n✅ <b>{esc(title)}</b> ({esc(year)}) is already in your library.")
        else:
            await finish(q, f"\n\n⚠️ <b>Couldn't add {esc(title)}</b>\n{reason}")
        return
    new_id = (res or {}).get("id") if isinstance(res, dict) else None
    seasons = sorted({se.get("seasonNumber") for se in s.get("seasons", [])
                      if se.get("monitored") and (se.get("seasonNumber") or 0) > 0})
    await finish(q, f"\n\n✅ <b>Got it: {esc(title)} ({esc(year)}) is added.</b>\n"
                    "📦 Finished seasons → <b>complete season packs</b> (no loose episodes).\n"
                    "📅 A season still airing → new episodes <b>week by week</b>.\n"
                    "I'll only ask you to pick when a finished season has no full pack that meets your settings.")
    if new_id:
        spawn(grab_full_seasons(ctx.application, new_id, title, seasons))


async def btn_cancel(q, ctx, prefix, rest):
    """Cancel button: blocking Radarr/qBittorrent calls run in a thread so a slow Radarr cannot
    freeze the bot; a finished or imported download is never deleted."""
    h = rest.lower()
    if not HASH_RE.match(h):
        await q.answer()
        return
    if h in CANCEL_BUSY:
        await q.answer("Already cancelling, one moment…")
        return
    await q.answer("Cancelling…")
    CANCEL_BUSY.add(h)
    try:
        suffix, retry = await asyncio.to_thread(cancel_download, h)
    except Exception as e:
        log.warning("cancel %s failed: %s", h[:10], e)
        suffix, retry = "\n\n⚠️ <b>Couldn't cancel</b>: something went wrong. Check qBittorrent.", True
    finally:
        CANCEL_BUSY.discard(h)
    log.info("cancel %s -> %s", h[:10], re.sub(r"<[^>]+>", "", suffix).strip()[:90])
    if retry:                                        # keep the button so the user can try again
        try:
            await _edit_msg(q, _msg_html(q) + suffix, parse_mode=ParseMode.HTML, reply_markup=q.message.reply_markup)
        except Exception:
            await finish(q, suffix)
        return
    await finish(q, suffix)
    await drop_buttons(ctx.bot, CANCEL_MSGS.pop(h, None))   # the other users' copies too


async def btn_refile(q, ctx, prefix, rest):
    mid_s, _, code = rest.partition(":")
    if not mid_s.isdigit():
        await q.answer()
        return
    dest = MOVIE_ROOT if code == "default" else MOVIE_LIBS.get(code)
    if not dest:
        await q.answer("That library isn't set up.", show_alert=True)
        return
    await q.answer("Moving…")
    try:
        sc, res = await asyncio.to_thread(rput, "movie/editor", {"movieIds": [int(mid_s)], "rootFolderPath": dest,
                                                                "moveFiles": True})
    except Exception as e:
        log.warning("re-file failed: %s", type(e).__name__)
        await finish(q, "\n\n⚠️ Couldn't move it: Radarr didn't answer.")
        return
    if sc in (200, 202):
        await finish(q, f"\n\n📁 Filed in <b>{esc(_folder(dest))}</b>.")
    else:
        await finish(q, f"\n\n⚠️ Couldn't move it: {_reason(sc, res)}")


async def btn_force_list(q, ctx, prefix, rest):
    if not rest.isdigit():
        await q.answer()
        return
    mid = int(rest)
    await q.answer("Searching every release…")
    try:
        await _edit_msg(q, _msg_html(q) + "\n\n🔎 <b>Searching every release… (up to a minute)</b>",
                        parse_mode=ParseMode.HTML)
    except Exception:
        pass
    try:
        rels = await asyncio.to_thread(rsearch_releases, mid)
    except Exception as e:
        log.warning("release search %s failed: %s", mid, type(e).__name__)
        await finish(q, "\n\n⚠️ The release search failed: Radarr didn't answer.")
        return
    if not rels:
        await finish(q, "\n\n⚠️ No releases exist for this one on your indexers.")
        return
    rels = [r for r in rels if not is_cam(r)]
    if not rels:
        await finish(q, "\n\n🎬 Only cinema recordings (CAM/TS) exist for this one so far, so none are offered. "
                        "Radarr will get a proper copy once it is released.")
        return
    rels = sorted(rels, key=lambda r: -(r.get("seeders") or 0))[:6]
    if len(REL_CACHE) > 100:
        REL_CACHE.pop(next(iter(REL_CACHE)))
    REL_CACHE[mid] = rels
    lines, btns = ["⚡ <b>Force-grab a release</b>: tap a number (this skips every filter):\n"], []
    for idx, r in enumerate(rels):
        flag = "🚫 " if cf_negative(r) else ("✅ " if not r.get("rejections") else "⚠️ ")
        lines.append(_rel_line(idx, r, flag))
        label = str(idx + 1) + (" 🚫" if cf_negative(r) else "")
        btns.append(InlineKeyboardButton(label, callback_data=f"fg:{mid}:{idx}"))
    if any(cf_negative(r) for r in rels):
        lines.append(CF_LEGEND)
    rows = [btns[j:j + 3] for j in range(0, len(btns), 3)]
    await _edit_msg(q, "\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=InlineKeyboardMarkup(rows))


async def btn_force_grab(q, ctx, prefix, rest):
    ids = _ints(rest, 2)
    if not ids:
        await q.answer()
        return
    mid, idx = ids
    rels = REL_CACHE.get(mid) or []
    if not 0 <= idx < len(rels):
        await q.answer("That list expired; search again.", show_alert=True)
        return
    r = rels[idx]
    await q.answer("Force-grabbing…")
    FORCE_IDS.add(mid, r.get("title"), r.get("size"))   # THIS release passes the Grab check unjudged
    try:
        sc, res = await asyncio.to_thread(rpost, "release",
                                          {"guid": r["guid"], "indexerId": r["indexerId"], "movieId": mid})
    except Exception as e:
        log.warning("force grab failed: %s", type(e).__name__)
        sc, res = None, "Radarr didn't answer"
    if sc in (200, 201):
        gb = (r.get("size") or 0) / GIB
        await finish(q, f"\n\n⚡ <b>Force-grabbed</b> {gb:.2f} GB: downloading now, no approval needed.")
    else:
        FORCE_IDS.discard(mid, r.get("title"))
        await finish(q, f"\n\n⚠️ Couldn't grab it: {_reason(sc, res)}")


async def btn_fallback_grab(q, ctx, prefix, rest):
    tok, _, idx_s = rest.partition(":")
    ent = FB_CACHE.get(tok)
    if not idx_s.isdigit() or not ent or int(idx_s) >= len(ent.get("rels", [])):
        await q.answer("This prompt expired; search again with ⚡.", show_alert=True)
        return
    rel = ent["rels"][int(idx_s)]
    await q.answer("Grabbing…")
    try:
        if ent["kind"] == "movie":
            FORCE_IDS.add(ent["mid"], rel.get("title"), rel.get("size"))   # THIS release passes unjudged
            sc, res = await asyncio.to_thread(rpost, "release", {"guid": rel["guid"], "indexerId": rel["indexerId"],
                                                                 "movieId": ent["mid"]})
            if sc not in (200, 201):
                FORCE_IDS.discard(ent["mid"], rel.get("title"))
        else:
            sc, res = await asyncio.to_thread(spost, "release", {"guid": rel["guid"], "indexerId": rel["indexerId"]})
    except Exception as e:
        log.warning("closest-release grab failed: %s", type(e).__name__)
        sc, res = None, "the app didn't answer"
    FB_CACHE.pop(tok, None)
    if sc in (200, 201):
        gb = (rel.get("size") or 0) / GIB
        await finish(q, f"\n\n⚡ <b>Grabbing the closest match</b> ({gb:.2f} GB): downloading now.")
    else:
        await finish(q, f"\n\n⚠️ Couldn't grab it: {_reason(sc, res)}")


async def btn_fallback_skip(q, ctx, prefix, rest):
    await q.answer("Skipped")
    FB_CACHE.pop(rest, None)
    await finish(q, "\n\n👍 Skipped: left it for now. Tap ⚡ later to try again.")


async def btn_confirm(q, ctx, prefix, rest):
    """Yes/No on a question from a host script: the first answer wins and is written to <id>.reply."""
    cid = rest
    if not CONFIRM_ID_RE.match(cid):
        await q.answer()
        return
    answer = "yes" if prefix == "cok" else "no"
    if cid in ANSWERED:
        await q.answer(f"Already answered: {ANSWERED[cid]}", show_alert=True)
        try:
            await q.edit_message_reply_markup(None)
        except Exception:
            pass
        return
    try:
        await asyncio.to_thread(write_reply, cid, answer)
    except OSError as e:
        log.warning("confirmation %s: answer not written (%s)", cid, type(e).__name__)
        await q.answer("Couldn't record the answer: the state folder is not writable.", show_alert=True)
        return
    if len(ANSWERED) > 1000:
        ANSWERED.clear()
    ANSWERED[cid] = answer
    await q.answer("Answer sent: " + answer)
    await finish(q, "\n\n✅ <b>Approved</b>: the job goes ahead." if answer == "yes"
                 else "\n\n❌ <b>Declined</b>: the job leaves it alone.")
    here = (q.message.chat_id, q.message.message_id) if q.message else None
    await drop_buttons(ctx.bot, [m for m in CONFIRM_MSGS.pop(cid, []) if m != here])


BUTTONS = {
    "nrskip": btn_new_skip,
    "g720": btn_720,
    "g720no": btn_720,
    "addm": btn_add_movie,
    "adds": btn_add_series,
    "cancel": btn_cancel,
    "mvroot": btn_refile,
    "force": btn_force_list,
    "fg": btn_force_grab,
    "fba": btn_fallback_grab,
    "fbx": btn_fallback_skip,
    "cok": btn_confirm,
    "cno": btn_confirm,
}


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q is None:
        return
    uid = q.from_user.id if q.from_user else None
    if uid not in ALLOWED_SET:                       # strangers: an empty answer, nothing else
        log_stranger(uid)
        await q.answer()
        return
    prefix, _, rest = (q.data or "").partition(":")
    handler = BUTTONS.get(prefix)
    if handler is None:
        await q.answer()
        return
    await handler(q, ctx, prefix, rest)


# ================================================================== access control
def log_stranger(uid):
    """The one log line a stranger produces (at most every 10 minutes per id)."""
    now = time.time()
    if now - _STRANGERS.get(uid, 0) < 600:
        return
    if len(_STRANGERS) > 1000:
        _STRANGERS.clear()
    _STRANGERS[uid] = now
    log.info("unauthorised user id=%s", uid)


async def on_unauth(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Anyone not on the allow-list: logged, and sent nothing at all."""
    u = update.effective_user
    log_stranger(u.id if u else None)


async def on_error(update, ctx):
    log.warning("handler error: %s", ctx.error)     # e.g. a transient Telegram timeout: log, don't crash


COMMANDS = [
    ("start", cmd_start), ("help", cmd_start), ("report", cmd_report),
    ("downloading", cmd_downloading), ("status", cmd_downloading), ("dl", cmd_downloading),
    ("new", cmd_new), ("fill", cmd_fill),
]


# ================================================================== start-up
class _Redact(logging.Filter):
    """Last line of defence: blank out the token, the webhook secret and the API keys in any log line."""

    def __init__(self, secrets):
        super().__init__()
        self._secrets = sorted({s for s in secrets if s and len(s) >= 6}, key=len, reverse=True)

    def _clean(self, text):
        for s in self._secrets:
            text = text.replace(s, "***")
        return text

    def filter(self, record):
        try:
            msg = record.getMessage()
        except Exception:
            return True
        clean = self._clean(msg)
        if clean != msg:
            record.msg, record.args = clean, None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self._clean(record.exc_text)
        return True


def install_redaction():
    flt = _Redact([TOKEN, SECRET, RKEY, SKEY, JKEY, QBIT_PASS])
    for handler in logging.getLogger().handlers:
        handler.addFilter(flt)


def config_problems():
    """Settings without which the bot must not run (it never runs open or unauthenticated)."""
    probs = []
    if not TOKEN:
        probs.append("TELEGRAM_BOT_TOKEN is empty.")
    if not ALLOWED:
        probs.append("ALLOWED_TELEGRAM_USER_IDS is empty (or has no numeric ids): refusing to start, "
                     "the bot answers only the users listed there.")
    if not SECRET:
        probs.append("WEBHOOK_SECRET is empty: refusing to run the Radarr/Sonarr webhooks without a secret.")
    if not RKEY:
        probs.append("RADARR_API_KEY is empty: run ./install.sh (it reads the key from Radarr).")
    return probs


def build_app():
    """The Telegram application with every handler; nothing touches the network until it starts.

    Allowed users are served in their private chat with the bot only, so results never land in a
    group where others could read them. Concurrent updates keep the bot answering while a long
    job (a release search, /fill) runs for one user.
    """
    app = (Application.builder().token(TOKEN).concurrent_updates(8)
           .connect_timeout(15).read_timeout(25).write_timeout(25).pool_timeout(15)
           .get_updates_connect_timeout(15).get_updates_read_timeout(40)
           .build())
    allowed = filters.User(user_id=ALLOWED) & filters.ChatType.PRIVATE
    for name, fn in COMMANDS:
        app.add_handler(CommandHandler(name, fn, filters=allowed))
    app.add_handler(MessageHandler(allowed & filters.UpdateType.MESSAGE & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(~filters.User(user_id=ALLOWED), on_unauth))   # strangers: silence
    app.add_handler(CallbackQueryHandler(on_button))                          # checks the user itself
    app.add_error_handler(on_error)
    return app


async def run():
    app = build_app()
    runner = web.AppRunner(make_web(app), access_log=None)   # the access log would print the secret path
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    async with app:
        await app.start()
        await app.updater.start_polling(drop_pending_updates=True,
                                        allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
        await runner.setup()
        await web.TCPSite(runner, "0.0.0.0", PORT).start()
        log.info("moviebot running: webhook on :%s, %d allowed user(s)", PORT, len(ALLOWED))
        spawn(init_arr_defaults())
        spawn(confirm_watcher(app))
        spawn(dead_cleanup(app))
        spawn(fake_cleanup(app))
        spawn(disk_watch(app))
        spawn(daily_loop(REPORT_HOUR, 0, functools.partial(send_report, app), "report"))
        if JKEY:
            spawn(daily_loop(NEW_HOUR, 0, functools.partial(send_new_releases, app), "new releases"))
        else:
            log.info("new releases off: JELLYSEERR_API_KEY is empty")
        if SKEY:
            spawn(daily_loop(FILL_HOUR, 30, functools.partial(run_missing_fill, app), "missing-episode fill"))
        else:
            log.info("TV features off: SONARR_API_KEY is empty")
        await stop.wait()
        log.info("stopping")
        await app.updater.stop()
        await app.stop()
        await runner.cleanup()


def main():
    install_redaction()
    problems = config_problems()
    if problems:
        for p in problems:
            log.error(p)
        return 2
    if _BAD_IDS:
        log.warning("ALLOWED_TELEGRAM_USER_IDS: ignored %d entr%s that are not numeric user ids",
                    _BAD_IDS, "y" if _BAD_IDS == 1 else "ies")
    if _BAD_LIBS:
        log.warning("MOVIE_LIBRARIES: ignored %d entr%s (use <language code>=<folder>, e.g. fr=movies-fr)",
                    len(_BAD_LIBS), "y" if len(_BAD_LIBS) == 1 else "ies")
    if len(SECRET) < 16:
        log.warning("WEBHOOK_SECRET is short; use a long random value (install.sh generates one)")
    if not state_writable():
        log.warning("%s is not writable for this user: answers to maintenance questions and the bot's own "
                    "state cannot be saved. Make the state folder belong to PUID:PGID.", STATE_DIR)
    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
