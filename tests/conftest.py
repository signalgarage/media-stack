"""Shared setup for the media-stack test-suite.

Every test gets a throw-away stack folder (MEDIA_STACK_DIR) with a small .env, and
scripts/settings.py is reloaded against it, so no test reads or writes a real installation.
Sockets are blocked: a test that tries to reach the network fails instead of talking to
Telegram or an app. No test needs Docker.
"""
from __future__ import annotations

import importlib
import importlib.util
import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
for _path in (REPO / "scripts", REPO / "moviebot"):     # also set in pyproject.toml; repeated for odd runners
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))


def _env_example_keys() -> list:
    keys = []
    for line in (REPO / ".env.example").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            keys.append(line.split("=", 1)[0].strip())
    return keys


# Every setting a developer's shell might export. None of them may leak into a test.
ENV_KEYS = sorted(set(_env_example_keys()) | {"DRY_RUN", "MEDIA_STACK_DIR"})

# On a real install the repository folder IS the stack folder and its .env holds real tokens.
# Point settings.py at an empty folder before any test module imports it.
for _key in ENV_KEYS:
    os.environ.pop(_key, None)
_BOOT_DIR = tempfile.mkdtemp(prefix="media-stack-tests-")
os.environ["MEDIA_STACK_DIR"] = _BOOT_DIR


def pytest_unconfigure(config):
    shutil.rmtree(_BOOT_DIR, ignore_errors=True)


def load_path(name: str, path: Path):
    """Import a file that is not on sys.path (configure.py, tools/block_scan.py), once."""
    if name not in sys.modules:
        spec = importlib.util.spec_from_file_location(name, str(path))
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


class Stack:
    """A temporary stack folder: <root>/.env and <root>/media/{downloads,movies,tv}."""

    def __init__(self, root: Path, monkeypatch):
        self.root = root
        self.media = root / "media"
        self.monkeypatch = monkeypatch
        self.values = {"MEDIA_ROOT": str(self.media), "TELEGRAM_BOT_TOKEN": "", "TZ": "Etc/UTC"}

    def write_env(self, **values):
        """Write .env from the defaults plus `values`; reload settings and return it."""
        self.values.update({k: str(v) for k, v in values.items()})
        return self.write_env_text("".join("%s=%s\n" % kv for kv in self.values.items()))

    def write_env_text(self, text: str):
        """Write .env exactly as given; reload settings and return it."""
        (self.root / ".env").write_text(text, encoding="utf-8")
        return self.reload()

    def setenv(self, name: str, value: str):
        """Set a process environment variable (it wins over .env); reload settings and return it."""
        self.monkeypatch.setenv(name, value)
        return self.reload()

    @staticmethod
    def reload():
        import settings
        return importlib.reload(settings)


@pytest.fixture(autouse=True)
def stack(tmp_path, monkeypatch):
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    root = tmp_path / "stack"
    for sub in ("downloads", "movies", "tv"):
        (root / "media" / sub).mkdir(parents=True)
    monkeypatch.setenv("MEDIA_STACK_DIR", str(root))
    st = Stack(root, monkeypatch)
    st.write_env()
    return st


class NetworkBlocked(RuntimeError):
    """Raised when a test tries to open a network connection."""


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def refuse(*args, **kwargs):
        raise NetworkBlocked("the test-suite never touches the network")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(socket, "getaddrinfo", refuse)


@pytest.fixture
def configure():
    """configure.py at the repository root."""
    return load_path("configure", REPO / "configure.py")


@pytest.fixture
def block_scan():
    """tools/block_scan.py."""
    return load_path("block_scan", REPO / "tools" / "block_scan.py")
