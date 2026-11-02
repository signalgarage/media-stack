#!/usr/bin/env python3
"""Shared settings for every host-side script in media-stack.

Everything a guard needs comes from here: the stack folder, the .env values, the
media paths, the app URLs and API keys, Telegram, Docker and ffprobe helpers. No
script hardcodes a path, address or key of its own.

Lookup order for a value: process environment, then the stack's .env file, then
the default given in the call. API keys that are left empty are read from each
app's own config file under config/<app>/.

Only the standard library is used, and the code stays compatible with Python 3.9
so it runs on the distributions people actually install this on.
"""
from __future__ import annotations

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

# ---------------------------------------------------------------- stack folder

def _find_stack_dir() -> str:
    env = os.environ.get("MEDIA_STACK_DIR")
    if env:
        return os.path.abspath(env)
    here = os.path.dirname(os.path.abspath(__file__))
    for cand in (here, os.path.dirname(here)):
        if os.path.exists(os.path.join(cand, "docker-compose.yml")):
            return cand
    return os.path.dirname(here)


STACK_DIR = _find_stack_dir()
ENV_FILE = os.path.join(STACK_DIR, ".env")


def _parse_env_file(path: str) -> dict:
    out = {}
    try:
        with open(path, encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                else:
                    # an unquoted value ends at " #": compose reads it the same way
                    val = re.split(r"\s+#", val, maxsplit=1)[0].rstrip()
                out[key] = val
    except FileNotFoundError:
        pass
    return out


_FILE_ENV = _parse_env_file(ENV_FILE)


def get(name: str, default: str = "") -> str:
    """Value of a setting: process environment first, then .env, then default."""
    val = os.environ.get(name)
    if val is None or val == "":
        val = _FILE_ENV.get(name, "")
    return val if val != "" else default


def get_bool(name: str, default: bool = False) -> bool:
    val = get(name, "").strip().lower()
    if not val:
        return default
    return val in ("1", "true", "yes", "on")


def get_int(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


def get_float(name: str, default: float) -> float:
    try:
        return float(get(name, str(default)))
    except ValueError:
        return default


def get_list(name: str, default: str = "") -> list:
    return [x.strip() for x in get(name, default).split(",") if x.strip()]


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(STACK_DIR, path))


def dry_run() -> bool:
    return "--dry-run" in sys.argv or "--dry" in sys.argv or get_bool("DRY_RUN")


def profile_enabled(name: str) -> bool:
    return name in get_list("COMPOSE_PROFILES")


# ---------------------------------------------------------------- paths

MEDIA_ROOT = _abs(get("MEDIA_ROOT", "/srv/media"))
DOWNLOADS_DIR = get("DOWNLOADS_DIR", "downloads").strip("/")
MOVIES_DIR = get("MOVIES_DIR", "movies").strip("/")
TV_DIR = get("TV_DIR", "tv").strip("/")
DOWNLOADS = os.path.join(MEDIA_ROOT, DOWNLOADS_DIR)
MOVIES = os.path.join(MEDIA_ROOT, MOVIES_DIR)
TV = os.path.join(MEDIA_ROOT, TV_DIR)
STATE_DIR = _abs(get("STATE_DIR", "./state"))
LOG_DIR = _abs(get("LOG_DIR", "./logs"))
BACKUP_DIR = _abs(get("BACKUP_DIR", "./backups"))
CONFIG_DIR = os.path.join(STACK_DIR, "config")
CONFIRM_DIR = os.path.join(STATE_DIR, "confirms")   # shared with the bot (its /state/confirms)

CONTAINER_DATA = "/data"
CONTAINER_DOWNLOADS = CONTAINER_DATA + "/" + DOWNLOADS_DIR
CONTAINER_MOVIES = CONTAINER_DATA + "/" + MOVIES_DIR
CONTAINER_TV = CONTAINER_DATA + "/" + TV_DIR

VIDEO_EXT = (".mkv", ".mp4", ".avi", ".m4v", ".mpg", ".mpeg", ".wmv", ".mov", ".ts", ".webm", ".m2ts", ".vob")


def language_code(language) -> str:
    """ISO 639-1 code for a code ("fr", "pt-BR") or a name ("French"); "" when unknown."""
    lang = str(language or "").strip().lower()
    if not lang:
        return ""
    base = lang.split("-")[0].split("_")[0]
    if len(base) == 2 and base.isalpha() and base in LANGUAGE_NAMES:
        return base
    return LANGUAGE_CODES.get(lang, "")


def _safe_subfolder(folder: str) -> str:
    """A folder name below MEDIA_ROOT, or "" when it would escape it or is empty."""
    folder = str(folder or "").strip().strip("/")
    if not folder or folder.startswith(("/", "~")) or "\\" in folder:
        return ""
    if any(part in ("", ".", "..") for part in folder.split("/")):
        return ""
    return folder


def movie_libraries() -> dict:
    """Extra movie roots by language code: {"fr": "/srv/media/movies-fr"}. Empty when off.

    Entries are "<code or language name>=<folder below MEDIA_ROOT>"; anything else is skipped,
    exactly like the bot's own parser, so both sides file a title into the same folder."""
    out = {}
    for part in get_list("MOVIE_LIBRARIES"):
        if "=" not in part:
            continue
        lang, folder = part.split("=", 1)
        code, folder = language_code(lang), _safe_subfolder(folder)
        if code and folder:
            out[code] = os.path.join(MEDIA_ROOT, folder)
    return out


def movie_roots() -> list:
    """Every movie library root on the host, default first."""
    return [MOVIES] + list(movie_libraries().values())


def library_roots() -> list:
    """Every library root (movies, extra movie libraries, tv); never the downloads folder."""
    return movie_roots() + [TV]


def to_host(container_path: str) -> str:
    """/data/... as the containers see it -> the host path."""
    if container_path == CONTAINER_DATA or container_path.startswith(CONTAINER_DATA + "/"):
        return os.path.join(MEDIA_ROOT, container_path[len(CONTAINER_DATA):].lstrip("/"))
    return container_path


def to_container(host_path: str) -> str:
    """Host path under MEDIA_ROOT -> /data/... as the containers see it."""
    host_path = os.path.abspath(host_path)
    if host_path == MEDIA_ROOT:
        return CONTAINER_DATA
    if host_path.startswith(MEDIA_ROOT + os.sep):
        return CONTAINER_DATA + "/" + os.path.relpath(host_path, MEDIA_ROOT).replace(os.sep, "/")
    return host_path


def ensure_dirs() -> None:
    for d in (STATE_DIR, LOG_DIR, CONFIRM_DIR):
        os.makedirs(d, exist_ok=True)


def state_path(name: str) -> str:
    ensure_dirs()
    return os.path.join(STATE_DIR, name)


def heartbeat(job: str) -> None:
    try:
        with open(state_path(f".{job}_heartbeat"), "w") as fh:
            fh.write(str(int(time.time())))
    except OSError:
        pass


def heartbeat_age(job: str) -> float:
    """Seconds since the job last ran, or a very large number when it never did."""
    try:
        return time.time() - os.path.getmtime(state_path(f".{job}_heartbeat"))
    except OSError:
        return 1e12


def load_json(path: str, default=None):
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {} if default is None else default


def save_json(path: str, obj) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True)
    os.replace(tmp, path)


