"""Pure helpers of the remaining guards: link_orphans, size_cap_watch, missing_sweep, mount_guard,
integrity_scan, update_check, verify_imports, config_backup, restore_drill, library_guard,
the space-saver pair and subtitle_sweep."""
from __future__ import annotations

import io
import os
import tarfile
import zipfile
from datetime import datetime, timezone

import config_backup
import integrity_scan
import library_guard
import link_orphans
import missing_sweep
import mount_guard
import pytest
import resolve_shrink_imports
import restore_drill
import shrink_oversized
import size_cap_watch
import subtitle_sweep
import update_check
import verify_imports

# ---------------------------------------------------------------- link_orphans


@pytest.mark.parametrize("name, is_file, expected", [
    ("Tears.of.Steel.2012.1080p.WEB-DL.x264-GROUP.mkv", True, ("Tears of Steel", "2012")),
    ("Big Buck Bunny (2008)", False, ("Big Buck Bunny", "2008")),
    ("www.example.com - Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv", True, ("Sintel", "2010")),
    ("[GROUP] Sintel 2010 1080p.mkv", True, ("Sintel", "2010")),
    ("Sintel.2010.2012.1080p.mkv", True, ("Sintel", "2010")),        # the first usable year
    ("2010.mkv", True, (None, None)),                                 # no title before the year
    ("Sintel.1080p.WEB-DL.mkv", True, (None, None)),                  # no year: not routed at all
    ("ubuntu-24.04-desktop-amd64.iso", True, (None, None)),
])
def test_parse_title_year(name, is_file, expected):
    assert link_orphans.parse_title_year(name, is_file=is_file) == expected


def test_title_year_key_and_sanitize():
    key = link_orphans.title_year_key
    assert key("The Big Buck Bunny", 2008) == key("Big Buck Bunny!", "2008")
    assert key("Sintel", 2010) != key("Sintel", 2011)
    assert link_orphans.sanitize('Sintel: <Director> "Edition"/2010? *. ') == "Sintel Director Edition 2010"
    assert link_orphans.clean_title("Elephants_Dream.[Extras]") == "Elephants Dream"


def test_pick_radarr_movie_needs_every_word_and_the_exact_year():
    movies = [{"id": 1, "title": "Tears of Steel", "year": 2012},
              {"id": 2, "title": "Tears of Steel Extended", "year": 2012},
              {"id": 3, "title": "Sintel", "year": 2010},
              {"id": 4, "title": "Sintel", "year": 2010}]
    download = link_orphans.tokens("Tears.of.Steel.Extended.2012.1080p.WEB-DL.x264-GROUP")
    assert link_orphans.pick_radarr_movie(movies, download, "2012") == (movies[1], False)   # most specific title
    plain = link_orphans.tokens("Tears.of.Steel.2012.1080p")
    assert link_orphans.pick_radarr_movie(movies, plain, "2012") == (movies[0], False)
    assert link_orphans.pick_radarr_movie(movies, plain, "2013") == (None, False)          # the year must match
    sintel = link_orphans.tokens("Sintel.2010.1080p")
    assert link_orphans.pick_radarr_movie(movies, sintel, "2010") == (None, True)          # a tie is ambiguous


def test_pick_lookup_result():
    results = [{"tmdbId": 10, "title": "Sintel", "year": 2010},
               {"tmdbId": 11, "title": "Sintel", "year": 2011},
               {"tmdbId": 10, "title": "Sintel", "year": 2010}]
    assert link_orphans.pick_lookup_result(results, {"sintel"}, "2010") == (results[0], False)
    assert link_orphans.pick_lookup_result(results, {"sintel"}, "2012") == (None, False)
    two = [{"tmdbId": 10, "title": "Sintel", "year": 2010}, {"tmdbId": 12, "title": "Sintel Short", "year": 2010}]
    assert link_orphans.pick_lookup_result(two, {"sintel", "short"}, "2010") == (None, True)
    late = [{"tmdbId": i, "title": "Other", "year": 2010} for i in range(4)] + [{"tmdbId": 9, "title": "Sintel",
                                                                                "year": 2010}]
    assert link_orphans.pick_lookup_result(late, {"sintel"}, "2010") == (None, False)    # only the first four count


