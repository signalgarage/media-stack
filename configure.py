#!/usr/bin/env python3
"""Wire the media-stack apps together through their own APIs.

install.sh runs this once the containers are up; run it again whenever you like. It is
idempotent: every object is looked up by name and updated in place, and only created when
it is missing. It never adds indexers and never deletes anything.

What each part sets up
  qbit        web UI login, download folder and categories, seeding limits, queueing, blocked
              file types, and the localhost bypass the VPN port script relies on
  radarr      root folders, the qBittorrent download client, size limits per quality, custom
  sonarr      formats, the "HD-1080p" quality profile, media management, file naming and
              notifications (bot webhook, on-grab guard, Plex, Telegram health alerts);
              Sonarr also gets the "Block MULTI dubs" release profile
  prowlarr    sync to Radarr and Sonarr, the FlareSolverr proxy and its tag, health alerts
  jellyseerr  first-run setup with your Plex token, then links Radarr and Sonarr
  cron        installs scripts/cron.template into your crontab between the media-stack markers

Usage
  python3 configure.py [--dry-run] [--only qbit,radarr,sonarr,prowlarr,jellyseerr,cron]

--dry-run reads from the apps but changes nothing and prints what it would do.
Exit status: 0 when every selected part worked, 1 when one failed, 2 on bad usage.
Python 3.9+, standard library only; settings come from .env through scripts/settings.py.
"""
from __future__ import annotations

import argparse
import copy
import http.cookiejar
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "scripts"))
import settings  # noqa: E402

PARTS = ("qbit", "radarr", "sonarr", "prowlarr", "jellyseerr", "cron")
DRY = False

PROFILE_NAME = "HD-1080p"
BLOCK_SCORE = -10000          # a matching release is never grabbed
MASK = "********"             # how newer app versions return stored secrets
DOWNLOAD_CLIENT = "qBittorrent"
QBIT_HOST, QBIT_PORT = "gluetun", 8080   # qBittorrent lives in gluetun's network namespace
PROXY_TRIES, PROXY_PAUSE = 8, 15        # FlareSolverr may need a while on its very first start
MARK_BEGIN = "# >>> media-stack >>>"
MARK_END = "# <<< media-stack <<<"

# Qualities the HD-1080p profile allows (their order comes from the app's own quality list).
SONARR_ALLOWED = {"HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p"}
RADARR_ALLOWED = {"HDTV-720p", "Bluray-720p", "HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p",
                  "Remux-1080p"}

MULTI_FORMAT = "Multi/Dual audio tag"
MULTI_REGEX = r"\b(MULTi|DUAL)\b(?![ ._-]?subs?\b)"   # a "MULTi" or "DUAL" tag, but not "multi subs"
MULTI_RELEASE_TERM = r"/\bMULTi\b/i"
RELEASE_PROFILE = "Block MULTI dubs"
LANGUAGE_ORIGINAL, LANGUAGE_UNKNOWN = -2, 0

RADARR_NAMING = {
    "standardMovieFormat": "{Movie Title} ({Release Year}) {Quality Full}",
    "movieFolderFormat": "{Movie Title} ({Release Year})",
}
SONARR_NAMING = {
    "standardEpisodeFormat": "{Series Title} - S{season:00}E{episode:00} - {Episode Title} {Quality Full}",
    "dailyEpisodeFormat": "{Series Title} - {Air-Date} - {Episode Title} {Quality Full}",
    "seriesFolderFormat": "{Series Title}",
    "seasonFolderFormat": "Season {season}",
}

# File types qBittorrent refuses to write: programs, scripts and shortcuts never belong in a
# movie or TV download. Matching is case sensitive, hence both spellings.
_EXCLUDED = ["*.apk", "*.application", "*.appx", "*.appxbundle", "*.bat", "*.chm", "*.cmd", "*.com", "*.cpl",
             "*.dll", "*.exe", "*.gadget", "*.hta", "*.jar", "*.js", "*.jse", "*.lnk", "*.msi", "*.msix",
             "*.msixbundle", "*.msp", "*.pif", "*.ps1", "*.psm1", "*.reg", "*.scf", "*.scr", "*.sys", "*.url",
             "*.vbe", "*.vbs", "*.website", "*.ws", "*.wsf", "*.wsh"]
QBIT_EXCLUDED = _EXCLUDED + [x.upper() for x in _EXCLUDED]