def log(prefix: str, msg: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), prefix, msg, flush=True)


# ---------------------------------------------------------------- apps

class App:
    def __init__(self, name: str, url: str, key: str, api: str):
        self.name = name
        self.url = url.rstrip("/")
        self.key = key
        self.api = api           # "/api/v3", "/api/v1", "/api" ...

    def __repr__(self) -> str:
        return f"App({self.name}, {self.url})"


def _read_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    except PermissionError:
        r = subprocess.run(["sudo", "-n", "cat", path], capture_output=True, text=True)
        return r.stdout if r.returncode == 0 else ""
    except OSError:
        return ""


def api_key(app: str) -> str:
    """API key for an app: the .env value, else the app's own config file."""
    val = get(f"{app.upper()}_API_KEY")
    if val:
        return val
    if app in ("radarr", "sonarr", "prowlarr"):
        m = re.search(r"<ApiKey>([^<]+)</ApiKey>", _read_file(os.path.join(CONFIG_DIR, app, "config.xml")))
        return m.group(1).strip() if m else ""
    if app == "bazarr":
        txt = _read_file(os.path.join(CONFIG_DIR, "bazarr", "config", "config.yaml"))
        m = re.search(r"(?m)^\s*apikey:\s*['\"]?([A-Za-z0-9]+)", txt)
        return m.group(1) if m else ""
    if app == "jellyseerr":
        try:
            return json.loads(_read_file(os.path.join(CONFIG_DIR, "jellyseerr", "settings.json")))["main"]["apiKey"]
        except (ValueError, KeyError, TypeError):
            return ""
    return ""