def test_is_within_is_strictly_inside():
    assert link_orphans.is_within("/srv/media/movies/a.mkv", ["/srv/media/movies"])
    assert not link_orphans.is_within("/srv/media/movies2/a.mkv", ["/srv/media/movies"])
    assert not link_orphans.is_within("/srv/media/movies", ["/srv/media/movies"])
    assert verify_imports.is_within("/srv/media/tv/x.mkv", ["/srv/media/tv"])
    assert not verify_imports.is_within("/srv/media/tvshows/x.mkv", ["/srv/media/tv"])


# ---------------------------------------------------------------- size_cap_watch

GIB = size_cap_watch.GIB
EPISODE = {"seasonNumber": 1, "episodeNumber": 3}


def screl(size_gb, rejections=("larger than maximum allowed 1.8 GB",), lang="English", cf=0, weight=1000,
          seeders=10, full=False, episodes=(3,), season=1, **extra):
    r = {"title": "Elephants.Dream.S%02dE%02d.1080p.WEB-DL.x264-GROUP" % (season, episodes[0] if episodes else 0),
         "seasonNumber": season, "episodeNumbers": list(episodes), "fullSeason": full,
         "rejected": bool(rejections), "rejections": ["%.1f GB is %s" % (size_gb, x) for x in rejections],
         "languages": [{"name": lang}], "customFormatScore": cf, "size": int(size_gb * GIB),
         "qualityWeight": weight, "seeders": seeders, "quality": {"quality": {"resolution": 1080, "source": "web"}}}
    r.update(extra)
    return r


def classify(releases, wanted=("English",), cap_gb=2.5):
    return size_cap_watch.classify(EPISODE, releases, set(wanted), cap_gb * GIB)


def test_size_cap_watch_offers_a_copy_refused_for_size_alone():
    offer = screl(2.1)
    assert classify([offer]) == ("size_miss", offer, "1.8 GB")


def test_size_cap_watch_never_offers_anything_else():
    assert classify([screl(3.0)])[0] == "nothing"                                    # above the flat cap
    assert classify([screl(2.1, rejections=("larger than maximum allowed 1.8 GB", "Unknown Series"))])[0] == "nothing"
    assert classify([screl(2.1, lang="French")])[0] == "nothing"                     # wrong language
    assert classify([screl(2.1, lang="French")], wanted=("English", "French"))[0] == "size_miss"   # original language
    assert classify([screl(2.1, cf=-10000)])[0] == "nothing"                         # the custom formats reject it
    assert classify([screl(9.0, full=True)])[0] == "nothing"                         # never a season pack
    assert classify([screl(2.1, episodes=(4,))])[0] == "nothing"                     # another episode
    assert classify([screl(2.1, episodes=(3, 4))])[0] == "nothing"                   # a double episode
    assert classify([])[0] == "nothing"


def test_size_cap_watch_leaves_episodes_sonarr_can_grab_itself():
    assert classify([screl(1.0, rejections=())]) == ("grabbable", None, None)
    assert classify([screl(2.1), screl(1.0, rejections=(), weight=1000)])[0] == "grabbable"
    better = screl(2.1, weight=1000)
    assert classify([better, screl(0.9, rejections=(), weight=500)])[:2] == ("size_miss", better)


def test_size_cap_watch_picks_the_best_offer_and_reads_mapped_numbers():
    small, big, popular = screl(1.9), screl(2.3), screl(2.2, seeders=50)
    assert classify([small, big])[1] is small                      # same rank and seeders: the smaller copy
    assert classify([small, big, popular])[1] is popular           # more seeders first
    mapped = screl(2.1, season=2, episodes=(9,), mappedSeasonNumber=1, mappedEpisodeNumbers=[3])
    assert classify([mapped])[0] == "size_miss"


