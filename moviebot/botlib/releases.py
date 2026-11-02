"""Release and torrent rules the bot applies before it grabs, offers or removes anything.

Pure functions only: every value (size caps, the time, the profile) is passed in, nothing is
read from the environment, files or the network. The inputs are the JSON objects that
Radarr/Sonarr's /api/v3/release and qBittorrent's /api/v2/torrents endpoints return.
"""
from __future__ import annotations

import os
import re
import time
from datetime import datetime

from .text import esc, norm_title, parse_iso

GIB = 1073741824

# ---------------------------------------------------------------- cinema recordings
# A movie still in cinemas usually has only CAM/TS copies, and those carry the most seeders,
# so every seeder-ranked list would put them first. They are never offered or auto-picked;
# Radarr keeps monitoring and grabs a proper copy once one is released. Radarr's own quality
# parse catches most of them; the title tokens catch the ones it parses as something else.
CAM_QUALITIES = {"cam", "telesync", "telecine", "workprint", "dvdscr", "regional"}
CAM_TOKENS = {"hdcam", "camrip", "hqcam", "hdts", "telesync", "hdtc", "telecine", "dvdscr", "screener",
              "workprint", "pdvd", "predvd"}


def rel_quality_name(r) -> str:
    """'WEBDL-1080p' from a release's nested quality object ('?' when missing)."""
    return (((r or {}).get("quality") or {}).get("quality") or {}).get("name") or "?"


def rel_resolution(r):
    """1080 / 720 / 2160 from a release's quality object, or None."""
    return (((r or {}).get("quality") or {}).get("quality") or {}).get("resolution")


def is_cam(r) -> bool:
    if rel_quality_name(r).lower() in CAM_QUALITIES:
        return True
    return any(w in CAM_TOKENS for w in re.split(r"[^a-z0-9]+", ((r or {}).get("title") or "").lower()))


def _seeders(r) -> int:
    s = (r or {}).get("seeders")
    return s if isinstance(s, int) and not isinstance(s, bool) else 0


def pick_closest(rels, limit=3):
    """Rank releases: real seeders first (health), then closeness to 1080p, then the smaller size."""
    def rank(r):
        q = rel_quality_name(r).lower()
        prox = 0 if "1080" in q else (1 if ("2160" in q or "4k" in q) else (2 if "720" in q else 3))
        return (-_seeders(r), prox, r.get("size") or 0)
    return sorted(rels or [], key=rank)[:limit]


# ---------------------------------------------------------------- custom-format guard
# Radarr/Sonarr score every release with the custom formats configured in them (low-quality
# groups, wrong-language tags, ... at -10000). A release scored BELOW 0 is one the settings
# reject. POST /api/v3/release grabs WITHOUT re-checking that, so the bot must never auto-pick
# one, and the manual lists mark them so they are not tapped by mistake. A release with no
# score at all (field missing) counts as clean.
def cf_score(r):
    s = (r or {}).get("customFormatScore")
    return s if isinstance(s, (int, float)) and not isinstance(s, bool) else None


def cf_negative(r) -> bool:
    s = cf_score(r)
    return s is not None and s < 0


def cf_names(r):
    out = []
    for c in (r or {}).get("customFormats") or []:
        n = c.get("name") if isinstance(c, dict) else c
        if n:
            out.append(str(n))
    return out


def cf_flag(r) -> str:
    """Marker line for a manual list: '' for a clean release, a warning for a CF-negative one."""
    if not cf_negative(r):
        return ""
    names = ", ".join(cf_names(r)[:3]) or "custom formats"
    return f"\n     🚫 <b>rejected by your custom formats</b>: score {int(cf_score(r))} ({esc(names)})"


def lang_note(r, accepted=("English",)) -> str:
    """' · 🗣 French, English' when a release carries a language outside `accepted` (info only)."""
    ok = {str(a).lower() for a in accepted} | {"unknown"}
    names = [x.get("name") for x in ((r or {}).get("languages") or []) if isinstance(x, dict) and x.get("name")]
    if any(str(n).lower() not in ok for n in names):
        return " · 🗣 " + esc(", ".join(str(n) for n in names[:4]))
    return ""


def closest_clean_first(rels, limit=3):
    """Candidates for a manual prompt: no CAM copies, clean releases first, CF-negative ones only to fill up."""
    rels = [r for r in rels or [] if not is_cam(r)]
    clean = [r for r in rels if not cf_negative(r)]
    neg = [r for r in rels if cf_negative(r)]
    return (pick_closest(clean, limit) + pick_closest(neg, limit))[:limit]


