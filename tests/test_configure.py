"""configure.py: what it would send to the apps, checked against fakes of their APIs.

FakeApi stands in for configure.Api (answers reads from a dict, records every write), so the
payload builders run exactly as against a real Radarr or Sonarr, but nothing leaves the process.
"""
from __future__ import annotations

import copy
import json
import os
import re
from pathlib import Path

import malware_guard
import pytest

REPO = Path(__file__).resolve().parent.parent
TEMPLATE = (REPO / "scripts" / "cron.template").read_text(encoding="utf-8")
MASK = "********"


class FakeApi:
    """configure.Api look-alike: reads come from `data`, writes and saves are recorded."""

    def __init__(self, name: str, data: dict):
        self.name = name
        self.data = data
        self.writes = []
        self.saved = []

    def get(self, endpoint, params=None, timeout=60):
        return copy.deepcopy(self.data.get(endpoint))

    def write(self, method, endpoint, data=None, params=None):
        self.writes.append((method, endpoint, copy.deepcopy(data)))
        if method == "PUT" and endpoint.startswith("qualitydefinition/"):   # the app keeps the new sizes
            for d in self.data["qualitydefinition"]:
                if d["id"] == int(endpoint.rsplit("/", 1)[1]):
                    d.update(data)
        return data

    def save(self, endpoint, obj, existing):
        self.saved.append((endpoint, copy.deepcopy(obj)))
        return "updated" if existing else "created"


def fields(obj: dict) -> dict:
    return {f["name"]: f.get("value") for f in obj.get("fields", [])}


@pytest.fixture(autouse=True)
def not_dry(configure, monkeypatch):
    monkeypatch.setattr(configure, "DRY", False)
    monkeypatch.setattr(configure, "_ARR_ENV_DONE", False)


# ---------------------------------------------------------------- cron block


def test_render_cron_default_jobs(configure, stack):
    stack.reload()
    block, jobs = configure.render_cron(TEMPLATE, "/opt/media-stack")
    lines = block.splitlines()
    assert lines[0] == configure.MARK_BEGIN and lines[-1] == configure.MARK_END
    assert jobs == 16                                     # subtitles needs its profile; the space-saver is off
    assert "{{STACK}}" not in block and "##" not in block
    assert "subtitle_sweep" not in block
    assert '/bin/bash "/opt/media-stack/scripts/run_guard.sh" regrab_guard' in block
    shrink = [x for x in lines if "shrink_oversized" in x or "resolve_shrink_imports" in x]
    assert len(shrink) == 2 and all(x.startswith("#SHRINK ") for x in shrink)


def test_render_cron_with_the_subtitles_profile_and_the_space_saver(configure, stack):
    stack.write_env(COMPOSE_PROFILES="subtitles", SHRINK_ENABLED="true")
    block, jobs = configure.render_cron(TEMPLATE, "/opt/media-stack")
    assert jobs == 19
    assert '10 8 * * * /bin/bash "/opt/media-stack/scripts/run_guard.sh" subtitle_sweep' in block.splitlines()
    assert "#SHRINK" not in block and "#PROFILE" not in block


EXPECTED_SCHEDULES = {
    "regrab_guard": "*/10 * * * *", "link_orphans": "25 */4 * * *", "vpn_watchdog": "*/5 * * * *",
    "verify_imports": "7,37 * * * *", "missing_sweep": "15 5 * * *", "library_guard": "40 9 * * *",
    "config_backup": "30 3 * * *", "integrity_scan": "15 7 * * *", "subtitle_sweep": "10 8 * * *",
    "update_check": "0 9 * * 1", "restore_drill": "0 11 * * 0", "dead_torrent_reaper": "*/30 * * * *",
    "malware_guard": "*/10 * * * *", "mount_guard": "*/2 * * * *", "arr_health_guard": "*/10 * * * *",
    "size_cap_watch": "*/10 * * * *", "naming_guard": "15 10 * * 0", "shrink_oversized": "0 6 * * *",
    "resolve_shrink_imports": "25 * * * *",
}


