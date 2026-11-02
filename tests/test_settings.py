"""scripts/settings.py: .env parsing, paths, app settings and the small shared helpers."""
from __future__ import annotations

import io
import json
import os
import sys
import urllib.error

import pytest
from botlib import langs

# ---------------------------------------------------------------- .env and the environment


def test_env_file_parsing(stack):
    s = stack.write_env_text(
        "# a comment line\n"
        "\n"
        'export TZ="Etc/UTC"\n'
        "SINGLE='single quoted'\n"
        'DOUBLE="double quoted"\n'
        "  SPACED =  spaced value  \n"
        "URL=http://localhost:7878/path?a=b=c\n"
        'HALF="only one quote\n'
        "NOT_A_SETTING\n"
        "EMPTY=\n"
        "MEDIA_ROOT=%s\n" % stack.media)
    assert s.get("TZ") == "Etc/UTC"                      # export prefix and quotes removed
    assert s.get("SINGLE") == "single quoted"
    assert s.get("DOUBLE") == "double quoted"
    assert s.get("SPACED") == "spaced value"
    assert s.get("URL") == "http://localhost:7878/path?a=b=c"   # split on the first '=' only
    assert s.get("HALF") == '"only one quote'             # unbalanced quotes are kept as written
    assert s.get("NOT_A_SETTING", "default") == "default"
    assert s.get("EMPTY", "default") == "default"
    assert s.get("NEVER_SET") == ""


def test_environment_wins_over_the_env_file(stack):
    stack.write_env(PREFERRED_LANGUAGE="French")
    s = stack.setenv("PREFERRED_LANGUAGE", "German")
    assert s.get("PREFERRED_LANGUAGE") == "German"
    assert s.PREFERRED_LANGUAGE == "German"


def test_an_empty_environment_value_falls_back_to_the_env_file(stack):
    stack.write_env(PREFERRED_LANGUAGE="French")
    s = stack.setenv("PREFERRED_LANGUAGE", "")
    assert s.get("PREFERRED_LANGUAGE") == "French"


def test_a_missing_env_file_means_defaults(stack):
    (stack.root / ".env").unlink()
    s = stack.reload()
    assert s.MEDIA_ROOT == "/srv/media"
    assert s.get("TZ", "Etc/UTC") == "Etc/UTC"
    assert s.STACK_DIR == str(stack.root)


def test_inline_comments_are_stripped_like_docker_compose_does(stack):
    s = stack.write_env_text("MEDIA_ROOT=%s  # the big disk\n" % stack.media)
    assert s.MEDIA_ROOT == str(stack.media)


@pytest.mark.parametrize("raw, expected", [
    ("1", True), ("true", True), ("TRUE", True), ("yes", True), (" on ", True),
    ("0", False), ("false", False), ("no", False), ("off", False), ("maybe", False),
])
def test_get_bool(stack, raw, expected):
    s = stack.write_env(SOME_FLAG=raw)
    assert s.get_bool("SOME_FLAG") is expected


def test_get_bool_int_float_list_defaults(stack):
    s = stack.write_env(AN_INT="42", NOT_INT="4.5", A_FLOAT="2.5", BAD_FLOAT="abc", A_LIST=" a, b ,,c ")
    assert s.get_bool("UNSET") is False and s.get_bool("UNSET", True) is True
    assert s.get_int("AN_INT", 1) == 42
    assert s.get_int("NOT_INT", 7) == 7
    assert s.get_int("UNSET", 5) == 5
    assert s.get_float("A_FLOAT", 0.0) == 2.5
    assert s.get_float("BAD_FLOAT", 1.5) == 1.5
    assert s.get_list("A_LIST") == ["a", "b", "c"]
    assert s.get_list("UNSET") == []
    assert s.get_list("UNSET", "x,y") == ["x", "y"]