# ---------------------------------------------------------------- season packs
def covered_seasons(title, default_sn, all_seasons):
    """Season numbers a pack's title spans.

    An explicit range (S01-S08, Seasons 1-8) gives that range; a numberless 'complete series'
    pack covers every monitored season; anything else is just `default_sn`. The range forms
    need a hyphen and refuse a trailing digit or 'p', so 'S03.1080p' is not read as a range.
    """
    t = title or ""
    m = (re.search(r"[Ss](\d{1,2})\s*-\s*[Ss]?(\d{1,2})(?![\dp])", t)
         or re.search(r"[Ss]easons?[.\s_]*(\d{1,2})\s*(?:-|–|to)\s*(\d{1,2})(?![\dp])", t))
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if a <= b <= a + 40:
            return set(range(a, b + 1))
    if re.search(r"(?i)complete[.\s_-]*(series|collection|seasons)", t):
        return set(all_seasons)
    return {default_sn}


def season_complete(eps, sn, now: datetime) -> bool:
    """True only when the season's episodes are known and none is dated in the future.

    No episode rows yet (Sonarr still loading them) counts as 'still airing', which sends
    the season down the safe episode-search path instead of a pack grab.
    """
    se = [e for e in eps or [] if e.get("seasonNumber") == sn]
    if not se:
        return False
    for e in se:
        ad = e.get("airDateUtc")
        if ad:
            try:
                if parse_iso(ad) > now:
                    return False
            except ValueError:
                pass
    return True


PACK_MIN_SEEDERS = 3   # an auto-picked pack below this rarely finishes


def choose_season_pack(rels, sn, seasons, covered):
    """Automatic pick for a finished season.

    Returns (best pack or None, every full-season pack, how many healthy packs were dropped for
    a negative custom-format score). Never returns a CF-negative pack, and prefers one that
    does not re-include a season an earlier pack already covers.
    """
    packs = [r for r in rels or [] if r.get("fullSeason")]
    healthy = [r for r in packs if _seeders(r) >= PACK_MIN_SEEDERS]
    ok = [r for r in healthy if not cf_negative(r)]
    pool = [r for r in ok if not (covered_seasons(r.get("title"), sn, seasons) & set(covered))] or ok
    return (pick_closest(pool, 1)[0] if pool else None), packs, len(healthy) - len(ok)


# ---------------------------------------------------------------- grab criteria (Radarr's own rules)
def allowed_qualities(profile):
    """Quality names a Radarr/Sonarr quality profile allows (members of an allowed group included)."""
    names = set()
    for it in (profile or {}).get("items", []) or []:
        if not it.get("allowed"):
            continue
        if it.get("quality"):
            names.add(it["quality"]["name"])
        for sub in it.get("items", []) or []:
            if sub.get("quality"):
                names.add(sub["quality"]["name"])
    return names


def grab_problems(rel, mv, profile, qdefs):
    """Reasons a grab breaks the movie's quality profile or size limits; [] when it meets them all.

    rel is the 'release' block of Radarr's Grab webhook (quality is a plain name there),
    mv the movie from /api/v3/movie/<id>, profile its quality profile, qdefs the quality
    definitions by name. Radarr's size limits are MB per minute of runtime.
    """
    probs = []
    rel, mv = rel or {}, mv or {}
    q = rel.get("quality") or ""
    if profile:
        if q and q not in allowed_qualities(profile):
            probs.append(f"quality {q} is not allowed in the '{profile.get('name')}' profile")
        score = rel.get("customFormatScore")
        minimum = profile.get("minFormatScore") or 0
        if isinstance(score, (int, float)) and not isinstance(score, bool) and score < minimum:
            cfs = ", ".join(str(c) for c in (rel.get("customFormats") or [])) or "custom formats"
            probs.append(f"custom-format score {score} is below the minimum {minimum} ({cfs})")
    d = (qdefs or {}).get(q)
    try:
        runtime = int(mv.get("runtime") or 0)
    except (TypeError, ValueError):
        runtime = 0
    try:
        size_mb = float(rel.get("size") or 0) / 1048576
    except (TypeError, ValueError):
        size_mb = 0.0
    if d and runtime and size_mb:
        lo, hi = d.get("minSize"), d.get("maxSize")
        if lo and size_mb < lo * runtime:
            probs.append(f"too small: {size_mb / 1024:.2f} GB, minimum for {runtime} min is "
                         f"{lo * runtime / 1024:.2f} GB")
        if hi and size_mb > hi * runtime:
            probs.append(f"too big: {size_mb / 1024:.2f} GB, maximum for {runtime} min is "
                         f"{hi * runtime / 1024:.2f} GB")
    return probs


