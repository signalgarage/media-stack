#!/usr/bin/env bash
# Proves the VPN kill switch: with the VPN container stopped, qBittorrent must have no network.
#
# With the stack running it
#   1. shows qBittorrent's public address as seen through the tunnel (and this server's own
#      address, which must differ),
#   2. stops gluetun and checks that qBittorrent can reach nothing and has no network
#      interface other than loopback,
#   3. starts gluetun again, restarts qBittorrent so it joins the new network namespace,
#      waits until the tunnel works again, and prints PASS or FAIL.
# It contacts a public "what is my IP" service (api.ipify.org) from both places, nothing else.
# Downloads pause for a minute or two while it runs.

set -uo pipefail

STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
IP_URL="https://api.ipify.org"
WAIT_HEALTHY=180      # seconds gluetun gets to reconnect

say()  { printf '%s\n' "$*"; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

env_value() { # KEY -> value from .env (read, never sourced)
  local v=""
  if [[ -r "$STACK/.env" ]]; then
    v="$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$STACK/.env" | tail -n 1 | cut -d= -f2- || true)"
  fi
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  v="${v#[\"\']}"
  v="${v%[\"\']}"
  printf '%s' "$v"
}

if [[ "$(env_value VPN_MODE)" == off ]]; then
  say "VPN_MODE=off: this is a test install without a VPN, so there is no kill switch to test."
  exit 0
fi

DOCKER=(docker)
command -v docker >/dev/null 2>&1 || die "docker is not installed"
if ! docker info >/dev/null 2>&1; then DOCKER=(sudo docker); fi
d() { "${DOCKER[@]}" "$@"; }

running() { [[ "$(d inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" == true ]]; }
running gluetun || die "gluetun is not running (start the stack first: docker compose up -d)"
running qbittorrent || die "qbittorrent is not running (start the stack first: docker compose up -d)"

qbit_ip() { # public address as seen from inside qbittorrent, empty when there is no network
  d exec qbittorrent sh -c "wget -qO- -T 8 '$IP_URL' 2>/dev/null || curl -fsS -m 8 '$IP_URL' 2>/dev/null" 2>/dev/null \
    | tr -d '[:space:]'
}
qbit_interfaces() { d exec qbittorrent sh -c 'ls /sys/class/net' 2>/dev/null | tr '\n' ' '; }
gluetun_health() { d inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' gluetun 2>/dev/null; }

STOPPED=0
restore() {
  if [[ $STOPPED -eq 1 ]]; then
    say "restoring: starting gluetun and restarting qbittorrent..."
    d start gluetun >/dev/null 2>&1
    local i
    for ((i = 0; i < WAIT_HEALTHY; i += 5)); do
      [[ "$(gluetun_health)" == healthy ]] && break
      sleep 5
    done
    d restart qbittorrent >/dev/null 2>&1
    STOPPED=0
  fi
}
trap restore EXIT INT TERM

result=PASS
fail() { say "  FAIL: $*"; result=FAIL; }

say "1. Through the tunnel"
host_ip="$(curl -fsS -m 8 "$IP_URL" 2>/dev/null | tr -d '[:space:]')"
vpn_ip="$(qbit_ip)"
say "   this server's public address : ${host_ip:-unknown}"
say "   qbittorrent's public address : ${vpn_ip:-none}"
if [[ -z "$vpn_ip" ]]; then
  fail "qbittorrent cannot reach the internet even with the VPN up (check: docker logs gluetun)"
elif [[ -n "$host_ip" && "$vpn_ip" == "$host_ip" ]]; then
  fail "qbittorrent uses this server's own address: its traffic is NOT going through the VPN"
fi

say "2. VPN container stopped"
# Plain `docker stop` on purpose: compose may also stop the services that depend on gluetun,
# and qbittorrent has to keep running for this check to mean anything.
d stop gluetun >/dev/null || die "could not stop gluetun"
STOPPED=1
sleep 3
running qbittorrent || fail "qbittorrent stopped together with gluetun (cannot check it)"
leak_ip="$(qbit_ip)"
ifaces="$(qbit_interfaces)"
say "   qbittorrent's public address : ${leak_ip:-none (good)}"
say "   network interfaces           : ${ifaces:-none}"
if [[ -n "$leak_ip" ]]; then fail "qbittorrent still reached the internet with the VPN stopped: LEAK"; fi
if [[ -n "${ifaces// /}" && "${ifaces// /}" != lo ]]; then fail "qbittorrent still has interfaces other than lo: $ifaces"; fi

say "3. VPN back"
restore
back_ip=""
for ((i = 0; i < 60; i += 5)); do
  back_ip="$(qbit_ip)"
  [[ -n "$back_ip" ]] && break
  sleep 5
done
say "   gluetun health               : $(gluetun_health)"
say "   qbittorrent's public address : ${back_ip:-none}"
if [[ -z "$back_ip" ]]; then fail "qbittorrent did not get its network back (docker logs gluetun; docker restart qbittorrent)"; fi

say ""
say "$result"
[[ "$result" == PASS ]]
