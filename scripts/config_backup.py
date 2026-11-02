#!/usr/bin/env python3
"""config_backup: back up every piece of the stack's configuration, nightly.

Why it exists: the media is re-downloadable; the configuration is not. Losing it means
rebuilding by hand: series and their monitoring, root folders, indexers with their proxy
tags and limits, quality profiles and size caps, custom formats, Bazarr providers, the bot's
state. That is a weekend of work sitting in a few hundred megabytes.

Where: settings.BACKUP_DIR (default ./backups). Point it at a DIFFERENT physical disk from the
one that holds config/, or a single disk failure takes the configuration and its backups
together. The folder is created mode 700 and every archive is written mode 600: they contain
API keys, the qBittorrent password hash and the stack's .env.

How
 - Radarr, Sonarr, Prowlarr have a native Backup command that writes a consistent zip of
   their live SQLite database. A naive file copy can capture a torn snapshot, so this asks
   for that backup (POST command {"name": "Backup"}), waits for it, finds the newest file
   through GET system/backup in config/<app>/Backups/, and copies it. A complete-but-stale
   archive passes every integrity check while being the wrong data, so a source older than
   30 minutes is refused rather than stamped with today's date, and a half-written one (the
   apps write incrementally) is waited for and then opened and tested before it counts.
 - Bazarr, Tautulli, Jellyseerr, Uptime Kuma (each only when its profile is on), qBittorrent
   (its settings only, not the torrent session), the bot's and guards' state (./state) and
   the stack's .env are tarred, skipping caches and logs (logs restore nothing and can
   balloon an archive a hundredfold). SQLite files inside them are captured through
   SQLite's own backup API, which is safe while the app is running.
 - Every archive is verified (opened and read end to end) before it replaces anything, and
   the newest 7 per app are kept. Only files this script named are ever rotated out.
 - It touches no media file.

Runs nightly from cron. A missing or stale backup of any enabled app is reported once
(alertstate) and makes the run exit 1, so a push monitor turns red; success is silent.
Flags: --dry-run  list what would be backed up; ask no app for a backup, write nothing.
Exit:  0 all backups fresh and verified, 1 something is missing or stale, 2 the guard failed.
"""
from __future__ import annotations

import argparse
import datetime
import html
import os
import re
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import time
import traceback
import urllib.parse
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import alertstate  # noqa: E402
import settings  # noqa: E402

JOB = "config_backup"
TAG = "[config_backup]"

KEEP = 7                       # archives kept per app
MAX_SOURCE_AGE_S = 1800        # an app backup older than 30 min is not this run's
COMMAND_WAIT_S = 180           # how long an app may take to finish its Backup command
POLL_S = 5
STABLE_TRIES, STABLE_GAP_S = 24, 5   # up to 2 min for an app to finish writing its zip
COPY_RETRY_WAIT_S = 10
API_TIMEOUT = 60

ARR_APPS = (("radarr", "Radarr"), ("sonarr", "Sonarr"), ("prowlarr", "Prowlarr"))
# (archive name, label, folder, extra folder names to skip, profile or None for always-on)
TAR_APPS = (
    ("bazarr", "Bazarr", ("config", "bazarr"), {"backup"}, "subtitles"),
    ("tautulli", "Tautulli", ("config", "tautulli"), {"backups", "newsletters"}, "monitoring"),
    ("jellyseerr", "Jellyseerr", ("config", "jellyseerr"), set(), None),
    ("uptime-kuma", "Uptime Kuma", ("config", "uptime-kuma"), {"screenshots"}, "monitoring"),
    # settings only: the torrent session (BT_backup) is large and rebuilt by the apps' re-grabs
    ("qbittorrent", "qBittorrent", ("config", "qbittorrent"), {"BT_backup", "GeoDB"}, None),
    ("state", "Bot and guard state", ("state",), {"confirms"}, None),
)
SKIP_DIRS = {"cache", "Cache", "log", "logs", "Logs", "__pycache__", "tmp"}
SKIP_FILE = re.compile(r"(\.log(\.\d+)?|\.out|\.pid|\.lock|\.sock|\.tmp)$", re.I)
SQLITE_EXT = (".db", ".sqlite", ".sqlite3")
SQLITE_SIDE = ("-wal", "-shm", "-journal")


# ----------------------------------------------------------------- archives

def _private_open(path: str, mode: str = "wb"):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.chmod(path, 0o600)
    return os.fdopen(fd, mode)


def verify_zip(path: str) -> bool:
    """A copied archive only counts if it opens and every member checksums."""
    try:
        with zipfile.ZipFile(path) as z:
            return z.testzip() is None and bool(z.namelist())
    except Exception:
        return False


