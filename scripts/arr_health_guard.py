#!/usr/bin/env python3
"""arr_health_guard: turn the apps' own health reports into alarms.

Why it exists: an uptime monitor that only loads each web page stays green while Radarr's
own health page says "root folder missing" or "download client unavailable" and downloads
sit at zero for hours. The apps already know when the pipeline is broken; this guard
listens to them.

What it checks
  FAIL (exit 1 so a push monitor turns red, plus one Telegram message per new problem)
    - any health item of type "error" in Radarr, Sonarr or Prowlarr
    - any health item from a pipeline-breaking source, even when the app only calls it a
      warning: root folders missing, download client unreachable, remote path mapping
      broken, no indexer available for RSS or search, import mechanism broken
    - a root folder with under 50 GB free (imports start failing), or one whose free space
      cannot be read at all (typically a stale network-share connection)
    - an app API that does not answer
    - qBittorrent not answering, or several torrents losing their files at once (a share
      or disk has gone away)
  ALERT ONLY (one message per item; these are per download, not systemic, exit stays 0)
    - a queue item stuck at the import step for more than 6 hours
    - a torrent that qBittorrent reports in its "error" state
  IGNORED
    - update notices, and Prowlarr warnings that SOME indexers are failing (one indexer
      being down is normal; "no indexer available" shows up in Radarr/Sonarr instead)
  Bazarr has no comparable health API and is not checked.

A problem is announced once, then once more when it clears (own state file). A check that
could not run this time (a timeout, an app down) keeps its last known state: a failed call
must never read as "resolved". The guard never changes anything in any app.

Runs every 10 minutes from cron (scripts/run_guard.sh arr_health_guard).
Flags: --dry-run  check and print, send nothing, leave the state file untouched.
Exit:  0 healthy (alert-only items allowed), 1 pipeline problem, 2 the guard itself failed.
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
import settings  # noqa: E402

JOB = "arr_health_guard"
TAG = "[arr_health_guard]"
STATE_NAME = ".arr_health_state.json"

STUCK_HOURS = 6            # a download may wait at import for a while; 6 h at that step means it never will
MIN_FREE_GB = 50           # under this, importing a few large files starts failing
MISSING_FILES_OUTAGE = 5   # one or two torrents missing files is routine; this many at once means a share is gone
QBIT_RETRY_WAIT_S = 20     # qBittorrent stops answering while it moves big files: retry one timeout before "down"
API_TIMEOUT_S = 45         # a stale share can make an app API hang; never wait on it forever
QUEUE_PAGE_SIZE = 1000     # one page is plenty for a home queue
MAX_LINES = 12             # bullets per message section, so one bad night cannot produce a wall of text

APPS = (("radarr", "Radarr"), ("sonarr", "Sonarr"), ("prowlarr", "Prowlarr"))

# Health sources that never say anything about the download pipeline (or are covered better elsewhere).
IGNORE_SOURCES = {"UpdateCheck", "IndexerStatusCheck", "IndexerLongTermStatusCheck",
                  "NotificationStatusCheck", "ImportListStatusCheck"}
# Sources that break the pipeline even when the app files them as a warning.
FAIL_SOURCES = {"RootFolderCheck", "MovieCollectionRootFolderCheck", "DownloadClientCheck",
                "DownloadClientStatusCheck", "DownloadClientRootFolderCheck",
                "DownloadClientSortingCheck", "RemotePathMappingCheck", "ImportMechanismCheck",
                "IndexerRssCheck", "IndexerSearchCheck", "IndexerDownloadClientCheck",
                "MountCheck", "ApplicationLongTermStatusCheck"}
IMPORT_STAGE = {"importPending", "importBlocked", "importFailed", "failedPending"}
# Keys of these kinds are alert-only; everything else is a FAIL.
ALERT_PREFIXES = ("stuck:", "qberr:")


# ----------------------------------------------------------------- helpers

def _is_timeout(e: BaseException) -> bool:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    if isinstance(e, urllib.error.URLError) and isinstance(e.reason, (socket.timeout, TimeoutError)):
        return True
    return "timed out" in str(e).lower()


def _err(e: BaseException) -> str:
    """Short human reason for a failed call (alerts never carry stack traces)."""
    if isinstance(e, settings.ApiError):
        return "the API key was rejected" if e.status in (401, 403) else "HTTP %d" % e.status
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


def _folder(path: str) -> str:
    return "'%s'" % (os.path.basename(str(path).rstrip("/")) or str(path))


def _bullets(items, limit: int = MAX_LINES) -> str:
    items = list(items)
    lines = ["• " + html.escape(t, quote=False) for t in items[:limit]]
    if len(items) > limit:
        lines.append("• ...and %d more" % (len(items) - limit))
    return "\n".join(lines)


def _send(text: str, dry: bool) -> None:
    if dry:
        settings.log(TAG, "[dry-run] would send: " + text.replace("\n", " | "))
    else:
        settings.notify(text)


def _carry_app(carry: set, label: str) -> None:
    carry.update(p % label for p in ("health:%s:", "free:%s:", "disk:%s:", "stuck:%s:", "rootfolder:%s"))


# ----------------------------------------------------------------- checks

def check_app(name: str, label: str, fails: dict, alerts: dict, first_seen: dict,
              stuck_now: set, carry: set) -> None:
    a = settings.app(name)
    if not a.key:
        fails["down:" + label] = ("%s: no API key found (set %s_API_KEY in .env, or check that "
                                  "the app has written its config file)." % (label, name.upper()))
        _carry_app(carry, label)
        return
    try:
        health = settings.api(a, "health", timeout=API_TIMEOUT_S) or []
    except Exception as e:
        fails["down:" + label] = "%s is not answering its API (%s)." % (label, _err(e))
        _carry_app(carry, label)
        return
    for h in health:
        if not isinstance(h, dict):
            continue
        src, typ, msg = h.get("source", ""), h.get("type", ""), (h.get("message") or "").strip()
        if src in IGNORE_SOURCES:
            continue
        if typ == "error" or src in FAIL_SOURCES:
            fails["health:%s:%s" % (label, src)] = "%s: %s" % (label, msg[:220])
    if name == "prowlarr":
        return                               # no root folders, no download queue
    check_free_space(a, label, fails, carry)
    check_queue(a, name, label, alerts, first_seen, stuck_now, carry)


def check_free_space(a, label: str, fails: dict, carry: set) -> None:
    """Free space per root folder. The root-folder list is the reliable source: the
    diskspace endpoint can leave out network shares entirely, so it is only a fallback for
    an app that has no usable root folder yet. A root folder that is reachable but cannot
    report its free space is a fault in itself: a stale share connection does exactly that,
    and the app then silently skips its free-space check before importing."""
    try:
        folders = settings.api(a, "rootfolder", timeout=API_TIMEOUT_S) or []
    except Exception as e:
        carry.update(("free:%s:" % label, "disk:%s:" % label))
        fails["rootfolder:" + label] = ("%s cannot list its root folders (%s); a share connection "
                                        "may be stale." % (label, _err(e)))
        return
    seen_any = False
    for rf in folders:
        if not isinstance(rf, dict) or rf.get("accessible") is False:
            continue                         # RootFolderCheck already reports a missing folder
        seen_any = True
        path, free = rf.get("path") or "?", rf.get("freeSpace")
        if free is None:
            fails["free:%s:%s" % (label, path)] = (
                "%s cannot read the free space of its %s folder; the share connection is "
                "probably stale." % (label, _folder(path)))
        elif free / 1e9 < MIN_FREE_GB:
            fails["disk:%s:%s" % (label, path)] = (
                "%s: only %.0f GB free for its %s folder; imports will start failing."
                % (label, free / 1e9, _folder(path)))
    if not seen_any:
        check_diskspace(a, label, fails, carry)


def check_diskspace(a, label: str, fails: dict, carry: set) -> None:
    try:
        disks = settings.api(a, "diskspace", timeout=API_TIMEOUT_S) or []
    except Exception as e:
        carry.add("disk:%s:" % label)
        settings.log(TAG, "%s diskspace not readable: %s" % (label, _err(e)))
        return
    data = settings.CONTAINER_DATA
    for d in disks:
        path = (d or {}).get("path") or ""
        if path != data and not path.startswith(data + "/"):
            continue                         # only the media mount matters here
        free = d.get("freeSpace")
        if isinstance(free, (int, float)) and free / 1e9 < MIN_FREE_GB:
            fails["disk:%s:%s" % (label, path)] = (
                "%s: only %.0f GB free on the media disk; imports will start failing."
                % (label, free / 1e9))


def check_queue(a, name: str, label: str, alerts: dict, first_seen: dict, stuck_now: set,
                carry: set) -> None:
    params = {"pageSize": QUEUE_PAGE_SIZE,
              ("includeUnknownMovieItems" if name == "radarr" else "includeUnknownSeriesItems"): "true"}
    try:
        q = settings.api(a, "queue", params=params, timeout=API_TIMEOUT_S) or {}
    except Exception as e:
        carry.add("stuck:%s:" % label)
        settings.log(TAG, "%s queue check skipped: %s" % (label, _err(e)))
        return
    now = time.time()
    for r in q.get("records", []) if isinstance(q, dict) else []:
        if r.get("trackedDownloadState") not in IMPORT_STAGE:
            continue
        # The age counts from when THIS guard first saw it stuck, not from the grab: a grab can
        # take days to download and still import normally.
        k = "stuck:%s:%s" % (label, r.get("downloadId") or r.get("id"))
        stuck_now.add(k)
        hours = (now - first_seen.setdefault(k, now)) / 3600.0
        if hours < STUCK_HOURS:
            continue
        why = "; ".join(m for s in (r.get("statusMessages") or []) for m in (s.get("messages") or []))
        alerts[k] = "%s: '%s' has been stuck at the import step for %.0f h. %s" % (
            label, (r.get("title") or "")[:70], hours, (why or r.get("errorMessage") or "")[:160])


def _qbit_errored():
    try:
        return settings.qbit("torrents/info?filter=errored", timeout=API_TIMEOUT_S)
    except Exception as e:
        if not _is_timeout(e):
            raise
        settings.log(TAG, "qBittorrent timed out (it stalls while moving big files); "
                          "retrying once in %d s" % QBIT_RETRY_WAIT_S)
        time.sleep(QBIT_RETRY_WAIT_S)
        return settings.qbit("torrents/info?filter=errored", timeout=API_TIMEOUT_S)


def check_qbit(fails: dict, alerts: dict, carry: set) -> None:
    try:
        errored = _qbit_errored()
        if not isinstance(errored, list):
            raise RuntimeError("unexpected reply")
    except Exception as e:
        carry.update(("qberr:", "missing:qbit"))
        if qbit_switched_off():
            # Stopped on purpose (downloads-off.sh or a manual stop): not a pipeline fault.
            settings.log(TAG, "qBittorrent's container is stopped (downloads switched off); not checked")
            return
        fails["down:qBittorrent"] = "qBittorrent is not answering its API (%s)." % _err(e)
        return
    for t in errored:
        if t.get("state") == "error":
            alerts["qberr:" + t.get("hash", "?")] = "qBittorrent reports an error on '%s'." % (t.get("name") or "")[:80]
    # MANY torrents losing their files at once means a share or disk has gone. One or two is
    # normal: an import that moves a file leaves its torrent without data until it is tidied.
    miss = [t for t in errored if t.get("state") == "missingFiles"]
    if len(miss) >= MISSING_FILES_OUTAGE:
        fails["missing:qbit"] = ("%d torrents have lost their files at once (e.g. %s); a media share or "
                                 "disk is probably missing." % (len(miss), ", ".join((t.get("name") or "")[:40]
                                                                                     for t in miss[:3])))


# ----------------------------------------------------------------- main

def run(dry: bool) -> int:
    st_path = settings.state_path(STATE_NAME)
    state = settings.load_json(st_path, {})
    first_seen = dict(state.get("first_seen") or {})
    prev = dict(state.get("open") or {})
    fails, alerts = {}, {}
    stuck_now = set()                        # queue rows at the import step in THIS run
    carry = set()                            # key prefixes whose check could not run this time

    for name, label in APPS:
        check_app(name, label, fails, alerts, first_seen, stuck_now, carry)
    check_qbit(fails, alerts, carry)

    # A check that could not run keeps its last known state instead of reading as "resolved"
    # (one queue timeout once sent a false all-clear and restarted the stuck clock).
    for k, v in prev.items():
        if k not in fails and k not in alerts and any(k.startswith(p) for p in carry):
            (alerts if k.startswith(ALERT_PREFIXES) else fails)[k] = v
    current = dict(fails)
    current.update(alerts)
    new = [k for k in current if k not in prev]
    cleared = [prev[k] for k in prev if k not in current]

    sections = []
    new_f = [current[k] for k in new if k in fails]
    new_a = [current[k] for k in new if k in alerts]
    if new_f:
        sections.append("Download pipeline: PROBLEM\n\n" + _bullets(new_f))
    if new_a:
        sections.append("Download pipeline: needs a look\n\n" + _bullets(new_a))
    if cleared:
        sections.append("Download pipeline: resolved\n\n" + _bullets(c[:160] for c in cleared))
    if sections:
        _send("\n\n".join(sections), dry)

    if not dry:
        keep_seen = {k: v for k, v in first_seen.items()
                     if k in stuck_now or any(k.startswith(p) for p in carry)}
        try:
            settings.save_json(st_path, {"open": current, "first_seen": keep_seen})
        except OSError as e:
            settings.log(TAG, "could not save state: %s" % e)

    for v in fails.values():
        settings.log(TAG, "FAIL " + v)
    for v in alerts.values():
        settings.log(TAG, "ALERT " + v)
    if not current:
        settings.log(TAG, "healthy: no pipeline problems reported")
    return 1 if fails else 0


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Alarm on the apps' own health reports.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="check and print only; send nothing, keep the state file")
    args = p.parse_args(argv)
    dry = args.dry_run or settings.get_bool("DRY_RUN")
    if not dry:
        settings.heartbeat(JOB)
    try:
        return run(dry)
    except Exception as e:                   # the guard itself broke: say so, exit 2
        settings.log(TAG, "internal error: %s: %s" % (type(e).__name__, e))
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
