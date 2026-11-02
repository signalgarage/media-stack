#!/usr/bin/env bash
# downloads-on.sh: switch downloading ON (for example at night, when nobody is streaming).
#
# Starts the VPN (gluetun) and then qBittorrent through docker compose, logs in to
# qBittorrent's web API, resumes every torrent, applies the "night" limits below, and asks
# Radarr and Sonarr to search for everything they are missing. Safe to run any number of
# times. Pair it with downloads-off.sh, for example in cron:
#   0 1 * * *  <stack>/scripts/run_guard.sh downloads-on
#   0 9 * * *  <stack>/scripts/run_guard.sh downloads-off
#
# Settings come from the environment or the stack's .env: QBIT_URL, QBIT_USERNAME,
# QBIT_PASSWORD, RADARR_URL, SONARR_URL and the optional RADARR_API_KEY / SONARR_API_KEY.
# The .env file is read as data with grep, never sourced. Empty API keys are read from
# config/<app>/config.xml. Secrets never appear on a command line (other users can see those):
# the password reaches curl on stdin and the API keys through a curl config on stdin.
#
# Exit: 0 done, 1 a step failed (the rest still ran), 2 docker unusable.

set -uo pipefail

# Night limits: full speed while nobody watches, a modest upload cap, more torrents at once.
NIGHT_DL_LIMIT=$((40 * 1024 * 1024))   # bytes/s (40 MB/s)
NIGHT_UP_LIMIT=$((2 * 1024 * 1024))    # bytes/s (2 MB/s)
NIGHT_MAX_ACTIVE=8                     # active torrents in total
NIGHT_MAX_ACTIVE_DL=6
NIGHT_MAX_ACTIVE_UP=3
HEALTH_TRIES=36                        # x 5 s: up to 3 min for the VPN tunnel to come up
WEBUI_TRIES=40                         # x 3 s: qBittorrent needs a while after a cold start

STACK="${MEDIA_STACK_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
ENV_FILE="$STACK/.env"
status=0

