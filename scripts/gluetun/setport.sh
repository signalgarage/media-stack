#!/bin/sh
# Sets qBittorrent's listening port to the port the VPN provider forwarded, so peers can
# connect in. gluetun runs it (VPN_PORT_FORWARDING_UP_COMMAND) each time the forwarded port
# changes, with the new port as $1. vpn_watchdog.py also calls it to repair a mismatch.
#
# POSIX sh on purpose: the gluetun image has no bash. qBittorrent shares gluetun's network
# namespace, so its web UI is on 127.0.0.1:8080 here, and configure.py turns on qBittorrent's
# "bypass authentication for clients on localhost", so no password is needed or stored.
#
# It retries until the new port reads back from qBittorrent: gluetun is usually ready before
# qBittorrent has started, and a single attempt would leave qBittorrent on the old port.

PORT="${1:-}"
API="http://127.0.0.1:8080/api/v2"
TRIES=80        # x 3 s = 4 minutes, longer than qBittorrent needs to start
PAUSE=3

case "$PORT" in
  ''|*[!0-9]*) echo "[setport] not a port number: '$PORT'"; exit 1 ;;
esac
if [ "$PORT" -lt 1 ] || [ "$PORT" -gt 65535 ]; then
  echo "[setport] port out of range: $PORT"
  exit 1
fi

i=0
while [ "$i" -lt "$TRIES" ]; do
  if wget -q -O- -T 3 "$API/app/version" >/dev/null 2>&1; then
    wget -q -O- -T 5 --post-data "json={\"listen_port\":$PORT,\"random_port\":false,\"upnp\":false}" \
      "$API/app/setPreferences" >/dev/null 2>&1
    cur=$(wget -q -O- -T 5 "$API/app/preferences" 2>/dev/null | sed -n 's/.*"listen_port":\([0-9]*\).*/\1/p')
    if [ "$cur" = "$PORT" ]; then
      echo "[setport] qBittorrent listen_port set to $PORT (attempt $i)"
      exit 0
    fi
  fi
  i=$((i + 1))
  sleep "$PAUSE"
done
echo "[setport] FAILED to set qBittorrent listen_port to $PORT after $TRIES attempts"
exit 1