def test_cron_schedules_and_scripts(configure, stack):
    stack.write_env(COMPOSE_PROFILES="subtitles", SHRINK_ENABLED="true")
    block, _ = configure.render_cron(TEMPLATE, "/opt/media-stack")
    found = {}
    for line in block.splitlines():
        if line.startswith("#"):
            continue
        m = re.fullmatch(r'(\S+ \S+ \S+ \S+ \S+) /bin/bash "/opt/media-stack/scripts/run_guard\.sh" (\w+)', line)
        assert m, "not a cron line: %r" % line
        found[m.group(2)] = m.group(1)
    assert found == EXPECTED_SCHEDULES
    for job in found:
        assert (REPO / "scripts" / (job + ".py")).is_file()


def test_render_cron_quotes_the_stack_folder_safely(configure, stack):
    stack.reload()
    block, _ = configure.render_cron(TEMPLATE, "/srv/my media 100%")
    assert '"/srv/my media 100\\%/scripts/run_guard.sh"' in block          # % ends a crontab command
    for bad in ('/srv/my"stack', "/srv/it's", "/srv/a\nb"):
        with pytest.raises(configure.StepFailed):
            configure.render_cron(TEMPLATE, bad)


USER_CRONTAB = 'MAILTO=""\n0 1 * * * /usr/local/bin/my-backup\n'


def test_replace_block_adds_replaces_and_removes_only_its_own_lines(configure):
    block = "%s\n*/10 * * * * new-job\n%s\n" % (configure.MARK_BEGIN, configure.MARK_END)
    installed = configure.replace_block(USER_CRONTAB, block)
    assert installed == USER_CRONTAB + block
    assert configure.replace_block(installed, block) == installed                     # idempotent
    old = USER_CRONTAB + "%s\n*/5 * * * * old-job\n%s\n15 2 * * * after-block\n" % (configure.MARK_BEGIN,
                                                                                  configure.MARK_END)
    replaced = configure.replace_block(old, block)
    assert "old-job" not in replaced and "new-job" in replaced
    assert replaced.endswith("15 2 * * * after-block\n") and replaced.startswith(USER_CRONTAB)
    assert configure.replace_block(installed, "") == USER_CRONTAB                    # GUARDS_ENABLED=false
    assert configure.replace_block("", block) == block
    assert configure.replace_block("", "") == ""


@pytest.mark.parametrize("broken", [
    "# >>> media-stack >>>\n* * * * * x\n",
    "# <<< media-stack <<<\n# >>> media-stack >>>\n",
])
def test_replace_block_refuses_unmatched_markers(configure, broken):
    with pytest.raises(configure.StepFailed):
        configure.replace_block(broken, "")


def test_every_kuma_push_setting_names_a_cron_job():
    jobs = set(EXPECTED_SCHEDULES)
    pushes = {line.split("=", 1)[0][len("KUMA_PUSH_"):].lower()
              for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines()
              if line.startswith("KUMA_PUSH_")}
    assert pushes and pushes <= jobs


# ---------------------------------------------------------------- quality sizes


def _arr(configure, name, data=None):
    return configure.Arr(FakeApi(name, data or {}))


def test_quality_size_targets_follow_the_cap_settings(configure, stack):
    stack.reload()
    radarr, sonarr = _arr(configure, "radarr"), _arr(configure, "sonarr")
    assert radarr._sizes("Bluray-1080p") == (1, 22, 25)
    assert radarr._sizes("WEBDL-2160p") == (1, 22, 25)
    assert radarr._sizes("HDTV-720p") == (1, 22, 25)
    assert radarr._sizes("SDTV") is None and radarr._sizes("BR-DISK") is None      # left as the app has them
    assert sonarr._sizes("WEBDL-1080p") == (4, 35, 38)
    assert sonarr._sizes("HDTV-720p") is None and sonarr._sizes("Bluray-1080p Remux") is None
    stack.write_env(MOVIE_SIZE_CAP_MB_PER_MIN="10", TV_SIZE_CAP_MB_PER_MIN="4")
    assert radarr._sizes("Bluray-1080p") == (1, 7, 10)                              # preferred stays under the cap
    with pytest.raises(configure.StepFailed):
        sonarr._sizes("WEBDL-1080p")                                                 # a cap below the minimum