def verify_tgz(path: str) -> bool:
    """Read every member to the end: that checks the gzip CRC as well as the tar structure."""
    try:
        with tarfile.open(path, "r:gz") as t:
            count = 0
            for m in t:
                count += 1
                if m.isfile():
                    f = t.extractfile(m)
                    while f is not None and f.read(1 << 20):
                        pass
            return count > 0
    except Exception:
        return False


def stable_zip(path: str) -> bool:
    """True once the file has stopped growing AND opens as a complete archive. The apps write
    the archive incrementally; a mid-write copy has a valid header and no central directory,
    which passes a naive existence check. Size-stability alone is not enough either (a stall
    mid-write looks stable), so the archive is actually opened and tested."""
    last = -1
    for _ in range(STABLE_TRIES):
        try:
            sz = os.path.getsize(path)
        except OSError:
            return False
        if sz == last and sz > 0 and verify_zip(path):
            return True
        last = sz
        time.sleep(STABLE_GAP_S)
    return False


def _sqlite_base(name: str) -> str:
    """'app.db' for 'app.db-wal' / 'app.db-shm' / 'app.db-journal', else ''."""
    for s in SQLITE_SIDE:
        if name.endswith(s) and name[: -len(s)].endswith(SQLITE_EXT):
            return name[: -len(s)]
    return ""


def _sqlite_snapshot(src: str, dst: str) -> bool:
    """Consistent copy of a live SQLite database (includes committed WAL content). Opened
    read-only, so the app's own database can never be written to from here."""
    try:
        s = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(src), uri=True, timeout=30)
        try:
            d = sqlite3.connect(dst)
            try:
                s.backup(d)
            finally:
                d.close()
        finally:
            s.close()
        return True
    except sqlite3.Error:
        return False


def tar_folder(src: str, arcroot: str, out_tmp: str, skip_dirs: set, exclude_real: list):
    """Write a .tgz of `src` to out_tmp. Returns (files added, unreadable files, raw db copies)."""
    added, unreadable, raw_db = 0, [], []
    with tempfile.TemporaryDirectory(prefix="cfgbak-") as snapdir, \
            _private_open(out_tmp) as fo, tarfile.open(fileobj=fo, mode="w:gz") as tar:
        for dp, dirs, names in os.walk(src):
            snapped = set()                   # databases captured through the backup API here
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and d not in skip_dirs
                             and not any(os.path.realpath(os.path.join(dp, d)) == x for x in exclude_real))
            for n in sorted(names):
                p = os.path.join(dp, n)
                if SKIP_FILE.search(n) or os.path.islink(p) or not os.path.isfile(p):
                    continue
                arc = os.path.join(arcroot, os.path.relpath(p, src))
                if _sqlite_base(n) in snapped:
                    continue                  # its content is inside the database snapshot
                if n.endswith(SQLITE_EXT):
                    snap = os.path.join(snapdir, "%d.db" % added)
                    if _sqlite_snapshot(p, snap):
                        tar.add(snap, arcname=arc)
                        snapped.add(n)
                        added += 1
                        continue
                    raw_db.append(arc)        # not a database, or not readable as one: copy as is
                try:
                    tar.add(p, arcname=arc)
                    added += 1
                except OSError:
                    unreadable.append(arc)
    return added, unreadable, raw_db


# ----------------------------------------------------------------- per app