# Low-quality release lists (well-known public group lists, kept verbatim). Each entry is
# (specification name, regular expression); the apps match them case-insensitively.
LQ_FORMATS = {
    "radarr": {
        "LQ": ("ReleaseGroupSpecification", [
            ("24xHD", r"\b(24xHD)\b"), ("41RGB", r"^(41RGB)$"), ("4K4U", r"^(4K4U)$"), ("AOC", r"^(AOC)$"),
            ("AROMA", r"^(AROMA)$"), ("aXXo", r"^(aXXo)$"), ("AZAZE", r"^(AZAZE)$"), ("BARC0DE", r"^(BARC0DE)$"),
            ("BAUCKLEY", r"^(BAUCKLEY)$"), ("BdC", r"^(BdC)$"), ("beAst", r"^(beAst)$"), ("BTM", r"^(BTM)$"),
            ("C1NEM4", r"^(C1NEM4)$"), ("C4K", r"^(C4K)$"), ("CDDHD", r"^(CDDHD)$"), ("CHAOS", r"^(CHAOS)$"),
            ("CHD", r"^(CHD)$"), ("CiNE", r"^(CiNE)$"), ("CLEANUP", r"^(CLEANUP)$"),
            ("COLLECTiVE", r"^(COLLECTiVE)$"), ("CREATiVE24", r"^(CREATiVE24)$"), ("CrEwSaDe", r"^(CrEwSaDe)$"),
            ("CTFOH", r"^(CTFOH)$"), ("d3g", r"^(d3g)$"), ("DDR", r"^(DDR)$"), ("DNL", r"^(DNL)$"),
            ("DRX", r"^(DRX)$"), ("E", r"^(E)$"), ("EPiC", r"^(EPiC)$"), ("EuReKA", r"^(EuReKA)$"),
            ("FaNGDiNG0", r"^(FaNGDiNG0)$"), ("Feranki1980", r"^(Feranki1980)$"), ("FGT", r"^(FGT)$"),
            ("FMD", r"^(FMD)$"), ("FRDS", r"^(FRDS)$"), ("FS", r"^(FS)$"), ("FZHD", r"^(FZHD)$"),
            ("GalaxyRG", r"^(GalaxyRG)$"), ("GHD", r"^(GHD)$"), ("GPTHD", r"^(GPTHD)$"),
            ("HDS", r"^(HDS)$"), ("HDT", r"^(HDT)$"), ("HDTime", r"^(HDTime)$"), ("HDWinG", r"^(HDWinG)$"),
            ("iNTENSO", r"^(iNTENSO)$"), ("iPlanet", r"^(iPlanet)$"), ("iVy", r"^(iVy)$"),
            ("JFF", r"^(JFF)$"), ("KC", r"^(KC)$"),
            ("KiNGDOM", r"^(KiNGDOM)$"), ("KIRA", r"^(KIRA)$"), ("L0SERNIGHT", r"^(L0SERNIGHT)$"),
            ("LAMA", r"^(LAMA)$"), ("Leffe", r"^(Leffe)$"), ("Liber8", r"^(Liber8)$"), ("LiGaS", r"^(LiGaS)$"),
            ("LUCY", r"^(LUCY)$"), ("MarkII", r"^(MarkII)$"), ("MeGusta", r"^(MeGusta)$"), ("Mesc", r"^(Mesc)$"),
            ("mHD", r"^(mHD)$"), ("mSD", r"^(mSD)$"), ("MTeam", r"^(MTeam|MT)$"), ("MySiLU", r"^(MySiLU)$"),
            ("NhaNc3", r"^(NhaNc3)$"), ("nHD", r"^(nHD)$"), ("nikt0", r"^(nikt0)$"), ("NoGroup", r"NoGr(ou)?p"),
            ("nSD", r"^(nSD)$"), ("OFT", r"^(OFT)$"), ("PATOMiEL", r"^(PATOMiEL)$"),
            ("PRODJi", r"^(PRODJi)$"), ("PSA", r"^(PSA)$"), ("PTNK", r"^(PTNK)$"),
            ("RBB", r"^(RBB)$"), ("RDN", r"^(RDN)$"), ("RiffTrax", r"^(Rifftrax)$"), ("RU4HD", r"^(RU4HD)$"),
            ("SANTi", r"^(SANTi)$"), ("Scene", r"^(Scene)$"), ("SHD", r"^(SHD)$"),
            ("ShieldBearer", r"^(ShieldBearer)$"), ("STUTTERSHIT", r"^(STUTTERSHIT)$"),
            ("SUNSCREEN", r"^(SUNSCREEN)$"), ("SyncUP", r"^(SyncUP)$"), ("TBS", r"^(TBS)$"),
            ("TEKNO3D", r"^(TEKNO3D)$"), ("Tigole", r"^(Tigole)$"), ("TIKO", r"^(TIKO)$"),
            ("VISIONPLUSHDR", r"^(VISIONPLUSHDR(-X|1000)?)$"), ("WAF", r"^(WAF)$"), ("WiKi", r"^(WiKi)$"),
            ("x0r", r"^(x0r)$"), ("Zeus", r"^(Zeus)$"),
        ]),
        "LQ (Release Title)": ("ReleaseTitleSpecification", [
            ("1XBET", r"\b(1XBET)\b"), ("BEN THE MEN", r"\b(BEN[ ._-]THE[ ._-]MEN)\b"), ("D3US", r"(-D3US|D3US-)"),
            ("EVO (no WEBDL)", r"(?<!\bweb[ ._-]?(dl|rip)?\b.*)-(EVO)\b"), ("Feranki1980", r"\b(Feranki1980)\b"),
            ("GalaxyRG", r"\b(GalaxyRG)\b"), ("HHWEB (No MA)", r"^(?!.*\bMA\b.*\bWEB-?DL\b).*\b(HHWEB)\b"),

            ("PiRaTeS (no WEBDL)", r"(?<!\bweb[ ._-]?(dl|rip)?\b.*)-(PiRaTeS)\b"), ("R&H", r"\b(R&H)\b"),
            ("READ NOTE", r"\b(READ(\s|\.)+NOTE)\b"), ("SWTYBLZ", r"\b(SWTYBLZ)\b"), ("TeeWee", r"\b(TeeWee)\b"),
            ("TEKNO3D", r"\b(TEKNO3D)\b"), ("UnKn0wn (NoRemux)", r"(?<!\b(remux).*?)\b(unkn0wn)\b"),
            ("Will1869", r"\b(Will1869)\b"),
        ]),
    },
    "sonarr": {
        "LQ": ("ReleaseGroupSpecification", [
            ("BRiNK", r"^(BRiNK)$"), ("BTM", r"^(BTM)$"), ("CHX", r"^(CHX)$"), ("CTFOH", r"^(CTFOH)$"),
            ("d3g", r"^(d3g)$"), ("DepraveD", r"^(DepraveD)$"), ("EVO", r"^(EVO)$"),
            ("Feranki1980", r"^(Feranki1980)$"), ("FGT", r"^(FGT)$"), ("FMD", r"^(FMD)$"), ("GHOSTS", r"^(GHOSTS)$"),
            ("HiQVE", r"^(HiQVE)$"), ("iNTENSO", r"^(iNTENSO)$"), ("iVy", r"^(iVy)$"), ("JFF", r"^(JFF)$"),
            ("KC", r"^(KC)$"), ("MeGusta", r"^(MeGusta)$"), ("nhanc3", r"^(nhanc3)$"), ("OFT", r"^(OFT)$"),
            ("PSA", r"^(PSA)$"), ("SasukeducK", r"^(SasukeducK)$"),
            ("SHD", r"^(SHD)$"), ("ShieldBearer", r"^(ShieldBearer)$"), ("TBS", r"^(TBS)$"), ("TG", r"^(TG)$"),
            ("VIDEOHOLE", r"^(VIDEOHOLE)$"), ("worldmkv", r"^(worldmkv)$"), ("XLF", r"^(XLF)$"),
            ("Zero00", r"^(Zero00)$"),
        ]),
        "LQ (Release Title)": ("ReleaseTitleSpecification", [
            ("BEN THE MEN", r"\b(BEN[ ._-]THE[ ._-]MEN)\b"),
            ("BiTOR (2160p)", r"(?=.*?(\b2160p\b))(?=.*?(\bBiTOR\b))"), ("CREATiVE24", r"\b(CREATiVE24)\b"),
            ("Feranki1980", r"\b(Feranki1980)\b"), ("R&H", r"\b(R&H)\b"), ("TeeWee", r"\b(TeeWee)\b"),
        ]),
    },
}


# ---------------------------------------------------------------- output and errors

class StepFailed(Exception):
    """A step could not be completed; the message is written for the person running this."""


def say(app: str, what: str, result: str) -> None:
    print(f"{app:<11} {what:<46} {result}".rstrip(), flush=True)


def note(text: str) -> None:
    for line in text.splitlines():
        print(f"{'':<12}{line}".rstrip(), flush=True)


def api_error_text(body: str) -> str:
    """The human part of an app's error reply (validation failures, messages)."""
    try:
        data = json.loads(body)
    except ValueError:
        return (body or "").strip()[:200] or "no details"
    if isinstance(data, list):
        msgs = [str(d.get("errorMessage") or d.get("message") or "") for d in data if isinstance(d, dict)]
        msgs = [m for m in msgs if m]
        if msgs:
            return "; ".join(dict.fromkeys(msgs))[:300]
    if isinstance(data, dict):
        for k in ("message", "errorMessage", "error", "description"):
            if data.get(k):
                return str(data[k])[:300]
    return str(body).strip()[:200]


