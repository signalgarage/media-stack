#!/usr/bin/env python3
"""mount_guard: keep the media storage mounted, responsive and VISIBLE inside every container.

Why it exists: after a power cut a network share or USB disk can come back minutes after
the server. If its one mount attempt fails, nothing retries it, and the apps run on an empty
folder (root folders "missing", torrents "missing files", downloads at zero) while every
web page still loads, so a page monitor stays green.

What it does (every 2 minutes from cron)
 1. HOST   Every path in MOUNT_PATHS must be a mount point in the kernel's mount table
           (read from /proc/self/mountinfo, because stat() on a hung share can block
           forever). A missing one that is listed in /etc/fstab gets its systemd mount unit
           started (`systemd-escape -p --suffix=mount <path>`, through `sudo -n` unless the
           guard runs as root); if that does not work it is reported and retried next run.
           With MOUNT_PATHS empty, MEDIA_ROOT just has to exist.
 2. ALIVE  Each path (and MEDIA_ROOT) must answer a directory listing within 25 s and report
           its free space: a stale share connection can still list folders yet fail
           free-space queries, and the apps then silently skip their free-space checks before
           importing. The probes run in a child process that is abandoned on timeout, never
           in this process: a process stuck on a dead share can be unkillable.
           Optional canary: MOUNT_CANARY_FILE must have been modified within
           MOUNT_CANARY_MAX_AGE_S (default 900 s). Let a job on the storage side touch it every
           few minutes; a stale canary means the storage stopped taking writes, or that job
           died. This guard only alerts on it.
 3. INSIDE Docker binds are rprivate: a share mounted AFTER a container started stays invisible
           inside it (the container keeps the empty folder underneath, or an old instance of
           the share) until that container restarts. For every running container that binds
           MEDIA_ROOT at /data (radarr, sonarr, bazarr, moviebot, plex, qbittorrent), the mount
           device behind /data (and behind each MOUNT_PATHS entry under MEDIA_ROOT) is compared
           with the host's. Only the containers' own mount tables are read, so this cannot
           hang on the share. A stale container is restarted ONE AT A TIME with a pause
           (several simultaneous restarts plus a big file move once froze a USB disk),
           qBittorrent last, each container at most once per 30 minutes and 3 times a day.
           Rejected alternative: `rslave` bind propagation; with nested binds a late outer
           mount hides the inner one, and an app would see an empty folder.
 Exit 0 only when every path is mounted, alive and correctly visible everywhere; anything
 else exits 1, so the push monitor turns red.

Safety: never unmounts, never touches a media file, never restarts a container while the
storage itself is unhealthy (a restart cannot fix that and can hang on a dead mount), and a
run lock stops overlapping runs piling up behind a hung share. Alerts are de-duplicated
through its own state file; repairs and recoveries are always reported.

Deliberately NOT automated: clearing a stale CIFS directory cache by writing to
/sys/module/cifs/parameters/drop_dir_cache. It works, but the same write has triggered a
kernel "scheduling while atomic" bug. The safe repair is a clean remount at a quiet moment.

Settings: MEDIA_ROOT, MOUNT_PATHS, MOUNT_CANARY_FILE, MOUNT_CANARY_MAX_AGE_S, VPN_PORT_FORWARDING.
Flags: --dry-run  report what it would do; mount nothing, restart nothing, send nothing.
"""
from __future__ import annotations

import argparse
import fcntl
import html
import json
import os
import re
import subprocess
import sys
import time
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

JOB = "mount_guard"
TAG = "[mount_guard]"
STATE_NAME = ".mount_guard_state.json"
LOCK_NAME = ".mount_guard.lock"

PROBE_TIMEOUT_S = 25           # a healthy share lists a folder in well under a second
MOUNT_TIMEOUT_S = 120          # a slow NAS can take a while to accept a mount
DOCKER_TIMEOUT_S = 30
RESTART_TIMEOUT_S = 180
SETPORT_TIMEOUT_S = 300        # setport.sh retries until qBittorrent's API is up again
RESTART_COOLDOWN_S = 1800      # a given container is restarted at most once per 30 min...
MAX_RESTARTS_PER_DAY = 3       # ...and 3 times a day; after that it only alerts
RESTART_GAP_S = 20             # pause between two container restarts: never a burst
VERIFY_TRIES, VERIFY_EVERY_S = 18, 5   # up to 90 s for a restarted container to see the media
CANARY_MAX_AGE_DEFAULT_S = 900 # a canary touched every couple of minutes is stale after 15
# Filesystems through which Docker Desktop passes host folders into its containers: their device
# ids never match the host's, so a mismatch there says nothing about a stale view.
VM_PASSTHROUGH_FS = ("virtiofs", "fakeowner", "grpcfuse", "fuse.grpcfuse", "osxfs")
# Containers that bind MEDIA_ROOT at /data. qBittorrent goes last when several need a restart.
DATA_CONTAINERS = ("radarr", "sonarr", "bazarr", "moviebot", "plex", "qbittorrent")

