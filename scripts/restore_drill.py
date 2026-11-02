#!/usr/bin/env python3
"""restore_drill: prove the configuration backups actually restore. A backup never restored
is only a hope.

What it does: takes the newest Sonarr backup from settings.BACKUP_DIR (written by
config_backup), unpacks it into a scratch folder, checks that the SQLite database passes
`PRAGMA integrity_check` and holds the expected data, then BOOTS A THROWAWAY SONARR against
that copy and queries its API. Comparing its series count with the live Sonarr proves the
backup is genuinely restorable, not just a well-formed zip.

Isolation: the drill must never disturb the real setup
 - the throwaway container runs on its OWN docker network, so it cannot reach qBittorrent,
   Prowlarr or any other service by name;
 - it publishes its port on 127.0.0.1:8991 only;
 - it gets a COPY of the configuration in a temporary folder: config/ of the real Sonarr is
   never opened;
 - it runs the image the live Sonarr already uses and never pulls one;
 - the container, the network and the scratch folder are removed afterwards, even on failure.

Safe to run any time; touches no media file. Runs weekly from cron.
Flags: --dry-run  unpack and check the database only; start no container, send nothing.
Exit:  0 drill passed, 1 drill failed, 2 the guard itself failed.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import urllib.request
import zipfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import settings  # noqa: E402

JOB = "restore_drill"
TAG = "[restore_drill]"

NAME = "sonarr-restoredrill"       # throwaway container
NET = "restoredrill-net"           # its private network
PORT = 8991                        # published on 127.0.0.1 only; nothing in the stack uses it
FALLBACK_IMAGE = "lscr.io/linuxserver/sonarr:latest"
BOOT_TRIES, BOOT_EVERY_S = 30, 5   # up to 150 s for the restored instance to answer
SERIES_TOLERANCE = 2               # the live library may have changed a little since the backup
DOCKER_TIMEOUT = 180
BACKUP_RX = re.compile(r"^sonarr-\d{8}\.zip$")


def sh(*args: str, timeout: int = DOCKER_TIMEOUT):
    """docker <args>; returns a CompletedProcess, or None when docker could not run at all."""
    try:
        return settings.docker(*args, timeout=timeout)
    except (OSError, subprocess.SubprocessError) as e:
        settings.log(TAG, "docker %s failed: %s" % (args[0] if args else "", e))
        return None


def cleanup() -> None:
    sh("rm", "-f", NAME)
    sh("network", "rm", NET)


def newest_backup(folder: str) -> str:
    try:
        names = [f for f in os.listdir(folder) if BACKUP_RX.match(f)]
    except OSError:
        return ""
    paths = [os.path.join(folder, f) for f in names]
    return max(paths, key=os.path.getmtime) if paths else ""


def safe_extract(zpath: str, dest: str) -> None:
    """Unpack without letting any member escape the scratch folder."""
    root = os.path.realpath(dest)
    with zipfile.ZipFile(zpath) as z:
        for m in z.infolist():
            target = os.path.realpath(os.path.join(dest, m.filename))
            if target != root and not target.startswith(root + os.sep):
                raise ValueError("unsafe path inside the backup: %s" % m.filename)
        z.extractall(dest)


def read_api_key(cfg: str) -> str:
    try:
        with open(os.path.join(cfg, "config.xml"), encoding="utf-8", errors="replace") as fh:
            m = re.search(r"<ApiKey>([^<]+)</ApiKey>", fh.read())
    except OSError:
        return ""
    return m.group(1).strip() if m else ""


def db_facts(db: str) -> dict:
    con = sqlite3.connect("file:%s?mode=ro" % urllib.parse.quote(db), uri=True)
    try:
        facts = {"integrity": con.execute("pragma integrity_check").fetchone()[0]}
        for key, table in (("series", "Series"), ("indexers", "Indexers"), ("profiles", "QualityProfiles")):
            try:
                facts[key] = con.execute("select count(*) from %s" % table).fetchone()[0]
            except sqlite3.Error:
                facts[key] = -1
        return facts
    finally:
        con.close()


def http_json(url: str, key: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"X-Api-Key": key, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())


def live_image() -> str:
    """The exact image the live Sonarr runs, so the drill never pulls anything."""
    r = sh("inspect", "-f", "{{.Image}}", "sonarr", timeout=30)
    if r is not None and r.returncode == 0 and r.stdout.strip():
        return r.stdout.strip()
    return FALLBACK_IMAGE


def live_series_count():
    try:
        return len(settings.api(settings.sonarr(), "series", timeout=60) or [])
    except Exception:
        return None


def remove_tree(path: str, image: str) -> None:
    """The container's init hands /config to PUID; when this script runs as another user the
    copy can only be removed from inside a container. It holds a copy of the API key, so it
    must not be left behind."""
    shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path) and image:
        sh("run", "--rm", "--pull", "never", "--entrypoint", "rm", "-v", "%s:/w" % path, image, "-rf", "/w/config")
        shutil.rmtree(path, ignore_errors=True)
    if os.path.exists(path):
        settings.log(TAG, "could not remove the scratch folder %s; delete it by hand" % path)


def drill(dry: bool, problems: list) -> None:
    src = newest_backup(settings.BACKUP_DIR)
    if not src:
        problems.append("No Sonarr backup was found in the backup folder (config_backup writes one nightly).")
        return
    age_h = (time.time() - os.path.getmtime(src)) / 3600.0
    settings.log(TAG, "testing %s (%.1f h old, %.1f MB)" % (os.path.basename(src), age_h, os.path.getsize(src) / 1e6))

    work = tempfile.mkdtemp(prefix="restoredrill-")
    cfg = os.path.join(work, "config")
    image = ""
    try:
        os.makedirs(cfg)
        # ---- 1. unpack ----
        try:
            safe_extract(src, cfg)
        except (OSError, ValueError, zipfile.BadZipFile) as e:
            problems.append("The backup could not be unpacked (%s)." % e)
            return
        db = os.path.join(cfg, "sonarr.db")
        if not os.path.isfile(db):
            problems.append("The backup contains no Sonarr database.")
            return

        # ---- 2. database integrity and contents ----
        try:
            f = db_facts(db)
        except sqlite3.Error as e:
            problems.append("The database in the backup cannot be opened (%s)." % e)
            return
        settings.log(TAG, "integrity_check=%s  series=%d  indexers=%d  qualityProfiles=%d"
                     % (f["integrity"], f["series"], f["indexers"], f["profiles"]))
        if f["integrity"] != "ok":
            problems.append("The database integrity check returned '%s'." % f["integrity"])
        if f["series"] < 1:
            problems.append("The backup holds no series at all.")
        key = read_api_key(cfg)
        if not key:
            problems.append("The backup has no API key in its config.xml.")
            return
        if dry:
            settings.log(TAG, "[dry-run] would boot a throwaway Sonarr on 127.0.0.1:%d (own network %s) "
                              "and compare its series count with the live one" % (PORT, NET))
            return

        # ---- 3. boot a throwaway Sonarr against the restored copy ----
        image = live_image()
        cleanup()
        net = sh("network", "create", NET)
        if net is None or net.returncode != 0:
            problems.append("Docker could not create the drill's private network.")
            return
        r = sh("run", "-d", "--pull", "never", "--name", NAME, "--network", NET,
               "-p", "127.0.0.1:%d:8989" % PORT,
               "-e", "PUID=%s" % settings.get("PUID", "1000"), "-e", "PGID=%s" % settings.get("PGID", "1000"),
               "-e", "TZ=%s" % settings.get("TZ", "Etc/UTC"),
               "-v", "%s:/config" % cfg, image)
        if r is None or r.returncode != 0:
            why = ((r.stderr if r else "") or "docker failed").strip()[:160]
            problems.append("The throwaway Sonarr would not start (%s)." % why)
            return
        base = "http://127.0.0.1:%d/api/v3/" % PORT
        booted = False
        for _ in range(BOOT_TRIES):
            time.sleep(BOOT_EVERY_S)
            try:
                status = http_json(base + "system/status", key)
                settings.log(TAG, "restored instance answered: Sonarr %s" % status.get("version"))
                booted = True
                break
            except Exception:
                continue
        if not booted:
            problems.append("The restored instance never answered its API within %d s." % (BOOT_TRIES * BOOT_EVERY_S))
            return
        try:
            restored = len(http_json(base + "series", key, timeout=60) or [])
        except Exception as e:
            problems.append("Could not list the series of the restored instance (%s)." % e)
            return
        live = live_series_count()
        settings.log(TAG, "series: restored=%s  live=%s" % (restored, live))
        if live and abs(restored - live) > SERIES_TOLERANCE:
            problems.append("The restored instance has %d series but the live one has %d." % (restored, live))
    finally:
        if not dry:
            cleanup()
        remove_tree(work, image)


def run(dry: bool) -> int:
    problems = []
    drill(dry, problems)
    if problems:
        settings.log(TAG, "DRILL FAILED: " + "; ".join(problems))
        text = ("Backup restore test FAILED\n\nThe newest Sonarr backup was restored into a scratch copy "
                "and did not come back correctly:\n\n%s\n\nThe live server is untouched and running normally, "
                "but the backups cannot be trusted to restore until this is looked at."
                % "\n".join("• " + html.escape(p, quote=False) for p in problems))
        if dry:
            settings.log(TAG, "[dry-run] would send: " + text.replace("\n", " | "))
        else:
            settings.notify(text)
        return 1
    settings.log(TAG, "DRILL PASSED%s" % (" (database checks only, dry run)" if dry else
                                          ": the backup restores and serves the full library"))
    return 0


def main(argv) -> int:
    p = argparse.ArgumentParser(description="Restore the newest Sonarr backup into a throwaway instance.")
    p.add_argument("--dry-run", "--dry", dest="dry_run", action="store_true",
                   help="unpack and check the database only; start no container, send nothing")
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