def test_num(configure):
    assert configure.num(25.0) == 25 and isinstance(configure.num(25.0), int)
    assert configure.num(2.345) == 2.35


def _definitions():
    return [{"id": 1, "title": "SDTV", "minSize": 2, "preferredSize": 95, "maxSize": 100},
            {"id": 2, "title": "Bluray-1080p", "minSize": 2, "preferredSize": 95, "maxSize": 100},
            {"id": 3, "title": "WEBDL-2160p", "minSize": 1, "preferredSize": 22, "maxSize": 25}]


def test_quality_definitions_are_put_one_by_one_and_verified(configure, stack, capsys):
    stack.reload()
    api = FakeApi("radarr", {"qualitydefinition": _definitions()})
    configure.Arr(api).quality_definitions()
    assert api.writes == [("PUT", "qualitydefinition/2", dict(_definitions()[1], minSize=1, preferredSize=22,
                                                              maxSize=25))]
    assert "updated 1, verified (2 at 1/22/25 MB/min)" in capsys.readouterr().out


def test_quality_definitions_fail_when_the_app_does_not_keep_them(configure, stack):
    stack.reload()

    class Forgetful(FakeApi):
        def write(self, method, endpoint, data=None, params=None):
            self.writes.append((method, endpoint, data))

    with pytest.raises(configure.StepFailed, match="did not keep the new sizes for: Bluray-1080p"):
        configure.Arr(Forgetful("radarr", {"qualitydefinition": _definitions()})).quality_definitions()
    with pytest.raises(configure.StepFailed, match="no matching quality definitions"):
        configure.Arr(FakeApi("sonarr", {"qualitydefinition": _definitions()[:1]})).quality_definitions()


# ---------------------------------------------------------------- quality profile and cutoff


def _item(qid, name):
    return {"quality": {"id": qid, "name": name}, "items": [], "allowed": False}


PROFILE_SCHEMA = {
    "name": "", "upgradeAllowed": False, "cutoff": 0, "minFormatScore": 0, "cutoffFormatScore": 0,
    "items": [_item(1, "SDTV"), _item(4, "HDTV-720p"), _item(6, "Bluray-720p"), _item(9, "HDTV-1080p"),
              {"id": 1001, "name": "WEB 1080p", "allowed": False,
               "items": [_item(3, "WEBDL-1080p"), _item(15, "WEBRip-1080p")]},
              _item(7, "Bluray-1080p"), _item(30, "Remux-1080p"), _item(19, "Bluray-2160p")],
    "formatItems": [{"format": 11, "name": "LQ", "score": 0}, {"format": 15, "name": "Other Format", "score": 50}],
    "language": {"id": 1, "name": "English"},
}
FORMATS = [{"id": 11, "name": "LQ"}, {"id": 12, "name": "LQ (Release Title)"},
           {"id": 13, "name": "Multi/Dual audio tag"}, {"id": 14, "name": "Language: Not English or Original"},
           {"id": 15, "name": "Other Format"}]
LANGUAGES = [{"id": -2, "name": "Original"}, {"id": 0, "name": "Unknown"}, {"id": 1, "name": "English"},
             {"id": 2, "name": "French"}]


def _profile_api(name, existing=None):
    return FakeApi(name, {"qualityprofile/schema": PROFILE_SCHEMA, "customformat": FORMATS,
                          "qualityprofile": existing or [], "language": LANGUAGES})


def _allowed(profile) -> set:
    names = set()
    for item in profile["items"]:
        if item["allowed"]:
            names |= {s["quality"]["name"] for s in item["items"]} or {item["quality"]["name"]}
    return names