def test_dry_run_and_profiles(stack, monkeypatch):
    s = stack.reload()
    assert s.dry_run() is False
    monkeypatch.setattr(sys, "argv", ["guard.py", "--dry-run"])
    assert s.dry_run() is True
    monkeypatch.setattr(sys, "argv", ["guard.py"])
    s = stack.write_env(DRY_RUN="true", COMPOSE_PROFILES="plex, subtitles")
    assert s.dry_run() is True
    assert s.profile_enabled("subtitles") and s.profile_enabled("plex")
    assert not s.profile_enabled("monitoring")


# ---------------------------------------------------------------- paths


def test_paths_come_from_the_env_file(stack):
    s = stack.write_env(DOWNLOADS_DIR="/incoming/", MOVIES_DIR="films", TV_DIR="shows")
    media = str(stack.media)
    assert s.MEDIA_ROOT == media
    assert s.DOWNLOADS == os.path.join(media, "incoming")
    assert s.MOVIES == os.path.join(media, "films")
    assert s.TV == os.path.join(media, "shows")
    assert s.CONTAINER_DOWNLOADS == "/data/incoming"
    assert (s.CONTAINER_MOVIES, s.CONTAINER_TV) == ("/data/films", "/data/shows")
    assert s.STATE_DIR == str(stack.root / "state")
    assert s.LOG_DIR == str(stack.root / "logs")
    assert s.BACKUP_DIR == str(stack.root / "backups")
    assert s.CONFIG_DIR == str(stack.root / "config")
    assert s.CONFIRM_DIR == os.path.join(s.STATE_DIR, "confirms")


def test_relative_paths_are_resolved_against_the_stack_folder(stack):
    s = stack.write_env(MEDIA_ROOT="media", STATE_DIR="./var/state")
    assert s.MEDIA_ROOT == str(stack.media)
    assert s.STATE_DIR == str(stack.root / "var" / "state")


@pytest.mark.parametrize("container_path", [
    "/data/downloads/movies/Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv",
    "/data/movies/Big Buck Bunny (2008)/Big Buck Bunny (2008) WEBDL-1080p.mkv",
    "/data/tv/Tears of Steel/Season 01/Tears.of.Steel.2012.S01E03.720p.HDTV.x264-GROUP.mkv",
])
def test_container_and_host_paths_round_trip(stack, container_path):
    s = stack.reload()
    host = s.to_host(container_path)
    assert host.startswith(s.MEDIA_ROOT + os.sep)
    assert s.to_container(host) == container_path


def test_paths_outside_the_media_bind_are_left_alone(stack):
    s = stack.reload()
    assert s.to_host("/config/config.xml") == "/config/config.xml"
    assert s.to_host("/database/file") == "/database/file"        # '/data' must be a whole path part
    assert os.path.normpath(s.to_host("/data")) == s.MEDIA_ROOT
    sibling = s.MEDIA_ROOT + "2/movies/x.mkv"                     # same prefix, different folder
    assert s.to_container(sibling) == sibling
    assert s.to_container(s.MEDIA_ROOT + "/movies/../tv/x.mkv") == "/data/tv/x.mkv"


def test_to_container_keeps_a_trailing_dot(stack):
    s = stack.reload()
    assert s.to_container(s.MEDIA_ROOT + "/movies/Sintel.") == "/data/movies/Sintel."


def test_movie_libraries(stack):
    s = stack.write_env(MOVIE_LIBRARIES="fr=movies-fr, ES=/movies-es/, it=, =orphan, junk")
    media = s.MEDIA_ROOT
    fr, es = os.path.join(media, "movies-fr"), os.path.join(media, "movies-es")
    assert s.movie_libraries() == {"fr": fr, "es": es}
    assert s.movie_roots() == [s.MOVIES, fr, es]
    assert s.library_roots() == [s.MOVIES, fr, es, s.TV]
    assert s.DOWNLOADS not in s.library_roots()


def test_movie_libraries_are_off_by_default(stack):
    s = stack.reload()
    assert s.movie_libraries() == {}
    assert s.movie_roots() == [s.MOVIES]
    assert s.library_roots() == [s.MOVIES, s.TV]


