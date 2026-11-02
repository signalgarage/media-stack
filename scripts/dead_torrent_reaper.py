#!/usr/bin/env python3
"""dead_torrent_reaper: clear dead-swarm torrents before they freeze the download queue.

The failure it exists to stop: qBittorrent counts a `stalledDL` torrent as ACTIVE. A torrent
whose swarm has no seeders will never finish and never leave that state, so it holds an
active slot forever. Every backfill round adds a few more; they pile up silently until the
active count reaches `max_active_torrents`, and then the whole queue freezes: download speed
0, dozens of healthy torrents waiting in `queuedDL` behind dead ones. Nothing in qBittorrent
or the apps cleans these up: the apps' own stalled handling only covers items they still
track, and only after their own long timers.

The test that matters: num_complete versus num_seeds
  num_complete = seeders that EXIST in the swarm
  num_seeds    = seeders we are CONNECTED to
Only num_complete == 0 means genuinely dead. Seeders that exist while we reach none is OUR
connectivity (VPN, forwarded port): reaping then would be wrong and would hide a real fault,
so those are left alone and reported instead.

Safety
 - A torrent must look dead for GRACE_MIN (qBittorrent's own inactivity time, or this
   script's memory across runs) before it is touched: a brief stall while trackers churn must
   never trigger a removal.
 - A stopped/paused torrent does not announce, so its peer counts are always 0 whatever the
   swarm looks like: it is never judged in that state. It is resumed instead (unless the pause
   flag is set) and judged later on real numbers, after RESUME_IMMUNITY_S.
 - Removal goes through Sonarr/Radarr with blocklist=true, so the same dead release is never
   grabbed again, and a replacement search is started. A torrent no app tracks is deleted in
   qBittorrent; its files are deleted only when they live in the download folders.
 - Partial progress is irrelevant (90 % with no seeders is still unfinishable), but only the
   download is ever discarded, never a library file.
 - A per-run cap, so a mass tracker outage cannot empty the whole queue.

Pause flag: create <STATE_DIR>/.downloads_paused to stop this script resuming stopped
torrents (honoured for 24 hours after the file was last touched).

Runs every 30 minutes from cron and holds the shared downloads lock while it works.
Flags: --dry-run  classify and report; resume nothing, remove nothing, send nothing.
Exit:  0 normal, 1 qBittorrent unreachable, 2 the guard itself failed.
"""
from __future__ import annotations

import argparse
import html
import os
import socket
import subprocess
import sys
import time
import traceback
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "dead_torrent_reaper"
TAG = "[dead_reaper]"
STATE_NAME = ".dead_reaper_state.json"
PAUSE_FLAG_NAME = ".downloads_paused"

GRACE_MIN = 45              # must look dead this long before anything happens
MAX_REAP = 25               # per-run cap: a tracker outage cannot wipe the queue in one go
RESUME_IMMUNITY_S = 3600    # a just-resumed torrent needs time to re-announce before it is judged
RESUME_RETRY_S = 86400      # one resume attempt per torrent per day; never flap
PAUSE_FLAG_HONOUR_S = 86400 # the pause flag counts for a day after it was last touched
META_STUCK_HOURS = 3        # metadata is a few KB and arrives in seconds from real peers; hours
                            # of failure mean the peer list is stale ghosts
UNREACHABLE_ALERT_MIN = 3   # one torrent whose only seed is offline is normal; a VPN/port fault hits many
QBIT_TIMEOUT = 120
ARR_TIMEOUT = 180
MAX_LIST = 10


def _is_timeout(e: BaseException) -> bool:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(e.reason, (socket.timeout, TimeoutError))


def _err(e: BaseException) -> str:
    if isinstance(e, settings.ApiError):
        return "HTTP %d" % e.status
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d" % e.code
    if _is_timeout(e):
        return "timed out"
    if isinstance(e, urllib.error.URLError):
        if isinstance(e.reason, ConnectionRefusedError):
            return "connection refused"
        return str(e.reason)[:80]
    if isinstance(e, ConnectionRefusedError):
        return "connection refused"
    return (str(e) or type(e).__name__)[:80]


