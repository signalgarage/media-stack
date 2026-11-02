"""Text helpers for the bot's Telegram messages.

Pure functions only: nothing here reads the environment, files or the network, so the
test-suite can import this module without python-telegram-bot or a running stack.
Messages use Telegram's HTML parse mode, so every piece of outside text goes through esc().
"""
from __future__ import annotations

import html
import re
from datetime import datetime

# Telegram counts message and caption lengths in UTF-16 code units, so an emoji counts twice.
CAPTION_LIMIT = 1024
# New-release cards stay a little under the caption limit: the head lines carry emoji and the
# limit is checked after Telegram parses the HTML, so a small margin avoids a rejected send.
CARD_LIMIT = 1000

_TAG = re.compile(r"<[^>]+>")
_PLAN_SUFFIX = re.compile(r"\s+(?:(?:basic|standard)\s+with\s+ads|with\s+ads|premium|essential)$", re.I)
_PLUS_SUFFIX = re.compile(r"\s+plus$", re.I)
_FRACTION = re.compile(r"(\.\d{6})\d+")


def esc(s) -> str:
    """Escape text for Telegram's HTML parse mode (&, < and >)."""
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def tg_len(text) -> int:
    """Length of a plain string the way Telegram counts it (UTF-16 code units)."""
    return len(str(text or "").encode("utf-16-le")) // 2


def plain_len(html_text) -> int:
    """Telegram length of an HTML message once the tags are gone and entities are decoded."""
    return tg_len(html.unescape(_TAG.sub("", html_text or "")))


def fmt_dur(sec) -> str:
    """Seconds -> '42 min' or '2h 05m'."""
    m = int((sec or 0) // 60)
    return f"{m} min" if m < 60 else f"{m // 60}h {m % 60:02d}m"


def fmt_bytes(n) -> str:
    """Bytes -> '850 GB' or '1.2 TB' (decimal units, like disk vendors and df -H)."""
    try:
        n = float(n or 0)
    except (TypeError, ValueError):
        n = 0.0
    if n >= 1e12:
        return f"{n / 1e12:.1f} TB"
    return f"{n / 1e9:.0f} GB"


def bar(pct) -> str:
    """Ten-step progress bar for a percentage."""
    try:
        f = max(0, min(10, int(round(float(pct) / 10))))
    except (TypeError, ValueError):
        f = 0
    return "▰" * f + "▱" * (10 - f)


def norm_title(t) -> str:
    """Release title normalised for comparison: lower case, single spaces."""
    return " ".join(str(t or "").lower().split())


def parse_iso(s) -> datetime:
    """ISO-8601 timestamp from the apps' APIs -> aware datetime.

    Radarr and Sonarr write 'Z' for UTC and sometimes seven fraction digits, which older
    Python versions refuse; both are normalised first. Raises ValueError on bad input.
    """
    s = str(s or "").strip().replace("Z", "+00:00")
    return datetime.fromisoformat(_FRACTION.sub(r"\1", s))


def critics_line(rt, imdb):
    """(verdict, scores) in plain words from the published scores, never a made-up opinion.

    rt is Jellyseerr's Rotten Tomatoes block (criticsScore, criticsRating, audienceScore),
    imdb its IMDb block (criticsScore holds the 0-10 user rating). Critics come first; the
    audience and IMDb numbers are listed after them.
    """
    parts = []
    c = (rt or {}).get("criticsScore")
    if isinstance(c, (int, float)) and not isinstance(c, bool):
        word = ("Critics loved it" if c >= 85 else "Critics mostly liked it" if c >= 70
                else "Critics were split" if c >= 50 else "Critics mostly didn't like it")
        label = (rt or {}).get("criticsRating")
        parts.append(f"Rotten Tomatoes critics {c}%" + (f" ({label})" if label else ""))
    else:
        word = "No critic reviews yet"
    a = (rt or {}).get("audienceScore")
    if a is not None:
        parts.append(f"audience {a}%")
    i = (imdb or {}).get("criticsScore")
    if i:
        parts.append(f"IMDb {i}/10")
    return word, " · ".join(parts)


def show_name(name) -> str:
    """Short display form of a provider name: 'Example Plus' -> 'Example+'."""
    name = " ".join(str(name or "").split())
    return _PLUS_SUFFIX.sub("+", name)


def clean_service(name, known=()) -> str:
    """Display name of a streaming provider without plan or reseller suffixes.

    'Example Standard with Ads' -> 'Example'. A reseller listing such as
    'Example Plus Some Store Channel' folds into 'Example+' when the plain 'Example Plus'
    is one of the `known` provider names of the same title; otherwise it is kept whole,
    because the length of a store's name cannot be guessed safely.
    """
    name = " ".join(str(name or "").split())
    low = name.lower()
    if low.endswith(" channel"):
        for k in sorted((" ".join(str(x or "").split()) for x in known), key=len, reverse=True):
            if k and low != k.lower() and low.startswith(k.lower() + " "):
                name = k
                break
    prev = None
    while prev != name:
        prev, name = name, _PLAN_SUFFIX.sub("", name).strip()
    return show_name(name or prev)


def trim_caption(head_lines, about, tail, limit=CARD_LIMIT) -> str:
    """Join a card: head lines, the story (shortened to fit) and the tail line.

    head_lines and tail are HTML; about is plain text and is escaped here. The story is cut
    at a word boundary so the whole card stays within `limit` Telegram characters, and is left
    out when too little room remains for it to be useful.
    """
    about = " ".join(str(about or "").split())
    room = limit - plain_len("\n".join(list(head_lines) + [tail])) - 8
    if tg_len(about) > room:
        if room < 40:
            about = ""
        else:
            cut = about[: max(room - 1, 0)]
            while cut and tg_len(cut) > room - 1:
                cut = cut[:-1]
            about = (cut.rsplit(" ", 1)[0] if " " in cut else cut) + "…"
    lines = list(head_lines) + ([f"📝 {esc(about)}"] if about else []) + [tail]
    return "\n".join(lines)


def ep_codes(pairs, limit=8) -> str:
    """{(1, 1), (1, 2), (1, 3), (2, 5)} -> 'S01E01-E03, S02E05'.

    Consecutive episodes of a season collapse into a range; past `limit` ranges the rest is
    summarised as '+N more'.
    """
    eps = sorted({(int(s), int(e)) for s, e in pairs})
    runs = []
    for s, e in eps:
        if runs and runs[-1][0] == s and runs[-1][2] == e - 1:
            runs[-1][2] = e
        else:
            runs.append([s, e, e])
    parts = [f"S{s:02d}E{a:02d}" + (f"-E{b:02d}" if b != a else "") for s, a, b in runs]
    if len(parts) > limit:
        rest = sum(b - a + 1 for _, a, b in runs[limit:])
        parts = parts[:limit] + [f"+{rest} more"]
    return ", ".join(parts)
