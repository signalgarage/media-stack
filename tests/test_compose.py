"""docker-compose.yml, the test override and .env.example: structure and the no-secrets rule.

Parsed with PyYAML only; Docker is never needed (CI runs `docker compose config` separately).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parent.parent
COMPOSE_TEXT = (REPO / "docker-compose.yml").read_text(encoding="utf-8")
COMPOSE = yaml.safe_load(COMPOSE_TEXT)
SERVICES = COMPOSE["services"]
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")

HEX32 = re.compile(r"[0-9a-fA-F]{32,}")
TELEGRAM_TOKEN = re.compile(r"\d{6,}:[A-Za-z0-9_-]{30,}")
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
DOC_RANGES = ("192.0.2.", "198.51.100.", "203.0.113.")


def _strings(node):
    if isinstance(node, dict):
        for k, v in node.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(node, list):
        for v in node:
            yield from _strings(v)
    elif node is not None:
        yield str(node)


class _OverrideLoader(yaml.SafeLoader):
    """Compose's `!reset` tag (Compose 2.24+) is not plain YAML."""


_OverrideLoader.add_constructor("!reset", lambda loader, node: "!reset")


def _env_example() -> dict:
    out = {}
    for line in ENV_EXAMPLE.splitlines():
        if line.strip() and not line.lstrip().startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            out[key.strip()] = value.strip()
    return out


# ---------------------------------------------------------------- the VPN and the ports


def test_qbittorrent_lives_inside_the_vpn_container():
    qbit = SERVICES["qbittorrent"]
    assert qbit["network_mode"] == "service:gluetun"
    assert "ports" not in qbit and "networks" not in qbit
    assert qbit["depends_on"]["gluetun"]["condition"] == "service_healthy"


def test_gluetun_publishes_the_qbittorrent_ports():
    gluetun = SERVICES["gluetun"]
    assert "8080:8080" in gluetun["ports"]
    assert {"6881:6881", "6881:6881/udp"} <= set(gluetun["ports"])
    assert gluetun["cap_add"] == ["NET_ADMIN"]
    assert gluetun["environment"]["VPN_PORT_FORWARDING_UP_COMMAND"] == "/scripts/setport.sh {{PORT}}"


@pytest.mark.parametrize("name", ["moviebot", "flaresolverr", "qbittorrent"])
def test_internal_services_publish_no_ports(name):
    assert not SERVICES[name].get("ports")


@pytest.mark.parametrize("name, port", [("radarr", 7878), ("sonarr", 8989), ("prowlarr", 9696), ("jellyseerr", 5055),
                                        ("plex", 32400), ("uptime-kuma", 3001), ("tautulli", 8181), ("bazarr", 6767)])
def test_published_ports(name, port):
    assert SERVICES[name]["ports"] == ["%d:%d" % (port, port)]


# ---------------------------------------------------------------- every service


@pytest.mark.parametrize("name", sorted(SERVICES))
def test_every_service_restarts_logs_with_rotation_and_has_a_fixed_name(name):
    svc = SERVICES[name]
    assert svc["restart"] == "unless-stopped"
    assert svc["container_name"] == name                  # the guard scripts address containers by name
    assert svc["logging"] == {"driver": "json-file", "options": {"max-size": "10m", "max-file": "3"}}
    if name not in ("gluetun", "uptime-kuma"):            # both need privileges no-new-privileges would take away
        assert svc["security_opt"] == ["no-new-privileges:true"]
    if name != "qbittorrent":
        assert svc["networks"] == ["media"]


def test_optional_services_sit_behind_profiles():
    profiles = {name: svc.get("profiles") for name, svc in SERVICES.items() if svc.get("profiles")}
    assert profiles == {"plex": ["plex"], "uptime-kuma": ["monitoring"], "tautulli": ["monitoring"],
                        "bazarr": ["subtitles"]}
    assert COMPOSE["networks"] == {"media": {"driver": "bridge"}}