log() { printf '%s [downloads-on] %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$*"; }

# env_get NAME [DEFAULT]: process environment first, then .env, then the default.
env_get() {
  local name="$1" def="${2:-}" val=""
  val="${!name:-}"
  if [ -z "$val" ] && [ -f "$ENV_FILE" ]; then
    val="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}[[:space:]]*=" "$ENV_FILE" | tail -n 1 | cut -d= -f2-)"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    case "$val" in
      \"*\") val="${val#\"}"; val="${val%\"}" ;;
      \'*\') val="${val#\'}"; val="${val%\'}" ;;
    esac
  fi
  printf '%s' "${val:-$def}"
}

if docker info >/dev/null 2>&1; then
  DOCKER=(docker)
elif sudo -n docker info >/dev/null 2>&1; then
  DOCKER=(sudo -n docker)
else
  log "ERROR: docker is not usable by this user, and 'sudo -n docker' is not allowed"
  exit 2
fi
cd "$STACK" || exit 2
compose() { "${DOCKER[@]}" compose --project-directory "$STACK" "$@"; }

# start_service NAME: start an existing container; create it only if it does not exist yet.
start_service() {
  if compose start "$1" >/dev/null 2>&1 || compose up -d "$1" >/dev/null 2>&1; then
    log "$1 started"
  else
    log "ERROR: could not start $1"
    status=1
  fi
}

log "=== downloads ON ==="
start_service gluetun
for _ in $(seq 1 "$HEALTH_TRIES"); do
  health="$("${DOCKER[@]}" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' gluetun 2>/dev/null)"
  [ "$health" = "healthy" ] || [ "$health" = "none" ] && break
  sleep 5
done
[ "${health:-}" = "healthy" ] || [ "${health:-}" = "none" ] || log "WARNING: gluetun is not healthy yet (${health:-unknown})"
start_service qbittorrent

QBIT_URL="$(env_get QBIT_URL http://localhost:8080)"
QBIT_URL="${QBIT_URL%/}"
QBIT_USER="$(env_get QBIT_USERNAME admin)"
QBIT_PASS="$(env_get QBIT_PASSWORD)"

code=000
for _ in $(seq 1 "$WEBUI_TRIES"); do
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$QBIT_URL/api/v2/app/version" || true)"
  [ "$code" != "000" ] && break
  sleep 3
done
if [ "$code" = "000" ]; then
  log "ERROR: qBittorrent's web UI did not come up at $QBIT_URL"
  exit 1
fi

JAR="$(mktemp)"
trap 'rm -f "$JAR"' EXIT
# Older qBittorrent versions answer 200 "Ok." (or "Fails."); 5.x answers 204 with no body, and 401 when refused.
login="$(printf '%s' "$QBIT_PASS" | curl -s -m 15 -c "$JAR" -H "Referer: $QBIT_URL" -w '%{http_code}' \
  --data-urlencode "username=$QBIT_USER" --data-urlencode "password@-" "$QBIT_URL/api/v2/auth/login" || true)"
if [ "$login" != "Ok.200" ] && [ "$login" != "204" ]; then
  log "ERROR: qBittorrent login failed (check QBIT_USERNAME / QBIT_PASSWORD in .env)"
  exit 1
fi

# qbit_post PATH [curl data args...] -> prints the HTTP status code
qbit_post() {
  local path="$1"
  shift
  curl -s -m 30 -o /dev/null -w '%{http_code}' -b "$JAR" -H "Referer: $QBIT_URL" "$@" "$QBIT_URL/api/v2/$path" || true
}

# qBittorrent 5 renamed torrents/resume to torrents/start; try the new name first.
code="$(qbit_post torrents/start --data "hashes=all")"
[ "$code" = "404" ] && code="$(qbit_post torrents/resume --data "hashes=all")"
if [ "$code" = "200" ]; then log "all torrents resumed"; else log "ERROR: resume failed (HTTP $code)"; status=1; fi

prefs="{\"dl_limit\":$NIGHT_DL_LIMIT,\"up_limit\":$NIGHT_UP_LIMIT,\"max_active_torrents\":$NIGHT_MAX_ACTIVE,\"max_active_downloads\":$NIGHT_MAX_ACTIVE_DL,\"max_active_uploads\":$NIGHT_MAX_ACTIVE_UP}"
code="$(qbit_post app/setPreferences --data-urlencode "json=$prefs")"
if [ "$code" = "200" ]; then
  log "night limits set: $((NIGHT_DL_LIMIT / 1048576)) MB/s down, $((NIGHT_UP_LIMIT / 1048576)) MB/s up, $NIGHT_MAX_ACTIVE active"
else
  log "ERROR: could not set the night limits (HTTP $code)"
  status=1
fi

# arr_key APP UPPER: the .env key, else the app's own config.xml.
arr_key() {
  local key
  key="$(env_get "${2}_API_KEY")"
  if [ -z "$key" ] && [ -r "$STACK/config/$1/config.xml" ]; then
    key="$(sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' "$STACK/config/$1/config.xml" | head -n 1)"
  fi
  printf '%s' "$key"
}

# arr_command APP UPPER URL_DEFAULT COMMAND
arr_command() {
  local key url code
  key="$(arr_key "$1" "$2")"
  url="$(env_get "${2}_URL" "$3")"
  url="${url%/}"
  if [ -z "$key" ]; then
    log "ERROR: no API key for $1; skipped $4"
    status=1
    return
  fi
  code="$(printf 'header = "X-Api-Key: %s"\n' "$key" | curl -s -m 30 -K - -o /dev/null -w '%{http_code}' \
    -X POST -H "Content-Type: application/json" -d "{\"name\":\"$4\"}" "$url/api/v3/command" || true)"
  case "$code" in
    2??) log "$1: $4 started" ;;
    *) log "ERROR: $1 did not accept $4 (HTTP $code)"; status=1 ;;
  esac
}
arr_command radarr RADARR http://localhost:7878 MissingMoviesSearch
arr_command sonarr SONARR http://localhost:8989 MissingEpisodeSearch

log "=== downloads running ==="
exit "$status"