def test_size_cap_watch_text():
    def quality(resolution, source):
        return {"quality": {"quality": {"resolution": resolution, "source": source}}}

    assert size_cap_watch.describe(quality(1080, "webdl")) == "1080p web copy"
    assert size_cap_watch.describe(quality(720, "television")) == "720p TV copy"
    assert size_cap_watch.describe({}) == "copy"
    text = size_cap_watch.question_text([{"series": "Elephants Dream", "season": 1, "episode": 3,
                                          "ep_title": "Episode <i>three</i> & more", "size_gb": 2.1,
                                          "limit": "1.8 GB", "desc": "1080p web copy"}])
    assert "<b>Elephants Dream</b> · Season 1" in text
    assert "“Episode &lt;i&gt;three&lt;/i&gt; &amp; more”: best copy is 2.10 GB (limit 1.8 GB)" in text
    assert text.endswith("will not download this by itself. Download anyway?")


def test_parse_utc():
    expected = datetime(2026, 10, 31, 12, 34, 56, tzinfo=timezone.utc)
    for module in (size_cap_watch, missing_sweep):
        assert module.parse_utc("2026-10-31T12:34:56.1234567Z") == expected
        assert module.parse_utc("") is None and module.parse_utc("junk") is None


# ---------------------------------------------------------------- missing_sweep

NOW = datetime(2026, 10, 31, 12, tzinfo=timezone.utc)


def _ep(i, series, season, number, aired=True, monitored=True):
    return {"id": i, "seriesId": series, "seasonNumber": season, "episodeNumber": number, "monitored": monitored,
            "airDateUtc": "2026-01-01T00:00:00Z" if aired else "2027-01-01T00:00:00Z"}


SERIES = {1: {"title": "Tears of Steel"}, 2: {"title": "Elephants Dream"}}
EPISODES = {1: [_ep(101, 1, 1, 1), _ep(102, 1, 1, 2), _ep(103, 1, 1, 3), _ep(104, 1, 2, 1), _ep(105, 1, 2, 2),
                _ep(100, 1, 0, 1)],
            2: [_ep(201, 2, 1, 1), _ep(202, 2, 1, 2, aired=False)]}
MISSING = [_ep(102, 1, 1, 2), _ep(104, 1, 2, 1), _ep(105, 1, 2, 2), _ep(100, 1, 0, 1),
           _ep(201, 2, 1, 1), _ep(202, 2, 1, 2, aired=False), _ep(301, 3, 1, 1)]


def test_plan_series_splits_whole_seasons_from_gaps():
    seasons, episodes = missing_sweep.plan_series(SERIES, MISSING, lambda sid: EPISODES[sid], {},
                                                  NOW.timestamp(), NOW)
    assert seasons == [("s:2:1:season", 2, 1, "Elephants Dream", 1), ("s:1:2:season", 1, 2, "Tears of Steel", 2)]
    assert episodes == [("s:1:1:eps", 1, 1, "Tears of Steel", [102])]     # specials, unaired, unknown: skipped


def test_plan_series_cooldown_and_errors():
    now = NOW.timestamp()
    recent = {"s:1:2:season": now - 3600, "s:2:1:season": now - 22 * 86400}
    seasons, _ = missing_sweep.plan_series(SERIES, MISSING, lambda sid: EPISODES[sid], recent, now, NOW)
    assert [job[0] for job in seasons] == ["s:2:1:season"]           # searched an hour ago: skipped

    def broken(sid):
        raise OSError("Sonarr went away")

    assert missing_sweep.plan_series(SERIES, MISSING, broken, {}, now, NOW) == ([], [])


