"""The host-side guard scripts (scripts/*.py): import hygiene and the pure helpers they rely on.

Nothing here talks to an app, qBittorrent, Docker or Telegram: files live under tmp_path and
every alert goes to stdout because the test stack has no bot token.
"""
from __future__ import annotations

import importlib
import io
import json
import os
import re
import subprocess
import sys
import urllib.error
import zipfile
from pathlib import Path

import alertstate
import dead_torrent_reaper
import link_orphans
import malware_guard
import naming_guard
import pytest
import regrab_guard
import tg

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = REPO / "scripts"


def cron_jobs() -> list:
    """Every job named in scripts/cron.template, including the profile and space-saver lines."""
    jobs = []
    for line in (SCRIPTS / "cron.template").read_text(encoding="utf-8").splitlines():
        m = re.search(r'run_guard\.sh"?\s+(\w+)\s*$', line)
        if m and not line.startswith("##"):
            jobs.append(m.group(1))
    return jobs


# ---------------------------------------------------------------- import hygiene

AUDIT = r"""
import importlib, importlib.util, json, os, sys
scripts, moviebot, files = sys.argv[1], sys.argv[2], sys.argv[3:]
sys.path[:0] = [scripts, moviebot]
import settings, alertstate, tg   # the shared modules first: settings reads .env on import, by design
WATCH = ("socket.", "subprocess.", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "os.fork",
         "os.mkdir", "os.remove", "os.rename", "os.rmdir", "os.chmod", "os.chown", "os.utime",
         "os.truncate", "os.symlink", "os.link", "shutil.", "urllib.Request", "sqlite3.connect")
events = []
def hook(event, args):
    if event.startswith(WATCH):
        events.append(event)
    elif event == "open" and args and isinstance(args[0], str):
        if not args[0].endswith((".py", ".pyc")) and "__pycache__" not in args[0]:
            events.append("open " + args[0])
sys.addaudithook(hook)
result = {}
for item in files:
    del events[:]
    if item.endswith(".py"):
        name = "audited_" + os.path.splitext(os.path.basename(item))[0]
        spec = importlib.util.spec_from_file_location(name, item)
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
    else:
        importlib.import_module(item)
    result[item] = sorted(set(events))
print(json.dumps(result))
"""


def test_importing_any_script_does_no_io(stack):
    files = sorted(str(p) for p in SCRIPTS.glob("*.py") if p.name != "settings.py")
    files += [str(REPO / "configure.py"), str(REPO / "tools" / "block_scan.py"),
              "botlib.text", "botlib.langs", "botlib.releases"]
    env = dict(os.environ, MEDIA_STACK_DIR=str(stack.root))
    out = subprocess.run([sys.executable, "-I", "-B", "-c", AUDIT, str(SCRIPTS), str(REPO / "moviebot")] + files,
                         capture_output=True, text=True, timeout=60, env=env, cwd=str(stack.root))
    assert out.returncode == 0, out.stderr
    result = json.loads(out.stdout)
    assert len(result) == len(files) > 20
    noisy = {item: events for item, events in result.items() if events}
    assert noisy == {}, "these modules do I/O when imported: %s" % noisy


@pytest.mark.parametrize("job", cron_jobs())
def test_every_cron_job_is_a_guard_with_main_and_dry_run(job):
    path = SCRIPTS / (job + ".py")
    assert path.is_file(), "cron.template runs %s but scripts/%s.py does not exist" % (job, job)
    module = importlib.import_module(job)
    assert callable(getattr(module, "main", None))
    assert (module.__doc__ or "").strip()
    source = path.read_text(encoding="utf-8")
    assert "--dry-run" in source
    assert 'if __name__ == "__main__":' in source


def test_cron_template_lists_every_guard_once():
    jobs = cron_jobs()
    assert len(jobs) == len(set(jobs)) == 19


# ---------------------------------------------------------------- episode numbers (three parsers agree)