@pytest.mark.parametrize("app, upgrades, cutoff", [
    ("radarr", "false", 4), ("radarr", "true", 30), ("sonarr", "false", 9), ("sonarr", "true", 7),
])
def test_quality_profile_cutoff(configure, stack, app, upgrades, cutoff):
    stack.write_env(ALLOW_UPGRADES=upgrades)
    api = _profile_api(app)
    configure.Arr(api).quality_profile()
    (method, endpoint, profile), = api.writes
    assert (method, endpoint) == ("POST", "qualityprofile")
    assert profile["name"] == "HD-1080p"
    assert profile["cutoff"] == cutoff                     # upgrades off: the lowest allowed quality already meets it
    assert profile["upgradeAllowed"] is (upgrades == "true")
    wanted = configure.RADARR_ALLOWED if app == "radarr" else configure.SONARR_ALLOWED
    assert _allowed(profile) == wanted


def test_quality_profile_blocks_the_unwanted_formats(configure, stack):
    stack.reload()
    api = _profile_api("radarr")
    configure.Arr(api).quality_profile()
    profile = api.writes[0][2]
    scores = {fi["format"]: fi["score"] for fi in profile["formatItems"]}
    assert scores == {11: -10000, 12: -10000, 13: -10000, 14: -10000, 15: 0}
    assert profile["language"]["id"] == -2                 # Radarr: the film's original language
    assert (profile["minFormatScore"], profile["cutoffFormatScore"]) == (0, 0)


def test_quality_profile_multi_audio_can_be_allowed(configure, stack):
    stack.write_env(BLOCK_MULTI_AUDIO="false")
    api = _profile_api("sonarr")
    configure.Arr(api).quality_profile()
    scores = {fi["format"]: fi["score"] for fi in api.writes[0][2]["formatItems"]}
    assert scores.get(13, 0) == 0 and scores[11] == -10000      # not blocked: no score (the same as 0)
    assert api.writes[0][2]["language"] == PROFILE_SCHEMA["language"]   # Sonarr profiles keep the schema's


def test_an_unchanged_quality_profile_is_left_alone(configure, stack):
    stack.reload()
    first = _profile_api("radarr")
    configure.Arr(first).quality_profile()
    existing = dict(first.writes[0][2], id=99)
    second = _profile_api("radarr", existing=[existing])
    configure.Arr(second).quality_profile()
    assert second.writes == []
    stack.write_env(ALLOW_UPGRADES="true")
    third = _profile_api("radarr", existing=[existing])
    configure.Arr(third).quality_profile()
    assert [(m, e) for m, e, _ in third.writes] == [("PUT", "qualityprofile/99")]


# ---------------------------------------------------------------- custom formats


CF_SCHEMA = [{"implementation": "LanguageSpecification", "name": "", "negate": False, "required": False,
              "fields": [{"name": "value", "value": 0}, {"name": "exceptLanguage", "value": False}]},
             {"implementation": "ReleaseTitleSpecification", "fields": [{"name": "value", "value": ""}]},
             {"implementation": "ReleaseGroupSpecification", "fields": [{"name": "value", "value": ""}]}]


def test_wanted_formats(configure, stack):
    stack.reload()
    api = FakeApi("radarr", {"customformat/schema": CF_SCHEMA, "language": LANGUAGES})
    formats = dict(configure.Arr(api)._wanted_formats())
    assert list(formats) == ["LQ", "LQ (Release Title)", "Multi/Dual audio tag", "Language: Not English or Original"]
    language = formats["Language: Not English or Original"]
    assert [(fields(s)["value"], fields(s)["exceptLanguage"], s["required"]) for s in language] == \
        [(1, True, True), (-2, True, True), (0, True, True)]
    assert all(s["implementation"] == "ReleaseGroupSpecification" for s in formats["LQ"])


def test_wanted_formats_follow_the_preferred_language(configure, stack):
    stack.write_env(PREFERRED_LANGUAGE="fr")
    assert configure.language_format_name() == "Language: Not French or Original"
    formats = dict(configure.Arr(FakeApi("sonarr", {"language": LANGUAGES}))._wanted_formats())
    first = formats["Language: Not French or Original"][0]
    assert fields(first)["value"] == 2 and first["name"] == "Has a language other than French"
    stack.write_env(PREFERRED_LANGUAGE="Klingon")
    with pytest.raises(configure.StepFailed):
        configure.Arr(FakeApi("sonarr", {"language": LANGUAGES}))._wanted_formats()