def test_missing_sweep_budgets():
    assert missing_sweep.degraded((6, 10, 8)) == (2, 3, 2)
    assert missing_sweep.degraded((1, 1, 1)) == missing_sweep.DEGRADED_FLOOR
    assert missing_sweep.fresh({"k": 100.0}, "k", 100.0 + 86400)
    assert not missing_sweep.fresh({"k": 100.0}, "k", 100.0 + missing_sweep.COOLDOWN_DAYS * 86400)
    assert not missing_sweep.fresh({"k": "junk"}, "k", 100.0)
    assert missing_sweep.aired({"airDateUtc": "2026-01-01T00:00:00Z"}, NOW)
    assert not missing_sweep.aired({"airDateUtc": None}, NOW)


# ---------------------------------------------------------------- mount_guard

MOUNTINFO = ("22 1 253:0 / / rw,relatime - xfs /dev/mapper/root rw\n"
             "90 22 0:52 / /srv/media rw - cifs //server/share rw\n"
             "91 22 0:53 / /srv/media/movies\\040disk rw - nfs4 server:/export rw\n"
             "92 22 0:60 / /mnt/auto rw - autofs systemd-1 rw\n"
             "93 22 0:61 / /srv/media rw - cifs //server/share rw\n"
             "garbage line\n")


def test_parse_mountinfo():
    mounts = mount_guard.parse_mountinfo(MOUNTINFO)
    assert mounts["/srv/media/movies disk"] == ("nfs4", "0:53")    # octal escapes decoded
    assert mounts["/srv/media"] == ("cifs", "0:61")                # the topmost mount of a point wins
    assert mount_guard.covering_device(mounts, "/srv/media/tv/x.mkv") == "0:61"
    assert mount_guard.covering_device(mounts, "/srv/media/movies disk/x.mkv") == "0:53"
    assert mount_guard.covering_device(mounts, "/home") == "253:0"
    assert mount_guard.covering_device({}, "/home") is None
    assert mount_guard.is_mounted(mounts, "/srv/media")
    assert not mount_guard.is_mounted(mounts, "/mnt/auto")         # an automount trap is not mounted yet
    assert not mount_guard.is_mounted(mounts, "/mnt/other")


@pytest.mark.parametrize("seconds, text", [(59, "0 min"), (90 * 60, "90 min"), (5 * 3600, "5 h"),
                                           (3 * 86400, "3 days")])
def test_span(seconds, text):
    assert mount_guard.span(seconds) == text


# ---------------------------------------------------------------- integrity_scan


def test_integrity_excluder(stack):
    s = stack.write_env(BACKUP_DIR=str(stack.root / "backups"))
    ex = integrity_scan.Excluder(["archive", "tv/Tears of Steel/Season 02", str(stack.root / "elsewhere"), " "])
    assert ex.dirname(s.MOVIES, "ARCHIVE")                       # a bare name matches anywhere, any case
    assert ex.path(os.path.join(s.MEDIA_ROOT, "tv/Tears of Steel/Season 02/e.mkv"))
    assert not ex.path(os.path.join(s.MEDIA_ROOT, "tv/Tears of Steel/Season 01/e.mkv"))
    assert ex.path(os.path.join(s.DOWNLOADS, "a.mkv"))           # downloads are never the library
    assert ex.path(str(stack.root / "elsewhere" / "a.mkv"))
    assert ex.dirname(s.TV, "Extras") and ex.dirname(s.TV, ".grab") and ex.dirname(s.TV, "@eaDir")
    assert not ex.dirname(s.TV, "Season 01")


def test_integrity_text_helpers():
    assert integrity_scan.is_sample("Sintel.2010-sample.mkv") and integrity_scan.is_sample("sample.mkv")
    assert not integrity_scan.is_sample("Sintel.2010.Sampler.mkv")
    assert integrity_scan.human("suspicious duration 12s") == "only 12s long; likely truncated"
    assert integrity_scan.human("/data/tv/x.mkv: Invalid data found when processing input") == \
        "Invalid data found when processing input"                # no file paths in alerts
    assert integrity_scan.human("ffprobe timed out") == "timed out reading the file"
    assert integrity_scan.human("") == "unreadable"