# Runs in a child process (python3 -I) so a hung share can only ever stall the child.
_PROBE = r"""
import os, sys
what, path = sys.argv[1], sys.argv[2]
try:
    if what == "list":
        os.listdir(path)
    elif what == "statfs":
        os.statvfs(path)
    else:
        print(int(os.stat(path).st_mtime))
except FileNotFoundError:
    print("MISSING")
    sys.exit(3)
except OSError as e:
    print(e.strerror or type(e).__name__)
    sys.exit(4)
"""


# ----------------------------------------------------------------- process helpers

def run_cmd(cmd: list, timeout: int):
    """(returncode, output) without ever blocking past the timeout. A child stuck on a dead
    share can be unkillable, so on timeout it is killed and abandoned, never waited for.
    returncode is None when the command could not run or did not finish."""
    try:
        p = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True)
    except OSError as e:
        return None, str(e)
    try:
        out, _ = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            p.kill()
        except OSError:
            pass
        return None, "no answer within %d s" % timeout
    return p.returncode, out or ""


def probe(what: str, path: str, timeout: int = PROBE_TIMEOUT_S):
    """('ok', detail) | ('missing', '') | ('error', reason) | ('timeout', reason)."""
    rc, out = run_cmd([sys.executable, "-I", "-c", _PROBE, what, path], timeout)
    out = (out or "").strip()
    if rc is None:
        return "timeout", out
    if rc == 0:
        return "ok", out
    if rc == 3:
        return "missing", ""
    return "error", (out.splitlines() or ["failed"])[-1][:100]


def docker_run(*args: str, timeout: int = DOCKER_TIMEOUT_S):
    return run_cmd(settings.docker_cmd() + list(args), timeout)


# ----------------------------------------------------------------- mount tables

def _unescape(s: str) -> str:
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), s)


def parse_mountinfo(text: str) -> dict:
    """{mount point: (fstype, "major:minor")}; the LAST (topmost) mount of a point wins.
    The device id identifies the mounted instance, so an old instance of a share that a
    container still holds after the host remounted it is told apart from the live one."""
    res = {}
    for line in text.splitlines():
        left, _, right = line.partition(" - ")
        f = left.split()
        if len(f) >= 5 and right.split():
            res[_unescape(f[4])] = (right.split()[0], f[2])
    return res


def host_mounts():
    try:
        with open("/proc/self/mountinfo", encoding="utf-8", errors="replace") as fh:
            return parse_mountinfo(fh.read())
    except OSError:
        return None


def covering(mounts: dict, path: str):
    """(fstype, device) of the mount that holds `path` (the longest mount point that is a
    prefix of it), or None."""
    best = None
    for mp, entry in mounts.items():
        if path == mp or mp == "/" or path.startswith(mp.rstrip("/") + "/"):
            if best is None or len(mp) > len(best[0]):
                best = (mp, entry)
    return best[1] if best else None


def covering_device(mounts: dict, path: str):
    entry = covering(mounts, path)
    return entry[1] if entry else None