@pytest.mark.parametrize("title, blocked", [
    ("Sintel.2010.MULTi.1080p.WEB-DL.x264-GROUP", True),
    ("Sintel.2010.DUAL.1080p.WEB-DL.x264-GROUP", True),
    ("Sintel.2010.Multi.Subs.1080p.WEB-DL.x264-GROUP", False),      # several subtitle languages are fine
    ("Sintel.2010.1080p.WEB-DL.x264-GROUP", False),
])
def test_multi_audio_regex(configure, title, blocked):
    assert bool(re.search(configure.MULTI_REGEX, title, re.I)) is blocked


# ---------------------------------------------------------------- download client and notifications

DC_SCHEMA = [{"implementation": "QBittorrent", "name": "", "enable": False, "protocol": "torrent", "priority": 1,
              "presets": [], "id": 0, "fields": [{"name": n, "value": ""} for n in
                                                 ("host", "port", "useSsl", "urlBase", "username", "password",
                                                  "movieCategory", "tvCategory")]}]


def test_download_client_payload(configure, stack):
    stack.write_env(QBIT_USERNAME="admin", QBIT_PASSWORD="test-password")
    api = FakeApi("radarr", {"downloadclient": [], "downloadclient/schema": DC_SCHEMA})
    configure.Arr(api).download_client()
    (endpoint, obj), = api.saved
    assert endpoint == "downloadclient"
    assert obj["name"] == "qBittorrent" and obj["enable"] is True and "presets" not in obj and "id" not in obj
    assert obj["removeCompletedDownloads"] and obj["removeFailedDownloads"]
    f = fields(obj)
    assert (f["host"], f["port"], f["useSsl"], f["username"], f["password"]) == ("gluetun", 8080, False, "admin",
                                                                                   "test-password")
    assert f["movieCategory"] == "radarr"
    assert api.writes[-1][:2] == ("POST", "downloadclient/test")
    sonarr = FakeApi("sonarr", {"downloadclient": [], "downloadclient/schema": DC_SCHEMA})
    configure.Arr(sonarr).download_client()
    assert fields(sonarr.saved[0][1])["tvCategory"] == "sonarr"


def test_download_client_needs_the_qbittorrent_password(configure, stack):
    stack.reload()
    with pytest.raises(configure.StepFailed, match="QBIT_PASSWORD"):
        configure.Arr(FakeApi("radarr", {"downloadclient/schema": DC_SCHEMA})).download_client()
    stack.write_env(QBIT_PASSWORD="test-password")
    old_schema = copy.deepcopy(DC_SCHEMA)
    old_schema[0]["fields"] = [f for f in old_schema[0]["fields"] if f["name"] != "movieCategory"]
    with pytest.raises(configure.StepFailed, match="missing: movieCategory"):
        configure.Arr(FakeApi("radarr", {"downloadclient/schema": old_schema})).download_client()


def _notification_schema():
    events = {"onGrab": False, "onDownload": False, "onUpgrade": False, "onRename": False, "onMovieAdded": False,
              "onHealthIssue": False}

    def template(impl, names, **extra):
        return dict(events, implementation=impl, name="", fields=[{"name": n, "value": ""} for n in names], **extra)

    return [template("Webhook", ("url", "method")), template("CustomScript", ("path",)),
            template("PlexServer", ("host", "port", "useSsl", "authToken", "updateLibrary")),
            template("Telegram", ("botToken", "chatId"), includeHealthWarnings=True)]


def _notify_api(name):
    return FakeApi(name, {"notification": [], "notification/schema": _notification_schema()})


def _events(obj) -> set:
    return {k for k, v in obj.items() if k.startswith("on") and v is True}