@pytest.mark.parametrize("name, episodes", [
    ("Tears.of.Steel.2012.S01E03.720p.HDTV.x264-GROUP.mkv", [(1, 3)]),
    ("tears.of.steel.s1e2.mkv", [(1, 2)]),
    ("Elephants.Dream.S01E02E03.1080p.WEB-DL.x264-GROUP.mkv", [(1, 2), (1, 3)]),
    ("Elephants.Dream.S01E02-E03.1080p.WEB-DL.x264-GROUP.mkv", [(1, 2), (1, 3)]),
    ("Elephants.Dream.S01.E02.1080p.mkv", [(1, 2)]),
    ("Elephants Dream 1x02.mkv", [(1, 2)]),
    ("Elephants.Dream.21x09.mkv", [(21, 9)]),
    ("Elephants.Dream.S01E100.mkv", [(1, 100)]),
    ("Sintel.2010.1080p.WEB-DL.DDP5.1x264-GROUP.mkv", []),     # 5.1 audio followed by x264 is not 1x26
    ("Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv", []),
    ("Big Buck Bunny (2008).mkv", []),
    ("Elephants.Dream.10x100.mkv", []),
])
def test_episode_numbers_in_file_names(name, episodes):
    assert regrab_guard.episodes_in_name(name) == episodes
    assert regrab_guard.is_episode_name(name) is bool(episodes)
    assert naming_guard.episode_of(name) == (episodes[0] if episodes else None)
    assert link_orphans.is_episode_name(name) is bool(episodes)


def test_episode_key():
    assert regrab_guard.episode_key(1, 3) == "S01E03"
    assert regrab_guard.episode_key("12", "105") == "S12E105"


# ---------------------------------------------------------------- re-download guard: title matching


def test_title_tokens():
    tokens = regrab_guard.tokens
    assert tokens("The Big Buck Bunny 2008") == {"big", "buck", "bunny"}          # no years, no stopwords
    assert tokens("Sintel 2") == {"sintel", "2"}                                   # sequel digits stay
    assert tokens("Tears of Steel in Part 2 and A an") == {"tears", "steel", "2"}  # stopwords, "in" included
    assert tokens("Big.Buck.Bunny.2008.1080p.WEB-DL.x264-GROUP") == {"big", "buck", "bunny", "1080p", "web",
                                                                     "x264", "group"}
    assert tokens("") == set() and tokens(None) == set()
    assert "in" in regrab_guard.STOPWORDS
    assert link_orphans.tokens("The Big Buck Bunny 2008") == tokens("The Big Buck Bunny 2008")


@pytest.mark.parametrize("title, year, file_name, matches", [
    ("Sintel", 2010, "Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv", True),
    ("Big Buck Bunny", 2008, "Big Buck Bunny (2008) WEBDL-1080p.mkv", True),
    ("Sintel", 2010, "Sintel.2011.1080p.WEB-DL.x264-GROUP.mkv", False),     # another year
    ("Sintel", 2010, "Sintel.1080p.WEB-DL.x264-GROUP.mkv", False),          # no year in the name
    ("Sintel 2", 2010, "Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv", False),   # a sequel is another film
    ("Tears of Steel", 2012, "Tears.Steel.2012.mkv", True),                 # stopwords may be missing
    ("Elephants Dream", 2006, "Elephants.2006.mkv", False),                 # every title word must be there
    ("Sintel", None, "Sintel.1080p.mkv", True),                             # no year known: words only
    ("The", 2010, "The.2010.mkv", False),                                   # nothing to match on
])
def test_movie_name_matches(title, year, file_name, matches):
    assert regrab_guard.movie_name_matches(title, year, file_name) is matches


def test_series_key_and_pack_rule():
    assert regrab_guard.series_key("The Elephants' Dream!") == "theelephantsdream"
    assert regrab_guard.series_key("Elephants Dream") != regrab_guard.series_key("The Elephants Dream")
    assert regrab_guard.pack_cancellable([{"id": 1}, {"id": 2}], {1: True, 2: True})
    assert not regrab_guard.pack_cancellable([{"id": 1}, {"id": 2}], {1: True, 2: False})   # a mixed pack stays
    assert not regrab_guard.pack_cancellable([], {})


def _video(stack, rel: str, size: int = 2048) -> str:
    path = stack.media / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\0" * size)
    return str(path)