def test_images_come_from_their_publishers_and_the_bot_is_built_here():
    images = {name: svc.get("image") for name, svc in SERVICES.items()}
    assert images.pop("moviebot") is None and SERVICES["moviebot"]["build"] == "./moviebot"
    assert images["gluetun"] == "qmcgaw/gluetun:latest"
    assert images["flaresolverr"] == "ghcr.io/flaresolverr/flaresolverr:latest"
    assert images["jellyseerr"] == "fallenbagel/jellyseerr:latest"
    assert images["uptime-kuma"] == "louislam/uptime-kuma:2"
    for name in ("qbittorrent", "radarr", "sonarr", "prowlarr", "bazarr", "tautulli", "plex"):
        assert images[name] == "lscr.io/linuxserver/%s:latest" % name


@pytest.mark.parametrize("name, mode", [("qbittorrent", ""), ("radarr", ""), ("sonarr", ""), ("bazarr", ""),
                                        ("moviebot", ":ro"), ("plex", ":ro")])
def test_one_media_bind_per_service(name, mode):
    """Hardlinks need ONE bind of one filesystem: never extra binds for sub-folders."""
    data_binds = [v for v in SERVICES[name]["volumes"] if re.search(r":/data(:|$)", v)]
    assert data_binds == ["${MEDIA_ROOT:?run ./install.sh first}:/data" + mode]


def test_the_bot_gets_state_and_in_network_urls():
    bot = SERVICES["moviebot"]
    env = bot["environment"]
    assert env["STATE_DIR"] == "/state" and "${STATE_DIR:-./state}:/state" in bot["volumes"]
    assert env["RADARR_URL"] == "http://radarr:7878" and env["QBIT_URL"] == "http://gluetun:8080"
    assert bot["user"] == "${PUID:-1000}:${PGID:-1000}"


# ---------------------------------------------------------------- no secrets, no addresses


def test_no_value_in_the_compose_file_looks_like_a_secret():
    for text in list(_strings(COMPOSE)) + COMPOSE_TEXT.splitlines():   # values, and the comments too
        assert not HEX32.search(text), text
        assert not TELEGRAM_TOKEN.search(text), text
        for ip in IPV4.findall(text):
            assert ip in ("0.0.0.0", "127.0.0.1"), text


def test_secrets_reach_the_containers_only_through_env_variables():
    secret_keys = ("TOKEN", "PASSWORD", "SECRET", "API_KEY", "PRIVATE_KEY", "PRESHARED_KEY", "CLAIM")
    for name, svc in SERVICES.items():
        env = svc.get("environment") or {}
        for key, value in env.items():
            if any(part in key for part in secret_keys):
                assert re.fullmatch(r"\$\{%s(:-)?\}" % key, str(value)), "%s.%s=%s" % (name, key, value)


def test_every_variable_the_compose_file_uses_is_documented():
    used = set(re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_]*)", COMPOSE_TEXT))
    assert used <= set(_env_example())


def test_the_test_override_only_swaps_gluetun():
    override = yaml.load((REPO / "compose" / "test-no-vpn.override.yml").read_text(encoding="utf-8"),
                         Loader=_OverrideLoader)
    assert list(override["services"]) == ["gluetun"]
    gluetun = override["services"]["gluetun"]
    assert gluetun["image"].startswith("alpine:")
    assert gluetun["cap_add"] == gluetun["devices"] == gluetun["environment"] == "!reset"
    assert gluetun["healthcheck"]["test"] == ["CMD", "true"]


def test_env_example_ships_no_secrets_and_no_private_addresses():
    env = _env_example()
    secret_name = re.compile(r"TOKEN|PASSWORD|SECRET|API_KEY|PRIVATE_KEY|PRESHARED|CLAIM|KUMA_PUSH")
    secret_keys = [k for k in env if secret_name.search(k)]
    assert len(secret_keys) > 20
    assert {k: env[k] for k in secret_keys if env[k]} == {}
    assert env["TZ"] == "Etc/UTC"
    assert env["VPN_MODE"] == "on"                        # "off" is for throw-away test installs only
    for line in ENV_EXAMPLE.splitlines():
        assert not HEX32.search(line) and not TELEGRAM_TOKEN.search(line), line
        for ip in IPV4.findall(line):
            assert ip in ("0.0.0.0", "127.0.0.1") or ip.startswith(DOC_RANGES), line
    assert len(env) == len([x for x in ENV_EXAMPLE.splitlines() if re.match(r"[A-Z_]+=", x)])    # no key twice