_APPS = {
    "radarr": ("RADARR_URL", "http://localhost:7878", "/api/v3"),
    "sonarr": ("SONARR_URL", "http://localhost:8989", "/api/v3"),
    "prowlarr": ("PROWLARR_URL", "http://localhost:9696", "/api/v1"),
    "bazarr": ("BAZARR_URL", "http://localhost:6767", "/api"),
    "jellyseerr": ("JELLYSEERR_URL", "http://localhost:5055", "/api/v1"),
}


def app(name: str) -> App:
    var, default, api = _APPS[name]
    return App(name, get(var, default), api_key(name), api)


def radarr() -> App:
    return app("radarr")


def sonarr() -> App:
    return app("sonarr")


def prowlarr() -> App:
    return app("prowlarr")


def bazarr() -> App:
    return app("bazarr")


def jellyseerr() -> App:
    return app("jellyseerr")


class ApiError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.status = status
        self.body = body


def api(a: App, endpoint: str, method: str = "GET", data=None, params: dict = None, timeout: int = 90):
    """Call an app API and return the parsed JSON (None for an empty reply)."""
    url = a.url + a.api + "/" + endpoint.lstrip("/")
    if params:
        url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
    body = None
    headers = {"X-Api-Key": a.key, "Accept": "application/json"}
    if data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        raise ApiError(e.code, e.read().decode(errors="replace"), url) from None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


# ---------------------------------------------------------------- qBittorrent

_QBIT_OPENER = None


def qbit_url() -> str:
    return get("QBIT_URL", "http://localhost:8080").rstrip("/")


def qbit(path: str, data: dict = None, timeout: int = 30):
    """Call the qBittorrent WebUI API (v2). Logs in once per process. Returns parsed JSON or text."""
    global _QBIT_OPENER
    base = qbit_url() + "/api/v2/"
    if _QBIT_OPENER is None:
        jar = http.cookiejar.CookieJar()
        opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        creds = urllib.parse.urlencode({"username": get("QBIT_USERNAME", "admin"),
                                        "password": get("QBIT_PASSWORD", "")}).encode()
        try:
            with opener.open(urllib.request.Request(base + "auth/login", data=creds,
                                                    headers={"Referer": qbit_url()}), timeout=timeout) as r:
                body = r.read().decode(errors="replace").strip().lower()
                # older versions answer 200 "Ok." (or "Fails."); 5.x answers 204, and 401 when refused
                if r.status != 204 and not body.startswith("ok"):
                    raise RuntimeError("qBittorrent login failed (check QBIT_USERNAME/QBIT_PASSWORD)")
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                raise RuntimeError("qBittorrent refused the login (HTTP %d): check QBIT_USERNAME and QBIT_PASSWORD"
                                   % e.code) from None
            raise
        _QBIT_OPENER = opener
    body = urllib.parse.urlencode(data).encode() if data is not None else None
    with _QBIT_OPENER.open(urllib.request.Request(base + path.lstrip("/"), data=body,
                                                  headers={"Referer": qbit_url()}), timeout=timeout) as r:
        raw = r.read()
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")


