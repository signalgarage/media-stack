"""moviebot/botlib/releases.py: the rules the bot applies before it grabs, offers or removes anything.

Releases are shaped like the JSON Radarr and Sonarr return from /api/v3/release; the titles are
made up from open films.
"""
from __future__ import annotations

import ast
import importlib
import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from botlib import releases as R

REPO = Path(__file__).resolve().parent.parent
GB = 1e9


def rel(title, quality="WEBDL-1080p", resolution=1080, seeders=10, size=2 * GB, **extra):
    r = {"title": title, "quality": {"quality": {"name": quality, "resolution": resolution}},
         "seeders": seeders, "size": size}
    r.update(extra)
    return r


# ---------------------------------------------------------------- cinema recordings


def test_quality_name_and_resolution_tolerate_missing_fields():
    assert R.rel_quality_name(rel("Sintel.2010.1080p.WEB-DL.x264-GROUP")) == "WEBDL-1080p"
    assert R.rel_resolution(rel("Sintel.2010.720p.WEB-DL.x264-GROUP", resolution=720)) == 720
    assert R.rel_quality_name({}) == "?" and R.rel_quality_name(None) == "?"
    assert R.rel_resolution({"quality": None}) is None


@pytest.mark.parametrize("quality", ["CAM", "TELESYNC", "TELECINE", "WORKPRINT", "DVDSCR", "REGIONAL"])
def test_is_cam_by_quality(quality):
    assert R.is_cam(rel("Sintel.2010.x264-GROUP", quality=quality))


@pytest.mark.parametrize("token", ["HDCAM", "CAMRip", "HQCAM", "HDTS", "TeleSync", "HDTC", "TeleCine",
                                   "DVDScr", "Screener", "WORKPRINT", "PDVD", "PreDVD"])
def test_is_cam_by_title_token(token):
    assert R.is_cam(rel("Sintel.2010.%s.x264-GROUP" % token))


@pytest.mark.parametrize("title", [
    "Elephants.Dream.Cam.2006.1080p.WEB-DL.x264-GROUP",          # the plain word "Cam" in a title
    "Big.Buck.Bunny.2008.Shorts.1080p.WEB-DL.x264-GROUP",        # "ts" inside a word
    "Sintel.2010.Highlights.and.Credits.1080p.WEB-DL.x264-GROUP",
    "Tears.of.Steel.2012.1080p.BluRay.DTS-HD.MA.5.1.x264-GROUP",  # DTS audio is not a telesync
    "Cosmos.Laundromat.2015.Directors.Cuts.1080p.WEB-DL.x264-GROUP",
])
def test_is_cam_false_positive_guards(title):
    assert not R.is_cam(rel(title))


def test_is_cam_on_empty_input():
    assert not R.is_cam({}) and not R.is_cam(None)


# ---------------------------------------------------------------- ranking


def test_pick_closest_ranks_seeders_then_resolution_then_size():
    hd = rel("Sintel.2010.1080p.WEB-DL.x264-GROUP", seeders=40, size=3 * GB)
    hd_small = rel("Sintel.2010.1080p.WEB-DL.x265-GROUP", seeders=40, size=2 * GB)
    uhd = rel("Sintel.2010.2160p.WEB-DL.x265-GROUP", quality="WEBDL-2160p", resolution=2160, seeders=40)
    hd7 = rel("Sintel.2010.720p.WEB-DL.x264-GROUP", quality="WEBDL-720p", resolution=720, seeders=40)
    sd = rel("Sintel.2010.DVDRip.x264-GROUP", quality="DVD", resolution=480, seeders=40)
    popular = rel("Sintel.2010.720p.HDTV.x264-GROUP", quality="HDTV-720p", resolution=720, seeders=90)
    assert R.pick_closest([sd, hd7, uhd, hd, hd_small, popular], limit=6) == [popular, hd_small, hd, uhd, hd7, sd]
    assert R.pick_closest([sd, hd7, uhd]) == [uhd, hd7, sd]
    assert R.pick_closest([hd, uhd, hd7], limit=1) == [hd]
    assert R.pick_closest(None) == []


