#!/usr/bin/env python3
"""Telegram helper for the host-side scripts.

send(text, chat_id=None) -> bool
    One message to the owner (Bot API sendMessage, HTML, link previews off).
    Text over Telegram's size limit is split at line breaks. When Telegram
    cannot be reached the text is appended to <STATE_DIR>/tg_undelivered.log
    (capped) and delivered after the next send that works, so an outage never
    swallows an alert without a trace. With no bot token or chat id the text is
    printed instead and False is returned: a guard never fails because Telegram
    is not set up.

ask(question, timeout_s=600) -> "yes" | "no" | "__TIMEOUT__"
    A Yes/No question through the bot. Telegram allows only one getUpdates
    consumer and the bot is that consumer, so this never polls Telegram itself.
    It writes <id>.ask (JSON {"text": ...}) into the confirms folder; the bot
    shows the question with Yes/No buttons and writes <id>.reply ("yes"/"no");
    this function waits for that file. The bot must be running for an answer
    to arrive.

post_ask(), read_reply() and drop_ask() are the same exchange split in two,
for jobs that ask in one run and collect the answer in a later run.

Command line:
    tg.py send "message"
    tg.py ask "question" [seconds]
    --dry-run prints instead of sending.

Importing this module does no I/O.
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import settings  # noqa: E402

TELEGRAM_API = "https://api.telegram.org"
MAX_CHARS = 4000             # Telegram rejects texts over 4096 characters; keep some headroom
SEND_TIMEOUT_S = 30
CHUNK_PAUSE_S = 1.0          # Telegram throttles bursts to one chat; about one message a second is safe
SPOOL_NAME = "tg_undelivered.log"
SPOOL_MAX_LINES = 500        # keeps the spool bounded during a long outage
_SPOOL_LOCK_NAME = ".tg_undelivered.lock"
_SPOOL_LOCK_WAIT_S = 15.0
ASK_POLL_S = 3               # how often ask() looks for the bot's reply file
DEFAULT_ASK_TIMEOUT_S = 600
STALE_REPLY_S = 7 * 86400    # replies nobody collected (a tap after a timeout) are cleaned up after this
TIMEOUT = "__TIMEOUT__"
_PARSE_ERROR = "can't parse entities"
_TAG_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*(?:\s[^<>]*)?>")


# ---------------------------------------------------------------- helpers

def _config(chat_id=None):
    token = settings.get("TELEGRAM_BOT_TOKEN").strip()
    chat = str(chat_id if chat_id not in (None, "") else settings.notify_chat_id()).strip()
    return token, chat


def _why(e: Exception) -> str:
    """Short reason for a failure. Never includes the request URL (it carries the token)."""
    if isinstance(e, urllib.error.HTTPError):
        return "HTTP %d" % e.code
    if isinstance(e, urllib.error.URLError):
        return "network: %s" % (e.reason,)
    return type(e).__name__


def split_message(text: str, limit: int = MAX_CHARS) -> list:
    """Split text into chunks of at most `limit` characters, at line breaks when possible."""
    if len(text) <= limit:
        return [text]
    chunks, cur = [], ""
    for line in text.split("\n"):
        while len(line) > limit:
            if cur:
                chunks.append(cur)
                cur = ""
            chunks.append(line[:limit])
            line = line[limit:]
        candidate = line if not cur else cur + "\n" + line
        if len(candidate) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = candidate
    if cur:
        chunks.append(cur)
    return chunks


def plain_text(text: str) -> str:
    """HTML message text -> plain text (used for the recovered-alerts message)."""
    return html.unescape(_TAG_RE.sub("", text))


class _FileLock:
    """Blocking exclusive lock with a time limit; degrades to unlocked."""

    def __init__(self, name: str, wait_s: float) -> None:
        self.name = name
        self.wait_s = wait_s
        self.fh = None

    def __enter__(self):
        try:
            import fcntl
            self.fh = open(settings.state_path(self.name), "a")
            deadline = time.time() + self.wait_s
            while True:
                try:
                    fcntl.flock(self.fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        break
                    time.sleep(0.1)
        except (ImportError, OSError):
            pass
        return self

    def __exit__(self, *exc) -> bool:
        if self.fh is not None:
            try:
                self.fh.close()
            except OSError:
                pass
        return False


def _post(token: str, chat: str, text: str, as_html: bool) -> None:
    fields = {"chat_id": chat, "text": text, "disable_web_page_preview": "true"}
    if as_html:
        fields["parse_mode"] = "HTML"
    req = urllib.request.Request(TELEGRAM_API + "/bot" + token + "/sendMessage",
                                 data=urllib.parse.urlencode(fields).encode())
    with urllib.request.urlopen(req, timeout=SEND_TIMEOUT_S) as r:
        r.read()


def _deliver(token: str, chat: str, text: str, as_html: bool = True) -> None:
    """Send one logical message (split when long). Raises on failure."""
    for i, chunk in enumerate(split_message(text)):
        if i:
            time.sleep(CHUNK_PAUSE_S)
        try:
            _post(token, chat, chunk, as_html)
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode(errors="replace").lower()
            except Exception:  # noqa: BLE001 - the body is only a hint
                pass
            if as_html and e.code == 400 and _PARSE_ERROR in body:
                # Text that is not valid HTML (a stray "<" or "&" in a file name):
                # deliver it as plain text rather than losing it.
                _post(token, chat, chunk, False)
            else:
                raise


# ---------------------------------------------------------------- spool

def _spool(text: str) -> None:
    """Keep an alert that could not be delivered (capped at SPOOL_MAX_LINES lines)."""
    try:
        path = settings.state_path(SPOOL_NAME)
        with _FileLock(_SPOOL_LOCK_NAME, _SPOOL_LOCK_WAIT_S):
            try:
                with open(path, encoding="utf-8") as fh:
                    lines = fh.read().splitlines()
            except OSError:
                lines = []
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            lines += [stamp + " | " + line for line in text.splitlines() if line.strip()]
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                fh.write("\n".join(lines[-SPOOL_MAX_LINES:]) + "\n")
            os.replace(tmp, path)
    except Exception as e:  # noqa: BLE001 - spooling must never crash the caller
        print("[tg] could not spool the message (%s)" % type(e).__name__, flush=True)


def _flush_spool(token: str, chat: str) -> None:
    """After a send that worked, deliver whatever failed earlier."""
    try:
        path = settings.state_path(SPOOL_NAME)
        if not os.path.exists(path):
            return
        with _FileLock(_SPOOL_LOCK_NAME, _SPOOL_LOCK_WAIT_S):
            try:
                with open(path, encoding="utf-8") as fh:
                    body = fh.read().strip()
            except OSError:
                return
            if body:
                _deliver(token, chat, "Alerts that could not be delivered earlier:\n\n" + plain_text(body),
                         as_html=False)
            os.remove(path)
        print("[tg] delivered the spooled alerts", flush=True)
    except Exception as e:  # noqa: BLE001 - try again after the next successful send
        print("[tg] spooled alerts not delivered yet (%s)" % _why(e), flush=True)


# ---------------------------------------------------------------- public API

def send(text, chat_id=None) -> bool:
    """Send text to the owner (or chat_id). True when Telegram accepted it."""
    text = str(text or "")
    if not text.strip():
        return False
    token, chat = _config(chat_id)
    if not token or not chat:
        print("[tg] Telegram is not configured (TELEGRAM_BOT_TOKEN and a chat id); message follows:", flush=True)
        print(text, flush=True)
        return False
    try:
        _deliver(token, chat, text)
    except Exception as e:  # noqa: BLE001 - any failure ends in the spool, never in a crashed guard
        _spool(text)
        print("[tg] send failed (%s); kept for later delivery" % _why(e), flush=True)
        return False
    _flush_spool(token, chat)
    return True


def _new_id(prefix: str) -> str:
    # millisecond clock plus a random suffix: two questions written in the same
    # millisecond must never share a file name
    return "%s%d%s" % (prefix, int(time.time() * 1000), os.urandom(2).hex())


def _cleanup_stale_replies() -> None:
    try:
        now = time.time()
        for fn in os.listdir(settings.CONFIRM_DIR):
            if fn.endswith(".reply"):
                p = os.path.join(settings.CONFIRM_DIR, fn)
                if now - os.path.getmtime(p) > STALE_REPLY_S:
                    os.remove(p)
    except OSError:
        pass


def post_ask(text: str, prefix: str = "c"):
    """Write <id>.ask for the bot and return the id; None when Telegram is not configured."""
    if not settings.get("TELEGRAM_BOT_TOKEN").strip():
        print("[tg] Telegram is not configured; question not asked:", flush=True)
        print(text, flush=True)
        return None
    settings.ensure_dirs()
    _cleanup_stale_replies()
    cid = _new_id(prefix)
    path = os.path.join(settings.CONFIRM_DIR, cid + ".ask")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"text": text}, fh)
    os.replace(tmp, path)   # the bot only ever sees a complete file
    return cid


def parse_reply(raw: str):
    """'yes' / 'no' from a reply file's content; None while it is still empty."""
    raw = (raw or "").strip()
    if not raw:
        return None
    if raw.startswith("{"):
        try:
            obj = json.loads(raw)
        except ValueError:
            obj = {}
        if isinstance(obj, dict):
            raw = str(next((obj[k] for k in ("answer", "reply", "decision", "value") if k in obj), ""))
    return "yes" if raw.strip().lower() == "yes" else "no"   # anything but an explicit yes is a no


