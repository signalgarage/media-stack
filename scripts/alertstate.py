#!/usr/bin/env python3
"""Shared alert de-duplication, the guards' shared lock and the mount canary.

Why: a guard that re-sends the same failure on every run trains people to ignore
its alerts. filter_new() returns only the problems that were never reported
before; problems that persist are summarised at most once a day through
digest_due(). A key disappears from the state as soon as the caller stops
reporting it, so a problem that clears and later comes back is reported again.

State lives in <STATE_DIR>/.alert_state.json. It is safe to delete at any time
(worst case: one repeat alert per open problem).

acquire_lock() is the non-blocking lock shared by the guards that act on the
downloads tree, so two of them never race on the same files.

mount_canary() implements the rule every guard that reads the disk follows: when
the media folder (or a configured network mount) is missing, skip the run and
say so once a day. An absent share must never look like an empty library.

Dry runs: filter_new() and digest_due() take dry_run (default: the --dry-run
flag of the running script). In a dry run they report what they would return
but record nothing, so a later real run still sends the alert.

Importing this module does no I/O.
"""
from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import settings  # noqa: E402

STATE_NAME = ".alert_state.json"
LOCK_NAME = ".media_guard.lock"
DIGEST_KEY = "_digest"
# Several guards update the state file; a short exclusive lock keeps one from
# overwriting another's update. Never wait longer than this: a lost update is a
# repeated alert, a stuck guard is worse.
_STATE_LOCK_NAME = ".alert_state.lock"
_STATE_LOCK_WAIT_S = 10.0
_STATE_LOCK_POLL_S = 0.1


def _dry(dry_run) -> bool:
    return settings.dry_run() if dry_run is None else bool(dry_run)


def _state_path() -> str:
    return settings.state_path(STATE_NAME)


class _StateLock:
    """Short exclusive lock around one read-modify-write of the state file.

    Degrades to running unlocked when the lock cannot be taken in time or the
    lock file is unusable.
    """

    def __init__(self) -> None:
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
            self.fh = open(settings.state_path(_STATE_LOCK_NAME), "a")
            deadline = time.time() + _STATE_LOCK_WAIT_S
            while True:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(_STATE_LOCK_POLL_S)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, *exc) -> bool:
        if self.fh is not None:
            try:
                self.fh.close()   # closing the file releases the flock
            except OSError:
                pass
        return False


def _load() -> dict:
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            st = json.load(fh)
        return st if isinstance(st, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(st: dict) -> None:
    try:
        settings.save_json(_state_path(), st)
    except OSError as e:
        settings.log("[alertstate]", "could not save the alert state (%s)" % e)


def filter_new(category: str, keys, dry_run=None):
    """Split the CURRENTLY present problems into (new, persisting).

    keys: stable strings, one per problem that exists right now. A key seen for
    the first time is "new" (alert it); a key reported before is "persisting"
    (do not repeat it). Keys of this category that are not in `keys` are
    forgotten, so a problem that clears and returns is new again.
    """
    current, seen = [], set()
    for k in keys:
        k = str(k)
        if k not in seen:
            seen.add(k)
            current.append(k)
    now = int(time.time())
    new, persisting = [], []
    with _StateLock():
        st = _load()
        cat = st.get(category)
        if not isinstance(cat, dict):
            cat = {}
        for k in current:
            entry = cat.get(k)
            if isinstance(entry, dict):
                persisting.append(k)
                entry["last"] = now
            else:
                new.append(k)
                cat[k] = {"first": now, "last": now}
        for k in [k for k in cat if k not in seen]:
            del cat[k]
        st[category] = cat
        if not _dry(dry_run):
            _save(st)
    return new, persisting


def digest_due(category: str, dry_run=None) -> bool:
    """True at most once per calendar day per category (then records the day)."""
    today = time.strftime("%Y-%m-%d")
    with _StateLock():
        st = _load()
        digest = st.get(DIGEST_KEY)
        if not isinstance(digest, dict):
            digest = {}
        if digest.get(category) == today:
            return False
        digest[category] = today
        st[DIGEST_KEY] = digest
        if not _dry(dry_run):
            _save(st)
    return True


def acquire_lock(name=None):
    """Take a non-blocking exclusive lock; keep the returned object for the whole run.

    name: None for the shared guard lock (<STATE_DIR>/.media_guard.lock), a bare
    file name for a lock in STATE_DIR, or a full path.

    Returns the open lock file (the lock lasts until it is closed or the process
    exits), None when another process holds the lock, or the string "unlocked"
    when the lock file itself cannot be used: a broken lock must degrade to
    running without protection, never to a crashed guard (a dead guard is worse
    than a rare race).
    """
    try:
        import fcntl
    except ImportError:
        return "unlocked"
    if not name:
        path = settings.state_path(LOCK_NAME)
    elif os.path.dirname(name):
        path = name
    else:
        path = settings.state_path(name)
    try:
        fh = open(path, "a")
    except OSError as e:
        print("lock file unusable (%s) - proceeding WITHOUT lock" % e, flush=True)
        return "unlocked"
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return fh
    except OSError:
        fh.close()
        return None


def describe_path(path: str) -> str:
    """Plain-language name for one of the stack's folders (alerts carry no raw paths)."""
    p = os.path.normpath(path)
    if p == os.path.normpath(settings.MEDIA_ROOT):
        return "the media folder (MEDIA_ROOT)"
    if p == os.path.normpath(settings.DOWNLOADS):
        return "the downloads folder"
    if p == os.path.normpath(settings.MOVIES):
        return "the movies library"
    if p == os.path.normpath(settings.TV):
        return "the TV library"
    for code, root in settings.movie_libraries().items():
        if p == os.path.normpath(root):
            return "the '%s' movie library" % code
    if p in [os.path.normpath(x) for x in settings.get_list("MOUNT_PATHS")]:
        return "a network mount listed in MOUNT_PATHS"
    return "a media folder"


def mount_canary(job: str, paths=(), dry_run=None) -> bool:
    """True when MEDIA_ROOT, every MOUNT_PATHS entry and every path given is a directory.

    Otherwise log what is missing, alert at most once a day for this job and
    return False; the caller then exits 0 without doing anything.
    """
    check = [settings.MEDIA_ROOT] + settings.get_list("MOUNT_PATHS") + list(paths or ())
    missing = []
    for p in check:
        if p and p not in missing and not os.path.isdir(p):
            missing.append(p)
    if not missing:
        return True
    settings.log("[%s]" % job, "media folder missing or not mounted: %s - skipping this run "
                 "(an absent share must not look like an empty library)" % ", ".join(missing))
    if digest_due("mount-canary-" + job, dry_run=dry_run):
        names = []
        for p in missing:
            name = describe_path(p)
            if name not in names:
                names.append(name)
        text = ("%s paused: media folder missing\n\n" % job
                + "\n".join("• " + n + " is missing or not mounted" for n in names)
                + "\n\nThe job skips its runs until the folder is back, so an absent share "
                  "is never mistaken for an empty library.")
        if _dry(dry_run):
            print("[dry-run] would send:\n" + text, flush=True)
        else:
            settings.notify(text)
    return False