def arr_backup(name: str, label: str, dest: str, stamp: str):
    """Ask the app for a consistent backup and copy it. Returns (ok, detail)."""
    a = settings.app(name)
    if not a.key:
        return False, "no API key found"
    try:
        cmd = settings.api(a, "command", method="POST", data={"name": "Backup"}, timeout=API_TIMEOUT) or {}
        deadline = time.time() + COMMAND_WAIT_S
        while cmd.get("id") and cmd.get("status") not in ("completed", "failed", "aborted", "cancelled"):
            if time.time() > deadline:
                break
            time.sleep(POLL_S)
            cmd = settings.api(a, "command/%d" % cmd["id"], timeout=API_TIMEOUT) or {}
        if cmd.get("status") in ("failed", "aborted"):
            settings.log(TAG, "%s: its Backup command reported %s" % (label, cmd.get("status")))
    except Exception as e:
        settings.log(TAG, "%s: backup command failed (%s); looking for an existing archive" % (label, e))

    bdir = os.path.join(settings.CONFIG_DIR, name, "Backups")
    cands = []
    try:
        for b in settings.api(a, "system/backup", timeout=API_TIMEOUT) or []:
            p = os.path.join(bdir, b.get("type") or "manual", b.get("name") or "")
            if b.get("type") == "manual" and os.path.isfile(p):
                cands.append(p)
    except Exception as e:
        settings.log(TAG, "%s: backup list unavailable (%s)" % (label, e))
    if not cands:
        # Only manual/: the app writes the backup WE asked for there. scheduled/ holds archives
        # weeks old that would otherwise look like a valid "newest".
        mdir = os.path.join(bdir, "manual")
        for root in ([mdir] if os.path.isdir(mdir) else [bdir]):
            for dp, _d, files in os.walk(root):
                cands += [os.path.join(dp, f) for f in files if f.endswith(".zip")]
    cands = [c for c in cands if os.path.isfile(c)]
    if not cands:
        return False, "no backup archive found"
    src = max(cands, key=os.path.getmtime)
    age = time.time() - os.path.getmtime(src)
    if age > MAX_SOURCE_AGE_S:
        return False, "no fresh backup was produced (the newest is %.1f h old)" % (age / 3600.0)
    if not stable_zip(src):
        return False, "the app's backup archive never finished writing"
    dst = os.path.join(dest, "%s-%s.zip" % (name, stamp))
    tmp = dst + ".partial"
    for attempt in (1, 2):
        try:
            with open(src, "rb") as fi, _private_open(tmp) as fo:
                shutil.copyfileobj(fi, fo)
        except OSError as e:
            _remove(tmp)
            return False, "copy failed (%s)" % (e.strerror or e)
        if verify_zip(tmp):
            os.replace(tmp, dst)
            return True, "%.1f MB, verified" % (os.path.getsize(dst) / 1e6)
        settings.log(TAG, "%s: copied archive failed verification (attempt %d)" % (label, attempt))
        time.sleep(COPY_RETRY_WAIT_S)
    _remove(tmp)                              # never leave a corrupt archive looking like a backup
    return False, "no verifiable copy could be written"


def tar_backup(name: str, src: str, skip: set, dest: str, stamp: str):
    if not os.path.isdir(src):
        if name == "state":
            return True, "no state folder yet; nothing to back up"
        return False, "its config folder does not exist"
    dst = os.path.join(dest, "%s-%s.tgz" % (name, stamp))
    tmp = dst + ".partial"
    exclude_real = [os.path.realpath(dest)]   # never archive the backups into themselves
    try:
        added, unreadable, raw_db = tar_folder(src, name, tmp, skip, exclude_real)
    except (OSError, tarfile.TarError) as e:
        _remove(tmp)
        return False, "archive failed (%s)" % e
    if not added or not verify_tgz(tmp):
        _remove(tmp)
        return False, "the archive is empty or did not verify"
    os.replace(tmp, dst)
    for arc in raw_db:
        settings.log(TAG, "%s: %s copied as a plain file (not readable as SQLite)" % (name, arc))
    if unreadable:
        return False, "%d file(s) could not be read (permissions), e.g. %s" % (len(unreadable), unreadable[0])
    return True, "%.1f MB, %d files, verified" % (os.path.getsize(dst) / 1e6, added)


def env_backup(dest: str, stamp: str):
    env = settings.ENV_FILE
    if not os.path.isfile(env):
        return False, "the stack's .env file does not exist"
    dst = os.path.join(dest, "env-%s.tgz" % stamp)
    tmp = dst + ".partial"
    try:
        with _private_open(tmp) as fo, tarfile.open(fileobj=fo, mode="w:gz") as tar:
            tar.add(env, arcname=".env")
            override = os.path.join(settings.STACK_DIR, "docker-compose.override.yml")
            if os.path.isfile(override):
                tar.add(override, arcname="docker-compose.override.yml")
    except (OSError, tarfile.TarError) as e:
        _remove(tmp)
        return False, "archive failed (%s)" % e
    if not verify_tgz(tmp):
        _remove(tmp)
        return False, "the archive did not verify"
    os.replace(tmp, dst)
    return True, "verified (mode 600)"


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def rotate(dest: str, names: list) -> None:
    """Keep the newest KEEP archives per app. Only names this script writes are touched."""
    for name in names:
        rx = re.compile(r"^%s-(\d{8})\.(zip|tgz)$" % re.escape(name))
        files = sorted((f for f in os.listdir(dest) if rx.match(f)),
                       key=lambda f: (rx.match(f).group(1), os.path.getmtime(os.path.join(dest, f))),
                       reverse=True)
        for old in files[KEEP:]:
            _remove(os.path.join(dest, old))
            settings.log(TAG, "rotated out %s" % old)


def newest_age_days(dest: str, name: str):
    rx = re.compile(r"^%s-\d{8}\.(zip|tgz)$" % re.escape(name))
    try:
        ages = [time.time() - os.path.getmtime(os.path.join(dest, f)) for f in os.listdir(dest) if rx.match(f)]
    except OSError:
        return None
    return min(ages) / 86400.0 if ages else None