# ---------------------------------------------------------------- update_check and verify_imports


@pytest.mark.parametrize("ref, expected", [
    ("lscr.io/linuxserver/sonarr:latest", ("lscr.io/linuxserver/sonarr", "latest")),
    ("louislam/uptime-kuma:2", ("louislam/uptime-kuma", "2")),
    ("alpine", ("alpine", "latest")),
    ("localhost:5000/img", ("localhost:5000/img", "latest")),       # a registry port is not a tag
])
def test_split_ref(ref, expected):
    assert update_check.split_ref(ref) == expected


def test_norm_repo():
    assert update_check._norm_repo("docker.io/library/alpine") == "alpine"
    assert update_check._norm_repo("docker.io/qmcgaw/gluetun") == "qmcgaw/gluetun"
    assert update_check._norm_repo("lscr.io/linuxserver/radarr") == "lscr.io/linuxserver/radarr"


def test_verify_imports_seen_list(stack):
    s = stack.reload()
    seen = {"a": 30, "b": 10, "c": 20, "d": 40}
    assert verify_imports.prune_seen(seen, keep=2) == {"a": 30, "d": 40}      # by when they were seen
    assert verify_imports.prune_seen(seen, keep=10) is seen
    path = s.state_path(".verify_imports_seen.json")
    s.save_json(path, {"ids": [1, 2]})                                         # the older layout
    assert verify_imports.load_seen(path) == {"1": 0, "2": 0}
    s.save_json(path, {"seen": {"7": 123.0, "8": "junk"}})
    assert verify_imports.load_seen(path) == {"7": 123}


# ---------------------------------------------------------------- config_backup and restore_drill


def test_verify_zip_and_tgz(tmp_path):
    good_zip, empty_zip = tmp_path / "radarr-20261031.zip", tmp_path / "empty.zip"
    with zipfile.ZipFile(str(good_zip), "w") as z:
        z.writestr("config.xml", "<Config/>")
    with zipfile.ZipFile(str(empty_zip), "w"):
        pass
    assert config_backup.verify_zip(str(good_zip))
    assert not config_backup.verify_zip(str(empty_zip))
    cut = tmp_path / "cut.zip"
    cut.write_bytes(good_zip.read_bytes()[:-30])                  # a copy taken while the app was still writing
    assert not config_backup.verify_zip(str(cut))

    good_tgz = tmp_path / "state-20261031.tgz"
    with tarfile.open(str(good_tgz), "w:gz") as t:
        data = b"{}" * 1000
        info = tarfile.TarInfo("state/.alert_state.json")
        info.size = len(data)
        t.addfile(info, io.BytesIO(data))
    assert config_backup.verify_tgz(str(good_tgz))
    broken = tmp_path / "broken.tgz"
    broken.write_bytes(good_tgz.read_bytes()[:-20])
    assert not config_backup.verify_tgz(str(broken))
    assert not config_backup.verify_tgz(str(tmp_path / "missing.tgz"))


def test_sqlite_side_files():
    assert config_backup._sqlite_base("radarr.db-wal") == "radarr.db"
    assert config_backup._sqlite_base("logs.db-shm") == "logs.db"
    assert config_backup._sqlite_base("radarr.db") == ""
    assert config_backup._sqlite_base("notes.txt-wal") == ""


def test_rotation_keeps_the_newest_and_touches_nothing_else(stack, tmp_path):
    stack.reload()
    dest = tmp_path / "backups"
    dest.mkdir()
    for day in range(1, 10):
        (dest / ("sonarr-202610%02d.zip" % day)).write_bytes(b"x")
    (dest / "sonarr-notes.txt").write_text("mine")
    (dest / "radarr-20261001.zip").write_bytes(b"x")
    config_backup.rotate(str(dest), ["sonarr"])
    kept = sorted(p.name for p in dest.iterdir())
    assert kept == ["radarr-20261001.zip"] + ["sonarr-202610%02d.zip" % d for d in range(3, 10)] + ["sonarr-notes.txt"]
    assert config_backup.KEEP == 7