def test_library_index_counts_only_library_folders(stack):
    stack.write_env(MOVIE_LIBRARIES="fr=movies-fr")
    owned = _video(stack, "movies/Big Buck Bunny (2008)/Big Buck Bunny (2008) WEBDL-1080p.mkv")
    _video(stack, "movies/Big Buck Bunny (2008)/Big.Buck.Bunny.2008.sample.mkv")
    _video(stack, "movies/Big Buck Bunny (2008)/Sample/Big.Buck.Bunny.2008.mkv")
    french = _video(stack, "movies-fr/Elephants Dream (2006)/Elephants Dream (2006).mkv")
    _video(stack, "downloads/movies/Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv")
    episode = _video(stack, "tv/Tears of Steel/Season 01/Tears.of.Steel.2012.S01E03.720p.HDTV.x264-GROUP.mkv")
    _video(stack, "tv/Tears.of.Steel.2012.S01E01.loose.mkv")
    (stack.media / "movies" / "notes.txt").write_text("not a video")
    index, unreadable = regrab_guard.build_index()
    assert unreadable == 0
    assert sorted(index["movies"]) == sorted([owned, french])
    assert index["tv"] == {"Tears of Steel": {"S01E03": episode}}

    lib = regrab_guard.Library(index)
    assert lib.movie_files("Big Buck Bunny", 2008) == [owned]
    assert lib.movie_files("Sintel", 2010) == []           # only in the downloads folder: not owned
    series = {"title": "Tears of Steel", "path": "/data/tv/Tears of Steel"}
    assert lib.episode_file(series, 1, 3) == episode
    assert lib.episode_file(series, 1, 4) is None
    assert lib.episode_file({"title": "tears of steel", "path": "/data/tv/Elsewhere"}, 1, 3) == episode
    os.remove(owned)
    assert lib.movie_files("Big Buck Bunny", 2008) == []    # re-checked on disk before anything is cancelled


def test_library_index_skips_a_downloads_folder_inside_a_library_root(stack):
    stack.write_env(DOWNLOADS_DIR="movies/incoming")
    owned = _video(stack, "movies/Sintel (2010)/Sintel (2010).mkv")
    _video(stack, "movies/incoming/Big.Buck.Bunny.2008.1080p.WEB-DL.x264-GROUP.mkv")
    index, _ = regrab_guard.build_index()
    assert index["movies"] == [owned]


# ---------------------------------------------------------------- naming guard


def test_hash_like_names():
    assert naming_guard.hash_like("3f9a1c2b7d4e5f60a1b2.mkv")                # 16+ hex characters
    assert naming_guard.hash_like("a1b2c3d4e5f6g7h8i9j0klmnopqr.mkv")        # long and full of digits
    assert not naming_guard.hash_like("abcdefghijklmnopqrstuvwxyz.mkv")      # letters only: a squashed title
    assert not naming_guard.hash_like("Big.Buck.Bunny.2008.mkv")
    assert not naming_guard.hash_like("0123456789abcde.mkv")                 # too short


def test_same_title_pair_key_and_short():
    assert naming_guard.same_title("Elephants Dream (2006)", "elephants-dream")
    assert naming_guard.same_title("Tears of Steel", "Tears of Steel Extended")
    assert not naming_guard.same_title("Sintel", "Tears of Steel")
    assert not naming_guard.same_title("", "Sintel")
    windows = naming_guard.pair_key("D:\\TV\\Elephants Dream\\Season 01\\Elephants.Dream.S01E01.mkv")
    assert windows == naming_guard.pair_key("/data/tv/Elephants Dream/Season 01/ELEPHANTS.DREAM.S01E01.MKV")
    assert naming_guard.pair_key("") == ("", "")
    assert naming_guard.pair_key("x.mkv") == ("", "x.mkv")
    assert naming_guard.short("/srv/media/tv/Elephants Dream/Season 01/x.mkv") == "Season 01/x.mkv"


# ---------------------------------------------------------------- malware guard


@pytest.fixture
def make_file(tmp_path):
    def make(name: str, data: bytes = b"") -> str:
        path = tmp_path / "files" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return str(path)
    return make