@pytest.mark.parametrize("bad", [None, "12", True])
def test_seeders_that_are_not_numbers_count_as_none(bad):
    odd = rel("Sintel.2010.1080p.WEB-DL.x264-ODD", seeders=bad)
    real = rel("Sintel.2010.720p.WEB-DL.x264-GROUP", quality="WEBDL-720p", resolution=720, seeders=1)
    assert R.pick_closest([odd, real]) == [real, odd]


def test_closest_clean_first_drops_cams_and_lists_clean_releases_first():
    web = rel("Big.Buck.Bunny.2008.1080p.WEB-DL.x264-GROUP", seeders=40)
    hd7 = rel("Big.Buck.Bunny.2008.720p.WEB-DL.x264-GROUP", quality="WEBDL-720p", resolution=720, seeders=40)
    cam = rel("Big.Buck.Bunny.2008.HDCAM.x264-GROUP", seeders=900)
    negative = rel("Big.Buck.Bunny.2008.1080p.WEB-DL.x264-LQGRP", seeders=300, customFormatScore=-10000)
    assert R.closest_clean_first([cam, negative, web, hd7]) == [web, hd7, negative]
    assert R.closest_clean_first([cam]) == []


# ---------------------------------------------------------------- custom formats


def test_cf_score_and_cf_negative():
    assert R.cf_score({"customFormatScore": -5}) == -5
    assert R.cf_score({"customFormatScore": True}) is None       # a bool is not a score
    assert R.cf_score({}) is None and R.cf_score(None) is None
    assert R.cf_negative({"customFormatScore": -1})
    assert not R.cf_negative({"customFormatScore": 0})
    assert not R.cf_negative({})                                  # no score at all counts as clean


def test_cf_flag_marks_rejected_releases_and_escapes_names():
    clean = rel("Sintel.2010.1080p.WEB-DL.x264-GROUP", customFormatScore=0)
    assert R.cf_flag(clean) == ""
    negative = rel("Sintel.2010.1080p.WEB-DL.x264-LQGRP", customFormatScore=-10000,
                   customFormats=[{"name": "LQ"}, {"name": "<b>"}, "Plain", {"name": "Fourth"}])
    flag = R.cf_flag(negative)
    assert "rejected by your custom formats" in flag and "-10000" in flag
    assert "&lt;b&gt;" in flag and "<b>" not in flag.replace("<b>rejected", "")
    assert "Fourth" not in flag                                    # at most three names
    assert "custom formats)" in R.cf_flag(rel("x", customFormatScore=-1))


def test_lang_note():
    english = rel("Sintel.2010.1080p.WEB-DL.x264-GROUP", languages=[{"name": "English"}])
    both = rel("Sintel.2010.1080p.WEB-DL.x264-GROUP", languages=[{"name": "French"}, {"name": "English"}])
    assert R.lang_note(english) == ""
    assert R.lang_note(both) == " · 🗣 French, English"
    assert R.lang_note(both, accepted=("French", "English")) == ""
    assert R.lang_note(rel("x", languages=[{"name": "Unknown"}])) == ""


# ---------------------------------------------------------------- season packs


@pytest.mark.parametrize("title, default, seasons, expected", [
    ("Elephants.Dream.S01-S03.1080p.WEB-DL.x264-GROUP", 1, [1, 2, 3, 4], {1, 2, 3}),
    ("Elephants.Dream.S02-03.1080p.WEB-DL.x264-GROUP", 2, [1, 2, 3], {2, 3}),
    ("Elephants.Dream.S03.1080p.WEB-DL.x264-GROUP", 3, [1, 2, 3], {3}),         # not a range
    ("Elephants Dream Seasons 1 to 2 1080p", 1, [1, 2, 3], {1, 2}),
    ("Elephants.Dream.Complete.Series.1080p.WEB-DL", 1, [1, 2, 3], {1, 2, 3}),
    ("Elephants.Dream.S03-S01.1080p", 3, [1, 2, 3], {3}),                      # backwards range ignored
    ("", 2, [1, 2], {2}),
])
def test_covered_seasons(title, default, seasons, expected):
    assert R.covered_seasons(title, default, seasons) == expected