def test_restore_drill_never_extracts_outside_its_folder(tmp_path):
    good, evil = tmp_path / "sonarr-20261031.zip", tmp_path / "evil.zip"
    with zipfile.ZipFile(str(good), "w") as z:
        z.writestr("config.xml", "<Config><ApiKey>drill-test-key</ApiKey></Config>")
        z.writestr("sonarr.db", b"")
    with zipfile.ZipFile(str(evil), "w") as z:
        z.writestr("../escaped.txt", "x")
    out = tmp_path / "scratch"
    out.mkdir()
    restore_drill.safe_extract(str(good), str(out))
    assert (out / "config.xml").is_file()
    assert restore_drill.read_api_key(str(out)) == "drill-test-key"
    assert restore_drill.read_api_key(str(tmp_path / "nowhere")) == ""
    with pytest.raises(ValueError):
        restore_drill.safe_extract(str(evil), str(out))
    assert not (tmp_path / "escaped.txt").exists()


def test_newest_backup(tmp_path):
    assert restore_drill.newest_backup(str(tmp_path / "missing")) == ""
    for name, age in (("sonarr-20261001.zip", 300), ("sonarr-20261002.zip", 100), ("radarr-20261003.zip", 0)):
        p = tmp_path / name
        p.write_bytes(b"x")
        os.utime(str(p), (1_000_000 - age, 1_000_000 - age))
    assert restore_drill.newest_backup(str(tmp_path)) == str(tmp_path / "sonarr-20261002.zip")


# ---------------------------------------------------------------- library_guard


def test_flaresolverr_users():
    indexers = [{"id": 1, "enable": True, "tags": [5]}, {"id": 2, "enable": True, "tags": []},
                {"id": 3, "enable": False, "tags": [5]}, {"id": 4, "enable": True, "tags": [6]}]
    proxy = {"implementation": "FlareSolverr", "tags": [5]}

    def users(proxies) -> list:
        return [i["id"] for i in library_guard.flaresolverr_users(indexers, proxies)]

    assert users([proxy]) == [1]
    assert users([dict(proxy, tags=[])]) == [1, 2, 4]          # an untagged proxy applies to every indexer
    assert users([]) == []
    assert users(None) == [1, 4]                               # proxies unknown: the tagged indexers


def test_flaresolverr_reply_ok():
    assert library_guard.flaresolverr_reply_ok('{"status": "ok", "sessions": []}')
    assert not library_guard.flaresolverr_reply_ok('{"status": "error"}')
    assert library_guard.flaresolverr_reply_ok('garbage "status": "ok" garbage')
    assert not library_guard.flaresolverr_reply_ok("")


def test_no_script_shadows_a_standard_library_module():
    assert library_guard.shadowing_files() == []


# ---------------------------------------------------------------- space-saver (off by default) and subtitles