def fstab_mountpoints() -> set:
    out = set()
    try:
        with open("/etc/fstab", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                f = line.split()
                if len(f) >= 2 and not f[0].startswith("#"):
                    out.add(os.path.normpath(_unescape(f[1])))
    except OSError:
        pass
    return out


def is_mounted(mounts: dict, path: str) -> bool:
    e = mounts.get(path)
    return e is not None and e[0] != "autofs"   # a bare automount trap is not mounted yet


def try_mount(path: str):
    """Start the mount for an fstab path. Returns (rc, output)."""
    rc, unit = run_cmd(["systemd-escape", "-p", "--suffix=mount", path], 10)
    unit = (unit or "").strip()
    cmd = ["systemctl", "start", unit] if rc == 0 and unit else ["mount", path]
    if os.geteuid() != 0:
        cmd = ["sudo", "-n"] + cmd
    return run_cmd(cmd, MOUNT_TIMEOUT_S)


# ----------------------------------------------------------------- containers

def container_binds_data(name: str) -> bool:
    rc, out = docker_run("inspect", name, "--format", "{{json .Mounts}}")
    if rc != 0:
        return False
    try:
        mounts = json.loads(out or "[]") or []
    except ValueError:
        return False
    media = os.path.normpath(settings.MEDIA_ROOT)
    for m in mounts:
        if m.get("Destination") == settings.CONTAINER_DATA:
            src = os.path.normpath(m.get("Source") or "")
            if src == media or os.path.realpath(src) == os.path.realpath(media):
                return True
            settings.log(TAG, "%s binds a different folder at /data; not checked" % name)
    return False


def stale_views(name: str, pairs: list, hmounts: dict):
    """Container paths in `name` that do not show the host's live mount, or None when the
    container's mount table cannot be read. Reads only /proc/self/mountinfo inside the
    container: no I/O to the share, so a dead share cannot hang this check."""
    rc, out = docker_run("exec", name, "cat", "/proc/self/mountinfo")
    if rc != 0:
        return None
    cmounts = parse_mountinfo(out)
    bad = []
    for host_path, cpath in pairs:
        want, have = covering(hmounts, host_path), covering(cmounts, cpath)
        if not want:
            continue
        if have and have[0] in VM_PASSTHROUGH_FS and have[0] != want[0]:
            continue                      # Docker Desktop: not comparable, never a reason to restart
        if not have or have[1] != want[1]:
            bad.append(cpath)
    return bad


def running_data_containers() -> list:
    """The media containers that are up and steady. One that is restarting (a bad token, a crash
    loop) has no mounts to read and is not this guard's problem, so it is left out."""
    rc, out = docker_run("ps", "--format", "{{.Names}}\t{{.State}}\t{{.Status}}")
    if rc != 0:
        return []
    steady = set()
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[1] == "running" and not parts[2].startswith("Restarting"):
            steady.add(parts[0])
        elif len(parts) >= 1 and len(parts) < 3 and parts[0]:
            steady.add(parts[0])          # an older docker without the State column
    return [n for n in DATA_CONTAINERS if n in steady and container_binds_data(n)]


def push_forwarded_port() -> None:
    """A lone qBittorrent restart comes back on its default port; the VPN's forwarded port is
    normally pushed only when the tunnel reconnects, so push it right away."""
    if not settings.get_bool("VPN_PORT_FORWARDING") or settings.get("VPN_MODE", "on").lower() == "off":
        return
    rc, fp = docker_run("exec", "gluetun", "cat", "/tmp/gluetun/forwarded_port")
    fp = (fp or "").strip()
    if rc == 0 and fp.isdigit():
        docker_run("exec", "gluetun", "/scripts/setport.sh", fp, timeout=SETPORT_TIMEOUT_S)


# ----------------------------------------------------------------- state and alerts

def label(path: str) -> str:
    if os.path.normpath(path) == os.path.normpath(settings.MEDIA_ROOT):
        return "The media folder"
    return "The mount '%s'" % (os.path.basename(path.rstrip("/")) or path)


def _bullets(items) -> str:
    return "\n".join("• " + html.escape(t, quote=False) for t in items)


def _send(text: str, dry: bool) -> None:
    if dry:
        settings.log(TAG, "[dry-run] would send: " + text.replace("\n", " | "))
    else:
        settings.notify(text)


# ----------------------------------------------------------------- main work

def check(dry: bool, st: dict) -> int:
    problems, events = {}, []          # problems: stable key -> human text
    mount_paths = []
    for p in settings.get_list("MOUNT_PATHS"):
        p = os.path.normpath(p)
        if p not in mount_paths:
            mount_paths.append(p)
    media = os.path.normpath(settings.MEDIA_ROOT)
    hmounts = host_mounts()
    healthy, remounted = {}, set()

    # ---------- 1. HOST: each MOUNT_PATHS entry must be mounted ----------
    if mount_paths and hmounts is None:
        problems["mounttable"] = "Cannot read the kernel's mount table, so the mounts cannot be checked."
    fstab = fstab_mountpoints() if mount_paths else set()
    for path in mount_paths if hmounts is not None else []:
        if is_mounted(hmounts, path):
            continue
        if path not in fstab:
            problems["notmounted:" + path] = (
                "%s is NOT mounted, and it is not in /etc/fstab, so it cannot be mounted "
                "automatically. Anything using it sees an empty folder." % label(path))
            healthy[path] = False
            continue
        if dry:
            settings.log(TAG, "[dry-run] %s is not mounted; would start its mount" % path)
            problems["notmounted:" + path] = "%s is NOT mounted (dry run: no remount tried)." % label(path)
            healthy[path] = False
            continue
        rc, out = try_mount(path)
        hmounts = host_mounts() or {}
        if is_mounted(hmounts, path):
            remounted.add(path)
            events.append("%s had dropped or never mounted; mounted it again." % label(path))
            settings.log(TAG, "mounted %s" % path)
        else:
            detail = ((out or "").strip().splitlines() or ["no output"])[-1][:120]
            problems["notmounted:" + path] = (
                "%s is NOT mounted and a mount attempt failed (%s). Anything using it sees an "
                "empty folder; trying again next run." % (label(path), detail))
            settings.log(TAG, "mount of %s failed: %s" % (path, detail))
            healthy[path] = False

    # ---------- 2. ALIVE: listing, free space, canary ----------
    for path in mount_paths + ([media] if media not in mount_paths else []):
        if healthy.get(path) is False:
            continue
        if any(healthy.get(p) is False for p in mount_paths if path.startswith(p.rstrip("/") + "/")):
            healthy[path] = False         # the mount it lives on is already reported
            continue
        res, why = probe("list", path)
        if res == "missing":
            problems["missing:" + path] = ("%s does not exist. Anything using it sees an empty or "
                                           "missing folder." % label(path))
            healthy[path] = False
            continue
        if res != "ok":
            problems["hung:" + path] = ("%s is not responding (%s). The storage behind it may be "
                                        "frozen or unreachable." % (label(path), why or "error"))
            healthy[path] = False
            continue
        res, why = probe("statfs", path)
        if res != "ok":
            problems["statfs:" + path] = (
                "%s cannot report its free space (%s): a stale share connection. Needs a clean "
                "remount at a quiet moment (stop the containers using it, unmount, mount)."
                % (label(path), why or "no answer"))
        healthy[path] = True

    canary = settings.get("MOUNT_CANARY_FILE")
    if canary:
        canary = os.path.normpath(canary)
        max_age = settings.get_int("MOUNT_CANARY_MAX_AGE_S", CANARY_MAX_AGE_DEFAULT_S)
        owners = [p for p in healthy if canary == p or canary.startswith(p.rstrip("/") + "/")]
        owner = max(owners, key=len) if owners else ""
        if owner and not healthy.get(owner):
            pass                          # its storage is already reported
        elif owner in remounted:
            pass                          # it still holds the time from before the outage
        else:
            res, out = probe("mtime", canary)
            if res == "ok":
                try:
                    age = time.time() - int(out.split()[0])
                except (ValueError, IndexError):
                    age = 0               # unreadable content: never block on format issues
                if age > max_age:
                    problems["canary:" + canary] = (
                        "%s: the canary file has not been refreshed for %s. The storage has "
                        "stopped taking writes, or the job that refreshes the canary has stopped."
                        % (label(owner or canary), span(age)))
                    if owner:
                        healthy[owner] = False
            elif res == "missing":
                settings.log(TAG, "canary file not found (MOUNT_CANARY_FILE); not checked")
            else:
                problems["canary:" + canary] = "%s: cannot read the canary file (%s)." % (
                    label(owner or canary), why_text(res, out))
                if owner:
                    healthy[owner] = False

    # ---------- 3. INSIDE: what each container sees at /data ----------
    def ok_for(host_path: str) -> bool:
        deps = [p for p in healthy if host_path == p or host_path.startswith(p.rstrip("/") + "/")
                or p == media]
        return all(healthy.get(p) for p in deps)

    pairs = []
    if ok_for(media):
        # Docker resolves symlinks in a bind source, so the host side must as well. Safe here:
        # these paths have just answered within the probe timeout.
        media_real = os.path.realpath(media)
        pairs.append((media_real, settings.CONTAINER_DATA))
        for mp in mount_paths:
            for base in (media, media_real):
                if mp != base and mp.startswith(base.rstrip("/") + "/") and ok_for(mp):
                    rel = os.path.relpath(mp, base).replace(os.sep, "/")
                    pairs.append((os.path.realpath(mp), settings.CONTAINER_DATA + "/" + rel))
                    break
    stale = []
    if hmounts is None:
        settings.log(TAG, "no kernel mount table here; container views not checked")
    elif pairs:
        for name in running_data_containers():
            bad = stale_views(name, pairs, hmounts)
            if bad is None:
                problems["inspect:" + name] = "Could not read the mounts inside container %s." % name
            elif bad:
                stale.append((name, bad))

    now = time.time()
    restarted = 0
    for name, bad in sorted(stale, key=lambda s: s[0] == "qbittorrent"):   # downloader last
        what = "the media" if bad == [settings.CONTAINER_DATA] else "part of the media"
        today = [t for t in st.setdefault("restart_log", {}).get(name, []) if now - t < 86400]
        last = st.setdefault("restarts", {}).get(name, 0)
        # ONE stable key per container (stale:<name>): alternating keys used to send a false
        # "resolved" every cycle while the container was still broken.
        if len(today) >= MAX_RESTARTS_PER_DAY:
            problems["stale:" + name] = ("%s still cannot see %s and was restarted %d times today; no "
                                         "more automatic restarts until tomorrow." % (name, what, len(today)))
            continue
        if now - last < RESTART_COOLDOWN_S:
            problems["stale:" + name] = ("%s still cannot see %s, but it was restarted %d min ago; not "
                                         "restarting again yet (restart-loop guard)."
                                         % (name, what, (now - last) // 60))
            continue
        if dry:
            settings.log(TAG, "[dry-run] %s has a stale view of %s; would restart it" % (name, ", ".join(bad)))
            problems["stale:" + name] = "%s cannot see %s (dry run: not restarted)." % (name, what)
            continue
        if restarted:
            time.sleep(RESTART_GAP_S)
        settings.log(TAG, "restarting %s (stale view of %s)" % (name, ", ".join(bad)))
        docker_run("restart", name, timeout=RESTART_TIMEOUT_S)
        restarted += 1
        st["restarts"][name] = time.time()
        st["restart_log"][name] = today + [time.time()]
        if name == "qbittorrent":
            push_forwarded_port()
        fixed = False
        for _ in range(VERIFY_TRIES):
            time.sleep(VERIFY_EVERY_S)
            if stale_views(name, pairs, host_mounts() or hmounts) == []:
                fixed = True
                break
        if fixed:
            events.append("Restarted %s so it can see %s again." % (name, what))
        else:
            problems["stale:" + name] = "%s was restarted but still cannot see %s." % (name, what)

    # ---------- alerts: repairs always, new problems once, recoveries once ----------
    opened = st.get("open") or {}
    new = [k for k in problems if k not in opened]
    resolved = [opened[k] for k in opened if k not in problems]
    st["open"] = dict(problems)
    sections = []
    if events:
        sections.append("Media storage: repaired automatically\n\n" + _bullets(events))
    if new:
        sections.append("Media storage: PROBLEM\n\n" + _bullets(problems[k] for k in new))
    if resolved:
        sections.append("Media storage: resolved\n\n" + _bullets(r[:160] for r in resolved))
    if sections:
        _send("\n\n".join(sections), dry)
    for v in problems.values():
        settings.log(TAG, "PROBLEM " + v)
    for v in events:
        settings.log(TAG, "REPAIRED " + v)
    if not problems:
        settings.log(TAG, "all good: storage mounted, responsive and visible in %s" %
                     ("every container" if hmounts is not None else "this host"))
    return 1 if problems else 0


def span(seconds: float) -> str:
    m = int(seconds // 60)
    if m < 120:
        return "%d min" % m
    return "%d h" % (m // 60) if m < 48 * 60 else "%d days" % (m // 1440)


def why_text(res: str, out: str) -> str:
    return "no answer" if res == "timeout" else (out or res)


def run(dry: bool) -> int:
    lock_path = settings.state_path(LOCK_NAME + (".dry" if dry else ""))   # a dry run never blocks a real one
    try:
        lockf = open(lock_path, "a")
    except OSError as e:
        settings.log(TAG, "cannot open the run lock (%s)" % e)
        return 2
    try:
        fcntl.flock(lockf, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        settings.log(TAG, "previous run still active (probably stuck on a dead share); not piling up")
        return 1
    try:
        st_path = settings.state_path(STATE_NAME)
        st = settings.load_json(st_path, {})
        rc = check(dry, st)
        if not dry:
            try:
                settings.save_json(st_path, st)
            except OSError as e:
                settings.log(TAG, "could not save state: %s" % e)
        return rc
    finally:
        lockf.close()


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Keep the media storage mounted and visible in every container.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="report what would be done; mount nothing, restart nothing, send nothing")
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
