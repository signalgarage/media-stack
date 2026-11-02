#!/usr/bin/env python3
"""shrink_oversized: replace oversized TV episode files with smaller releases.

WARNING: this RE-DOWNLOADS episodes you already own, only to save disk space. It is OFF by
default (SHRINK_ENABLED=false) and most people should leave it off: it spends indexer
queries and bandwidth, and every swap replaces a file that was working fine. Turn it on only
when disk space matters more than that, together with resolve_shrink_imports.py (which lands
the replacements), and uncomment both lines in the cron block.

What it does: finds episode files bigger than their own size cap (series runtime x
TV_SIZE_CAP_MB_PER_MIN, the same per-minute cap Sonarr enforces) and grabs a smaller release
for each, biggest savings first.

The safety rule it is built around: never delete a file before its replacement is in hand.
Deleting first and searching after would leave an episode GONE when no release turns up. So
it force-grabs a smaller release through Sonarr's manual-grab endpoint (which bypasses the
upgrade decision), and the old, larger file is only replaced when the new one imports. A
failed download costs nothing: the original file is still there.

Choosing the replacement: the LARGEST release that fits under the cap (best quality within
budget) among 1080p sources, at most 85 % of the current size (a swap must be worth it), at
least 8 MB per minute (below that it is probably a garbage encode), not multi-language and
not tagged with another language than PREFERRED_LANGUAGE (or the show's own), no negative
custom-format score, never a release already blocklisted for that episode (the manual-grab
endpoint ignores the blocklist), and from a swarm that looks alive: >= 10 seeders preferred,
>= 3 accepted only when nothing better exists (indexer seeder counts are often inflated).

Pacing: a release lookup costs one query on EVERY indexer, and indexers cap daily queries, so
the budget per run stays small and the work deliberately takes days. It stands down while the
download queue is busy (missing episodes come first), and an episode with nothing suitable is
looked at again only after 30 days.

Files in <STATE_DIR>
  .shrink_pending.json  episodes with a replacement in flight. regrab_guard exempts exactly
                        these from its "cancel re-downloads of owned episodes" rule. Written
                        after every grab. An entry leaves when the swap landed, or after 2
                        days, when its stalled grab is cancelled WITH blocklist so the next
                        attempt must pick a different release.
  .shrink_keep.json     episodes to keep exactly as they are: {"<episodeId>": {"title": ..,
                        "why": ..}}. Never shrunk. If the file exists but cannot be read as a
                        JSON object the script refuses to run (fail closed).
  .shrink_paused        create it to stop NEW swaps; swaps in flight still finish.
  .shrink_state.json    per-episode "nothing suitable" cooldowns.

Runs daily from cron when enabled.
Flags: --dry-run  look up at most a few candidates and print what would be grabbed; grab
                  nothing, cancel nothing, write nothing, send nothing.
Exit:  0 normal (also when disabled), 1 Sonarr unreadable or the keep-list unreadable,
       2 the script itself failed.
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "shrink_oversized"
TAG = "[shrink]"
DISABLED_WARNING = ("shrink_oversized is OFF (SHRINK_ENABLED is not true). It re-downloads episodes you "
                    "already own to save disk space; most people should leave it off. Nothing was done.")

FLOOR_MB_MIN = 8         # below this per minute a release is probably a garbage encode
MAX_FRACTION = 0.85      # a replacement must be at least 15 % smaller to be worth a download
BUDGET = 50              # episodes looked up per run (each lookup queries every indexer)
DRY_RUN_LOOKUPS = 3      # a dry run must not spend the day's indexer queries
TIME_BUDGET_S = 3600     # stop after an hour regardless
QUEUE_STANDDOWN = 60     # let the missing-episode backfill finish first
GAP_S = 8                # seconds between release lookups
COOLDOWN_DAYS = 30       # look at a "nothing suitable" episode again only occasionally
PENDING_STALE_DAYS = 2   # an episode-sized grab that has not finished in 2 days is dead
WELL_SEEDED = 10         # indexer seeder counts lie; prefer swarms that look clearly alive
MIN_SEEDERS = 3
DEFAULT_RUNTIME_MIN = 45 # when Sonarr does not know a series' runtime
API_TIMEOUT = 300        # a release lookup waits on every indexer
MAX_LIST = 12
# Only 1080p sources: a smaller file must never mean a lower resolution.
ALLOWED_Q = {"HDTV-1080p", "WEBRip-1080p", "WEBDL-1080p", "Bluray-1080p"}
MULTI_RX = re.compile(r"\bMULTi\b", re.I)
# Language tags in release names. A tagged release may default to a track in that language.
LANG_TAGS = {"italian": ("ITA", "iTALiAN"), "french": ("TRUEFRENCH", "FRENCH"), "german": ("GERMAN",),
             "spanish": ("SPANISH", "LATINO"), "russian": ("RUS",), "hindi": ("HINDI",)}
ALWAYS_FOREIGN_TAGS = ("VOSTFR", "DUAL")   # subtitled original / dual audio: either may default wrong


def preferred() -> str:
    return settings.PREFERRED_LANGUAGE or "English"


def foreign_tag_rx():
    pref = preferred().lower()
    tags = [t for lang, ts in LANG_TAGS.items() if lang != pref for t in ts] + list(ALWAYS_FOREIGN_TAGS)
    return re.compile(r"\b(%s)\b" % "|".join(tags), re.I)


def foreign_langs(langs, original=None) -> list:
    """Languages in a release that are neither the preferred one, Unknown, nor the show's own
    original language. Uses Sonarr's parsed 'languages' field: the title regex alone misses
    many foreign-first releases whose names carry no language tag."""
    ok = {preferred().lower(), "unknown", "original", (original or "").lower(), ""}
    names = {((x or {}).get("name") or "") for x in (langs or [])}
    return sorted(n for n in names if n.lower() not in ok)


def is_preferred(language) -> bool:
    """True for a show in the preferred language (or one whose language Sonarr does not know)."""
    return (language or "").lower() in ("", preferred().lower())


def norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


def _media_missing() -> str:
    for p in [settings.MEDIA_ROOT] + settings.get_list("MOUNT_PATHS"):
        if not os.path.isdir(p):
            return p
    return ""


def load_keep(path: str):
    """No file -> empty set; a file that is not a JSON object -> None (refuse to run);
    otherwise the set of str(episodeId)."""
    if not os.path.exists(path):
        return set()
    keep = settings.load_json(path, None)
    if not isinstance(keep, dict):
        return None
    return {str(k) for k in keep}


class Shrink:
    def __init__(self, dry: bool):
        self.dry = dry
        self.app = settings.sonarr()
        self.blocklist = None
        self.queue_rows = None
        self.cap_per_min = settings.TV_SIZE_CAP_MB_PER_MIN

    def api(self, endpoint: str, method: str = "GET", data=None, params=None):
        return settings.api(self.app, endpoint, method=method, data=data, params=params, timeout=API_TIMEOUT)

    def blocklisted(self, epid) -> set:
        """Normalised titles already blocklisted for this episode. Sonarr's blocklist only gates
        AUTOMATIC grabs; the manual-grab endpoint ignores it, so a dead release would otherwise
        be picked again every run."""
        if self.blocklist is None:
            seen, page = {}, 1
            try:
                while True:
                    d = self.api("blocklist", params={"page": page, "pageSize": 500}) or {}
                    for r in d.get("records", []):
                        for e in r.get("episodeIds") or []:
                            seen.setdefault(int(e), set()).add(norm_title(r.get("sourceTitle")))
                    if not d.get("records") or page * 500 >= (d.get("totalRecords") or 0):
                        break
                    page += 1
            except Exception as e:
                settings.log(TAG, "could not read the blocklist (%s); proceeding without it" % e)
            self.blocklist = seen
        return self.blocklist.get(int(epid), set())

    def queue(self) -> list:
        if self.queue_rows is None:
            self.queue_rows = (self.api("queue", params={"pageSize": 1000}) or {}).get("records", [])
        return self.queue_rows

    def prune_pending(self, pending: dict, now: float, freed: list) -> dict:
        """An entry leaves once the episode's file is within its cap (the swap worked) or once
        it is too old (the download never completed)."""
        still = {}
        for epid, rec in pending.items():
            age_d = (now - rec.get("grabbed", 0)) / 86400.0
            try:
                ep = self.api("episode/%s" % epid) or {}
                size = (ep.get("episodeFile") or {}).get("size") or 0
                cap = rec.get("cap", 0)
                if size and cap and size <= cap:
                    freed.append((rec.get("title", "?"), rec.get("oldSize", 0), size))
                    continue
            except Exception:
                pass
            if age_d < PENDING_STALE_DAYS:
                still[epid] = rec
                continue
            # Presumed dead swarm: cancel AND blocklist, so the next attempt has to choose a
            # different release instead of grabbing this same torrent again.
            try:
                for qr in self.queue():
                    if str(qr.get("episodeId")) == str(epid):
                        what = rec.get("title", epid)
                        if self.dry:
                            settings.log(TAG, "[dry-run] would abandon the stalled replacement for %s" % what)
                        else:
                            self.api("queue/%d" % qr["id"], method="DELETE",
                                     params={"removeFromClient": "true", "blocklist": "true"})
                            settings.log(TAG, "abandoned the stalled replacement for %s (blocklisted)" % what)
                        break
            except Exception as e:
                settings.log(TAG, "could not clean up the stalled grab %s: %s" % (epid, e))
        return still

    def candidates(self, pending: dict, keep: set, state: dict, now: float) -> list:
        cands = []
        for s in self.api("series") or []:
            rt = s.get("runtime") or DEFAULT_RUNTIME_MIN
            cap = rt * self.cap_per_min * 1e6
            try:
                eps = self.api("episode", params={"seriesId": s["id"], "includeEpisodeFile": "true"}) or []
            except Exception:
                continue
            for e in eps:
                f = e.get("episodeFile")
                if not e.get("hasFile") or not f:
                    continue
                size = f.get("size") or 0
                key = str(e["id"])
                if size <= cap or key in pending or key in keep:
                    continue                  # within cap, already being replaced, or kept on purpose
                last = state.get(key)
                if last and now - last < COOLDOWN_DAYS * 86400:
                    continue                  # nothing suitable found recently
                cands.append({"epid": e["id"], "title": s.get("title") or "?",
                              "orig": (s.get("originalLanguage") or {}).get("name"),
                              "sn": e.get("seasonNumber", 0), "en": e.get("episodeNumber", 0),
                              "rt": rt, "cap": cap, "cur": size, "save": size - cap})
        cands.sort(key=lambda c: -c["save"])   # biggest savings first
        return cands

    def pick(self, c: dict, releases: list):
        rx = foreign_tag_rx()
        ok = []
        for r in releases:
            if not isinstance(r, dict):
                continue
            qn = ((r.get("quality") or {}).get("quality") or {}).get("name", "")
            size = r.get("size") or 0
            title = r.get("title") or ""
            if qn not in ALLOWED_Q or size <= 0 or size > c["cap"]:
                continue
            if size < FLOOR_MB_MIN * c["rt"] * 1e6:
                continue                       # implausibly small: a bad encode
            if size > c["cur"] * MAX_FRACTION:
                continue                       # not a meaningful saving
            if (r.get("seeders") or 0) < MIN_SEEDERS or MULTI_RX.search(title):
                continue
            if (r.get("customFormatScore") or 0) < 0:
                continue                       # the owner's custom formats call it low quality / unwanted
            if (is_preferred(c.get("orig")) and rx.search(title)) or foreign_langs(r.get("languages"), c.get("orig")):
                continue                       # may default to an unwanted audio or subtitle track
            if norm_title(title) in self.blocklisted(c["epid"]):
                continue                       # already tried and abandoned
            ok.append(r)
        # Largest that fits = best quality within budget, but only among swarms that look alive:
        # the well-seeded pool wins outright, the thin pool is only a fallback.
        for pool in ([x for x in ok if (x.get("seeders") or 0) >= WELL_SEEDED], ok):
            if pool:
                return max(pool, key=lambda x: x.get("size") or 0)
        return None


def report(freed: list, grabbed: list, dry: bool) -> None:
    if not freed and not grabbed:
        settings.log(TAG, "nothing to report")
        return
    body = "Library shrink\n\n"
    if grabbed:
        tot = sum(o - n for _t, o, n in grabbed)
        body += ("Started replacing %d oversized episode%s with smaller copies (about %.0f GB will be "
                 "freed once they finish):\n\n" % (len(grabbed), "" if len(grabbed) == 1 else "s", tot / 1e9))
        for t, o, n in grabbed[:MAX_LIST]:
            body += "• %s  %.1f GB → %.1f GB\n" % (html.escape(t, quote=False), o / 1e9, n / 1e9)
        if len(grabbed) > MAX_LIST:
            body += "• ...and %d more\n" % (len(grabbed) - MAX_LIST)
    if freed:
        tot = sum(o - n for _t, o, n in freed)
        body += ("\n%d replacement%s finished and the old file%s replaced; %.0f GB reclaimed.\n"
                 % (len(freed), "" if len(freed) == 1 else "s", "" if len(freed) == 1 else "s", tot / 1e9))
    body += ("\nThe original file is kept until the smaller copy has fully imported, so nothing is lost "
             "if a download fails.")
    if dry:
        settings.log(TAG, "[dry-run] would send: " + body.replace("\n", " | "))
    else:
        settings.notify(body)


def run(dry: bool) -> int:
    now = started = time.time()
    keep_path = settings.state_path(".shrink_keep.json")
    keep = load_keep(keep_path)
    if keep is None:
        settings.log(TAG, "cannot read .shrink_keep.json; refusing to run rather than shrink kept episodes")
        return 1                               # fail closed: nothing is grabbed, the monitor turns red
    missing = _media_missing()
    if missing:
        settings.log(TAG, "media folder missing or not mounted (%s); standing down" % missing)
        if not dry and alertstate.digest_due("media-root-missing"):
            settings.notify("Media folder missing\n\nThe media folder is not available, so the guard "
                            "scripts that read or change media are standing down until it is back. "
                            "mount_guard reports the details.")
        return 0

    sh = Shrink(dry)
    if not sh.app.key:
        settings.log(TAG, "no Sonarr API key found; standing down")
        return 1
    state_path = settings.state_path(".shrink_state.json")
    pending_path = settings.state_path(".shrink_pending.json")
    state = settings.load_json(state_path, {})
    pending = settings.load_json(pending_path, {})

    def save_pending() -> None:
        if not dry:
            settings.save_json(pending_path, pending)   # regrab_guard reads this: keep it current

    freed, grabbed = [], []
    pending = sh.prune_pending(pending, now, freed)
    save_pending()
    if freed:
        settings.log(TAG, "confirmed %d replacement(s)" % len(freed))

    if os.path.exists(settings.state_path(".shrink_paused")):
        settings.log(TAG, "paused (.shrink_paused exists): no new re-downloads; swaps in flight still finish")
        report(freed, [], dry)
        return 0

    try:
        queued = (sh.api("queue", params={"pageSize": 1}) or {}).get("totalRecords", 0)
    except Exception as e:
        settings.log(TAG, "cannot read the queue, standing down: %s" % e)
        return 1                               # a genuine failure must not report success
    if queued >= QUEUE_STANDDOWN:
        settings.log(TAG, "queue busy (%d), standing down: missing episodes come first" % queued)
        report(freed, [], dry)
        return 0

    try:
        cands = sh.candidates(pending, keep, state, now)
    except Exception as e:
        settings.log(TAG, "cannot read the series list: %s" % e)
        return 1
    budget = DRY_RUN_LOOKUPS if dry else BUDGET
    settings.log(TAG, "%d oversized files; looking up at most %d" % (len(cands), budget))

    for c in cands[:budget]:
        label = "%s S%02dE%02d" % (c["title"], c["sn"], c["en"])
        if time.time() - started > TIME_BUDGET_S:
            settings.log(TAG, "time budget reached")
            break
        try:
            releases = sh.api("release", params={"episodeId": c["epid"]}) or []
        except Exception as e:
            settings.log(TAG, "lookup failed for %s: %s" % (label, e))
            time.sleep(GAP_S)
            continue
        best = sh.pick(c, releases)
        if not best:
            if not dry:
                state[str(c["epid"])] = now    # cooldown: look again in a month
            settings.log(TAG, "no suitable smaller release: %s (%.2f GB)" % (label, c["cur"] / 1e9))
            time.sleep(GAP_S)
            continue
        new_size = best.get("size") or 0
        if dry:
            settings.log(TAG, "[dry-run] would grab %s  %.2f -> %.2f GB (%s seeders)"
                         % (label, c["cur"] / 1e9, new_size / 1e9, best.get("seeders")))
            grabbed.append((label, c["cur"], new_size))
            time.sleep(GAP_S)
            continue
        try:
            sh.api("release", method="POST", data={"guid": best["guid"], "indexerId": best["indexerId"]})
        except Exception as e:
            settings.log(TAG, "grab failed for %s: %s" % (label, e))
            time.sleep(GAP_S)
            continue
        pending[str(c["epid"])] = {"grabbed": now, "oldSize": c["cur"], "cap": c["cap"], "title": label}
        grabbed.append((label, c["cur"], new_size))
        settings.log(TAG, "grabbed %s  %.2f -> %.2f GB" % (label, c["cur"] / 1e9, new_size / 1e9))
        save_pending()                         # immediately: regrab_guard must see it before it acts
        time.sleep(GAP_S)

    if not dry:
        settings.save_json(state_path, state)
    save_pending()
    report(freed, grabbed, dry)
    return 0


def main(argv) -> int:
    if not settings.get_bool("SHRINK_ENABLED"):
        print(DISABLED_WARNING, flush=True)
        return 0
    p = argparse.ArgumentParser(description="Replace oversized episodes with smaller releases (re-downloads!).")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="look up a few candidates and print what would be grabbed; change nothing")
    args = p.parse_args(argv)
    dry = args.dry_run or settings.get_bool("DRY_RUN")
    if not dry:
        settings.heartbeat(JOB)
    try:
        return run(dry)
    except Exception as e:
        settings.log(TAG, "internal error: %s: %s" % (type(e).__name__, e))
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