def describe(e: BaseException) -> str:
    if isinstance(e, settings.ApiError):
        if e.status == 401:
            return "HTTP 401: the API key was rejected"
        return f"HTTP {e.status}: {api_error_text(e.body)}"
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTP {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"cannot connect ({e.reason})"
    if isinstance(e, (TimeoutError, subprocess.TimeoutExpired)):
        return "timed out"
    if isinstance(e, StepFailed):
        return str(e)
    return f"{type(e).__name__}: {e}"


def run_step(app: str, what: str, fn) -> bool:
    """Run one step; a failure is printed as one line and never stops the other steps."""
    try:
        fn()
        return True
    except (StepFailed, settings.ApiError, OSError, ValueError, KeyError, TypeError, IndexError,
            subprocess.TimeoutExpired) as e:
        say(app, what, f"FAILED: {describe(e)}")
        return False


# ---------------------------------------------------------------- provider helpers

def by_name(items, name: str):
    for it in items or []:
        if isinstance(it, dict) and str(it.get("name", "")).lower() == name.lower():
            return it
    return None


def template(schema, implementation: str) -> dict:
    for t in schema or []:
        if t.get("implementation") == implementation:
            obj = copy.deepcopy(t)
            obj.pop("presets", None)
            obj.pop("id", None)
            return obj
    raise StepFailed(f"this app version has no '{implementation}' type")


def set_fields(obj: dict, values: dict) -> list:
    """Set provider fields by name. Returns the names this app version does not have."""
    have = {f.get("name"): f for f in obj.setdefault("fields", [])}
    missing = []
    for name, value in values.items():
        if name in have:
            have[name]["value"] = value
        else:
            missing.append(name)
    return missing


def comparable(obj: dict) -> dict:
    out = {}
    for k, v in obj.items():
        if k in ("fields", "id", "message", "presets", "infoLink", "implementationName") or k.startswith("supports"):
            continue
        out[k] = v
    for f in obj.get("fields") or []:
        out["field:" + str(f.get("name"))] = f.get("value")
    return out


def changed(existing: dict, desired: dict) -> bool:
    """True when something visible differs. Masked secrets cannot be compared and are skipped."""
    old, new = comparable(existing), comparable(desired)
    for k, v in new.items():
        if old.get(k) == MASK:
            continue
        if old.get(k) != v:
            return True
    return False


def disabled_copy(obj: dict) -> dict:
    """The same object with every trigger off, which the apps save without testing it."""
    o = copy.deepcopy(obj)
    if "enable" in o:
        o["enable"] = False
    for k in list(o):
        if k.startswith("on") and isinstance(o[k], bool):
            o[k] = False
    return o


def num(x: float):
    return int(x) if float(x).is_integer() else round(float(x), 2)


class Api:
    """settings.api() for one app, with dry-run handling and create/update in one place."""

    def __init__(self, name: str):
        self.name = name
        self.app = settings.app(name)

    def get(self, endpoint: str, params: dict = None, timeout: int = 60):
        return settings.api(self.app, endpoint, params=params, timeout=timeout)

    def write(self, method: str, endpoint: str, data=None, params: dict = None):
        if DRY:
            return data
        return settings.api(self.app, endpoint, method=method, data=data, params=params)

    def save(self, endpoint: str, obj: dict, existing) -> str:
        """Create or update a provider object (download client, notification, ...).

        Updates go out with forceSave so the app does not re-run its connection test on every
        run. When the test fails on create (the target is not running yet), the object is
        saved with its triggers off and then switched on, so it works as soon as the target
        is up; the result text says that the test failed.
        """
        if existing:
            obj["id"] = existing["id"]
            if not changed(existing, obj):
                if not DRY:
                    settings.api(self.app, f"{endpoint}/{existing['id']}", "PUT", obj, params={"forceSave": "true"})
                return "ok"
            if DRY:
                return "would update"
            settings.api(self.app, f"{endpoint}/{existing['id']}", "PUT", obj, params={"forceSave": "true"})
            return "updated"
        if DRY:
            return "would create"
        try:
            settings.api(self.app, endpoint, "POST", obj)
            return "created"
        except settings.ApiError as e:
            if e.status not in (400, 500):   # 400: validation failed; 500: the test could not connect
                raise
            why = api_error_text(e.body)
        made = settings.api(self.app, endpoint, "POST", disabled_copy(obj), params={"forceSave": "true"})
        if not isinstance(made, dict) or "id" not in made:
            raise StepFailed(why)
        obj["id"] = made["id"]
        try:
            settings.api(self.app, f"{endpoint}/{made['id']}", "PUT", obj, params={"forceSave": "true"})
        except settings.ApiError:
            return f"created but switched off: {why}"
        return f"created (its test failed: {why})"


def reachable(name: str):
    """(Api, version) when the app answers with a valid key, else None after printing why."""
    a = settings.app(name)
    if not a.key:
        say(name, "API key", f"missing: config/{name}/ has no key yet and {name.upper()}_API_KEY is empty - skipped")
        return None
    try:
        status = settings.api(a, "system/status", timeout=15)
    except (settings.ApiError, OSError, ValueError) as e:
        hint = "is the stack running? (docker compose ps)"
        if isinstance(e, settings.ApiError) and e.status == 401:
            hint = f"check {name.upper()}_API_KEY in .env, or leave it empty so config/{name}/ is read"
        say(name, f"API at {a.url}", f"unreachable: {describe(e)} - skipped ({hint})")
        return None
    version = status.get("version", "?") if isinstance(status, dict) else "?"
    say(name, f"API at {a.url}", f"ok (version {version})")
    return Api(name)


# ---------------------------------------------------------------- Radarr and Sonarr

_ARR_ENV_DONE = False


def write_arr_env() -> None:
    """scripts/arr/arr.env: the folder names the on-grab guard needs inside the containers."""
    global _ARR_ENV_DONE
    if _ARR_ENV_DONE:
        return
    _ARR_ENV_DONE = True
    path = os.path.join(settings.STACK_DIR, "scripts", "arr", "arr.env")
    text = "\n".join([
        "# Written by configure.py for scripts/arr/ongrab_guard.sh (seen as /scripts/arr.env inside",
        "# Radarr and Sonarr). Change the values in .env and re-run: python3 configure.py --only radarr",
        f"MOVIES_DIR={settings.MOVIES_DIR}",
        f"TV_DIR={settings.TV_DIR}",
        f"DOWNLOADS_DIR={settings.DOWNLOADS_DIR}",
        f"ONGRAB_DRYRUN={'1' if settings.get_bool('ONGRAB_DRYRUN') else '0'}",
    ]) + "\n"
    try:
        with open(path, encoding="utf-8") as fh:
            old = fh.read()
    except OSError:
        old = None
    if old == text:
        say("on-grab", "scripts/arr/arr.env", "ok")
        return
    if DRY:
        say("on-grab", "scripts/arr/arr.env", "would write")
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(text)
    os.chmod(tmp, 0o644)   # read by the app user inside the containers; holds no secrets
    os.replace(tmp, path)
    say("on-grab", "scripts/arr/arr.env", "written")


class Arr:
    def __init__(self, api: Api):
        self.api = api
        self.name = api.name
        self.movies = api.name == "radarr"
        self._notifications = None
        self._notif_schema = None

    def steps(self):
        out = [
            ("root folders", self.root_folders),
            (f"download client {DOWNLOAD_CLIENT}", self.download_client),
            ("quality definitions", self.quality_definitions),
            ("custom formats", self.custom_formats),
            (f"quality profile {PROFILE_NAME}", self.quality_profile),
        ]
        if not self.movies:
            out.append((f"release profile {RELEASE_PROFILE}", self.release_profile))
        out += [
            ("media management", self.media_management),
            ("file naming", self.naming),
            ("on-grab guard settings", write_arr_env),
            ("notification moviebot", self.notify_webhook),
            ("notification On-grab guard", self.notify_ongrab),
            ("notification Plex", self.notify_plex),
            ("notification Health alerts", self.notify_telegram),
        ]
        return out

    # -- folders and download client

    def root_folders(self) -> None:
        if self.movies:
            extra = [settings.to_container(p) for p in settings.movie_libraries().values()]
            wanted = [settings.CONTAINER_MOVIES] + extra
        else:
            wanted = [settings.CONTAINER_TV]
        have = {str(r.get("path", "")).rstrip("/") for r in self.api.get("rootfolder") or []}
        for path in wanted:
            if path.rstrip("/") in have:
                say(self.name, f"root folder {path}", "ok")
                continue
            self.api.write("POST", "rootfolder", {"path": path})
            say(self.name, f"root folder {path}", "would create" if DRY else "created")

    def download_client(self) -> None:
        user = settings.get("QBIT_USERNAME", "admin")
        password = settings.get("QBIT_PASSWORD")
        if not password:
            raise StepFailed("QBIT_PASSWORD is empty in .env (install.sh sets it)")
        if not DRY and not qbit_accepts_login():
            raise StepFailed("qBittorrent refuses the login in .env (QBIT_USERNAME/QBIT_PASSWORD), so the download "
                             "client was not saved: the app would retry the login until qBittorrent bans it. Run "
                             "`python3 configure.py --only qbit`, then this again.")
        existing = by_name(self.api.get("downloadclient"), DOWNLOAD_CLIENT)
        obj = copy.deepcopy(existing) if existing else template(self.api.get("downloadclient/schema"), "QBittorrent")
        obj.update(name=DOWNLOAD_CLIENT, enable=True, protocol="torrent", priority=1,
                   removeCompletedDownloads=True, removeFailedDownloads=True)
        category_field = "movieCategory" if self.movies else "tvCategory"
        missing = set_fields(obj, {"host": QBIT_HOST, "port": QBIT_PORT, "useSsl": False, "urlBase": "",
                                   "username": user, "password": password, category_field: self.name})
        if missing:
            raise StepFailed(f"unexpected download client fields, missing: {', '.join(missing)}")
        say(self.name, f"download client {DOWNLOAD_CLIENT}", self.api.save("downloadclient", obj, existing))
        if DRY:
            return
        try:
            self.api.write("POST", "downloadclient/test", obj)
            say(self.name, "download client test", "ok")
        except settings.ApiError as e:
            say(self.name, "download client test", f"failed: {api_error_text(e.body)}")
            note("qBittorrent must be running and logged in with QBIT_USERNAME/QBIT_PASSWORD;\n"
                 "check `docker logs gluetun` (the VPN) and `python3 configure.py --only qbit`.")

    # -- quality

    def _sizes(self, title: str):
        """(min, preferred, max) MB per minute for a quality title, or None to leave it alone."""
        if self.movies:
            if not any(r in title for r in ("720p", "1080p", "2160p")):
                return None
            cap, low, pref = settings.MOVIE_SIZE_CAP_MB_PER_MIN, 1, 22
        else:
            if not title.endswith("-1080p"):
                return None
            cap, low, pref = settings.TV_SIZE_CAP_MB_PER_MIN, 4, 35
        if cap < low + 1:
            raise StepFailed(f"size cap {cap} MB/min is below the minimum of {low + 1}")
        return low, max(low, min(pref, cap - 3)), cap

    def quality_definitions(self) -> None:
        # Each definition is PUT on its own: the bulk /qualitydefinition/update endpoint answers
        # 200 but changes nothing on current versions, so the result is read back and verified.
        defs = self.api.get("qualitydefinition") or []
        wanted, changed_n = {}, 0
        for d in defs:
            sizes = self._sizes(str(d.get("title") or d.get("quality", {}).get("name", "")))
            if sizes is None:
                continue
            low, pref, cap = (num(x) for x in sizes)
            wanted[d["id"]] = (low, pref, cap)
            if (d.get("minSize"), d.get("preferredSize"), d.get("maxSize")) == (low, pref, cap):
                continue
            new = dict(d, minSize=low, preferredSize=pref, maxSize=cap)
            self.api.write("PUT", f"qualitydefinition/{d['id']}", new)
            changed_n += 1
        if not wanted:
            raise StepFailed("no matching quality definitions found")
        if DRY:
            say(self.name, "quality definitions", f"would update {changed_n} of {len(wanted)}" if changed_n else "ok")
            return
        after = {d["id"]: d for d in self.api.get("qualitydefinition") or []}
        bad = [str(after.get(i, {}).get("title", i)) for i, (lo, pr, cap) in wanted.items()
               if not _close(after.get(i, {}), lo, pr, cap)]
        if bad:
            raise StepFailed(f"the app did not keep the new sizes for: {', '.join(bad)}")
        sizes = next(iter(wanted.values()))
        result = f"updated {changed_n}, verified" if changed_n else "ok"
        say(self.name, "quality definitions", f"{result} ({len(wanted)} at {sizes[0]}/{sizes[1]}/{sizes[2]} MB/min)")

    def _language_id(self, wanted: str) -> int:
        name = settings.LANGUAGE_NAMES.get(wanted.lower(), wanted) if len(wanted) == 2 else wanted
        for lang in self.api.get("language") or []:
            if str(lang.get("name", "")).lower() == name.lower():
                return int(lang["id"])
        raise StepFailed(f"PREFERRED_LANGUAGE={wanted} is not a language {self.name} knows")

    def _wanted_formats(self) -> list:
        """[(name, implementation entries)] for the custom formats this stack relies on."""
        templates = {}
        try:
            for t in self.api.get("customformat/schema") or []:
                templates[t.get("implementation")] = t
        except settings.ApiError:
            pass

        def spec(impl, name, fields, required=False):
            s = copy.deepcopy(templates.get(impl) or {"implementation": impl, "fields": []})
            s.pop("presets", None)
            s.pop("id", None)
            s.update(name=name, negate=False, required=required)
            for missing in set_fields(s, fields):
                s["fields"].append({"name": missing, "value": fields[missing]})
            return s

        out = []
        for fmt_name, (impl, entries) in LQ_FORMATS[self.name].items():
            out.append((fmt_name, [spec(impl, n, {"value": rx}) for n, rx in entries]))
        out.append((MULTI_FORMAT, [spec("ReleaseTitleSpecification", "MULTi or DUAL in title",
                                        {"value": MULTI_REGEX})]))
        lang = settings.PREFERRED_LANGUAGE
        lang_name = settings.LANGUAGE_NAMES.get(lang.lower(), lang) if len(lang) == 2 else lang
        lang_id = self._language_id(lang)
        out.append((language_format_name(), [
            spec("LanguageSpecification", f"Has a language other than {lang_name}",
                 {"value": lang_id, "exceptLanguage": True}, required=True),
            spec("LanguageSpecification", "Has a language other than the Original",
                 {"value": LANGUAGE_ORIGINAL, "exceptLanguage": True}, required=True),
            spec("LanguageSpecification", "Has at least one known language (not only Unknown)",
                 {"value": LANGUAGE_UNKNOWN, "exceptLanguage": True}, required=True),
        ]))
        return out

    def custom_formats(self) -> None:
        have = {str(c.get("name")) for c in self.api.get("customformat") or []}
        for name, specs in self._wanted_formats():
            if name in have:
                say(self.name, f"custom format {name}", "ok (exists)")
                continue
            self.api.write("POST", "customformat",
                           {"name": name, "includeCustomFormatWhenRenaming": False, "specifications": specs})
            say(self.name, f"custom format {name}", "would create" if DRY else "created")

    def quality_profile(self) -> None:
        prof = copy.deepcopy(self.api.get("qualityprofile/schema"))
        wanted = RADARR_ALLOWED if self.movies else SONARR_ALLOWED
        found, allowed_ids = set(), []
        for item in prof.get("items") or []:
            subs = item.get("items") or []
            if item.get("quality"):
                names, item_id = [item["quality"].get("name")], item["quality"].get("id")
            else:
                names, item_id = [s.get("quality", {}).get("name") for s in subs], item.get("id")
            on = any(n in wanted for n in names)
            item["allowed"] = on
            for s in subs:
                s["allowed"] = on
            if on:
                found.update(n for n in names if n in wanted)
                allowed_ids.append(item_id)
        if not allowed_ids:
            raise StepFailed("none of the profile's qualities exist in this app version")
        if wanted - found:
            say(self.name, f"quality profile {PROFILE_NAME}",
                f"note: not in this version: {', '.join(sorted(wanted - found))}")

        blocked = {"LQ", "LQ (Release Title)", language_format_name()}
        if settings.get_bool("BLOCK_MULTI_AUDIO", True):
            blocked.add(MULTI_FORMAT)
        formats = {str(c.get("name")): c.get("id") for c in self.api.get("customformat") or []}
        items = prof.get("formatItems") or []
        seen = set()
        for fi in items:
            fi["score"] = BLOCK_SCORE if fi.get("name") in blocked else 0
            seen.add(fi.get("format"))
        for name in sorted(blocked):
            if formats.get(name) is not None and formats[name] not in seen:
                items.append({"format": formats[name], "name": name, "score": BLOCK_SCORE})
        prof["formatItems"] = items
        # With upgrades off the cutoff is the lowest allowed quality, so any allowed file already
        # "meets cutoff" and is never replaced; with upgrades on it is the highest.
        prof.update(name=PROFILE_NAME, upgradeAllowed=settings.ALLOW_UPGRADES,
                    cutoff=allowed_ids[-1] if settings.ALLOW_UPGRADES else allowed_ids[0],
                    minFormatScore=0, cutoffFormatScore=0)
        if "minUpgradeFormatScore" in prof:
            prof["minUpgradeFormatScore"] = 1
        if self.movies:
            prof["language"] = self._original_language()
        existing = by_name(self.api.get("qualityprofile"), PROFILE_NAME)
        if existing:
            prof["id"] = existing["id"]
            if _profile_key(existing) == _profile_key(prof):
                say(self.name, f"quality profile {PROFILE_NAME}", "ok")
                return
            self.api.write("PUT", f"qualityprofile/{existing['id']}", prof)
            say(self.name, f"quality profile {PROFILE_NAME}", "would update" if DRY else "updated")
        else:
            prof.pop("id", None)
            self.api.write("POST", "qualityprofile", prof)
            say(self.name, f"quality profile {PROFILE_NAME}", "would create" if DRY else "created")

    def _original_language(self) -> dict:
        for lang in self.api.get("language") or []:
            if lang.get("id") == LANGUAGE_ORIGINAL:
                return {"id": LANGUAGE_ORIGINAL, "name": lang.get("name", "Original")}
        return {"id": LANGUAGE_ORIGINAL, "name": "Original"}

    def release_profile(self) -> None:
        block = settings.get_bool("BLOCK_MULTI_AUDIO", True)
        existing = by_name(self.api.get("releaseprofile"), RELEASE_PROFILE)
        if not block and not existing:
            say(self.name, f"release profile {RELEASE_PROFILE}", "off (BLOCK_MULTI_AUDIO=false)")
            return
        obj = copy.deepcopy(existing) if existing else {"name": RELEASE_PROFILE, "required": [], "indexerId": 0,
                                                        "tags": []}
        obj["enabled"] = block
        obj["ignored"] = MULTI_RELEASE_TERM if isinstance(obj.get("ignored"), str) else [MULTI_RELEASE_TERM]
        if existing:
            if all(existing.get(k) == obj[k] for k in ("enabled", "ignored")):
                say(self.name, f"release profile {RELEASE_PROFILE}", "ok")
                return
            self.api.write("PUT", f"releaseprofile/{existing['id']}", obj)
            say(self.name, f"release profile {RELEASE_PROFILE}", "would update" if DRY else "updated")
        else:
            self.api.write("POST", "releaseprofile", obj)
            say(self.name, f"release profile {RELEASE_PROFILE}", "would create" if DRY else "created")

    # -- media management and naming

    def _put_config(self, endpoint: str, label: str, updates: dict) -> None:
        cur = self.api.get(endpoint) or {}
        new = dict(cur)
        new.update(updates)
        if new == cur:
            say(self.name, label, "ok")
            return
        target = f"{endpoint}/{cur['id']}" if "id" in cur else endpoint
        self.api.write("PUT", target, new)
        diff = sorted(k for k in updates if cur.get(k) != updates[k])
        say(self.name, label, ("would update " if DRY else "updated ") + ", ".join(diff))

    def media_management(self) -> None:
        # The recycle bin setting is left exactly as it is.
        self._put_config("config/mediamanagement", "media management", {
            "copyUsingHardlinks": True,
            "minimumFreeSpaceWhenImporting": settings.get_int("MIN_FREE_SPACE_MB", 25000),
            "importExtraFiles": True,
            "extraFileExtensions": "srt,sub,idx",
            "enableMediaInfo": True,
        })

    def naming(self) -> None:
        rename = settings.get_bool("RENAME_FILES", True)
        if self.movies:
            updates = dict(RADARR_NAMING, renameMovies=rename)
        else:
            updates = dict(SONARR_NAMING, renameEpisodes=rename)
        self._put_config("config/naming", "file naming", updates)

    # -- notifications

    def _notification(self, name: str, implementation: str, fields: dict, events: set, extra: dict = None) -> None:
        if self._notifications is None:
            self._notifications = self.api.get("notification") or []
        existing = by_name(self._notifications, name)
        if existing:
            obj = copy.deepcopy(existing)
        else:
            if self._notif_schema is None:
                self._notif_schema = self.api.get("notification/schema") or []
            obj = template(self._notif_schema, implementation)
        obj["name"] = name
        for k in list(obj):
            if k.startswith("on") and isinstance(obj[k], bool):
                obj[k] = k in events
        unsupported = sorted(e for e in events if e not in obj)
        if extra:
            obj.update(extra)
        missing = set_fields(obj, fields)
        if missing:
            raise StepFailed(f"unexpected {implementation} fields, missing: {', '.join(missing)}")
        result = self.api.save("notification", obj, existing)
        if unsupported:
            result += f" (this version has no {', '.join(unsupported)})"
        say(self.name, f"notification {name}", result)

    def notify_webhook(self) -> None:
        secret = settings.get("WEBHOOK_SECRET")
        if not secret:
            raise StepFailed("WEBHOOK_SECRET is empty in .env (install.sh generates it)")
        port = settings.get("WEBHOOK_PORT", "8090")
        events = {"onGrab", "onDownload", "onUpgrade"} | ({"onMovieAdded"} if self.movies else set())
        self._notification("moviebot", "Webhook",
                           {"url": f"http://moviebot:{port}/{self.name}/{secret}", "method": 1}, events)

    def notify_ongrab(self) -> None:
        self._notification("On-grab guard", "CustomScript", {"path": "/scripts/ongrab_guard.sh"}, {"onGrab"})

    def notify_plex(self) -> None:
        url, token = settings.get("PLEX_URL"), settings.get("PLEX_TOKEN")
        if not (url and token):
            say(self.name, "notification Plex", "skipped (PLEX_URL and PLEX_TOKEN are not both set)")
            return
        u = urllib.parse.urlsplit(url)
        if not u.hostname:
            raise StepFailed(f"PLEX_URL is not a URL: {url}")
        self._notification("Plex", "PlexServer",
                           {"host": u.hostname, "port": u.port or 32400, "useSsl": u.scheme == "https",
                            "authToken": token, "updateLibrary": True},
                           {"onDownload", "onUpgrade", "onRename"})

    def notify_telegram(self) -> None:
        token, chat = settings.get("TELEGRAM_BOT_TOKEN"), settings.notify_chat_id()
        if not (token and chat):
            say(self.name, "notification Health alerts", "skipped (no Telegram token or chat id)")
            return
        self._notification("Health alerts", "Telegram", {"botToken": token, "chatId": chat},
                           {"onHealthIssue"}, extra={"includeHealthWarnings": False})


def language_format_name() -> str:
    lang = settings.PREFERRED_LANGUAGE
    lang = settings.LANGUAGE_NAMES.get(lang.lower(), lang) if len(lang) == 2 else lang
    return f"Language: Not {lang} or Original"


def _close(d: dict, low, pref, cap) -> bool:
    try:
        return all(abs(float(d.get(k)) - float(v)) < 0.01
                   for k, v in (("minSize", low), ("preferredSize", pref), ("maxSize", cap)))
    except (TypeError, ValueError):
        return False


def _profile_key(p: dict) -> str:
    """What decides whether two versions of the profile behave the same."""
    def items(lst):
        return [[i.get("id") or i.get("quality", {}).get("id"), bool(i.get("allowed")), items(i.get("items") or [])]
                for i in lst]
    return json.dumps({
        "items": items(p.get("items") or []),
        "cutoff": p.get("cutoff"), "upgradeAllowed": p.get("upgradeAllowed"),
        "minFormatScore": p.get("minFormatScore"), "cutoffFormatScore": p.get("cutoffFormatScore"),
        "minUpgradeFormatScore": p.get("minUpgradeFormatScore"),
        "language": (p.get("language") or {}).get("id"),
        "formats": sorted([fi.get("format"), fi.get("score")] for fi in p.get("formatItems") or []),
    }, sort_keys=True)


def configure_arr(name: str) -> bool:
    api = reachable(name)
    if api is None:
        return False
    ok = True
    for what, fn in Arr(api).steps():
        ok = run_step(name, what, fn) and ok
    return ok


def configure_radarr() -> bool:
    return configure_arr("radarr")


def configure_sonarr() -> bool:
    return configure_arr("sonarr")


# ---------------------------------------------------------------- Prowlarr

def configure_prowlarr() -> bool:
    api = reachable("prowlarr")
    if api is None:
        return False
    tag = {}

    def application(impl: str, app_name: str, port: int) -> None:
        key = settings.app(app_name).key
        if not key:
            raise StepFailed(f"no {app_name} API key yet")
        existing = by_name(api.get("applications"), impl)
        obj = copy.deepcopy(existing) if existing else template(api.get("applications/schema"), impl)
        obj.update(name=impl, enable=True, syncLevel="fullSync")
        missing = set_fields(obj, {"prowlarrUrl": "http://prowlarr:9696", "baseUrl": f"http://{app_name}:{port}",
                                   "apiKey": key})
        if missing:
            raise StepFailed(f"unexpected application fields, missing: {', '.join(missing)}")
        say("prowlarr", f"application {impl}", api.save("applications", obj, existing))

    def flaresolverr_tag() -> None:
        for t in api.get("tag") or []:
            if str(t.get("label", "")).lower() == "flaresolverr":
                tag["id"] = t["id"]
                say("prowlarr", "tag flaresolverr", "ok")
                return
        made = api.write("POST", "tag", {"label": "flaresolverr"})
        if isinstance(made, dict) and "id" in made:
            tag["id"] = made["id"]
        say("prowlarr", "tag flaresolverr", "would create" if DRY else "created")

    def flaresolverr_proxy() -> None:
        existing = by_name(api.get("indexerproxy"), "FlareSolverr")
        obj = copy.deepcopy(existing) if existing else template(api.get("indexerproxy/schema"), "FlareSolverr")
        obj["name"] = "FlareSolverr"
        if "id" in tag:
            obj["tags"] = sorted(set(obj.get("tags") or []) | {tag["id"]})
        missing = set_fields(obj, {"host": "http://flaresolverr:8191", "requestTimeout": 180})
        if missing:
            raise StepFailed(f"unexpected FlareSolverr fields, missing: {', '.join(missing)}")
        # FlareSolverr starts a browser first, and Prowlarr tests the proxy before saving it:
        # on a fresh install give it up to two minutes.
        for attempt in range(PROXY_TRIES):
            try:
                say("prowlarr", "indexer proxy FlareSolverr", api.save("indexerproxy", obj, existing))
                return
            except (settings.ApiError, StepFailed):
                if attempt == PROXY_TRIES - 1:
                    raise
                time.sleep(PROXY_PAUSE)

    def health_alerts() -> None:
        token, chat = settings.get("TELEGRAM_BOT_TOKEN"), settings.notify_chat_id()
        if not (token and chat):
            say("prowlarr", "notification Health alerts", "skipped (no Telegram token or chat id)")
            return
        existing = by_name(api.get("notification"), "Health alerts")
        obj = copy.deepcopy(existing) if existing else template(api.get("notification/schema"), "Telegram")
        obj["name"] = "Health alerts"
        for k in list(obj):
            if k.startswith("on") and isinstance(obj[k], bool):
                obj[k] = k == "onHealthIssue"
        obj["includeHealthWarnings"] = False
        missing = set_fields(obj, {"botToken": token, "chatId": chat})
        if missing:
            raise StepFailed(f"unexpected Telegram fields, missing: {', '.join(missing)}")
        say("prowlarr", "notification Health alerts", api.save("notification", obj, existing))

    ok = True
    for what, fn in (("application Radarr", lambda: application("Radarr", "radarr", 7878)),
                     ("application Sonarr", lambda: application("Sonarr", "sonarr", 8989)),
                     ("tag flaresolverr", flaresolverr_tag),
                     ("indexer proxy FlareSolverr", flaresolverr_proxy),
                     ("notification Health alerts", health_alerts)):
        ok = run_step("prowlarr", what, fn) and ok
    say("prowlarr", "indexers", "none added: that is up to you")
    note("Add indexers in Prowlarr and tag the ones behind Cloudflare with 'flaresolverr'.")
    return ok


# ---------------------------------------------------------------- qBittorrent

TEMP_PASSWORD = re.compile(r"temporary password is provided for this session:\s*(\S+)")


class Qbit:
    """Minimal qBittorrent WebUI API v2 client with its own login (settings.qbit cannot do the
    first-start login with the temporary password)."""

    def __init__(self, url: str):
        self.url = url.rstrip("/")
        self.status = 0
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, path: str, data: dict = None, timeout: int = 20) -> str:
        body = urllib.parse.urlencode(data).encode() if data is not None else None
        req = urllib.request.Request(f"{self.url}/api/v2/{path}", data=body,
                                     headers={"Referer": self.url, "Origin": self.url})
        with self.opener.open(req, timeout=timeout) as r:
            self.status = r.status
            return r.read().decode("utf-8", "replace")

    def json(self, path: str):
        return json.loads(self.call(path) or "null")

    def reachable(self):
        """(True, "") when the web UI answers at all; a 403 before login counts as answering."""
        try:
            self.call("app/version", timeout=10)
            return True, ""
        except urllib.error.HTTPError as e:
            return e.code in (401, 403), f"HTTP {e.code}"
        except OSError as e:
            return False, describe(e)

    def login(self, user: str, password: str) -> bool:
        """True when qBittorrent accepted the login. Older versions answer 200 "Ok." (and 200
        "Fails." for a wrong password); 5.x answers 204 with no body, and 401 for a wrong password."""
        try:
            body = self.call("auth/login", {"username": user, "password": password})
        except urllib.error.HTTPError as e:
            if e.code == 401:
                return False
            if e.code == 403:
                raise StepFailed("qBittorrent is refusing logins from this address after failed attempts; "
                                 "wait an hour or run `docker restart qbittorrent`") from None
            raise
        return self.status == 204 or body.strip().lower().startswith("ok")