# ----------------------------------------------------------------- main

def prepare_dest(dest: str) -> str:
    """'' when the backup folder is usable, else the reason. Never creates a folder tree under a
    share that is not mounted: an unmounted share must not be "backed up" into an empty dir."""
    for mp in settings.get_list("MOUNT_PATHS"):
        mp = os.path.normpath(mp)
        if (dest == mp or dest.startswith(mp + os.sep)) and not os.path.ismount(mp):
            return "the backup folder lives on %s, which is not mounted" % mp
    if not os.path.isdir(dest):
        if not os.path.isdir(os.path.dirname(dest)):
            return "the backup folder's parent folder does not exist"
        try:
            os.mkdir(dest, 0o700)
        except OSError as e:
            return "cannot create the backup folder (%s)" % (e.strerror or e)
    try:
        os.chmod(dest, 0o700)
    except OSError:
        pass
    return ""


def plan() -> list:
    """[(kind, name, label, extra)] for every enabled app, in run order."""
    items = [("arr", n, lbl, None) for n, lbl in ARR_APPS]
    for name, lbl, folder, skip, profile in TAR_APPS:
        if profile and not settings.profile_enabled(profile):
            continue
        base = settings.STATE_DIR if folder == ("state",) else os.path.join(settings.STACK_DIR, *folder)
        items.append(("tar", name, lbl, (base, skip)))
    items.append(("env", "env", "The stack's .env", None))
    return items


def run(dry: bool) -> int:
    dest = os.path.normpath(settings.BACKUP_DIR)
    stamp = datetime.datetime.now().strftime("%Y%m%d")
    items = plan()
    results = {}
    if dry:
        for kind, name, lbl, extra in items:
            if kind == "tar" and not os.path.isdir(extra[0]) and name != "state":
                results[name] = (lbl, False, "its config folder does not exist")
            elif kind == "arr" and not settings.app(name).key:
                results[name] = (lbl, False, "no API key found")
            elif kind == "env" and not os.path.isfile(settings.ENV_FILE):
                results[name] = (lbl, False, "the stack's .env file does not exist")
            else:
                settings.log(TAG, "[dry-run] would back up %s" % lbl)
        for lbl, _ok, detail in results.values():
            settings.log(TAG, "[dry-run] would FAIL: %s: %s" % (lbl, detail))
        return 1 if results else 0

    why = prepare_dest(dest)
    if why:
        settings.log(TAG, "ABORT: %s; refusing to write backups" % why)
        results["dest"] = ("Backup folder", False, why)
    else:
        for kind, name, lbl, extra in items:
            try:
                if kind == "arr":
                    ok, detail = arr_backup(name, lbl, dest, stamp)
                elif kind == "tar":
                    ok, detail = tar_backup(name, extra[0], extra[1], dest, stamp)
                else:
                    ok, detail = env_backup(dest, stamp)
            except Exception as e:            # one app's failure never stops the others
                ok, detail = False, "%s: %s" % (type(e).__name__, e)
            results[name] = (lbl, ok, detail)
            settings.log(TAG, "%s: %s%s" % (lbl, "" if ok else "FAILED: ", detail))
        rotate(dest, [i[1] for i in items])
        try:
            total = sum(os.path.getsize(os.path.join(dest, f)) for f in os.listdir(dest)
                        if os.path.isfile(os.path.join(dest, f)))
            settings.log(TAG, "done: %d archives this run, %.0f MB held in the backup folder"
                         % (sum(1 for r in results.values() if r[1]), total / 1e6))
        except OSError:
            pass

    failed = {k: v for k, v in results.items() if not v[1]}
    new, persisting = alertstate.filter_new("config_backup", sorted(failed))
    if new or (persisting and alertstate.digest_due("config_backup_digest")):
        lines = []
        for k in sorted(failed):
            lbl, _ok, detail = failed[k]
            age = newest_age_days(dest, k) if k != "dest" else None
            last = (" Last good copy: %.0f day(s) old." % age) if age is not None else ""
            lines.append("• %s: %s.%s" % (lbl, html.escape(detail, quote=False), last))
        settings.notify("Config backup incomplete\n\n%d of %d configuration backups failed tonight:\n\n%s\n\n"
                        "Your media is unaffected; this only covers the settings that would have to be "
                        "rebuilt by hand after a disk loss. The backup log has the details."
                        % (len(failed), len(items), "\n".join(lines)))
    return 1 if failed else 0


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Back up the stack's configuration.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="list what would be backed up; write nothing, send nothing")
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
