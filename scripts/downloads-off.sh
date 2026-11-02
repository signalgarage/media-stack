#!/usr/bin/env bash
# downloads-off.sh: switch downloading OFF (for example in the daytime, so streaming has the
# disks and the connection to itself).
#
# Stops only qBittorrent, with 30 s to save its state cleanly; the VPN keeps running, so
# downloads-on.sh brings everything back quickly. A container stopped with `docker compose
# stop` stays stopped across reboots until it is started again. arr_health_guard,
# malware_guard, dead_torrent_reaper and vpn_watchdog treat a stopped qBittorrent as
# "downloads switched off", not as a fault. Safe to run any number of times.
#
# Exit: 0 stopped (or already stopped), 1 the stop failed, 2 docker unusable.

set -uo pipefail

STOP_TIMEOUT_S=30      # time qBittorrent gets to write its resume data before it is killed

STACK="${MEDIA_STACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

log() { printf '%s [downloads-off] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  log "ERROR: docker is not usable by this user, and 'sudo -n docker' is not allowed"
  exit 2
fi
cd "$STACK" || exit 2

log "=== downloads OFF ==="
if "${DOCKER[@]}" compose --project-directory "$STACK" stop -t "$STOP_TIMEOUT_S" qbittorrent; then
  log "qBittorrent stopped; the VPN keeps running"
  exit 0
fi
log "ERROR: could not stop qBittorrent"
exit 1
