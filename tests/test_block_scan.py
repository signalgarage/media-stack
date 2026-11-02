"""tools/block_scan.py: blocking calls inside `async def` functions are found, wrapped ones are not."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

# Every line that must be reported ends with "# BAD"; nothing else may be.
SAMPLE = '''\
import asyncio
import functools
import subprocess
from time import sleep as nap

import requests

_SESSION = requests.Session()


def fetch(url):
    return requests.get(url, timeout=5)


def wrapper(url):
    return fetch(url)


def pure(x):
    return x * 2


class Client:
    def __init__(self):
        self.conn = requests.Session()

    def get(self):
        return self.conn.get("http://example.invalid/")

    def safe(self):
        return 1

    async def handler(self):
        self.safe()
        return self.get()  # BAD


async def good(loop):
    await asyncio.to_thread(fetch, "x")
    await asyncio.to_thread(lambda: wrapper("x"))
    await loop.run_in_executor(None, functools.partial(fetch, "x"))
    await asyncio.get_running_loop().run_in_executor(None, wrapper, "x")
    await asyncio.sleep(1)
    session = requests.Session()
    pure(3)

    def inner():
        return fetch("x")
    return session, inner


async def bad():
    fetch("x")  # BAD
    wrapper("y")  # BAD
    nap(1)  # BAD
    subprocess.run(["true"])  # BAD
    sorted([1], key=lambda r: fetch(r))  # BAD
    _SESSION.get("http://example.invalid/")  # BAD
    [fetch(i) for i in range(2)]  # BAD
    await asyncio.wait_for(asyncio.to_thread(fetch, "z"), 5)
'''

CLEAN = '''\
import asyncio


async def main():
    await asyncio.sleep(0)
'''


def _bad_lines() -> list:
    return [n for n, line in enumerate(SAMPLE.splitlines(), 1) if line.rstrip().endswith("# BAD")]


def test_findings_are_exactly_the_blocking_calls(block_scan):
    found = block_scan.Module("sample.py", SAMPLE).findings()
    assert [line for line, _, _ in found] == _bad_lines()
    assert {func for _, func, _ in found} == {"handler", "bad"}
    by_line = {line: what for line, _, what in found}
    assert "Client.get() blocks (Client.get -> conn.get)" in by_line[_bad_lines()[0]]
    assert by_line[_bad_lines()[2]] == "wrapper() blocks (wrapper -> fetch -> requests.get)"
    assert by_line[_bad_lines()[3]] == "time.sleep() blocks"


def test_wrappers_are_followed_through_plain_functions(block_scan):
    module = block_scan.Module("sample.py", SAMPLE)
    assert ("func", "wrapper") in module.blocking and ("method", "Client", "get") in module.blocking
    assert ("func", "pure") not in module.blocking and ("method", "Client", "safe") not in module.blocking


def test_main_exit_codes_and_output(block_scan, tmp_path, capsys):
    clean, sample, broken = tmp_path / "clean.py", tmp_path / "sample.py", tmp_path / "broken.py"
    clean.write_text(CLEAN, encoding="utf-8")
    sample.write_text(SAMPLE, encoding="utf-8")
    broken.write_text("async def (:\n", encoding="utf-8")
    assert block_scan.main([str(clean)]) == 0
    assert block_scan.main([str(sample)]) == 1
    out = capsys.readouterr()
    first = _bad_lines()[0]
    assert "%s:%d:handler: Client.get() blocks" % (sample, first) in out.out
    assert "%d blocking call(s) inside async functions" % len(_bad_lines()) in out.err
    assert block_scan.main([str(broken)]) == 2
    assert block_scan.main([str(tmp_path / "missing.py")]) == 2
    assert block_scan.main([]) == 2
    assert block_scan.main([str(sample), "-v"]) == 1
    assert "blocking wrapper -> fetch -> requests.get" in capsys.readouterr().out


@pytest.mark.skipif(sys.version_info < (3, 12), reason="bot.py is written for the Python 3.12 of its container")
def test_the_bot_has_no_blocking_calls_on_its_event_loop(block_scan):
    assert block_scan.main([str(REPO / "moviebot" / "bot.py")]) == 0