def test_movie_libraries_never_leave_the_media_root(stack):
    s = stack.write_env(MOVIE_LIBRARIES="de=../outside")
    assert "de" not in s.movie_libraries()


@pytest.mark.parametrize("language", ["fr", "FR", "French", " french "])
def test_library_for_language_picks_the_language_library(stack, language):
    s = stack.write_env(MOVIE_LIBRARIES="fr=movies-fr")
    assert s.library_for_language(language) == os.path.join(s.MEDIA_ROOT, "movies-fr")


@pytest.mark.parametrize("language", [None, "", "en", "English", "Klingon", "xx"])
def test_library_for_language_falls_back_to_the_default_root(stack, language):
    s = stack.write_env(MOVIE_LIBRARIES="fr=movies-fr")
    assert s.library_for_language(language) == s.MOVIES


def test_language_tables_match_the_bot(stack):
    s = stack.reload()
    assert s.LANGUAGE_NAMES == langs.LANGUAGES
    assert s.LANGUAGE_CODES["french"] == "fr"


# ---------------------------------------------------------------- apps and keys


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_api_keys_are_read_from_each_apps_config(stack):
    s = stack.reload()
    cfg = stack.root / "config"
    _write(cfg / "radarr" / "config.xml", "<Config>\n  <ApiKey>radarr-test-key</ApiKey>\n</Config>\n")
    _write(cfg / "prowlarr" / "config.xml", "<Config><ApiKey> prowlarr-test-key </ApiKey></Config>")
    _write(cfg / "bazarr" / "config" / "config.yaml", "auth:\n  apikey: 'bazarrtestkey'\n  type: null\n")
    _write(cfg / "jellyseerr" / "settings.json", json.dumps({"main": {"apiKey": "seerr-test-key"}}))
    assert s.api_key("radarr") == "radarr-test-key"
    assert s.api_key("prowlarr") == "prowlarr-test-key"
    assert s.api_key("sonarr") == ""                     # no config file yet
    assert s.api_key("bazarr") == "bazarrtestkey"
    assert s.api_key("jellyseerr") == "seerr-test-key"
    assert s.api_key("plex") == ""
    assert s.radarr().key == "radarr-test-key"


def test_an_api_key_in_the_environment_wins(stack):
    _write(stack.root / "config" / "radarr" / "config.xml", "<Config><ApiKey>from-file</ApiKey></Config>")
    s = stack.setenv("RADARR_API_KEY", "from-env")
    assert s.api_key("radarr") == "from-env"


def test_a_broken_jellyseerr_settings_file_means_no_key(stack):
    _write(stack.root / "config" / "jellyseerr" / "settings.json", "{not json")
    s = stack.reload()
    assert s.api_key("jellyseerr") == ""


def test_app_urls_and_api_roots(stack):
    s = stack.write_env(RADARR_URL="http://localhost:17878/")
    radarr = s.radarr()
    assert (radarr.name, radarr.url, radarr.api) == ("radarr", "http://localhost:17878", "/api/v3")
    assert s.sonarr().url == "http://localhost:8989"
    assert (s.prowlarr().api, s.jellyseerr().api, s.bazarr().api) == ("/api/v1", "/api/v1", "/api")
    assert s.qbit_url() == "http://localhost:8080"


class _Reply:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_api_builds_the_request_and_parses_the_reply(stack, monkeypatch):
    s = stack.reload()
    seen = []
    replies = [b'[{"id": 1}]', b"", b"plain text", b'{"ok": true}']

    def fake_urlopen(req, timeout):
        seen.append((req, timeout))
        return _Reply(replies.pop(0))

    monkeypatch.setattr(s.urllib.request, "urlopen", fake_urlopen)
    app = s.App("radarr", "http://localhost:7878/", "test-key", "/api/v3")
    assert s.api(app, "/movie", params={"page": 2, "tags": [1, 2]}) == [{"id": 1}]
    req, timeout = seen[-1]
    assert req.full_url == "http://localhost:7878/api/v3/movie?page=2&tags=1&tags=2"
    assert req.get_method() == "GET" and timeout == 90
    assert req.get_header("X-api-key") == "test-key"
    assert s.api(app, "command", "POST", data={"name": "RescanMovie"}, timeout=5) is None   # empty reply
    req, timeout = seen[-1]
    assert req.get_method() == "POST" and timeout == 5
    assert json.loads(req.data) == {"name": "RescanMovie"}
    assert req.get_header("Content-type") == "application/json"
    assert s.api(app, "system/status") == "plain text"
    assert s.api(app, "movie?x=1", params={"y": 2}) == {"ok": True}
    assert seen[-1][0].full_url.endswith("/api/v3/movie?x=1&y=2")