@pytest.mark.parametrize("name, head, verdict", [
    ("payload", b"MZ\x90\x00" + b"\x01" * 60, "exec"),                         # Windows program, no extension
    ("Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv", b"MZ\x90\x00" + b"\x01" * 60, "exec"),   # a program named as a video
    ("Sintel.2010.1080p.WEB-DL.x264-GROUP.mp4", b"\x7fELF" + b"\x02" * 60, "exec"),      # Linux program
    ("run", b"#!/bin/sh\necho hi\n", "exec"),                                   # script
    ("a.dat", b"\x4c\x00\x00\x00\x01\x14\x02\x00" + b"\x01" * 8, "exec"),       # Windows shortcut
    ("go.bat", b"echo", "exec"),                                                # text script, by extension
    ("Site.URL", b"[InternetShortcut]", "exec"),                                # extension in any case
    ("Big.Buck.Bunny.2008.1080p.WEB-DL.x264-GROUP.mkv", b"\x1aE\xdf\xa3" + b"\x01" * 60, "clean"),
    ("notes.txt", b"plain text, nothing to run", "clean"),
    ("Thumbs.db", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x01" * 8, "clean"),  # OLE is not judged by content
    ("part.mkv", b"\x00" * 64, "unknown"),                                       # not downloaded yet
    ("Sintel.2010.1080p.rar", b"Rar!\x1a\x07\x01\x00" + b"\x01" * 8, "archive"),
    ("Sintel.2010.1080p.7z", b"7z\xbc\xaf\x27\x1c" + b"\x01" * 10, "archive"),
    ("broken.zip", b"PK\x03\x04" + b"\x01" * 40, "unknown"),                    # zip not complete yet
])
def test_malware_classify_by_first_bytes(make_file, name, head, verdict):
    assert malware_guard.classify(make_file(name, head))[0] == verdict


def test_malware_classify_looks_inside_zips(tmp_path):
    bundle, subs = tmp_path / "bundle.zip", tmp_path / "subs.zip"
    with zipfile.ZipFile(str(bundle), "w") as z:
        z.writestr("Sintel.2010.1080p/setup.exe", b"MZ")
    with zipfile.ZipFile(str(subs), "w") as z:
        z.writestr("Sintel.2010.srt", b"1\n00:00:01,000 --> 00:00:02,000\nhello\n")
    verdict, why = malware_guard.classify(str(bundle))
    assert verdict == "exec" and why == "zip containing setup.exe"
    assert malware_guard.classify(str(subs)) == ("clean", "")
    assert malware_guard.classify(str(tmp_path / "missing.mkv"))[0] == "unknown"


def test_malware_extension_helpers():
    assert malware_guard.ext_of("Sintel.2010.1080p.MKV") == "mkv"
    assert malware_guard.ext_of("folder\\Setup.EXE") == "exe"
    assert malware_guard.ext_of("README") == ""
    excluded = malware_guard.QB_EXCLUDE
    assert "*.exe" in excluded and "*.EXE" in excluded and "*.mkv" not in excluded
    assert len(excluded) == 2 * len(malware_guard.EXEC_EXT)
    assert not (malware_guard.EXEC_EXT & malware_guard.MEDIA_EXT)


def test_malware_roots_follow_real_paths(stack):
    s = stack.reload()
    roots = malware_guard.Roots([s.DOWNLOADS])
    assert roots.unsafe() == []
    inside = os.path.join(s.DOWNLOADS, "movies", "x.exe")
    assert roots.contains(inside)
    assert roots.root_of(inside) == s.DOWNLOADS
    assert not roots.contains(os.path.join(s.MOVIES, "x.exe"))
    assert not roots.contains("")
    os.symlink(s.MOVIES, os.path.join(s.DOWNLOADS, "link"))
    assert not roots.contains(os.path.join(s.DOWNLOADS, "link", "x.exe"))     # a symlink cannot lead out
    assert malware_guard.Roots([s.MEDIA_ROOT]).unsafe()                         # never the media root
    assert malware_guard.Roots([os.sep]).unsafe()


def test_malware_storage_dir():
    single = {"content_path": "/data/downloads/movies/Sintel.2010.mkv"}
    assert malware_guard.storage_dir(single, [{"name": "Sintel.2010.mkv"}]) == "/data/downloads/movies"
    folder = {"content_path": "/data/downloads/tv/Pack"}
    assert malware_guard.storage_dir(folder, [{"name": "Pack/a.mkv"}, {"name": "Pack/b.mkv"}]) == "/data/downloads/tv"
    flat = {"content_path": "/data/downloads/tv"}
    assert malware_guard.storage_dir(flat, [{"name": "a.mkv"}, {"name": "b.nfo"}]) == "/data/downloads/tv"
    incomplete = {"progress": 0.5, "download_path": "/data/downloads/incomplete", "save_path": "/data/downloads"}
    assert malware_guard.storage_dir(incomplete, []) == "/data/downloads/incomplete"
    finished = {"progress": 1, "save_path": "/data/downloads/movies"}
    assert malware_guard.storage_dir(finished, []) == "/data/downloads/movies"


def test_malware_deletes_only_what_qbittorrent_lists_with_that_size(stack):
    s = stack.reload()
    roots = malware_guard.Roots([s.DOWNLOADS])
    path = os.path.join(s.DOWNLOADS, "setup.exe")
    with open(path, "wb") as fh:
        fh.write(b"MZ" + b"\0" * 8)
    assert malware_guard.torrent_owned(path, {path: ("h", 0, "setup.exe", 10)}, roots)
    assert not malware_guard.torrent_owned(path, {path: ("h", 0, "setup.exe", 11)}, roots)    # another size
    assert not malware_guard.torrent_owned(path, {path: ("h", 0, "setup.exe", None)}, roots)
    assert not malware_guard.torrent_owned(path, {}, roots)
    outside = os.path.join(s.MOVIES, "setup.exe")
    assert not malware_guard.torrent_owned(outside, {outside: ("h", 0, "setup.exe", 10)}, roots)


# ---------------------------------------------------------------- dead-torrent reaper


def _hashes(torrents) -> list:
    return [t["hash"] for t in torrents]


def test_reaper_dead_means_no_seeder_exists_not_none_connected():
    now = 1_000_000.0
    torrents = [
        {"hash": "dead", "state": "stalledDL", "progress": 0.9, "num_complete": 0, "num_seeds": 0},
        {"hash": "unreachable", "state": "stalledDL", "progress": 0.2, "num_complete": 4, "num_seeds": 0},
        {"hash": "stopped", "state": "stoppedDL", "progress": 0.1, "num_complete": 0},
        {"hash": "paused", "state": "pausedDL", "progress": 0.1, "num_complete": 0},
        {"hash": "meta_old", "state": "metaDL", "progress": 0, "added_on": now - 4 * 3600},
        {"hash": "meta_new", "state": "metaDL", "progress": 0, "added_on": now - 600},
        {"hash": "seeding", "state": "stalledUP", "progress": 1.0, "num_complete": 0},
        {"hash": "complete", "state": "stalledDL", "progress": 1.0, "num_complete": 0},
        {"hash": "busy", "state": "downloading", "progress": 0.5, "num_complete": 0},
    ]
    dead, unreachable, meta, paused = dead_torrent_reaper.classify(torrents, now, False)
    assert _hashes(dead) == ["dead"]
    assert _hashes(unreachable) == ["unreachable"]           # seeders exist, we reach none: our side, not reaped
    assert _hashes(meta) == ["meta_old"]
    assert _hashes(paused) == ["stopped", "paused"]          # resumed, never judged while stopped
    assert dead_torrent_reaper.classify(torrents, now, True)[3] == []    # the pause flag: leave them stopped


def test_reaper_download_area():
    area = dead_torrent_reaper._in_download_area
    assert area("/data/downloads/movies/x", "")
    assert area("/data/downloads", "")
    assert not area("/data/movies/x", "")
    assert not area("/data/downloadsX/x", "")
    assert area("/incomplete/x", "/incomplete/")


# ---------------------------------------------------------------- alert state


def test_filter_new_reports_each_problem_once(stack):
    stack.reload()
    assert alertstate.filter_new("cat", ["a", "b", "a"]) == (["a", "b"], [])
    assert alertstate.filter_new("cat", ["b", "c"]) == (["c"], ["b"])
    assert alertstate.filter_new("cat", ["a"]) == (["a"], [])       # cleared in between: new again
    assert alertstate.filter_new("other", ["a"]) == (["a"], [])     # categories are independent


def test_filter_new_in_a_dry_run_records_nothing(stack):
    stack.reload()
    assert alertstate.filter_new("cat", ["a"], dry_run=True) == (["a"], [])
    assert alertstate.filter_new("cat", ["a"]) == (["a"], [])
    assert alertstate.filter_new("cat", ["a"]) == ([], ["a"])


def test_digest_due_once_a_day(stack):
    stack.reload()
    assert alertstate.digest_due("d", dry_run=True)
    assert alertstate.digest_due("d")
    assert not alertstate.digest_due("d")
    assert alertstate.digest_due("e")


def test_a_broken_state_file_is_not_fatal(stack):
    s = stack.reload()
    with open(s.state_path(alertstate.STATE_NAME), "w", encoding="utf-8") as fh:
        fh.write("[1, 2")
    assert alertstate.filter_new("cat", ["a"]) == (["a"], [])
    assert alertstate.digest_due("d")


def test_the_shared_lock_is_exclusive(stack):
    stack.reload()
    first = alertstate.acquire_lock()
    assert first not in (None, "unlocked")
    try:
        assert alertstate.acquire_lock() is None
    finally:
        first.close()
    again = alertstate.acquire_lock()
    assert again not in (None, "unlocked")
    again.close()
    assert alertstate.acquire_lock(str(stack.root / "missing" / "x.lock")) == "unlocked"


def test_mount_canary(stack, capsys):
    s = stack.reload()
    assert alertstate.mount_canary("job")
    os.rename(s.MEDIA_ROOT, s.MEDIA_ROOT + ".away")
    assert not alertstate.mount_canary("job", dry_run=True)
    out = capsys.readouterr().out
    message = out.split("[dry-run] would send:", 1)[1]
    assert "the media folder (MEDIA_ROOT) is missing or not mounted" in message
    assert s.MEDIA_ROOT not in message                       # alerts name folders, never raw paths
    assert not alertstate.mount_canary("job")                 # a real run alerts (no bot here: printed)...
    assert "Telegram is not configured" in capsys.readouterr().out
    assert not alertstate.mount_canary("job")                 # ...at most once a day
    assert "Telegram is not configured" not in capsys.readouterr().out


def test_mount_canary_checks_mount_paths(stack):
    stack.write_env(MOUNT_PATHS=str(stack.root / "share"))
    assert not alertstate.mount_canary("job", dry_run=True)
    (stack.root / "share").mkdir()
    assert alertstate.mount_canary("job", dry_run=True)


def test_describe_path(stack):
    s = stack.write_env(MOVIE_LIBRARIES="fr=movies-fr", MOUNT_PATHS="/mnt/share")
    assert alertstate.describe_path(s.MEDIA_ROOT) == "the media folder (MEDIA_ROOT)"
    assert alertstate.describe_path(s.DOWNLOADS) == "the downloads folder"
    assert alertstate.describe_path(s.MOVIES + "/") == "the movies library"
    assert alertstate.describe_path(s.TV) == "the TV library"
    assert alertstate.describe_path(os.path.join(s.MEDIA_ROOT, "movies-fr")) == "the 'fr' movie library"
    assert alertstate.describe_path("/mnt/share") == "a network mount listed in MOUNT_PATHS"
    assert alertstate.describe_path("/somewhere/else") == "a media folder"


# ---------------------------------------------------------------- Telegram helper


def test_send_without_a_token_prints_and_returns_false(stack, capsys):
    stack.reload()
    assert tg.send("Sintel finished downloading") is False
    out = capsys.readouterr().out
    assert "Telegram is not configured" in out and "Sintel finished downloading" in out
    assert tg.send("   ") is False


def test_failed_sends_are_kept_and_delivered_after_the_next_success(stack, monkeypatch):
    s = stack.write_env(TELEGRAM_BOT_TOKEN="test-token", NOTIFY_CHAT_ID="42")
    sent, down = [], [True]

    def fake_post(token, chat, text, as_html):
        if down[0]:
            raise urllib.error.URLError("no route")
        sent.append((chat, text, as_html))

    monkeypatch.setattr(tg, "_post", fake_post)
    monkeypatch.setattr(tg, "CHUNK_PAUSE_S", 0)
    assert tg.send("first <b>alert</b>") is False
    assert tg.send("second alert") is False
    spool = os.path.join(s.STATE_DIR, tg.SPOOL_NAME)
    with open(spool, encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 2 and lines[0].endswith(" | first <b>alert</b>")
    down[0] = False
    assert tg.send("back online") is True
    assert sent[0] == ("42", "back online", True)
    chat, text, as_html = sent[1]
    assert text.startswith("Alerts that could not be delivered earlier") and "first alert" in text
    assert as_html is False and "<b>" not in text
    assert not os.path.exists(spool)


def test_the_spool_is_capped(stack, monkeypatch):
    s = stack.reload()
    monkeypatch.setattr(tg, "SPOOL_MAX_LINES", 20)
    for i in range(30):
        tg._spool("line %d" % i)
    with open(os.path.join(s.STATE_DIR, tg.SPOOL_NAME), encoding="utf-8") as fh:
        lines = fh.read().splitlines()
    assert len(lines) == 20
    assert lines[0].endswith("| line 10") and lines[-1].endswith("| line 29")     # the oldest lines go first


def test_text_telegram_cannot_parse_is_resent_as_plain_text(stack, monkeypatch):
    stack.write_env(TELEGRAM_BOT_TOKEN="test-token", NOTIFY_CHAT_ID="42")
    modes = []

    def fake_post(token, chat, text, as_html):
        modes.append(as_html)
        if as_html:
            body = io.BytesIO(b'{"description": "Bad Request: can\'t parse entities: unsupported start tag"}')
            raise urllib.error.HTTPError("sendMessage", 400, "Bad Request", {}, body)

    monkeypatch.setattr(tg, "_post", fake_post)
    assert tg.send("size <x 2 GB & more") is True
    assert modes == [True, False]


def test_split_message():
    text = "\n".join("line %04d %s" % (i, "x" * 60) for i in range(200))
    chunks = tg.split_message(text)
    assert len(chunks) == 4 and all(len(c) <= tg.MAX_CHARS for c in chunks)
    assert "\n".join(chunks) == text                            # split at line breaks, nothing lost
    long_line = "y" * (tg.MAX_CHARS * 2 + 10)
    assert [len(c) for c in tg.split_message(long_line)] == [tg.MAX_CHARS, tg.MAX_CHARS, 10]
    assert tg.split_message("short") == ["short"]


def test_parse_reply_and_plain_text():
    assert tg.parse_reply("") is None and tg.parse_reply("  ") is None
    assert tg.parse_reply("yes") == "yes" and tg.parse_reply(" YES\n") == "yes"
    assert tg.parse_reply("No") == "no" and tg.parse_reply("maybe") == "no"     # anything but yes is a no
    assert tg.parse_reply('{"answer": "yes"}') == "yes" and tg.parse_reply('{"decision": "no"}') == "no"
    assert tg.parse_reply("{broken") == "no"
    assert tg.plain_text("<b>Sintel</b> &amp; <i>more</i>") == "Sintel & more"


def test_questions_go_through_files_for_the_bot(stack, capsys):
    s = stack.reload()
    assert tg.post_ask("Anyone there?") is None                 # no bot token: nobody could answer
    assert "question not asked" in capsys.readouterr().out
    s = stack.write_env(TELEGRAM_BOT_TOKEN="test-token")
    cid = tg.post_ask("Download <b>Sintel</b> anyway?", prefix="sz")
    ask_file = os.path.join(s.CONFIRM_DIR, cid + ".ask")
    reply_file = os.path.join(s.CONFIRM_DIR, cid + ".reply")
    assert cid.startswith("sz")
    with open(ask_file, encoding="utf-8") as fh:
        assert json.load(fh) == {"text": "Download <b>Sintel</b> anyway?"}
    assert tg.read_reply(cid) is None
    with open(reply_file, "w", encoding="utf-8") as fh:
        fh.write("yes")
    assert tg.read_reply(cid, consume=False) == "yes"
    assert tg.read_reply(cid) == "yes" and not os.path.exists(reply_file)
    tg.drop_ask(cid)
    assert not os.path.exists(ask_file)
    assert tg.ask("Still there?", 0) == tg.TIMEOUT              # nobody answers in time: cleaned up
    assert [f for f in os.listdir(s.CONFIRM_DIR) if not f.startswith(".")] == []


def test_tg_command_line_dry_run(stack, capsys):
    stack.reload()
    assert tg.main(["send", "hello", "--dry-run"]) == 0
    assert "[dry-run] would send:\nhello" in capsys.readouterr().out