# ---------------------------------------------------------------- missing-episode filler
# Sonarr stores its size cap per MINUTE of runtime, so a flat per-episode cap does not translate:
# a 22-minute sitcom ends up capped near 0.8 GB and every healthy copy is refused for size. The
# filler takes copies Sonarr refused for size ALONE, as long as each episode stays under the flat
# EPISODE_CAP_GB. 720p is only ever taken after the user taps the offer.
FILL_MIN_SEEDERS = 5     # a copy below this rarely finishes
FILL_PACK_SEEDERS = 10   # a season pack must be this healthy to be preferred over single episodes


def ep_list(r):
    return r.get("mappedEpisodeNumbers") or r.get("episodeNumbers") or []


def fill_usable(r, want, n_season, res, cap_bytes) -> bool:
    """Can the filler take this release for the wanted episodes at resolution `res`?"""
    if _seeders(r) < FILL_MIN_SEEDERS or is_cam(r):
        return False
    if rel_resolution(r) != res:
        return False
    size = r.get("size") or 0
    if r.get("fullSeason"):
        per = size / max(n_season, 1)
    else:
        eps = set(ep_list(r))
        if not eps or not eps <= set(want):
            return False
        per = size / len(eps)
    if per > cap_bytes:
        return False
    allowed = ["larger than maximum allowed"]          # the per-minute cap
    if res == 720:
        allowed.append("is not wanted in profile")     # 720p only on the user's tap
    if r.get("fullSeason"):
        allowed += ["Existing file", "allow upgrades", "meets cutoff"]   # owned episodes inside the pack stay put
    return all(any(a in x for a in allowed) for x in (r.get("rejections") or []))


def fill_pick(rels, want, n_season, res, cap_bytes):
    """(releases to grab, episodes they cover) for the wanted episodes of one season.

    A whole missing season takes the best healthy pack. Otherwise single episodes are picked
    one by one; a pack is the fallback only when no single copy exists and at least half the
    season is missing, so a mostly-owned season is never downloaded again for one or two gaps.
    """
    want = set(want)
    cand = [r for r in rels or [] if fill_usable(r, want, n_season, res, cap_bytes)]

    def per(r):
        return (r.get("size") or 0) / (n_season if r.get("fullSeason") else max(len(ep_list(r)), 1))

    def rank(r):
        return (-min(_seeders(r), 10), per(r))         # well seeded first, then the smallest

    packs = sorted([r for r in cand if r.get("fullSeason") and _seeders(r) >= FILL_PACK_SEEDERS], key=rank)
    if packs and len(want) == n_season:
        return [packs[0]], set(want)
    chosen, covered = [], set()
    for ep in sorted(want):
        if ep in covered:
            continue
        c = sorted([r for r in cand if not r.get("fullSeason") and ep in ep_list(r)], key=rank)
        if c:
            chosen.append(c[0])
            covered |= set(ep_list(c[0]))
    if not chosen and packs and len(want) * 2 >= n_season:
        return [packs[0]], set(want)
    return chosen, covered


# ---------------------------------------------------------------- fake downloads
# Releases packed as archives, or carrying a program file instead of a video, sit in the queue
# forever ("unsupported extension") because Radarr/Sonarr cannot import them. A real video next
# to a stray .exe is NOT fake: some groups ship a harmless one, and the video is what counts.
VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".ts", ".m2ts", ".mov", ".wmv", ".webm", ".mpg", ".mpeg", ".vob"}
DANGER_EXT = {".exe", ".scr", ".bat", ".cmd", ".lnk", ".msi", ".js", ".vbs", ".ps1", ".jar", ".pif"}
ARCHIVE_RE = re.compile(r"\.(7z|rar|zip|r\d\d|\d{3}|iso|tar|gz)$", re.I)
VIDEO_MIN_BYTES = 50e6   # anything smaller is a sample or a padding file, not the video
UNSUPPORTED_RE = re.compile(r"unsupported extension: '\.(7z|rar|zip|iso|exe|scr|lnk)'", re.I)