def test_webhook_notification_payload(configure, stack):
    stack.write_env(WEBHOOK_SECRET="test-secret-path")
    radarr = _notify_api("radarr")
    configure.Arr(radarr).notify_webhook()
    obj = radarr.saved[0][1]
    assert obj["name"] == "moviebot"
    assert fields(obj) == {"url": "http://moviebot:8090/radarr/test-secret-path", "method": 1}
    assert _events(obj) == {"onGrab", "onDownload", "onUpgrade", "onMovieAdded"}
    sonarr = _notify_api("sonarr")
    configure.Arr(sonarr).notify_webhook()
    assert fields(sonarr.saved[0][1])["url"] == "http://moviebot:8090/sonarr/test-secret-path"
    assert _events(sonarr.saved[0][1]) == {"onGrab", "onDownload", "onUpgrade"}


def test_webhook_needs_its_secret(configure, stack):
    stack.reload()
    with pytest.raises(configure.StepFailed, match="WEBHOOK_SECRET"):
        configure.Arr(_notify_api("radarr")).notify_webhook()


def test_ongrab_plex_and_telegram_notifications(configure, stack):
    stack.reload()
    api = _notify_api("sonarr")
    arr = configure.Arr(api)
    arr.notify_ongrab()
    arr.notify_plex()                                   # no PLEX_URL/PLEX_TOKEN: skipped
    arr.notify_telegram()                               # no bot token: skipped
    assert [obj["name"] for _, obj in api.saved] == ["On-grab guard"]
    assert fields(api.saved[0][1]) == {"path": "/scripts/ongrab_guard.sh"}
    assert _events(api.saved[0][1]) == {"onGrab"}

    stack.write_env(PLEX_URL="http://plex:32400", PLEX_TOKEN="test-plex-token", TELEGRAM_BOT_TOKEN="test-bot-token",
                    ALLOWED_TELEGRAM_USER_IDS="111")
    api = _notify_api("radarr")
    arr = configure.Arr(api)
    arr.notify_plex()
    arr.notify_telegram()
    plex, telegram = (obj for _, obj in api.saved)
    assert fields(plex) == {"host": "plex", "port": 32400, "useSsl": False, "authToken": "test-plex-token",
                            "updateLibrary": True}
    assert _events(plex) == {"onDownload", "onUpgrade", "onRename"}
    assert fields(telegram) == {"botToken": "test-bot-token", "chatId": "111"}
    assert _events(telegram) == {"onHealthIssue"} and telegram["includeHealthWarnings"] is False


def test_release_profile(configure, stack):
    stack.reload()
    api = FakeApi("sonarr", {"releaseprofile": []})
    configure.Arr(api).release_profile()
    (method, endpoint, body), = api.writes
    assert (method, endpoint) == ("POST", "releaseprofile")
    assert body["name"] == "Block MULTI dubs" and body["enabled"] is True and body["ignored"] == [r"/\bMULTi\b/i"]
    same = FakeApi("sonarr", {"releaseprofile": [dict(body, id=3)]})
    configure.Arr(same).release_profile()
    assert same.writes == []
    stack.write_env(BLOCK_MULTI_AUDIO="false")
    off = FakeApi("sonarr", {"releaseprofile": [dict(body, id=3)]})
    configure.Arr(off).release_profile()
    assert off.writes[0][:2] == ("PUT", "releaseprofile/3") and off.writes[0][2]["enabled"] is False
    none = FakeApi("sonarr", {"releaseprofile": []})
    configure.Arr(none).release_profile()
    assert none.writes == []                            # switched off and never created: nothing to do


def test_root_folders(configure, stack):
    stack.write_env(MOVIE_LIBRARIES="fr=movies-fr")
    api = FakeApi("radarr", {"rootfolder": [{"path": "/data/movies/"}]})
    configure.Arr(api).root_folders()
    assert api.writes == [("POST", "rootfolder", {"path": "/data/movies-fr"})]
    sonarr = FakeApi("sonarr", {"rootfolder": []})
    configure.Arr(sonarr).root_folders()
    assert sonarr.writes == [("POST", "rootfolder", {"path": "/data/tv"})]