# ---------------------------------------------------------------- docker

_DOCKER = None


def docker_cmd() -> list:
    """['docker'] or ['sudo','-n','docker'], whichever works for this user."""
    global _DOCKER
    if _DOCKER is None:
        for cand in (["docker"], ["sudo", "-n", "docker"]):
            try:
                if subprocess.run(cand + ["version", "--format", "{{.Server.Version}}"],
                                  capture_output=True, text=True, timeout=30).returncode == 0:
                    _DOCKER = cand
                    break
            except (OSError, subprocess.TimeoutExpired):
                continue
        if _DOCKER is None:
            _DOCKER = ["docker"]
    return list(_DOCKER)


def _run(cmd: list, timeout: int, check: bool = False) -> subprocess.CompletedProcess:
    """subprocess.run that reports a missing binary as a failed command instead of raising."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=check)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: command not found")


def docker(*args: str, timeout: int = 120, check: bool = False) -> subprocess.CompletedProcess:
    return _run(docker_cmd() + list(args), timeout, check)


def compose(*args: str, timeout: int = 600) -> subprocess.CompletedProcess:
    return _run(docker_cmd() + ["compose", "--project-directory", STACK_DIR] + list(args), timeout)


def container_running(name: str) -> bool:
    try:
        r = docker("inspect", "-f", "{{.State.Running}}", name, timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return r.returncode == 0 and r.stdout.strip() == "true"


def image_present(image: str) -> bool:
    """True when the image is already on this machine (nothing is ever pulled by the scripts)."""
    try:
        return docker("image", "inspect", image, timeout=30).returncode == 0
    except subprocess.TimeoutExpired:
        return False


FFMPEG_IMAGE = "lscr.io/linuxserver/ffmpeg:latest"


def ffprobe_available() -> bool:
    """Is there any way to probe a media file on this machine?"""
    return bool(shutil.which("ffprobe")) or container_running("bazarr") or image_present(FFMPEG_IMAGE)


def ffprobe(host_path: str, timeout: int = 180) -> tuple:
    """(ok, detail) for a media file: a host ffprobe when present, else the one inside bazarr, else an
    ffmpeg image that is already on this machine. Returns ok=False with a plain reason when none exists."""
    probe_args = ["-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0"]
    if shutil.which("ffprobe"):
        cmd = ["ffprobe"] + probe_args + [host_path]
    elif container_running("bazarr"):
        cmd = docker_cmd() + ["exec", "bazarr", "ffprobe"] + probe_args + [to_container(host_path)]
    elif image_present(FFMPEG_IMAGE):
        # only an image that is already here: the scripts never pull anything on their own
        cmd = docker_cmd() + ["run", "--rm", "--pull", "never", "-v", f"{MEDIA_ROOT}:{CONTAINER_DATA}:ro",
                              "--entrypoint", "ffprobe", FFMPEG_IMAGE] + probe_args + [to_container(host_path)]
    else:
        return False, ("no ffprobe available: install ffmpeg on this machine (recommended), enable the "
                       "subtitles profile, or pull " + FFMPEG_IMAGE + " once by hand")
    try:
        r = _run(cmd, timeout)
    except subprocess.TimeoutExpired:
        return False, "ffprobe timed out"
    if r.returncode != 0:
        return False, (r.stderr.strip().splitlines() or ["ffprobe failed"])[-1][:120]
    try:
        dur = float(r.stdout.strip().rstrip(","))
    except ValueError:
        return False, "no duration readable"
    if dur < 60:
        return False, "suspicious duration %.0fs" % dur
    return True, "%.0fs" % dur


# ---------------------------------------------------------------- telegram

def notify_chat_id() -> str:
    cid = get("NOTIFY_CHAT_ID")
    if cid:
        return cid
    ids = get_list("ALLOWED_TELEGRAM_USER_IDS") or get_list("ALLOWED_CHAT_IDS")
    return ids[0] if ids else ""


def notify(text: str) -> bool:
    """Send a Telegram message to the owner. Never raises; failed sends are spooled by tg.py."""
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import tg  # noqa: E402
        return tg.send(text)
    except Exception as e:  # pragma: no cover - last resort
        log("[settings]", f"notify failed: {e}")
        return False


# ---------------------------------------------------------------- plex

def plex_url() -> str:
    """PLEX_URL as the HOST can reach it. With the plex profile the apps use http://plex:32400,
    a name that only resolves inside the compose network; from the host that is localhost."""
    url = get("PLEX_URL").rstrip("/")
    if not url:
        return ""
    parts = urllib.parse.urlsplit(url)
    if parts.hostname == "plex":
        netloc = "localhost:%d" % (parts.port or 32400)
        return urllib.parse.urlunsplit((parts.scheme or "http", netloc, parts.path, parts.query, "")).rstrip("/")
    return url


# ---------------------------------------------------------------- quality rules

EPISODE_CAP_GB = get_float("EPISODE_CAP_GB", 2.5)
TV_SIZE_CAP_MB_PER_MIN = get_float("TV_SIZE_CAP_MB_PER_MIN", 38)
MOVIE_SIZE_CAP_MB_PER_MIN = get_float("MOVIE_SIZE_CAP_MB_PER_MIN", 25)
ALLOW_UPGRADES = get_bool("ALLOW_UPGRADES", False)
PREFERRED_LANGUAGE = get("PREFERRED_LANGUAGE", "English")


# ---------------------------------------------------------------- languages
# ISO 639-1 code -> the language name as Radarr/Sonarr spell it. Used to match a
# title's original language against MOVIE_LIBRARIES entries.
LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "es": "Spanish", "de": "German", "it": "Italian", "pt": "Portuguese",
    "nl": "Dutch", "sv": "Swedish", "da": "Danish", "no": "Norwegian", "fi": "Finnish", "pl": "Polish",
    "cs": "Czech", "hu": "Hungarian", "ro": "Romanian", "el": "Greek", "tr": "Turkish", "ru": "Russian",
    "uk": "Ukrainian", "ar": "Arabic", "he": "Hebrew", "fa": "Persian", "hi": "Hindi", "pa": "Punjabi",
    "bn": "Bengali", "ta": "Tamil", "te": "Telugu", "ml": "Malayalam", "mr": "Marathi", "ur": "Urdu",
    "th": "Thai", "vi": "Vietnamese", "id": "Indonesian", "ms": "Malay", "tl": "Tagalog", "zh": "Chinese",
    "ja": "Japanese", "ko": "Korean", "bg": "Bulgarian", "hr": "Croatian", "sr": "Serbian", "sk": "Slovak",
    "sl": "Slovenian", "lt": "Lithuanian", "lv": "Latvian", "et": "Estonian", "is": "Icelandic", "ca": "Catalan",
}
LANGUAGE_CODES = {v.lower(): k for k, v in LANGUAGE_NAMES.items()}


def library_for_language(language) -> str:
    """Host movie root for a title's original language (code or name); the default root otherwise."""
    code = language_code(language)
    return movie_libraries().get(code, MOVIES) if code else MOVIES


if __name__ == "__main__":
    # `python3 settings.py` prints the resolved, non-secret settings for troubleshooting.
    print("stack dir  :", STACK_DIR)
    print("media root :", MEDIA_ROOT)
    print("downloads  :", DOWNLOADS)
    print("movies     :", movie_roots())
    print("tv         :", TV)
    print("state/log  :", STATE_DIR, LOG_DIR)
    for name in _APPS:
        a = app(name)
        print(f"{name:<11}: {a.url}  key={'set' if a.key else 'missing'}")
    print("docker     :", " ".join(docker_cmd()))
    print("notify to  :", "set" if notify_chat_id() else "missing")
