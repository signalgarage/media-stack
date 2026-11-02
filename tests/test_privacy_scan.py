"""tools/privacy_scan.sh: what it refuses, what it lets through, and that it never prints a finding's value.

Every secret-shaped value below is assembled at run time, so this file stays clean for the scan itself.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "tools" / "privacy_scan.sh"
pytestmark = pytest.mark.skipif(shutil.which("bash") is None, reason="the scan is a bash script")

LONG_HEX = "0123456789abcdef" * 2
BOT_TOKEN = "1234567" + "89:" + "AbCdEfGhIjKlMnOpQrStUvWxYz012345678"
TORRENT = "tor" + "rent"
NZB = "n" + "zb"


def ip(*parts) -> str:
    return ".".join(str(p) for p in parts)


def make_tree(root: Path, files: dict) -> Path:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content, encoding="utf-8")
    return root


def scan(root: Path) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(SCRIPT), str(root)], capture_output=True, text=True, timeout=60)


ALLOWED = "\n".join([
    "Web UI on 127.0.0.1 and 0.0.0.0; example LAN 192.0.2.0/24, hosts 198.51.100.7 and 203.0.113.5.",
    "Version 1.2.3.4.5 and 999.1.1.1 are not addresses, nor is v1.2.3.4.",
    "Author: Someone <12345+someone@users.noreply.github.com>",
    "Push URLs look like http://localhost:3001/api/push/<token>",
    "qBittorrent, SABnzbd and the word torrent are fine, and so is code: R.torrent_finished(t), qbit.nzb_count.",
    "Big.Buck.Bunny.2008.1080p.WEB-DL.x264-GROUP and ubuntu-24.04-desktop-amd64.iso",
    "",
])


def test_a_clean_tree_passes(tmp_path):
    root = make_tree(tmp_path, {"docs/SETUP.md": ALLOWED, ".env.example": "MEDIA_ROOT=/srv/media\nWEBHOOK_SECRET=\n",
                                "sub/ok.bin": b"\0" * 1048576})
    result = scan(root)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "privacy_scan: clean (3 files)" in result.stdout


@pytest.mark.parametrize("line, what", [
    ("api_key = " + LONG_HEX, "hex string of 32+ characters"),
    ("token " + BOT_TOKEN, "Telegram bot token"),
    ("lan " + ip(10, 1, 2, 3), "private IPv4 address"),
    ("lan " + ip(172, 20, 0, 1), "private IPv4 address"),
    ("lan " + ip(192, 168, 1, 10), "private IPv4 address"),
    ("subnets=" + ip(192, 0, 2, 0) + "/24," + ip(10, 0, 0, 0) + "/8", "private IPv4 address"),   # second in a list
    ("cgnat " + ip(100, 64, 0, 9), "CGNAT IPv4 address"),
    ("public " + ip(8, 8, 4, 4) + ".", "IPv4 address (only 0.0.0.0, 127.0.0.1 and the documentation ranges"),
    ("mail someone" + "@" + "example.com", "e-mail address"),
    ("push http://localhost:3001/api/" + "push/AbCdEf123?status=up", "Uptime Kuma push URL with a token"),
    ("get magnet" + ":?xt=urn:btih:x", "magnet link"),
    ("file Sintel.2010." + TORRENT + " here", "reference to a torrent or NZB file"),
    ("file Sintel.2010." + "NZB", "reference to a torrent or NZB file"),
])
def test_each_kind_of_leak_fails_with_file_and_line(tmp_path, line, what):
    root = make_tree(tmp_path, {"docs/notes.md": "first line\n" + line + "\n"})
    result = scan(root)
    assert result.returncode == 1
    assert "docs/notes.md:2: " + what in result.stdout
    assert "finding(s)" in result.stderr


def test_findings_never_print_the_value(tmp_path):
    secret_ip = ip(10, 9, 8, 7)
    root = make_tree(tmp_path, {"a.txt": "\n".join([LONG_HEX, BOT_TOKEN, secret_ip, "x" + "@" + "example.org"])})
    result = scan(root)
    assert result.returncode == 1
    for value in (LONG_HEX, BOT_TOKEN, secret_ip, "example.org"):
        assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("name", ["Big.Buck.Bunny.2008.1080p.MKV", "Sintel.mp4", "Sintel." + TORRENT, "Sintel." + NZB,
                                  "wg0.conf", "client.ovpn", "server.key", "server.pem"])
def test_forbidden_file_types(tmp_path, name):
    result = scan(make_tree(tmp_path, {"sub/" + name: ""}))
    assert result.returncode == 1
    assert "sub/%s: forbidden file type" % name in result.stdout


def test_files_over_one_mib(tmp_path):
    result = scan(make_tree(tmp_path, {"big.bin": b"\0" * (1048576 + 1)}))
    assert result.returncode == 1 and "big.bin: file larger than 1 MiB" in result.stdout


def test_runtime_folders_caches_and_untracked_env_files_are_skipped(tmp_path):
    leak = "token " + BOT_TOKEN + "\n"
    root = make_tree(tmp_path, {
        "state/x.json": leak, "config/radarr/config.xml": leak, "logs/regrab_guard.log": leak,
        "backups/a.txt": leak, ".venv/lib/x.py": leak, "scripts/__pycache__/x.txt": leak,
        ".ruff_cache/x": leak, ".pytest_cache/x": leak, ".env": leak, "sub/.env.local": leak,
        "docs/ok.md": "nothing to see\n"})
    result = scan(root)
    assert result.returncode == 0, result.stdout


def test_only_the_top_level_runtime_folders_are_skipped(tmp_path):
    root = make_tree(tmp_path, {"docs/config/example.md": "lan " + ip(10, 0, 0, 1) + "\n",
                                ".env.example": "PLEX_TOKEN=" + LONG_HEX + "\n"})
    result = scan(root)
    assert result.returncode == 1
    assert "docs/config/example.md:1: private IPv4 address" in result.stdout
    assert ".env.example:1: hex string" in result.stdout


def test_the_fake_marker_works_only_in_tests_and_only_for_keys(tmp_path):
    marked = "VALUE = '%s'  # privacy-scan: fake\n"
    root = make_tree(tmp_path, {"tests/test_x.py": marked % LONG_HEX + marked % BOT_TOKEN,
                                "tests/test_y.py": "HOST = '%s'  # privacy-scan: fake\n" % ip(192, 168, 0, 2),
                                "docs/x.md": marked % LONG_HEX})
    result = scan(root)
    assert result.returncode == 1
    assert "tests/test_x.py" not in result.stdout
    assert "tests/test_y.py:1: private IPv4 address" in result.stdout
    assert "docs/x.md:1: hex string" in result.stdout


def test_usage_errors(tmp_path):
    assert subprocess.run(["bash", str(SCRIPT), str(tmp_path / "missing")], capture_output=True).returncode == 2
    assert subprocess.run(["bash", str(SCRIPT), "a", "b"], capture_output=True).returncode == 2
