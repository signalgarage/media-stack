#!/usr/bin/env python3
"""vpn_watchdog: self-heal "qBittorrent went deaf after the VPN reconnected".

What breaks: gluetun re-establishes its tunnel (health restart, provider drop, port-forward
lease lapse). qBittorrent keeps its sockets bound to the OLD tunnel and silently stops
connecting to any peer, while everything gluetun does itself (DNS, HTTPS, ping) keeps
working, which is why it looks like "the internet is fine".

Symptoms it detects (measured, not guessed)
  connection_status "disconnected"            -> no network at all
  connection_status "firewalled"              -> no incoming connections (only checked with
                                                 VPN_PORT_FORWARDING=on: without a forwarded
                                                 port, behind a VPN that is the normal state)
  dht_nodes < 10                              -> magnet links can never resolve (only when DHT
                                                 is enabled in qBittorrent)
  forwarded port != qBittorrent listen_port   -> qBittorrent announces a dead port (with port
                                                 forwarding on); fixed directly by running
                                                 /scripts/setport.sh inside gluetun
The cure for the rest: `docker compose restart gluetun` (fresh tunnel, fresh forwarded port),
then restart every running container that shares its network namespace (qbittorrent): they
lose networking when gluetun's namespace is recreated.

Safety rails
 - acts only after FAILS_NEEDED consecutive bad checks (not on one blip)
 - never acts while data is flowing (download speed above ACTIVE_KBPS = it works)
 - a cooldown, so it can never restart-loop
 - after a restart it verifies patiently (DHT needs minutes to bootstrap) before judging
 - a stopped qBittorrent container (downloads switched off) is left stopped
 - every action is logged and reported; a failed repair gets an explicit all-clear later

Extra self-checks, each reported once: the media folder disappearing, the root disk under
10 GB free, magnet links stuck fetching metadata for more than a day.

Runs every 5 minutes from cron. Skipped (exit 0) when VPN_MODE=off.
Flags: --dry-run  check and print; restart nothing, change nothing, send nothing.
Exit:  0 healthy or recovering, 1 still broken after the threshold or the repair failed,
       2 the guard itself failed.
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

JOB = "vpn_watchdog"
TAG = "[vpn_watchdog]"
STATE_NAME = ".vpn_watchdog_state.json"

FAILS_NEEDED = 3            # consecutive bad checks before acting (3 x 5 min = 15 min)
COOLDOWN_MIN = 60           # never restart the tunnel more than once an hour
ACTIVE_KBPS = 100           # downloading faster than this means it works: leave it alone
MIN_DHT = 10                # fewer DHT nodes and magnet links cannot resolve
HEALTHY_TRIES, HEALTHY_EVERY_S = 36, 5   # up to 3 min for gluetun to report healthy again
VERIFY_TRIES, VERIFY_EVERY_S = 5, 60     # judge a repair over 5 min: a 45 s check once called a good repair failed
ROOT_DISK_MIN_GB = 10       # below this the server's own disk is about to cause trouble
METADL_STUCK_H = 24         # a magnet still without metadata after a day is dead
SETPORT_TIMEOUT_S = 300     # setport.sh retries until qBittorrent's API answers
QBIT_TIMEOUT = 20


def _is_timeout(e: BaseException) -> bool:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return True
    return isinstance(e, urllib.error.URLError) and isinstance(e.reason, (socket.timeout, TimeoutError))


def _err(e: BaseException) -> str:
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


def docker(*args: str, timeout: int = 60):
    """(returncode, stdout); returncode None when docker could not run."""
    try:
        r = settings.docker(*args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        return None, str(e)
    return r.returncode, r.stdout


def container_state(name: str):
    """(State.Status, State.Error) of a container; ("", "") when unknown."""
    rc, out = docker("inspect", "-f", "{{.State.Status}}|{{.State.Error}}", name, timeout=30)
    status, _, error = out.strip().partition("|") if rc == 0 else ("", "", "")
    return status, error.strip()


def qbit_switched_off() -> bool:
    """Stopped on purpose (downloads-off.sh or `docker compose stop`): stopped with no start
    error. One that FAILED to start (its VPN container was recreated under it) is a fault."""
    status, error = container_state("qbittorrent")
    return status in ("exited", "created") and not error


def _send(text: str, dry: bool) -> None:
    if dry:
        settings.log(TAG, "[dry-run] would send: " + text.replace("\n", " | "))
    else:
        settings.notify(text)


# ----------------------------------------------------------------- health

def forwarded_port() -> str:
    rc, out = docker("exec", "gluetun", "cat", "/tmp/gluetun/forwarded_port", timeout=30)
    out = (out or "").strip()
    return out if rc == 0 and out.isdigit() else ""


def health(pf_on: bool):
    """(ok, reasons, info)."""
    try:
        info = settings.qbit("transfer/info", timeout=QBIT_TIMEOUT)
        if not isinstance(info, dict):
            raise RuntimeError("unexpected reply")
    except Exception as e:
        return False, ["qBittorrent API not answering (%s)" % _err(e)], {}
    try:
        prefs = settings.qbit("app/preferences", timeout=QBIT_TIMEOUT)
        prefs = prefs if isinstance(prefs, dict) else {}
    except Exception:
        prefs = {}
    reasons = []
    status = info.get("connection_status")
    if status == "disconnected":
        reasons.append("connection_status=disconnected")
    elif status == "firewalled" and pf_on:
        reasons.append("connection_status=firewalled")
    dht = info.get("dht_nodes")
    if dht is None:
        try:
            maindata = settings.qbit("sync/maindata?rid=0", timeout=QBIT_TIMEOUT) or {}
            dht = (maindata.get("server_state") or {}).get("dht_nodes")
        except Exception:
            dht = None
    if prefs.get("dht", True) and (dht or 0) < MIN_DHT:
        reasons.append("dht_nodes=%s" % dht)
    fp, lp = "", str(prefs.get("listen_port", ""))
    if pf_on:
        fp = forwarded_port()
        if fp and lp and fp != lp:
            reasons.append("port mismatch: forwarded=%s qbit=%s" % (fp, lp))
    info["_fp"], info["_lp"], info["_dht"] = fp, lp, dht
    return not reasons, reasons, info


def deps_of_gluetun() -> list:
    """RUNNING containers sharing gluetun's network namespace: they must restart with it.
    A stopped one (downloads switched off) stays stopped."""
    rc, gid = docker("inspect", "-f", "{{.Id}}", "gluetun", timeout=30)
    gid = (gid or "").strip()
    rc2, names = docker("ps", "--format", "{{.Names}}", timeout=30)
    names = [n for n in (names or "").split() if n != "gluetun"] if rc2 == 0 else []
    if rc != 0 or not gid or not names:
        return []
    rc3, out = docker("inspect", "-f", "{{.Name}} {{.HostConfig.NetworkMode}}", *names, timeout=60)
    deps = []
    for line in (out or "").splitlines() if rc3 == 0 else []:
        parts = line.strip().split(" ", 1)
        if len(parts) == 2 and parts[1] in ("container:" + gid, "container:gluetun", "service:gluetun"):
            deps.append(parts[0].lstrip("/"))
    return deps


def repair(pf_on: bool):
    settings.log(TAG, "REPAIR: restarting gluetun")
    deps = deps_of_gluetun()
    settings.log(TAG, "  containers sharing its network: %s" % (", ".join(deps) or "(none)"))
    try:
        r = settings.compose("restart", "gluetun", timeout=300)
        if r.returncode != 0:
            settings.log(TAG, "  compose restart reported: %s" % (r.stderr or r.stdout).strip()[:200])
    except (OSError, subprocess.SubprocessError) as e:
        settings.log(TAG, "  compose restart failed: %s" % e)
    for i in range(HEALTHY_TRIES):
        time.sleep(HEALTHY_EVERY_S)
        rc, out = docker("inspect", "-f", "{{if .State.Health}}{{.State.Health.Status}}{{end}}", "gluetun", timeout=30)
        if rc == 0 and out.strip() == "healthy":
            settings.log(TAG, "  gluetun healthy after %d s" % ((i + 1) * HEALTHY_EVERY_S))
            break
    else:
        settings.log(TAG, "  WARNING: gluetun did not report healthy")
    for c in deps:
        docker("restart", c, timeout=180)
        settings.log(TAG, "  restarted %s" % c)
    status, error = container_state("qbittorrent")
    if status != "running" and error:
        # It failed to start (typically a stale reference to an old VPN container): only
        # recreating it joins the new network namespace. A deliberate stop has no error.
        settings.log(TAG, "  qbittorrent failed to start (%s); recreating it" % error[:120])
        try:
            settings.compose("up", "-d", "qbittorrent", timeout=300)
        except (OSError, subprocess.SubprocessError) as e:
            settings.log(TAG, "  could not recreate qbittorrent: %s" % e)
    ok, reasons, info = False, [], {}
    for i in range(VERIFY_TRIES):
        time.sleep(VERIFY_EVERY_S)
        ok, reasons, info = health(pf_on)
        settings.log(TAG, "  post-repair check %d/%d: ok=%s reasons=%s dht=%s dl=%.1f KB/s port=%s"
                     % (i + 1, VERIFY_TRIES, ok, reasons, info.get("_dht"),
                        (info.get("dl_info_speed") or 0) / 1024, info.get("_fp") or "-"))
        if ok:
            break
    return ok, reasons, info


# ----------------------------------------------------------------- extra self-checks

def extra_checks(st: dict, dry: bool) -> list:
    """Cheap self-checks for silent failures nothing else watches. Each alerts once."""
    alerts = []
    gone = [p for p in [settings.MEDIA_ROOT] + settings.get_list("MOUNT_PATHS") if not os.path.isdir(p)]
    if gone and not st.get("mount_alerted"):
        alerts.append("The media folder (or one of MOUNT_PATHS) is missing; mount_guard has the details.")
        st["mount_alerted"] = True
    elif not gone and st.get("mount_alerted"):
        alerts.append("The media folder is back.")
        st["mount_alerted"] = False
    try:
        v = os.statvfs("/")
        free_gb = v.f_bavail * v.f_frsize / 2 ** 30
        if free_gb < ROOT_DISK_MIN_GB and (dry or alertstate.digest_due("vpn_watchdog_root_disk")):
            alerts.append("The server's root disk is low: %.1f GB free." % free_gb)
    except OSError:
        pass
    try:
        tors = settings.qbit("torrents/info", timeout=QBIT_TIMEOUT)
    except Exception:
        tors = None
    if isinstance(tors, list):
        now = time.time()
        stuck = {t.get("hash"): (t.get("name") or "")[:40] for t in tors
                 if t.get("state") in ("metaDL", "forcedMetaDL")
                 and now - (t.get("added_on") or now) > METADL_STUCK_H * 3600}
        new = list(stuck) if dry else alertstate.filter_new("vpn_watchdog_metadl", sorted(stuck))[0]
        if new:
            alerts.append("Magnet link(s) stuck for over a day fetching metadata (likely dead, needs "
                          "replacing): " + "; ".join(stuck[h] for h in new[:4]))
    return alerts


# ----------------------------------------------------------------- main

def run(dry: bool) -> int:
    if settings.get("VPN_MODE", "on").strip().lower() == "off":
        settings.log(TAG, "VPN_MODE=off (test install): nothing to watch")
        return 0
    pf_on = settings.get_bool("VPN_PORT_FORWARDING")
    st_path = settings.state_path(STATE_NAME)
    st = settings.load_json(st_path, {})

    def save() -> None:
        if not dry:
            try:
                settings.save_json(st_path, st)
            except OSError as e:
                settings.log(TAG, "could not save state: %s" % e)

    try:
        alerts = extra_checks(st, dry)
    except Exception as e:
        alerts = []
        settings.log(TAG, "extra checks error: %s" % e)
    for a in alerts:
        settings.log(TAG, "SELFCHECK: " + a)
    if alerts:
        _send("Media watchdog\n\n" + "\n".join("• " + html.escape(a, quote=False) for a in alerts), dry)

    if qbit_switched_off():
        settings.log(TAG, "qBittorrent is stopped (downloads switched off); nothing to check")
        st["fails"] = 0
        save()
        return 0

    ok, reasons, info = health(pf_on)

    # A wrong listen port has a direct, cheap fix: push the forwarded port into qBittorrent.
    mismatch_seen = any(r.startswith("port mismatch") for r in reasons)
    if not ok and pf_on and mismatch_seen and info.get("_fp"):
        if dry:
            settings.log(TAG, "[dry-run] would run setport.sh %s inside gluetun" % info["_fp"])
        else:
            settings.log(TAG, "port mismatch: pushing the forwarded port %s into qBittorrent" % info["_fp"])
            docker("exec", "gluetun", "/scripts/setport.sh", info["_fp"], timeout=SETPORT_TIMEOUT_S)
            ok, reasons, info = health(pf_on)
            if not any(r.startswith("port mismatch") for r in reasons):
                new, _ = alertstate.filter_new("vpn_watchdog_port_fix", ["port-fixed"])
                if new:
                    settings.notify("VPN watchdog\n\nqBittorrent was listening on the wrong port; the VPN's "
                                    "forwarded port was pushed into it again. No action needed.")
    if not dry and not mismatch_seen:
        alertstate.filter_new("vpn_watchdog_port_fix", [])   # a clean run re-arms the one-time note

    dl_kbps = (info.get("dl_info_speed") or 0) / 1024
    if not ok and dl_kbps > ACTIVE_KBPS:
        settings.log(TAG, "unhealthy (%s) BUT downloading at %.0f KB/s; leaving it alone"
                     % (", ".join(reasons), dl_kbps))
        st["fails"] = 0
        save()
        return 0

    if ok:
        if st.get("fails"):
            settings.log(TAG, "recovered on its own (was %d failed check(s))" % st["fails"])
        if st.get("repair_failed_alerted"):
            _send("All clear: VPN\n\nThe VPN and downloads are healthy again. The earlier \"repair did not "
                  "fix it\" alert is resolved; no action needed.", dry)
            st["repair_failed_alerted"] = False
        st["fails"] = 0
        save()
        settings.log(TAG, "healthy: dht=%s port=%s dl=%.0f KB/s" % (info.get("_dht"), info.get("_lp") or "-", dl_kbps))
        return 0

    st["fails"] = int(st.get("fails") or 0) + 1
    settings.log(TAG, "UNHEALTHY (%d/%d): %s" % (st["fails"], FAILS_NEEDED, ", ".join(reasons)))
    save()
    if st["fails"] < FAILS_NEEDED:
        return 0
    mins_since = (time.time() - float(st.get("last_restart") or 0)) / 60
    if mins_since < COOLDOWN_MIN:
        settings.log(TAG, "cooldown active (%.0f/%d min); not restarting again yet" % (mins_since, COOLDOWN_MIN))
        return 1
    if dry:
        settings.log(TAG, "[dry-run] would restart gluetun and the containers sharing its network")
        return 1

    st["last_restart"] = time.time()
    st["fails"] = 0
    save()
    healed, reasons2, info2 = repair(pf_on)
    if healed:
        settings.notify("VPN watchdog\n\nqBittorrent had lost its peer connections (%s). The VPN tunnel and "
                        "qBittorrent were restarted and it is working again: DHT %s nodes, port %s, %.0f KB/s."
                        % (html.escape(", ".join(reasons), quote=False), info2.get("_dht"),
                           info2.get("_fp") or info2.get("_lp") or "-", (info2.get("dl_info_speed") or 0) / 1024))
        return 0
    st["repair_failed_alerted"] = True
    save()
    settings.notify("VPN watchdog\n\nDownloads lost connectivity and the automatic restart has not fixed it "
                    "yet (%s).\n\nIt keeps checking and sends an all-clear if it recovers by itself. If nothing "
                    "arrives within 30 minutes, check the VPN provider's status and the gluetun container's log."
                    % html.escape(", ".join(reasons2), quote=False))
    return 1


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Self-heal qBittorrent's connectivity behind the VPN.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="check and print only; restart nothing, send nothing")
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