def qbit_switched_off() -> bool:
    """True when the qbittorrent container was stopped on purpose (downloads-off.sh or a manual
    `docker compose stop`): stopped, with no start error. A container that FAILED to start (for
    example after its VPN container was recreated) carries an error and is a real fault."""
    try:
        r = settings.docker("inspect", "-f", "{{.State.Status}}|{{.State.Error}}", "qbittorrent", timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    status, _, error = r.stdout.strip().partition("|") if r.returncode == 0 else ("", "", "")
    return status in ("exited", "created") and not error.strip()


def _media_missing() -> str:
    for p in [settings.MEDIA_ROOT] + settings.get_list("MOUNT_PATHS"):
        if not os.path.isdir(p):
            return p
    return ""


def _send(text: str, dry: bool) -> None:
    if dry:
        settings.log(TAG, "[dry-run] would send: " + text.replace("\n", " | "))
    else:
        settings.notify(text)


def classify(tors: list, now: float, pause_flag_fresh: bool):
    """Split incomplete torrents into (dead, unreachable, meta_stuck, paused_live). Pure."""
    dead, unreachable, meta_stuck, paused_live = [], [], [], []
    for t in tors:
        if (t.get("progress") or 0) >= 1.0:
            continue
        state = t.get("state", "")
        if state == "stalledDL":
            (dead if (t.get("num_complete") or 0) == 0 else unreachable).append(t)
        elif state in ("stoppedDL", "pausedDL"):
            # A stopped torrent does not announce, so num_complete is ALWAYS 0 no matter how
            # healthy the swarm is: its peer counts carry no information and must never drive
            # a destructive decision (judging on them once destroyed a grab with hundreds of
            # real seeders). So: resume, never reap from this state.
            if not pause_flag_fresh:
                paused_live.append(t)
        elif state in ("metaDL", "forcedMetaDL"):
            if (now - (t.get("added_on") or now)) / 3600.0 > META_STUCK_HOURS:
                meta_stuck.append(t)
    return dead, unreachable, meta_stuck, paused_live


def qbit_start(hashes: str) -> None:
    """qBittorrent 5 renamed torrents/resume to torrents/start; support both."""
    try:
        settings.qbit("torrents/start", {"hashes": hashes}, timeout=60)
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        settings.qbit("torrents/resume", {"hashes": hashes}, timeout=60)


def _in_download_area(cpath: str, temp_path: str) -> bool:
    """True when a container path lies in the downloads folder or qBittorrent's temp folder."""
    cpath = (cpath or "").rstrip("/")
    for base in (settings.CONTAINER_DOWNLOADS, temp_path.rstrip("/") if temp_path else ""):
        if base and (cpath == base or cpath.startswith(base + "/")):
            return True
    return False


def _queue(name: str):
    a = settings.app(name)
    if not a.key:
        return a, []
    try:
        q = settings.api(a, "queue", params={"pageSize": 1000}, timeout=ARR_TIMEOUT) or {}
        return a, q.get("records", []) if isinstance(q, dict) else []
    except Exception as e:
        settings.log(TAG, "%s queue unreadable: %s" % (name, _err(e)))
        return a, []


def run_locked(dry: bool) -> int:
    missing = _media_missing()
    if missing:
        settings.log(TAG, "media folder missing or not mounted (%s); standing down" % missing)
        if not dry and alertstate.digest_due("media-root-missing"):
            settings.notify("Media folder missing\n\nThe media folder is not available, so the guard "
                            "scripts that read or change media are standing down until it is back. "
                            "mount_guard reports the details.")
        return 0

    now = time.time()
    st_path = settings.state_path(STATE_NAME)
    st = settings.load_json(st_path, {})
    try:
        tors = settings.qbit("torrents/info", timeout=QBIT_TIMEOUT)
        xfer = settings.qbit("transfer/info", timeout=QBIT_TIMEOUT)
        if not isinstance(tors, list) or not isinstance(xfer, dict):
            raise RuntimeError("unexpected reply")
    except Exception as e:
        if qbit_switched_off():
            settings.log(TAG, "qBittorrent is stopped (downloads switched off); nothing to do")
            return 0
        settings.log(TAG, "cannot reach qBittorrent, standing down: %s" % _err(e))
        return 1
    try:
        prefs = settings.qbit("app/preferences", timeout=QBIT_TIMEOUT)
    except Exception:
        prefs = {}
    temp_path = (prefs.get("temp_path") or "") if isinstance(prefs, dict) and prefs.get("temp_path_enabled") else ""

    flag = settings.state_path(PAUSE_FLAG_NAME)
    try:
        flag_fresh = now - os.path.getmtime(flag) < PAUSE_FLAG_HONOUR_S
    except OSError:
        flag_fresh = False
    if flag_fresh:
        settings.log(TAG, "pause flag set: stopped torrents are left stopped")
    dead, unreachable, meta_stuck, paused_live = classify(tors, now, flag_fresh)

    # --- grace period: must look dead across runs ---------------------
    seen = dict(st.get("seen") or {})
    resumed_at = {h: v for h, v in (st.get("resumed") or {}).items()
                  if h in {t.get("hash") for t in tors} and now - v < RESUME_RETRY_S}
    ripe = []
    for t in dead + meta_stuck:
        h = t["hash"]
        if now - resumed_at.get(h, 0) < RESUME_IMMUNITY_S:
            continue                  # resumed moments ago: its peer counts mean nothing yet
        first = seen.get(h)
        if not first:
            seen[h] = now
        # Two independent signals; either is enough. qBittorrent's own last_activity is ground
        # truth and works on the very first run; the cross-run memory is a fallback for clients
        # that do not report last_activity sensibly.
        la = t.get("last_activity") or 0
        inactive_min = (now - la) / 60.0 if la > 0 else (now - (t.get("added_on") or now)) / 60.0
        if inactive_min >= GRACE_MIN or (first is not None and (now - first) / 60.0 >= GRACE_MIN):
            ripe.append(t)
    live = {t["hash"] for t in dead + meta_stuck}
    seen = {h: v for h, v in seen.items() if h in live}      # forget the ones that recovered

    active = len([t for t in tors if not t.get("state", "").startswith("queued")
                  and "stopped" not in t.get("state", "") and "paused" not in t.get("state", "")])
    settings.log(TAG, "active=%d  dead=%d (ripe %d)  unreachable=%d  metaStuck=%d  down=%.2f MB/s"
                 % (active, len(dead), len(ripe), len(unreachable), len(meta_stuck),
                    (xfer.get("dl_info_speed") or 0) / 1048576))

    # --- resume stopped torrents ---------------------------------------
    resumed = []
    for t in paused_live:
        h = t["hash"]
        if h in resumed_at:
            continue                  # already tried today; do not flap
        if dry:
            settings.log(TAG, "[dry-run] would resume stopped torrent: %s" % t.get("name", "")[:52])
            continue
        try:
            qbit_start(h)
            resumed_at[h] = now
            seen.pop(h, None)         # fresh grace window: it just started
            resumed.append(t.get("name", ""))
            settings.log(TAG, "resumed stopped torrent: %s" % t.get("name", "")[:52])
        except Exception as e:
            settings.log(TAG, "could not resume %s: %s" % (t.get("name", "")[:40], _err(e)))

    if unreachable and not dead:
        settings.log(TAG, "seeders exist but none reachable: looks like a VPN/port problem, not dead swarms")

    # --- reap ------------------------------------------------------------
    reaped, eps, movies = [], set(), set()
    if ripe:
        sonarr, sq = _queue("sonarr")
        radarr, rq = _queue("radarr")
        by_s, by_r = {}, {}
        for r in sq:
            by_s.setdefault((r.get("downloadId") or "").lower(), []).append(r)
        for r in rq:
            by_r.setdefault((r.get("downloadId") or "").lower(), []).append(r)
        if len(ripe) > MAX_REAP:
            settings.log(TAG, "%d ripe; only %d this run (per-run cap)" % (len(ripe), MAX_REAP))
        for t in ripe[:MAX_REAP]:
            h = t["hash"].lower()
            name = t.get("name", "")
            if dry:
                tracked = "Sonarr" if h in by_s else "Radarr" if h in by_r else "no app"
                settings.log(TAG, "[dry-run] would reap (%s) %-52s %.0f%%"
                             % (tracked, name[:52], (t.get("progress") or 0) * 100))
                reaped.append((name, (t.get("progress") or 0) * 100))
                continue
            handled = False
            tracked = bool(by_s.get(h) or by_r.get(h))
            for app, rows, ids, key in ((sonarr, by_s.get(h, []), eps, "episodeId"),
                                        (radarr, by_r.get(h, []), movies, "movieId")):
                for r in rows:
                    try:
                        # blocklist=true: the app will never grab this same dead release again
                        settings.api(app, "queue/%d" % r["id"], method="DELETE",
                                     params={"removeFromClient": "true", "blocklist": "true"},
                                     timeout=ARR_TIMEOUT)
                        if r.get(key):
                            ids.add(r[key])
                        handled = True
                    except Exception as e:
                        settings.log(TAG, "%s remove failed: %s" % (app.name, _err(e)))
            if tracked and not handled:
                # An app tracks it but would not remove it: retry next run rather than a plain
                # delete, which would skip the blocklist and let the same release come back.
                settings.log(TAG, "kept %s for the next run: its app could not remove it" % name[:52])
                continue
            if not handled:
                # No app tracks it: drop it from the client. Its files go too only when they
                # live in the download folders; never anything that might be library content.
                where = t.get("content_path") or t.get("save_path") or ""
                delete_files = _in_download_area(where, temp_path)
                try:
                    settings.qbit("torrents/delete", {"hashes": t["hash"],
                                                      "deleteFiles": "true" if delete_files else "false"},
                                  timeout=60)
                    if not delete_files:
                        settings.log(TAG, "kept the files of %s: not in the download folders" % name[:52])
                except Exception as e:
                    settings.log(TAG, "orphan delete failed: %s" % _err(e))
                    continue
            reaped.append((name, (t.get("progress") or 0) * 100))
            seen.pop(t["hash"], None)
            settings.log(TAG, "reaped %-52s %.0f%%" % (name[:52], (t.get("progress") or 0) * 100))

    # --- re-search for replacements ---------------------------------------
    if eps:
        try:
            settings.api(settings.sonarr(), "command", method="POST",
                         data={"name": "EpisodeSearch", "episodeIds": sorted(eps)}, timeout=ARR_TIMEOUT)
            settings.log(TAG, "re-searching %d episode(s)" % len(eps))
        except Exception as e:
            settings.log(TAG, "episode re-search failed: %s" % _err(e))
    if movies:
        try:
            settings.api(settings.radarr(), "command", method="POST",
                         data={"name": "MoviesSearch", "movieIds": sorted(movies)}, timeout=ARR_TIMEOUT)
            settings.log(TAG, "re-searching %d movie(s)" % len(movies))
        except Exception as e:
            settings.log(TAG, "movie re-search failed: %s" % _err(e))

    if not dry:
        st.update({"seen": seen, "resumed": resumed_at,
                   "last_reap": now if reaped else st.get("last_reap", 0)})
        try:
            settings.save_json(st_path, st)
        except OSError as e:
            settings.log(TAG, "could not save state: %s" % e)

    # --- report ------------------------------------------------------------
    msgs = []
    if resumed:
        msgs.append("Restarted %d download%s that had been left stopped; they are judged again once "
                    "they have had time to find peers." % (len(resumed), "" if len(resumed) == 1 else "s"))
    if reaped:
        lines = ["• %s (%.0f%% done)" % (html.escape(n[:58], quote=False), p) for n, p in reaped[:MAX_LIST]]
        if len(reaped) > MAX_LIST:
            lines.append("• ...and %d more" % (len(reaped) - MAX_LIST))
        msgs.append("Cleared %d dead download%s that could never finish (nobody online had the file). "
                    "Replacements are being searched for automatically.\n\n%s"
                    % (len(reaped), "" if len(reaped) == 1 else "s", "\n".join(lines)))
    # vpn_watchdog already checks the tunnel every 5 minutes: alert only when MANY torrents are
    # affected, and at most once a day.
    if unreachable and not dead and len(unreachable) >= UNREACHABLE_ALERT_MIN \
            and (dry or alertstate.digest_due("reaper_unreachable")):
        msgs.append("%d downloads have seeders available but cannot connect to any of them. That "
                    "usually means the VPN's forwarded port has stopped working." % len(unreachable))
    if msgs:
        _send("Download cleanup\n\n" + "\n\n".join(msgs), dry)
    return 0


def run(dry: bool) -> int:
    lock = alertstate.acquire_lock()
    if lock is None:
        settings.log(TAG, "another guard is working on the downloads; trying again next run")
        return 0
    try:
        return run_locked(dry)
    finally:
        if hasattr(lock, "close"):
            lock.close()


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Clear dead-swarm torrents before they freeze the queue.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="classify and report only; change nothing, send nothing")
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