def test_arr_env_for_the_ongrab_guard(configure, stack):
    stack.write_env(MOVIES_DIR="films", ONGRAB_DRYRUN="true", QBIT_PASSWORD="test-password")
    configure.write_arr_env()
    path = stack.root / "scripts" / "arr" / "arr.env"
    text = path.read_text(encoding="utf-8")
    assert "MOVIES_DIR=films\nTV_DIR=tv\nDOWNLOADS_DIR=downloads\nONGRAB_DRYRUN=1\n" in text
    assert "test-password" not in text                  # read inside the containers: no secrets in it
    assert oct(os.stat(str(path)).st_mode & 0o777) == "0o644"


# ---------------------------------------------------------------- saving provider objects


def test_api_save_creates_updates_and_survives_a_failed_test(configure, stack, monkeypatch):
    s = stack.write_env(RADARR_API_KEY="test-key")
    calls, replies = [], {}

    def fake_api(app, endpoint, method="GET", data=None, params=None, timeout=90):
        calls.append((method, endpoint, params))
        reply = replies.get((method, endpoint), [None]).pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(s, "api", fake_api)
    api = configure.Api("radarr")
    hook = {"name": "moviebot", "onGrab": True, "fields": [{"name": "url", "value": "http://moviebot:8090/x"}]}

    assert api.save("notification", copy.deepcopy(hook), None) == "created"
    assert calls == [("POST", "notification", None)]

    calls.clear()
    replies[("POST", "notification")] = [s.ApiError(400, '[{"errorMessage": "Unable to send test message"}]', "u"),
                                         {"id": 7}]
    result = api.save("notification", copy.deepcopy(hook), None)
    assert result == "created (its test failed: Unable to send test message)"
    assert calls == [("POST", "notification", None), ("POST", "notification", {"forceSave": "true"}),
                     ("PUT", "notification/7", {"forceSave": "true"})]

    calls.clear()
    existing = dict(copy.deepcopy(hook), id=3)
    assert api.save("notification", copy.deepcopy(hook), existing) == "ok"            # re-applied, nothing changed
    assert calls == [("PUT", "notification/3", {"forceSave": "true"})]
    changed = copy.deepcopy(hook)
    changed["fields"][0]["value"] = "http://moviebot:8090/y"
    assert api.save("notification", changed, existing) == "updated"

    calls.clear()
    monkeypatch.setattr(configure, "DRY", True)
    assert api.save("notification", copy.deepcopy(hook), None) == "would create"
    assert api.save("notification", changed, existing) == "would update"
    assert api.save("notification", copy.deepcopy(hook), existing) == "ok"
    assert calls == []                                                                 # a dry run sends nothing


def test_changed_ignores_masked_secrets_and_ids(configure):
    existing = {"id": 3, "name": "Health alerts", "onHealthIssue": True, "implementationName": "Telegram",
                "supportsOnGrab": True,
                "fields": [{"name": "botToken", "value": MASK}, {"name": "chatId", "value": "42"}]}
    desired = copy.deepcopy(existing)
    desired["fields"][0]["value"] = "a-real-token"
    desired["id"] = 4
    assert not configure.changed(existing, desired)
    desired["fields"][1]["value"] = "43"
    assert configure.changed(existing, desired)
    assert configure.changed(existing, dict(existing, onHealthIssue=False))


def test_provider_helpers(configure):
    schema = [{"implementation": "Webhook", "id": 5, "presets": [1], "fields": [{"name": "url", "value": ""}]}]
    obj = configure.template(schema, "Webhook")
    assert "id" not in obj and "presets" not in obj
    obj["fields"][0]["value"] = "changed"
    assert schema[0]["fields"][0]["value"] == ""                                        # a copy, not the schema
    with pytest.raises(configure.StepFailed):
        configure.template(schema, "Email")
    assert configure.set_fields(obj, {"url": "u", "nope": 1}) == ["nope"]
    assert configure.by_name([{"name": "QBittorrent"}, "junk"], "qbittorrent") == {"name": "QBittorrent"}
    assert configure.by_name(None, "x") is None
    off = configure.disabled_copy({"enable": True, "onGrab": True, "onlyName": "keep", "fields": []})
    assert off == {"enable": False, "onGrab": False, "onlyName": "keep", "fields": []}