def test_api_turns_http_errors_into_api_error(stack, monkeypatch):
    s = stack.reload()

    def failing(req, timeout):
        raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, io.BytesIO(b'{"message": "nope"}'))

    monkeypatch.setattr(s.urllib.request, "urlopen", failing)
    with pytest.raises(s.ApiError) as err:
        s.api(s.App("radarr", "http://localhost:7878", "", "/api/v3"), "movie")
    assert err.value.status == 401
    assert "nope" in err.value.body


# ---------------------------------------------------------------- Telegram, Plex, quality settings


def test_notify_chat_id(stack):
    s = stack.reload()
    assert s.notify_chat_id() == ""
    s = stack.write_env(ALLOWED_CHAT_IDS="7, 8")
    assert s.notify_chat_id() == "7"
    s = stack.write_env(ALLOWED_TELEGRAM_USER_IDS="111, 222")
    assert s.notify_chat_id() == "111"
    s = stack.write_env(NOTIFY_CHAT_ID="333")
    assert s.notify_chat_id() == "333"


@pytest.mark.parametrize("url, expected", [
    ("", ""),
    ("http://plex:32400", "http://localhost:32400"),
    ("http://plex", "http://localhost:32400"),
    ("http://plex:32401/", "http://localhost:32401"),
    ("https://media.example.com:32400/", "https://media.example.com:32400"),
])
def test_plex_url_as_the_host_sees_it(stack, url, expected):
    s = stack.write_env(PLEX_URL=url)
    assert s.plex_url() == expected


def test_quality_settings(stack):
    s = stack.reload()
    assert (s.EPISODE_CAP_GB, s.TV_SIZE_CAP_MB_PER_MIN, s.MOVIE_SIZE_CAP_MB_PER_MIN) == (2.5, 38, 25)
    assert s.ALLOW_UPGRADES is False
    assert s.PREFERRED_LANGUAGE == "English"
    s = stack.write_env(EPISODE_CAP_GB="3", ALLOW_UPGRADES="true", PREFERRED_LANGUAGE="French")
    assert s.EPISODE_CAP_GB == 3.0
    assert s.ALLOW_UPGRADES is True
    assert s.PREFERRED_LANGUAGE == "French"


# ---------------------------------------------------------------- state and helpers


def test_state_helpers(stack):
    s = stack.reload()
    assert not os.path.exists(s.STATE_DIR)              # nothing is created on import
    path = s.state_path(".test_state.json")
    assert os.path.isdir(s.STATE_DIR) and os.path.isdir(s.CONFIRM_DIR)
    assert s.load_json(path, {"default": 1}) == {"default": 1}
    s.save_json(path, {"b": 2, "a": 1})
    assert s.load_json(path) == {"a": 1, "b": 2}
    assert not os.path.exists(path + ".tmp")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{broken")
    assert s.load_json(path) == {}


def test_heartbeat(stack):
    s = stack.reload()
    assert s.heartbeat_age("never_ran") >= 1e11
    s.heartbeat("some_job")
    assert 0 <= s.heartbeat_age("some_job") < 60


def test_a_missing_program_is_a_failed_command_not_an_exception(stack):
    s = stack.reload()
    result = s._run(["media-stack-test-no-such-program"], timeout=5)
    assert result.returncode == 127
    assert "not found" in result.stderr