def test_shrink_pick_never_lowers_quality(stack):
    stack.reload()
    shrink = shrink_oversized.Shrink(dry=True)
    shrink.blocklist = {5: {shrink_oversized.norm_title("Elephants.Dream.S01E01.1080p.WEB-DL.x264-DEAD")}}
    current = {"epid": 5, "cap": 45 * 38 * 1e6, "rt": 45, "cur": 3.0e9, "orig": "English"}
    q1080 = {"quality": {"name": "WEBDL-1080p"}}

    def r(group, size, seeders=50, quality=q1080, cf=0, **extra):
        out = {"title": "Elephants.Dream.S01E01.1080p.WEB-DL.x264-%s" % group, "size": size, "seeders": seeders,
               "quality": quality, "customFormatScore": cf}
        out.update(extra)
        return out

    best, thin = r("BIG", 1.6e9), r("THIN", 1.7e9, seeders=4)
    rejected = [r("DEAD", 1.65e9, seeders=80),                                  # blocklisted before
                dict(r("X", 1.62e9, seeders=90), title="Elephants.Dream.S01E01.MULTi.1080p.WEB-DL.x264-X"),
                r("SD", 0.9e9, quality={"quality": {"name": "WEBDL-720p"}}),  # never a lower resolution
                r("TINY", 0.2e9),                                             # implausibly small
                r("LQ", 1.68e9, cf=-100),                                     # rejected by the custom formats
                r("OVER", 1.8e9),                                             # still above the cap
                r("GERMAN", 1.5e9, languages=[{"name": "German"}]),
                dict(r("ITA", 1.5e9), title="Elephants.Dream.S01E01.iTALiAN.1080p.WEB-DL.x264-ITA")]
    assert shrink.pick(current, [best, thin] + rejected) is best
    assert shrink.pick(current, [thin] + rejected) is thin                      # a thin swarm only as a fallback
    assert shrink.pick(current, rejected) is None
    assert shrink.pick(dict(current, cur=1.8e9), [best]) is None               # under 15 % smaller: not worth it


def test_shrink_language_helpers(stack):
    stack.reload()
    assert resolve_shrink_imports.preferred_audio_codes() == {"", "en", "eng"}
    mixed = [{"name": "English"}, {"name": "German"}, {"name": "Unknown"}]
    assert resolve_shrink_imports.foreign_langs(mixed) == ["German"]
    assert resolve_shrink_imports.foreign_langs([{"name": "Hindi"}], "Hindi") == []
    assert (resolve_shrink_imports.is_preferred(None), resolve_shrink_imports.is_preferred("english"),
            resolve_shrink_imports.is_preferred("Hindi")) == (True, True, False)
    rx = shrink_oversized.foreign_tag_rx()
    assert rx.search("Tears.of.Steel.2012.iTALiAN.1080p.WEB-DL") and rx.search("Tears.of.Steel.2012.DUAL.1080p")
    assert not rx.search("Tears.of.Steel.2012.1080p.WEB-DL.x264-GROUP")
    stack.write_env(PREFERRED_LANGUAGE="French")
    assert resolve_shrink_imports.preferred_audio_codes() == {"", "fr", "fre", "fra"}
    assert not shrink_oversized.foreign_tag_rx().search("Sintel.2010.FRENCH.1080p")    # French is wanted now
    stack.write_env(PREFERRED_LANGUAGE="Klingon")
    assert resolve_shrink_imports.preferred_audio_codes() is None                       # cannot judge: say so


@pytest.mark.parametrize("message", ["Not an upgrade for existing episode file(s)",
                                     "Not a Custom Format upgrade for existing episode file(s)",
                                     "Not a quality revision upgrade for existing episode file(s)"])
def test_not_an_upgrade_messages(message):
    assert resolve_shrink_imports.NOT_UPGRADE.search(message)


def test_load_keep(tmp_path):
    for module in (shrink_oversized, resolve_shrink_imports):
        path = tmp_path / ".shrink_keep.json"
        assert module.load_keep(str(path)) == set()            # no file: nothing kept
        path.write_text("[1, 2]")
        assert module.load_keep(str(path)) is None              # not an object: refuse to run
        path.write_text('{"12": "keep", "13": "keep"}')
        assert module.load_keep(str(path)) == {"12", "13"}
        path.unlink()


def test_subtitle_search_params():
    assert subtitle_sweep.search_params({"code2": "fr", "forced": True, "hi": None}) == \
        {"language": "fr", "forced": "true", "hi": "false"}
    assert subtitle_sweep.search_params({}) == {"language": "en", "forced": "false", "hi": "false"}