def qbit_accepts_login() -> bool:
    """False only when qBittorrent answers and refuses QBIT_USERNAME/QBIT_PASSWORD (or has banned this
    address). True when it accepts the login, and also when it cannot be reached at all: then the apps'
    own connection test reports the problem, and no failed logins pile up."""
    q = Qbit(settings.qbit_url())
    try:
        return q.login(settings.get("QBIT_USERNAME", "admin"), settings.get("QBIT_PASSWORD"))
    except StepFailed:
        return False
    except Exception:  # unreachable, timeout, odd reply: nothing can be told from here
        return True


def qbit_temp_password() -> str:
    """The temporary password the linuxserver image prints at start until a password is set."""
    try:
        r = subprocess.run(settings.docker_cmd() + ["logs", "qbittorrent"], stdout=subprocess.PIPE,
                           stderr=subprocess.STDOUT, text=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    found = TEMP_PASSWORD.findall(r.stdout or "")
    return found[-1] if found else ""


def qbit_wanted_prefs() -> dict:
    return {
        "save_path": settings.CONTAINER_DOWNLOADS,
        "temp_path_enabled": False,
        "auto_tmm_enabled": True,              # category save paths only apply in automatic mode
        "max_ratio_enabled": True,
        "max_ratio": 2,
        "max_seeding_time_enabled": True,
        "max_seeding_time": 4320,              # minutes = 3 days
        "max_ratio_act": 0,                    # pause (stop) the torrent; never delete it
        "queueing_enabled": True,
        "max_active_downloads": 10,
        "max_active_uploads": 10,
        "max_active_torrents": 70,
        "dont_count_slow_torrents": True,
        "excluded_file_names_enabled": True,
        "excluded_file_names": "\n".join(QBIT_EXCLUDED),
        "bypass_local_auth": True,             # lets scripts/gluetun/setport.sh work without a password
        "upnp": False,
        "random_port": False,
        "web_ui_host_header_validation_enabled": False,
    }


def _pref_equal(key: str, cur, want) -> bool:
    if key == "excluded_file_names":
        def norm(v):
            return sorted(x.strip() for x in re.split(r"[\n,;]", str(v or "")) if x.strip())
        return norm(cur) == norm(want)
    if key == "save_path":
        return str(cur or "").rstrip("/") == str(want).rstrip("/")
    if isinstance(want, bool):
        return bool(cur) is want
    if isinstance(want, (int, float)):
        try:
            return abs(float(cur) - float(want)) < 1e-6
        except (TypeError, ValueError):
            return False
    return cur == want


def configure_qbit() -> bool:
    url = settings.qbit_url()
    q = Qbit(url)
    up, why = q.reachable()
    if not up:
        say("qbittorrent", f"web UI at {url}", f"unreachable: {why} - skipped")
        note("qBittorrent only runs while the VPN is healthy: check `docker logs gluetun`.")
        return False
    user, password = settings.get("QBIT_USERNAME", "admin"), settings.get("QBIT_PASSWORD")
    if not password:
        say("qbittorrent", "login", "FAILED: QBIT_PASSWORD is empty in .env (install.sh sets it)")
        return False
    try:
        logged_in = q.login(user, password)
        if logged_in:
            say("qbittorrent", "login", "ok")
        else:
            temp = qbit_temp_password()
            if not temp:
                say("qbittorrent", "login", "FAILED: QBIT_USERNAME/QBIT_PASSWORD rejected and no temporary "
                    "password in `docker logs qbittorrent`")
                note("Set QBIT_PASSWORD in .env to the web UI password you chose, then run this again.")
                return False
            if DRY:
                say("qbittorrent", "login", "would log in with the temporary password and set the .env login")
                return True
            if not q.login("admin", temp):
                say("qbittorrent", "login", "FAILED: the temporary password from the log was rejected")
                return False
            q.call("app/setPreferences", {"json": json.dumps({"web_ui_username": user, "web_ui_password": password})})
            q = Qbit(url)
            if not q.login(user, password):
                say("qbittorrent", "login", "FAILED: the new login from .env did not work")
                return False
            say("qbittorrent", "login", "set from .env (first start)")
    except (StepFailed, OSError, ValueError) as e:
        say("qbittorrent", "login", f"FAILED: {describe(e)}")
        return False

    def preferences() -> None:
        cur = q.json("app/preferences") or {}
        want = qbit_wanted_prefs()
        diff = {k: v for k, v in want.items() if not _pref_equal(k, cur.get(k), v)}
        if not diff:
            say("qbittorrent", "preferences", "ok")
        else:
            if not DRY:
                q.call("app/setPreferences", {"json": json.dumps(diff)})
            say("qbittorrent", "preferences", ("would update " if DRY else "updated ") + ", ".join(sorted(diff)))

    def categories() -> None:
        cats = q.json("torrents/categories") or {}
        for name, sub in (("radarr", "movies"), ("sonarr", "tv")):
            path = f"{settings.CONTAINER_DOWNLOADS}/{sub}"
            cur = cats.get(name)
            if cur is None:
                if not DRY:
                    q.call("torrents/createCategory", {"category": name, "savePath": path})
                say("qbittorrent", f"category {name} -> {path}", "would create" if DRY else "created")
            elif str(cur.get("savePath") or "").rstrip("/") != path:
                if not DRY:
                    q.call("torrents/editCategory", {"category": name, "savePath": path})
                say("qbittorrent", f"category {name} -> {path}", "would update" if DRY else "updated")
            else:
                say("qbittorrent", f"category {name} -> {path}", "ok")

    def forwarded_port() -> None:
        if settings.get("VPN_MODE", "on").lower() == "off":
            return
        if settings.get("VPN_PORT_FORWARDING", "off").lower() != "on":
            return
        r = settings.docker("exec", "gluetun", "cat", "/tmp/gluetun/forwarded_port", timeout=30)
        port = (r.stdout or "").strip()
        if r.returncode != 0 or not port.isdigit():
            say("qbittorrent", "listening port", "no forwarded port yet (vpn_watchdog keeps it in sync)")
            return
        cur = (q.json("app/preferences") or {}).get("listen_port")
        if str(cur) == port:
            say("qbittorrent", "listening port", f"ok ({port}, forwarded by the VPN)")
            return
        if not DRY:
            q.call("app/setPreferences", {"json": json.dumps({"listen_port": int(port)})})
        say("qbittorrent", "listening port", f"{'would set' if DRY else 'set'} to the forwarded port {port}")

    ok = True
    for what, fn in (("preferences", preferences), ("categories", categories), ("listening port", forwarded_port)):
        ok = run_step("qbittorrent", what, fn) and ok
    return ok


# ---------------------------------------------------------------- Jellyseerr

class Seerr:
    """Jellyseerr API client. Before the first-run setup is finished the session cookie from the
    Plex sign-in is what authorises the calls; afterwards the API key does."""

    def __init__(self):
        a = settings.jellyseerr()
        self.url, self.key = a.url, a.key
        self.use_key = True
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def call(self, method: str, path: str, data=None, params: dict = None, timeout: int = 60):
        url = f"{self.url}/api/v1/{path.lstrip('/')}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        headers = {"Accept": "application/json"}
        if self.use_key and self.key:
            headers["X-Api-Key"] = self.key
        body = None
        if data is not None:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with self.opener.open(req, timeout=timeout) as r:
                raw = r.read()
        except urllib.error.HTTPError as e:
            raise settings.ApiError(e.code, e.read().decode(errors="replace"), url) from None
        if not raw:
            return None
        try:
            return json.loads(raw)
        except ValueError:
            return raw.decode(errors="replace")


def jellyseerr_manual_steps(url: str) -> None:
    note(f"Jellyseerr needs one sign-in in the browser: open {url} (or the server's address on\n"
         "port 5055 from another device), sign in with your media server account and finish the\n"
         "wizard (the Radarr/Sonarr step can be skipped). Then link Radarr and Sonarr with:\n"
         "  python3 configure.py --only jellyseerr")


def jellyseerr_link(s: Seerr, kind: str) -> None:
    a = settings.app(kind)
    if not a.key:
        raise StepFailed(f"no {kind} API key yet")
    profiles = settings.api(a, "qualityprofile") or []
    prof = by_name(profiles, PROFILE_NAME) or (profiles[0] if profiles else None)
    if not prof:
        raise StepFailed(f"{kind} has no quality profile yet")
    movies = kind == "radarr"
    root = settings.CONTAINER_MOVIES if movies else settings.CONTAINER_TV
    body = {"name": kind.capitalize(), "hostname": kind, "port": 7878 if movies else 8989, "apiKey": a.key,
            "useSsl": False, "baseUrl": "", "activeProfileId": prof["id"], "activeProfileName": prof["name"],
            "activeDirectory": root, "is4k": False, "isDefault": True, "syncEnabled": True,
            "preventSearch": False, "tagRequests": False, "externalUrl": "", "tags": []}
    if movies:
        body["minimumAvailability"] = "released"
    else:
        body.update(activeAnimeProfileId=prof["id"], activeAnimeProfileName=prof["name"],
                    activeAnimeDirectory=root, animeTags=[], enableSeasonFolders=True)
    servers = s.call("GET", f"settings/{kind}") or []
    match = next((x for x in servers if isinstance(x, dict) and
                  (x.get("hostname") == kind or str(x.get("name", "")).lower() == kind)), None)
    if match:
        merged = dict(match)
        merged.update(body)
        if all(match.get(k) == v for k, v in body.items()):
            say("jellyseerr", f"{kind} link", "ok")
            return
        if not DRY:
            s.call("PUT", f"settings/{kind}/{match['id']}", merged)
        say("jellyseerr", f"{kind} link", "would update" if DRY else "updated")
    else:
        if not DRY:
            s.call("POST", f"settings/{kind}", body)
        say("jellyseerr", f"{kind} link", f"{'would create' if DRY else 'created'} ({prof['name']}, {root})")


def jellyseerr_plex(s: Seerr) -> None:
    url = settings.get("PLEX_URL")
    if not url:
        say("jellyseerr", "Plex server", "skipped (PLEX_URL not set)")
        return
    u = urllib.parse.urlsplit(url)
    if not u.hostname:
        raise StepFailed(f"PLEX_URL is not a URL: {url}")
    s.call("POST", "settings/plex", {"ip": u.hostname, "port": u.port or 32400, "useSsl": u.scheme == "https"})
    libraries = s.call("GET", "settings/plex/library", params={"sync": "true"}) or []
    ids = [str(lib["id"]) for lib in libraries if isinstance(lib, dict) and lib.get("id") is not None]
    if ids:
        s.call("GET", "settings/plex/library", params={"enable": ",".join(ids)})
    say("jellyseerr", "Plex server", f"linked ({len(ids)} libraries enabled)")


def configure_jellyseerr() -> bool:
    s = Seerr()
    try:
        public = s.call("GET", "settings/public", timeout=15)
    except (settings.ApiError, OSError, ValueError) as e:
        say("jellyseerr", f"API at {s.url}", f"unreachable: {describe(e)} - skipped")
        return False
    say("jellyseerr", f"API at {s.url}", "ok")
    if isinstance(public, dict) and public.get("initialized"):
        say("jellyseerr", "first-run setup", "done")
        if not s.key:
            say("jellyseerr", "API key", "missing: config/jellyseerr/settings.json not readable - cannot link")
            return False
        ok = run_step("jellyseerr", "radarr link", lambda: jellyseerr_link(s, "radarr"))
        return run_step("jellyseerr", "sonarr link", lambda: jellyseerr_link(s, "sonarr")) and ok
    token = settings.get("PLEX_TOKEN")
    if not token:
        say("jellyseerr", "first-run setup", "waiting for you (no PLEX_TOKEN to do it automatically)")
        jellyseerr_manual_steps(s.url)
        return True
    if DRY:
        say("jellyseerr", "first-run setup", "would sign in with PLEX_TOKEN, link Plex, Radarr, Sonarr and finish")
        return True
    s.use_key = False
    if not run_step("jellyseerr", "sign in with PLEX_TOKEN", lambda: s.call("POST", "auth/plex", {"authToken": token})):
        jellyseerr_manual_steps(s.url)
        return False
    say("jellyseerr", "sign in with PLEX_TOKEN", "ok (that Plex account is the Jellyseerr admin)")
    ok = run_step("jellyseerr", "Plex server", lambda: jellyseerr_plex(s))
    ok = run_step("jellyseerr", "radarr link", lambda: jellyseerr_link(s, "radarr")) and ok
    ok = run_step("jellyseerr", "sonarr link", lambda: jellyseerr_link(s, "sonarr")) and ok
    if run_step("jellyseerr", "finish setup", lambda: s.call("POST", "settings/initialize")):
        say("jellyseerr", "first-run setup", "done")
    else:
        ok = False
        jellyseerr_manual_steps(s.url)
    return ok


# ---------------------------------------------------------------- cron

def stack_for_cron() -> str:
    for cand in (settings.STACK_DIR, HERE):
        if os.path.isfile(os.path.join(cand, "scripts", "run_guard.sh")):
            return cand
    return HERE


def render_cron(text: str, stack: str):
    """The crontab block (with markers) and the number of active jobs in it."""
    if any(c in stack for c in "\n\"'"):
        raise StepFailed(f"the stack folder path cannot be used in a crontab: {stack!r}")
    stack = stack.replace("%", "\\%")     # % ends the command in a crontab line
    shrink = settings.get_bool("SHRINK_ENABLED")
    lines, jobs = [MARK_BEGIN], 0
    for raw in text.splitlines():
        line = raw.rstrip()
        if line.startswith("##"):
            continue              # notes for whoever edits the template
        m = re.match(r"#PROFILE:([A-Za-z0-9_-]+)\s+(.*)$", line)
        if m:
            if not settings.profile_enabled(m.group(1)):
                continue
            line = m.group(2)
        if line.startswith("#SHRINK ") and shrink:
            line = line[len("#SHRINK "):]
        line = line.replace("{{STACK}}", stack)
        if line and not line.startswith("#"):
            jobs += 1
        lines.append(line)
    lines.append(MARK_END)
    return "\n".join(lines) + "\n", jobs


def replace_block(current: str, block: str) -> str:
    """The crontab text with the media-stack block replaced (or removed when block is empty)."""
    lines = current.splitlines()
    begins = [i for i, x in enumerate(lines) if x.strip() == MARK_BEGIN]
    ends = [i for i, x in enumerate(lines) if x.strip() == MARK_END]
    if len(begins) != len(ends) or any(e < b for b, e in zip(begins, ends)):
        raise StepFailed("your crontab has unmatched media-stack markers; fix them with `crontab -e`")
    out, inside, placed = [], False, False
    for line in lines:
        if line.strip() == MARK_BEGIN:
            inside = True
            if block and not placed:
                out.extend(block.rstrip("\n").splitlines())
                placed = True
            continue
        if line.strip() == MARK_END:
            inside = False
            continue
        if not inside:
            out.append(line)
    if block and not placed:
        out.extend(block.rstrip("\n").splitlines())
    return "\n".join(out) + "\n" if out else ""


def read_crontab() -> str:
    r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
    if r.returncode == 0:
        return r.stdout
    if "no crontab" in (r.stderr or "").lower():
        return ""
    raise StepFailed(f"crontab -l failed: {(r.stderr or '').strip()[:200]}")


def configure_cron() -> bool:
    if not shutil.which("crontab"):
        say("cron", "guard jobs", "skipped: no crontab command (install cron, then run --only cron)")
        return True

    def install() -> None:
        stack = stack_for_cron()
        with open(os.path.join(stack, "scripts", "cron.template"), encoding="utf-8") as fh:
            block, jobs = render_cron(fh.read(), stack)
        enabled = settings.get_bool("GUARDS_ENABLED", True)
        current = read_crontab()
        new = replace_block(current, block if enabled else "")
        if new.strip() == current.strip():
            say("cron", "guard jobs", f"ok ({jobs} jobs)" if enabled else "none installed (GUARDS_ENABLED=false)")
            return
        if DRY:
            say("cron", "guard jobs",
                f"would install {jobs} jobs:" if enabled else "would remove them (GUARDS_ENABLED=false)")
            if enabled:
                note(block.rstrip("\n"))
            return
        r = subprocess.run(["crontab", "-"], input=new, capture_output=True, text=True, timeout=30)
        if r.returncode != 0:
            raise StepFailed(f"crontab refused the new table: {(r.stderr or '').strip()[:200]}")
        say("cron", "guard jobs", f"installed {jobs} jobs" if enabled else "removed (GUARDS_ENABLED=false)")

    return run_step("cron", "guard jobs", install)


# ---------------------------------------------------------------- main

RUNNERS = {
    "qbit": configure_qbit,
    "radarr": configure_radarr,
    "sonarr": configure_sonarr,
    "prowlarr": configure_prowlarr,
    "jellyseerr": configure_jellyseerr,
    "cron": configure_cron,
}


def main(argv) -> int:
    global DRY
    parser = argparse.ArgumentParser(prog="configure.py", description="Wire the media-stack apps together.",
                                     epilog="Parts run in this order: " + ", ".join(PARTS))
    parser.add_argument("--dry-run", action="store_true", help="read only: print what would change")
    parser.add_argument("--only", default="", metavar="PARTS", help="comma separated subset of: " + ",".join(PARTS))
    args = parser.parse_args(argv)
    DRY = args.dry_run
    chosen = [p.strip().lower() for p in args.only.split(",") if p.strip()] or list(PARTS)
    unknown = [p for p in chosen if p not in PARTS]
    if unknown:
        print(f"unknown part: {', '.join(unknown)} (choose from {', '.join(PARTS)})", file=sys.stderr)
        return 2
    print(f"media-stack configure: {settings.STACK_DIR}" + (" (dry run: nothing is changed)" if DRY else ""))
    failed = []
    for part in PARTS:
        if part not in chosen:
            continue
        try:
            ok = RUNNERS[part]()
        except Exception as e:  # last resort: one readable line instead of a traceback
            say(part, "unexpected error", f"{type(e).__name__}: {e}")
            ok = False
        if not ok:
            failed.append(part)
    if failed:
        print(f"\nNot finished: {', '.join(failed)}. Fix what is shown above and run this again "
              "(python3 configure.py is safe to repeat).")
        return 1
    print("\nAll done." if not DRY else "\nDry run finished: nothing was changed.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