def fake_reason(files):
    """Why a download can never become something playable, or None if it looks real (or is not known yet).

    `files` is qBittorrent's /api/v2/torrents/files list for the torrent.
    """
    if not files:
        return None                                    # metadata not fetched yet
    if any(os.path.splitext(f.get("name", ""))[1].lower() in VIDEO_EXT and (f.get("size") or 0) > VIDEO_MIN_BYTES
           for f in files):
        return None
    exts = {os.path.splitext(f.get("name", ""))[1].lower() for f in files}
    if exts & DANGER_EXT:
        return "had a program file instead of a video (" + ", ".join(sorted(exts & DANGER_EXT)) + "), likely malware"
    if any(ARCHIVE_RE.search(f.get("name", "")) for f in files):
        return "was a packed archive, not a video"
    return None


def queue_fake_reason(status_messages):
    """The same verdict from a queue item's status messages ('unsupported extension: .rar')."""
    msgs = " ".join(m for sm in status_messages or [] for m in (sm.get("messages") or []))
    return "was a packed archive, not a video" if UNSUPPORTED_RE.search(msgs) else None


# ---------------------------------------------------------------- qBittorrent records
def torrent_finished(t) -> bool:
    """A torrent that has finished downloading (seeding, stopped after completion, or moving)."""
    st = str((t or {}).get("state") or "")
    try:
        prog = float((t or {}).get("progress") or 0)
    except (TypeError, ValueError):
        prog = 0.0
    return prog >= 0.9999 or st.endswith("UP") or st == "moving"


def torrent_dead(t, now, dead_minutes) -> bool:
    """Stalled with no seeders AND no peers in the swarm, nothing downloaded, older than the grace time."""
    t = t or {}
    st = str(t.get("state") or "").lower()
    age_min = (now - (t.get("added_on") or now)) / 60
    return ("stalled" in st and not (t.get("num_complete") or 0) and not (t.get("num_incomplete") or 0)
            and not (t.get("progress") or 0) and age_min > dead_minutes)


def pick_profile(profiles, preferred="HD-1080p"):
    """Quality profile id to add titles with: the named one, else any 1080p one, else the first."""
    profiles = profiles or []
    for p in profiles:
        if (p.get("name") or "").strip() == preferred:
            return p["id"]
    for p in profiles:
        if "1080" in (p.get("name") or ""):
            return p["id"]
    return profiles[0]["id"] if profiles else None


# ---------------------------------------------------------------- user picks
class ForcePicks:
    """Releases the user picked in the bot (force-grab list, closest-release buttons).

    The Grab webhook lets exactly those through without judging them. Picks are keyed by
    movie id AND the picked release (normalised title, or the exact byte size when the title
    is missing), so an automatic grab of the same movie cannot use up the user's pass and get
    the user's own pick refused and blocklisted. Radarr's Grab webhook release.releaseTitle and
    .size are the same values /api/v3/release returns as title and size. add(mid) without a
    title means "any release of that movie". A pick whose grab never arrives expires after `ttl`.
    """

    def __init__(self, ttl=6 * 3600, clock=time.time):
        self._d = {}                                   # movieId -> [(norm title | None, size, expiry)]
        self._ttl = ttl
        self._clock = clock

    def _prune(self):
        now = self._clock()
        for mid in list(self._d):
            live = [p for p in self._d[mid] if p[2] > now]
            if live:
                self._d[mid] = live
            else:
                del self._d[mid]

    def add(self, mid, title=None, size=None):
        self._prune()
        try:
            sz = int(size or 0)
        except (TypeError, ValueError):
            sz = 0
        self._d.setdefault(mid, []).append((norm_title(title) if title else None, sz, self._clock() + self._ttl))

    def discard(self, mid, title=None):
        """Forget one pick (the given title) or, with no title, every pick for that movie."""
        if title is None:
            self._d.pop(mid, None)
            return
        t, lst = norm_title(title), self._d.get(mid, [])
        for i, p in enumerate(lst):
            if p[0] == t:
                del lst[i]
                break
        if not lst:
            self._d.pop(mid, None)

    def __contains__(self, mid):
        self._prune()
        return bool(self._d.get(mid))

    def take(self, mid, title, size=None) -> bool:
        """True (and the pick is used up) only when this grab IS a release the user picked for the movie."""
        self._prune()
        lst = self._d.get(mid) or []
        t = norm_title(title) if title else None
        try:
            sz = int(size or 0)
        except (TypeError, ValueError):
            sz = 0
        for i, (pt, psz, _) in enumerate(lst):
            if pt is None or (t and pt == t) or (sz and psz and sz == psz):
                del lst[i]
                if not lst:
                    self._d.pop(mid, None)
                return True
        return False