def read_reply(cid: str, consume: bool = True):
    """The answer to question `cid` ('yes'/'no'), or None when there is none yet."""
    path = os.path.join(settings.CONFIRM_DIR, cid + ".reply")
    try:
        with open(path, encoding="utf-8") as fh:
            answer = parse_reply(fh.read())
    except OSError:
        return None
    if answer is not None and consume:
        try:
            os.remove(path)
        except OSError:
            pass
    return answer


def drop_ask(cid: str) -> None:
    """Remove what is left of question `cid` (an unsent .ask, an uncollected .reply)."""
    for ext in (".ask", ".reply"):
        try:
            os.remove(os.path.join(settings.CONFIRM_DIR, cid + ext))
        except OSError:
            pass


def ask(question: str, timeout_s: int = DEFAULT_ASK_TIMEOUT_S) -> str:
    """Ask a Yes/No question through the bot and wait for the answer."""
    cid = post_ask(question)
    if not cid:
        return TIMEOUT
    deadline = time.time() + max(0, int(timeout_s))
    while time.time() < deadline:
        answer = read_reply(cid)
        if answer:
            drop_ask(cid)
            return answer
        time.sleep(ASK_POLL_S)
    drop_ask(cid)
    return TIMEOUT


# ---------------------------------------------------------------- command line

def main(argv) -> int:
    p = argparse.ArgumentParser(prog="tg.py", description="Send a Telegram message or ask a Yes/No question.")
    p.add_argument("mode", choices=("send", "ask"))
    p.add_argument("text")
    p.add_argument("seconds", nargs="?", type=int, default=DEFAULT_ASK_TIMEOUT_S,
                   help="ask only: how long to wait for the answer")
    p.add_argument("--dry-run", "--dry", dest="dry", action="store_true", help="print instead of sending")
    args = p.parse_args(argv)
    if args.mode == "send":
        if args.dry:
            print("[dry-run] would send:\n" + args.text)
            return 0
        ok = send(args.text)
        print("sent" if ok else "not sent")
        return 0 if ok else 1
    if args.dry:
        print("[dry-run] would ask (waiting up to %ds):\n%s" % (args.seconds, args.text))
        print(TIMEOUT)
        return 0
    print(ask(args.text, args.seconds))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