def test_error_texts(configure, stack):
    s = stack.reload()
    assert configure.api_error_text('[{"errorMessage": "a"}, {"errorMessage": "a"}, {"message": "b"}]') == "a; b"
    assert configure.api_error_text('{"message": "m"}') == "m"
    assert configure.api_error_text("plain words") == "plain words"
    assert configure.api_error_text("") == "no details"
    assert configure.describe(s.ApiError(401, "", "u")) == "HTTP 401: the API key was rejected"
    assert configure.describe(s.ApiError(400, '{"message": "bad path"}', "u")) == "HTTP 400: bad path"
    assert configure.describe(TimeoutError()) == "timed out"
    assert configure.describe(configure.StepFailed("plain reason")) == "plain reason"


# ---------------------------------------------------------------- qBittorrent


def test_qbittorrent_preferences(configure, stack):
    stack.reload()
    prefs = configure.qbit_wanted_prefs()
    assert prefs["save_path"] == "/data/downloads"
    assert prefs["max_ratio_act"] == 0                  # stop seeding at the limit, never delete
    assert prefs["bypass_local_auth"] is True and prefs["upnp"] is False
    assert prefs["excluded_file_names"].split("\n") == configure.QBIT_EXCLUDED


def test_configure_and_the_malware_guard_agree_on_blocked_file_types(configure):
    assert set(configure.QBIT_EXCLUDED) == set(malware_guard.QB_EXCLUDE)


def test_pref_equal(configure):
    eq = configure._pref_equal
    assert eq("excluded_file_names", "*.exe,*.bat\n*.cmd", "*.cmd\n*.exe\n*.bat")
    assert eq("save_path", "/data/downloads/", "/data/downloads")
    assert eq("upnp", 0, False) and not eq("upnp", 1, False)
    assert eq("max_ratio", "2.0", 2) and not eq("max_ratio", None, 2)
    assert eq("web_ui_username", "admin", "admin")


def test_temporary_password_from_the_log(configure):
    log = ("The WebUI administrator username is: admin\n"
           "The WebUI administrator password was not set. A temporary password is provided for this session: AbC123\n"
           "A temporary password is provided for this session: DeF456\n")
    assert configure.TEMP_PASSWORD.findall(log)[-1] == "DeF456"


def test_profile_key_and_close(configure):
    profile = {"items": [{"quality": {"id": 4}, "allowed": True, "items": []}], "cutoff": 4, "formatItems": [
        {"format": 11, "score": -10000}], "language": {"id": -2}}
    assert configure._profile_key(profile) == configure._profile_key(copy.deepcopy(profile))
    assert configure._profile_key(profile) != configure._profile_key(dict(profile, cutoff=7))
    assert configure._close({"minSize": 1.0, "preferredSize": 22.001, "maxSize": 25}, 1, 22, 25)
    assert not configure._close({"minSize": 1, "preferredSize": 22, "maxSize": None}, 1, 22, 25)


def test_unknown_parts_are_refused_before_anything_runs(configure, capsys):
    assert configure.main(["--only", "bogus"]) == 2
    assert "unknown part: bogus" in capsys.readouterr().err


def test_payloads_address_containers_by_compose_service_name(configure, stack):
    """The apps reach each other by service name on the compose network, never by an IP address."""
    stack.write_env(QBIT_PASSWORD="test-password", WEBHOOK_SECRET="test-secret-path")
    api = FakeApi("radarr", {"downloadclient": [], "downloadclient/schema": DC_SCHEMA,
                             "notification": [], "notification/schema": _notification_schema()})
    arr = configure.Arr(api)
    arr.download_client()
    arr.notify_webhook()
    sent = json.dumps([obj for _, obj in api.saved])
    assert '"gluetun"' in sent and "http://moviebot:8090/" in sent
    assert not re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", sent)