def test_season_complete():
    now = datetime(2026, 10, 31, tzinfo=timezone.utc)
    later = (now + timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S.1234567Z")
    eps = [{"seasonNumber": 1, "airDateUtc": "2026-01-01T00:00:00Z"},
           {"seasonNumber": 1, "airDateUtc": None},
           {"seasonNumber": 2, "airDateUtc": "2026-01-01T00:00:00Z"},
           {"seasonNumber": 2, "airDateUtc": later}]
    assert R.season_complete(eps, 1, now)
    assert not R.season_complete(eps, 2, now)        # an episode still to come
    assert not R.season_complete(eps, 3, now)        # no episode rows yet: still airing


def test_choose_season_pack_never_picks_a_format_negative_pack():
    clean = rel("Elephants.Dream.S01.1080p.WEB-DL.x264-GROUP", fullSeason=True, seeders=20)
    negative = rel("Elephants.Dream.S01.1080p.WEB-DL.x264-LQGRP", fullSeason=True, seeders=200,
                   customFormatScore=-10000)
    weak = rel("Elephants.Dream.S01.720p.WEB-DL.x264-GROUP", fullSeason=True, seeders=R.PACK_MIN_SEEDERS - 1)
    single = rel("Elephants.Dream.S01E01.1080p.WEB-DL.x264-GROUP", seeders=500)
    best, packs, dropped = R.choose_season_pack([negative, clean, weak, single], 1, [1, 2], set())
    assert best is clean
    assert packs == [negative, clean, weak]
    assert dropped == 1
    assert R.choose_season_pack([negative, weak], 1, [1], set())[0] is None


def test_choose_season_pack_prefers_a_pack_without_seasons_already_covered():
    overlap = rel("Elephants.Dream.S01-S02.1080p.WEB-DL.x264-GROUP", fullSeason=True, seeders=90)
    clean = rel("Elephants.Dream.S01.1080p.WEB-DL.x264-GROUP", fullSeason=True, seeders=20)
    assert R.choose_season_pack([overlap, clean], 1, [1, 2], {2})[0] is clean
    assert R.choose_season_pack([overlap], 1, [1, 2], {2})[0] is overlap    # nothing better: still a pack


# ---------------------------------------------------------------- grab criteria

PROFILE = {"name": "HD-1080p", "minFormatScore": 0, "items": [
    {"allowed": True, "quality": {"name": "Bluray-1080p"}},
    {"allowed": True, "name": "WEB 1080p", "items": [{"quality": {"name": "WEBDL-1080p"}},
                                                     {"quality": {"name": "WEBRip-1080p"}}]},
    {"allowed": False, "quality": {"name": "HDTV-720p"}}]}
QDEFS = {"WEBDL-1080p": {"minSize": 1, "maxSize": 25}}
MOVIE = {"runtime": 100}
GIB = 1024 ** 3


def test_allowed_qualities_includes_group_members():
    assert R.allowed_qualities(PROFILE) == {"Bluray-1080p", "WEBDL-1080p", "WEBRip-1080p"}
    assert R.allowed_qualities(None) == set()


def test_grab_problems():
    good = {"quality": "WEBDL-1080p", "size": 2 * GIB, "customFormatScore": 0}
    assert R.grab_problems(good, MOVIE, PROFILE, QDEFS) == []
    too_big = R.grab_problems(dict(good, size=3 * GIB), MOVIE, PROFILE, QDEFS)
    assert too_big == ["too big: 3.00 GB, maximum for 100 min is 2.44 GB"]
    assert R.grab_problems(dict(good, size=50 * 1024 ** 2), MOVIE, PROFILE, QDEFS)[0].startswith("too small")
    wrong_quality = R.grab_problems(dict(good, quality="HDTV-720p"), MOVIE, PROFILE, QDEFS)
    assert wrong_quality == ["quality HDTV-720p is not allowed in the 'HD-1080p' profile"]
    low_score = R.grab_problems(dict(good, customFormatScore=-10000, customFormats=["LQ"]), MOVIE, PROFILE, QDEFS)
    assert low_score == ["custom-format score -10000 is below the minimum 0 (LQ)"]
    assert R.grab_problems(dict(good, size=99 * GIB), {"runtime": 0}, None, QDEFS) == []   # no runtime: no size rule


# ---------------------------------------------------------------- missing-episode filler

CAP = 2.5 * GB                                   # EPISODE_CAP_GB as the bot passes it
SIZE_ONLY = ["2.1 GB is larger than maximum allowed 1.8 GB"]


def ep(number, size=1.2 * GB, seeders=20, rejections=SIZE_ONLY, resolution=1080, title=None, numbers=None):
    return rel(title or "Tears.of.Steel.2012.S01E%02d.1080p.WEB-DL.x264-GROUP" % number, resolution=resolution,
               size=size, seeders=seeders, episodeNumbers=numbers or [number], rejections=list(rejections))


def pack(size=12 * GB, seeders=30, rejections=(), resolution=1080):
    return rel("Tears.of.Steel.2012.S01.1080p.WEB-DL.x264-GROUP", resolution=resolution, size=size,
               seeders=seeders, fullSeason=True, rejections=list(rejections))


def test_fill_usable_takes_copies_refused_for_size_alone():
    assert R.fill_usable(ep(1), {1, 2}, 8, 1080, CAP)
    assert R.fill_usable(ep(1, rejections=[]), {1}, 8, 1080, CAP)
    assert not R.fill_usable(ep(1, rejections=SIZE_ONLY + ["Unknown Series"]), {1}, 8, 1080, CAP)
    assert not R.fill_usable(ep(1, rejections=["Not an upgrade for existing episode file(s)"]), {1}, 8, 1080, CAP)


def test_fill_usable_keeps_the_flat_cap_per_episode():
    assert R.fill_usable(ep(1, size=2.4 * GB), {1}, 8, 1080, CAP)
    assert not R.fill_usable(ep(1, size=3 * GB), {1}, 8, 1080, CAP)
    assert R.fill_usable(ep(1, size=4.8 * GB, numbers=[1, 2]), {1, 2}, 8, 1080, CAP)    # 2.4 GB each
    assert R.fill_usable(pack(size=20 * GB), {1}, 8, 1080, CAP)                        # 2.5 GB each
    assert not R.fill_usable(pack(size=21 * GB), {1}, 8, 1080, CAP)


def test_fill_usable_other_rules():
    assert not R.fill_usable(ep(1, seeders=R.FILL_MIN_SEEDERS - 1), {1}, 8, 1080, CAP)
    assert R.fill_usable(ep(1, seeders=R.FILL_MIN_SEEDERS), {1}, 8, 1080, CAP)
    assert not R.fill_usable(ep(1, title="Tears.of.Steel.2012.S01E01.HDCAM.x264-GROUP"), {1}, 8, 1080, CAP)
    assert not R.fill_usable(ep(1, resolution=720), {1}, 8, 1080, CAP)                  # wrong resolution
    assert not R.fill_usable(ep(1, numbers=[1, 2]), {1}, 8, 1080, CAP)                  # brings an unwanted episode
    no_numbers = dict(ep(1), episodeNumbers=[])
    assert not R.fill_usable(no_numbers, {1}, 8, 1080, CAP)


def test_fill_usable_720p_only_on_the_users_tap():
    not_wanted = ["WEBDL-720p is not wanted in profile"]
    assert R.fill_usable(ep(1, resolution=720, rejections=not_wanted), {1}, 8, 720, CAP)
    assert not R.fill_usable(ep(1, rejections=not_wanted), {1}, 8, 1080, CAP)


def test_fill_usable_lets_a_pack_carry_owned_episodes():
    owned = ["Existing file on disk is of equal or higher preference"]
    assert R.fill_usable(pack(rejections=owned), {1}, 8, 1080, CAP)
    assert not R.fill_usable(ep(1, rejections=owned), {1}, 8, 1080, CAP)       # a single copy may not


def test_fill_pick_singles_for_a_few_gaps():
    one, two = ep(1), ep(2)
    picks, covered = R.fill_pick([one, two, pack()], {1, 2}, 8, 1080, CAP)
    assert picks == [one, two] and covered == {1, 2}


def test_fill_pick_prefers_seeders_then_the_smaller_copy():
    weak, strong = ep(1, seeders=6, size=1.0 * GB), ep(1, seeders=9, size=2.0 * GB)
    assert R.fill_pick([weak, strong], {1}, 8, 1080, CAP)[0] == [strong]
    big, small = ep(1, seeders=50, size=2.0 * GB), ep(1, seeders=12, size=1.0 * GB)   # both "well seeded"
    assert R.fill_pick([big, small], {1}, 8, 1080, CAP)[0] == [small]


def test_fill_pick_one_copy_covers_a_double_episode():
    double = ep(1, size=2.0 * GB, numbers=[1, 2])
    picks, covered = R.fill_pick([double, ep(2)], {1, 2}, 8, 1080, CAP)
    assert picks == [double] and covered == {1, 2}


def test_fill_pick_takes_a_pack_for_a_whole_missing_season():
    full = pack(seeders=R.FILL_PACK_SEEDERS)
    picks, covered = R.fill_pick([full, ep(1)], set(range(1, 9)), 8, 1080, CAP)
    assert picks == [full] and covered == set(range(1, 9))
    thin = pack(seeders=R.FILL_PACK_SEEDERS - 1)
    assert R.fill_pick([thin], set(range(1, 9)), 8, 1080, CAP) == ([], set())


def test_fill_pick_uses_a_pack_only_when_half_the_season_is_missing():
    full = pack()
    assert R.fill_pick([full], {1, 2, 3, 4}, 8, 1080, CAP) == ([full], {1, 2, 3, 4})
    assert R.fill_pick([full], {1, 2, 3}, 8, 1080, CAP) == ([], set())


# ---------------------------------------------------------------- fake downloads

ARCHIVE = "was a packed archive, not a video"


def test_fake_reason():
    video = {"name": "Sintel.2010.1080p.WEB-DL.x264-GROUP/Sintel.2010.1080p.WEB-DL.x264-GROUP.mkv", "size": 1.5 * GB}
    assert R.fake_reason([video, {"name": "Sintel/setup.exe", "size": 1e6}]) is None       # real video + stray program
    program = R.fake_reason([{"name": "Sintel.2010.1080p.WEB-DL.x264-GROUP.exe", "size": 2e6}])
    assert program.startswith("had a program file instead of a video (.exe)")
    assert R.fake_reason([{"name": "Sintel.2010.1080p.part01.rar", "size": 1 * GB}]) == ARCHIVE
    assert R.fake_reason([{"name": "ubuntu-24.04-desktop-amd64.iso", "size": 5 * GB}]) == ARCHIVE
    assert R.fake_reason([{"name": "Visit.Our.Site.com", "size": 100}]) is None            # '.com' here is a web site
    sample_only = [{"name": "Sintel.2010.sample.mkv", "size": 20e6}, {"name": "Sintel.2010.1080p.zip", "size": 1e9}]
    assert R.fake_reason(sample_only) == ARCHIVE                                           # a sample is not the video
    assert R.fake_reason([]) is None and R.fake_reason(None) is None                       # metadata not there yet


def test_queue_fake_reason():
    assert R.queue_fake_reason([{"messages": ["Found unsupported extension: '.rar'"]}]) == ARCHIVE
    assert R.queue_fake_reason([{"messages": ["No files found are eligible for import"]}]) is None
    assert R.queue_fake_reason(None) is None


# ---------------------------------------------------------------- qBittorrent records


def test_torrent_finished():
    assert R.torrent_finished({"state": "stalledUP", "progress": 1})
    assert R.torrent_finished({"state": "uploading", "progress": 1.0})
    assert R.torrent_finished({"state": "queuedUP", "progress": 0.5})       # any *UP state is past downloading
    assert R.torrent_finished({"state": "moving", "progress": 0.2})
    assert R.torrent_finished({"state": "pausedDL", "progress": 0.99995})
    assert not R.torrent_finished({"state": "downloading", "progress": 0.5})
    assert not R.torrent_finished({"state": "stalledDL", "progress": "junk"})


def test_torrent_dead():
    t0 = 1_000_000.0
    dead = {"state": "stalledDL", "num_complete": 0, "num_incomplete": 0, "progress": 0, "added_on": t0}
    assert R.torrent_dead(dead, t0 + 31 * 60, 30)
    assert not R.torrent_dead(dead, t0 + 10 * 60, 30)                         # still within the grace time
    assert not R.torrent_dead(dict(dead, num_complete=2), t0 + 3600, 30)      # seeders exist
    assert not R.torrent_dead(dict(dead, num_incomplete=3), t0 + 3600, 30)    # peers exist
    assert not R.torrent_dead(dict(dead, progress=0.1), t0 + 3600, 30)        # something arrived
    assert not R.torrent_dead(dict(dead, state="downloading"), t0 + 3600, 30)


def test_pick_profile():
    profiles = [{"id": 4, "name": "Any"}, {"id": 5, "name": "Ultra-HD"}, {"id": 6, "name": "HD-1080p"}]
    assert R.pick_profile(profiles) == 6
    assert R.pick_profile([{"id": 4, "name": "Any"}, {"id": 7, "name": "My 1080p"}]) == 7
    assert R.pick_profile([{"id": 4, "name": "Any"}]) == 4
    assert R.pick_profile([]) is None


# ---------------------------------------------------------------- user picks


def test_force_picks_take_discard_and_expiry():
    now = [1000.0]
    picks = R.ForcePicks(ttl=100, clock=lambda: now[0])
    picks.add(5, "Sintel.2010.1080p.WEB-DL.x264-GROUP", 123)
    assert 5 in picks
    assert not picks.take(5, "Sintel.2010.720p.WEB-DL.x264-OTHER", 999)   # another release is not the pick
    assert 5 in picks                                                     # ...and does not use it up
    assert picks.take(5, "  sintel.2010.1080p.web-dl.x264-group ", None)  # normalised title, once
    assert 5 not in picks and not picks.take(5, "Sintel.2010.1080p.WEB-DL.x264-GROUP", 123)

    picks.add(6)                                                          # any release of movie 6
    assert picks.take(6, "Anything-GROUP", 1)
    picks.add(7, "Sintel.2010.Other-GROUP", 4242)
    assert picks.take(7, None, 4242)                                      # the exact size, no title

    picks.add(8, "Sintel.2010.A-GROUP")
    picks.add(8, "Sintel.2010.B-GROUP")
    picks.discard(8, "sintel.2010.a-group")
    assert 8 in picks
    assert not picks.take(8, "Sintel.2010.A-GROUP")
    assert picks.take(8, "Sintel.2010.B-GROUP")
    picks.add(9, "x")
    picks.add(9, "y")
    picks.discard(9)
    assert 9 not in picks

    picks.add(10, "Sintel.2010.C-GROUP")
    now[0] += 101
    assert 10 not in picks and not picks.take(10, "Sintel.2010.C-GROUP")


# ---------------------------------------------------------------- the bot uses what botlib defines


@pytest.mark.skipif(sys.version_info < (3, 12), reason="bot.py is written for the Python 3.12 of its container")
def test_bot_imports_only_names_that_botlib_defines():
    tree = ast.parse((REPO / "moviebot" / "bot.py").read_text(encoding="utf-8"))
    imported = 0
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "botlib":
            module = importlib.import_module(node.module)
            for alias in node.names:
                found = hasattr(module, alias.name) or importlib.util.find_spec("%s.%s" % (node.module, alias.name))
                assert found, "bot.py imports %s from %s, which does not define it" % (alias.name, node.module)
                imported += 1
    assert imported > 20
