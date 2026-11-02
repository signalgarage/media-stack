"""moviebot/botlib/text.py: the helpers behind the bot's Telegram messages."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest
from botlib import text as T

# ---------------------------------------------------------------- scores


@pytest.mark.parametrize("score, verdict", [
    (100, "Critics loved it"), (85, "Critics loved it"),
    (84, "Critics mostly liked it"), (70, "Critics mostly liked it"),
    (69, "Critics were split"), (50, "Critics were split"),
    (49, "Critics mostly didn't like it"), (0, "Critics mostly didn't like it"),
])
def test_critics_line_verdict_bands(score, verdict):
    assert T.critics_line({"criticsScore": score}, None)[0] == verdict


def test_critics_line_lists_the_published_scores():
    full = T.critics_line({"criticsScore": 90, "criticsRating": "Certified Fresh", "audienceScore": 80},
                          {"criticsScore": 7.5})
    assert full == ("Critics loved it", "Rotten Tomatoes critics 90% (Certified Fresh) · audience 80% · IMDb 7.5/10")
    assert T.critics_line(None, None) == ("No critic reviews yet", "")
    assert T.critics_line({"criticsScore": True, "audienceScore": 0}, {"criticsScore": 0}) == \
        ("No critic reviews yet", "audience 0%")                  # a bool is no score; 0% is still a score
    assert T.critics_line({}, {"criticsScore": 6.1}) == ("No critic reviews yet", "IMDb 6.1/10")


# ---------------------------------------------------------------- numbers and bars


@pytest.mark.parametrize("seconds, shown", [
    (0, "0 min"), (59, "0 min"), (60, "1 min"), (3599, "59 min"),
    (3600, "1h 00m"), (3900, "1h 05m"), (7325, "2h 02m"), (None, "0 min"),
])
def test_fmt_dur(seconds, shown):
    assert T.fmt_dur(seconds) == shown


@pytest.mark.parametrize("pct, filled", [
    (0, 0), (46, 5), (70, 7), ("70", 7), (100, 10), (150, 10), (-3, 0), (None, 0), ("junk", 0),
])
def test_bar(pct, filled):
    bar = T.bar(pct)
    assert len(bar) == 10
    assert bar == "▰" * filled + "▱" * (10 - filled)


def test_fmt_bytes():
    assert T.fmt_bytes(850e9) == "850 GB"
    assert T.fmt_bytes(1.24e12) == "1.2 TB"
    assert T.fmt_bytes(None) == "0 GB" and T.fmt_bytes("junk") == "0 GB"


# ---------------------------------------------------------------- text


def test_esc():
    assert T.esc("Sintel & <b>friends</b>") == "Sintel &amp; &lt;b&gt;friends&lt;/b&gt;"
    assert T.esc(None) == "" and T.esc(0) == "0"


def test_norm_title():
    assert T.norm_title("  Big  Buck\tBUNNY ") == "big buck bunny"
    assert T.norm_title(None) == ""


def test_telegram_lengths_count_utf16_units():
    assert T.tg_len("🆕") == 2 and T.tg_len("abc") == 3 and T.tg_len(None) == 0
    assert T.plain_len("<b>A &amp; B</b>") == 5


@pytest.mark.parametrize("name, shown", [
    ("Example Plus", "Example+"), ("Example  plus", "Example+"), ("  Example   Plus  ", "Example+"),
    ("Plus", "Plus"), ("Exampleplus", "Exampleplus"), ("Example Plus Extra", "Example Plus Extra"), (None, ""),
])
def test_show_name(name, shown):
    assert T.show_name(name) == shown


def test_clean_service():
    assert T.clean_service("Example Standard with Ads") == "Example"
    assert T.clean_service("Example Basic with Ads") == "Example"
    assert T.clean_service("Example Premium Plus") == "Example Premium+"
    assert T.clean_service("Example Plus Some Store Channel", ["Example Plus"]) == "Example+"
    assert T.clean_service("Lonely Store Channel") == "Lonely Store Channel"     # unknown store names stay whole
    assert T.clean_service("Premium") == "Premium"                               # never empties a name


def test_ep_codes():
    assert T.ep_codes({(1, 1), (1, 2), (1, 3), (2, 5)}) == "S01E01-E03, S02E05"
    assert T.ep_codes([("1", "2")]) == "S01E02"
    assert T.ep_codes([]) == ""
    assert T.ep_codes({(1, e) for e in range(1, 30, 2)}, limit=3) == "S01E01, S01E03, S01E05, +12 more"


# ---------------------------------------------------------------- timestamps


@pytest.mark.parametrize("stamp", [
    "2026-10-31T12:34:56Z",
    "2026-10-31T12:34:56.1234567Z",       # seven fraction digits, as Radarr and Sonarr write them
    "2026-10-31T12:34:56.123Z",
    "2026-10-31T14:34:56+02:00",
])
def test_parse_iso_gives_aware_datetimes(stamp):
    parsed = T.parse_iso(stamp)
    assert parsed.tzinfo is not None
    expected = datetime(2026, 10, 31, 12, 34, 56, tzinfo=timezone.utc)
    assert parsed.astimezone(timezone.utc).replace(microsecond=0) == expected


@pytest.mark.parametrize("bad", ["garbage", "", None])
def test_parse_iso_rejects_garbage(bad):
    with pytest.raises(ValueError):
        T.parse_iso(bad)


# ---------------------------------------------------------------- new-release cards

HEAD = ["🆕🎬 <b>Cosmos Laundromat</b> (2015)", "Animation · 0h 12m", "🎭 <b>Starring:</b> A, B"]
TAIL = "⭐ <b>Critics loved it</b>"


@pytest.mark.parametrize("story", [
    "word " * 400,
    "🎬" * 900,                            # emoji count twice in Telegram's limit
    "Sintel & <the> dragon " * 200,        # escaped entities are longer than the text Telegram counts
    "x" * 3000,                            # no space to cut at
])
def test_trim_caption_fits_a_telegram_caption(story):
    card = T.trim_caption(HEAD, story, TAIL)
    assert T.plain_len(card) <= T.CARD_LIMIT <= T.CAPTION_LIMIT == 1024
    assert card.startswith(HEAD[0]) and card.endswith(TAIL)
    assert card.count("…") == 1


def test_trim_caption_keeps_a_short_story_whole_and_escaped():
    card = T.trim_caption(HEAD, "A dragon & a girl.", TAIL)
    assert "📝 A dragon &amp; a girl." in card and "…" not in card
    assert T.trim_caption(HEAD, "", TAIL) == "\n".join(HEAD + [TAIL])


def test_trim_caption_drops_the_story_when_there_is_no_room():
    card = T.trim_caption(["H" * 990], "some story " * 20, "tail")
    assert "📝" not in card
    assert T.plain_len(T.trim_caption(["h"], "a" * 2000, "t", limit=1024)) <= 1024
