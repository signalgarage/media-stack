#!/bin/bash
# On-grab guard for Radarr and Sonarr (Settings > Connect > Custom Script "On-grab guard",
# On Grab only; configure.py sets it up). When a grabbed movie or episode is already in the
# library on disk, the download is cancelled and the release blocklisted straight away instead
# of downloading a second copy. scripts/regrab_guard.py on the host is the backstop: it checks
# the whole queue every 10 minutes, so this script may stay silent whenever it is unsure.
#
# Rules (each one closes a failure mode seen in practice):
#  - a movie counts as owned when one video file name contains the release year and every
#    significant word of the title (words under three letters and a few filler words are
#    ignored; numbers such as sequel digits always count);
#  - a title without a single significant word is never judged: it would match any file from
#    the same year (a two-letter title once matched an unrelated film of that year);
#  - when the movie's current file is below the profile cutoff the grab is an upgrade: allowed;
#  - an episode grab, season packs included, is cancelled only when EVERY episode in it is
#    already on disk; one missing episode is enough to let the whole grab through.
#
# Runs inside the radarr/sonarr containers (bash, curl and jq come with the images) with the
# media at /data and this folder at /scripts. Folder names come from /scripts/arr.env, written
# by configure.py. Cache and log live in /config/ongrab/. ONGRAB_DRYRUN=1 (or
# ONGRAB_DRYRUN=true in .env, applied by configure.py) logs decisions without cancelling.
# The ONGRAB_DATA/ONGRAB_CONFIG/ONGRAB_ARR_ENV/ONGRAB_API_URL overrides exist for tests.

DATA="${ONGRAB_DATA:-/data}"
CONF="${ONGRAB_CONFIG:-/config}"
ARR_ENV="${ONGRAB_ARR_ENV:-/scripts/arr.env}"
STATE="$CONF/ongrab"
LOG="$STATE/ongrab_guard.log"
CACHE="$STATE/movie_files.cache"
CACHE_MAX_AGE_MIN=60      # the owned-file list is rebuilt at most once an hour
QUEUE_WAIT_TRIES=12       # the queue entry shows up a few seconds after the grab event:
QUEUE_WAIT_SLEEP=10       # keep looking for it in the background for up to two minutes
LOG_MAX_BYTES=1048576     # beyond this the log is trimmed to its last LOG_KEEP_LINES lines
LOG_KEEP_LINES=2000
VIDEO_FIND=( -iname '*.mkv' -o -iname '*.mp4' -o -iname '*.avi' -o -iname '*.m4v' -o -iname '*.ts'
             -o -iname '*.m2ts' -o -iname '*.mov' -o -iname '*.wmv' -o -iname '*.mpg' -o -iname '*.mpeg'
             -o -iname '*.webm' -o -iname '*.vob' )

mkdir -p "$STATE" 2>/dev/null
log() { echo "$(date '+%F %T') $*" >> "$LOG" 2>/dev/null; }

trim_log() {
  local size
  [ -f "$LOG" ] || return 0
  size=$(wc -c < "$LOG")
  size="${size//[!0-9]/}"
  if [ "${size:-0}" -gt "$LOG_MAX_BYTES" ]; then
    tail -n "$LOG_KEEP_LINES" "$LOG" > "$LOG.tmp.$$" 2>/dev/null && mv -f "$LOG.tmp.$$" "$LOG"
  fi
}

arr_env() { # KEY DEFAULT -> value from arr.env, else the default
  local v=""
  if [ -r "$ARR_ENV" ]; then
    v=$(sed -n "s/^$1=//p" "$ARR_ENV" | tail -n 1)
    v="${v%\"}"; v="${v#\"}"; v="${v%\'}"; v="${v#\'}"
  fi
  printf '%s' "${v:-$2}"
}

xml_value() { # TAG -> value from the app's config.xml
  sed -n "s:.*<$1>\([^<]*\)</$1>.*:\1:p" "$CONF/config.xml" 2>/dev/null | head -n 1
}

init_app() { # DEFAULT_PORT
  KEY=$(xml_value ApiKey)
  if [ -z "$KEY" ]; then
    log "no API key readable in $CONF/config.xml: guard inactive"
    exit 0
  fi
  local port urlbase
  port=$(xml_value Port)
  urlbase=$(xml_value UrlBase)
  BASE="${ONGRAB_API_URL:-http://localhost:${port:-$1}${urlbase}}/api/v3"
  DL_DIR=$(arr_env DOWNLOADS_DIR downloads)
  TV_DIR=$(arr_env TV_DIR tv)
  DL_DIR="${DL_DIR%/}"; DL_DIR="${DL_DIR#/}"
  TV_DIR="${TV_DIR%/}"; TV_DIR="${TV_DIR#/}"
  case "${ONGRAB_DRYRUN:-$(arr_env ONGRAB_DRYRUN 0)}" in
    1|true|TRUE|True|yes|on) DRY=1 ;;
    *) DRY=0 ;;
  esac
  if ! command -v jq >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
    log "curl or jq not found in this container: guard inactive"
    exit 0
  fi
  trim_log
}

api_get() { curl -s -m 15 -H "X-Api-Key: $KEY" "$BASE/$1"; }

sig() { printf '%s\n' "$1" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' ' '; }   # significant tokens

is_filler() { case "$1" in the|and|part|for|a|an|of|'') return 0 ;; esac; return 1; }

sigcount() { # TOKENS... -> number of words that can identify a title on their own
  local n=0 t
  for t in "$@"; do
    is_filler "$t" && continue
    [ "${#t}" -ge 3 ] && n=$((n + 1))
  done
  echo "$n"
}

tokmatch() { # HAYSTACK(lowercase) TOKENS... -> 0 when every token is present
  local hay="$1" t
  shift
  for t in "$@"; do
    is_filler "$t" && continue
    if [ "${#t}" -lt 3 ]; then
      case "$t" in [0-9]*) ;; *) continue ;; esac
    fi
    case "$hay" in *"$t"*) ;; *) return 1 ;; esac
  done
  return 0
}

queue_id() { # DOWNLOAD_ID -> first queue record id for that download
  api_get "queue?pageSize=1000" \
    | jq -r --arg dl "$1" '[.records[]? | select(.downloadId == $dl) | .id][0] // empty' 2>/dev/null
}

remove_from_queue() { # QUEUE_ID LABEL
  if [ "$DRY" = 1 ]; then
    log "DRYRUN would cancel: $2 (queue id $1)"
    return
  fi
  # skipRedownload: the title is owned, so no replacement search either.
  local code
  code=$(curl -s -m 30 -o /dev/null -w '%{http_code}' -X DELETE -H "X-Api-Key: $KEY" \
    "$BASE/queue/$1?removeFromClient=true&blocklist=true&skipRedownload=true")
  case "$code" in
    2*) log "CANCELLED re-download: $2 (queue id $1)" ;;
    *) log "cancel FAILED (HTTP $code): $2 (queue id $1)" ;;
  esac
}

cancel() { # DOWNLOAD_ID LABEL
  local qid
  qid=$(queue_id "$1")
  if [ -n "$qid" ]; then
    remove_from_queue "$qid" "$2"
    return
  fi
  log "owned, waiting for its queue entry: $2"
  # Detached and with its output closed, so the app is not kept waiting for this script.
  (
    i=0
    while [ "$i" -lt "$QUEUE_WAIT_TRIES" ]; do
      sleep "$QUEUE_WAIT_SLEEP"
      qid=$(queue_id "$1")
      if [ -n "$qid" ]; then
        remove_from_queue "$qid" "$2"
        exit 0
      fi
      i=$((i + 1))
    done
    log "OWNED but it never showed up in the queue: $2 (regrab_guard will catch it)"
  ) </dev/null >/dev/null 2>&1 &
}

movie_roots() { # every top-level library folder: everything under /data except downloads and TV
  local d name
  for d in "$DATA"/*/; do
    [ -d "$d" ] || continue
    name=$(basename "$d")
    case "$name" in
      "$DL_DIR"|"$TV_DIR"|.*|@*|'#'*|lost+found) continue ;;
    esac
    printf '%s\n' "${d%/}"
  done
}

refresh_movie_cache() {
  if [ -s "$CACHE" ] && [ -n "$(find "$CACHE" -mmin "-$CACHE_MAX_AGE_MIN" 2>/dev/null)" ]; then
    return
  fi
  local roots=() r tmp="$CACHE.tmp.$$"
  while IFS= read -r r; do roots+=("$r"); done < <(movie_roots)
  if [ "${#roots[@]}" -eq 0 ]; then
    : > "$tmp" && mv -f "$tmp" "$CACHE"
    return
  fi
  # -prune also covers download or TV folders nested deeper than the top level.
  find "${roots[@]}" \( -path "$DATA/$DL_DIR" -o -path "$DATA/$TV_DIR" \) -prune \
      -o -type f \( "${VIDEO_FIND[@]}" \) -print 2>/dev/null \
    | sed 's#.*/##' | tr '[:upper:]' '[:lower:]' > "$tmp" && mv -f "$tmp" "$CACHE"
}

episodes_owned() { # DIR SEASON EPISODES(comma separated) -> 0 when every episode has a file
  local dir="$1" season="$2" files e sxe
  [ -d "$dir" ] || return 1
  files=$(find "$dir" -type f \( "${VIDEO_FIND[@]}" \) 2>/dev/null | tr '[:upper:]' '[:lower:]')
  [ -n "$files" ] || return 1
  for e in $(printf '%s' "$3" | tr ',' ' '); do
    case "$e" in ''|*[!0-9]*) return 1 ;; esac
    sxe=$(printf 's%02de%02d' "$((10#$season))" "$((10#$e))")
    printf '%s\n' "$files" | grep -q "$sxe" || return 1
  done
  return 0
}

# ---------------- RADARR (movies) ----------------
if [ "${radarr_eventtype:-}" = "Grab" ]; then
  init_app 7878
  title="${radarr_movie_title:-}"
  year="${radarr_movie_year:-}"
  dlid="${radarr_download_id:-}"
  [ -n "$dlid" ] || exit 0
  case "$year" in
    ''|0|*[!0-9]*) log "no release year, not judging: $title"; exit 0 ;;
  esac
  toks=$(sig "$title")
  # shellcheck disable=SC2086  # word splitting of the token list is intended
  if [ "$(sigcount $toks)" -eq 0 ]; then
    log "title too short to match safely, not judging: $title ($year)"
    exit 0
  fi
  cutoff=$(api_get "movie/${radarr_movie_id:-0}" | jq -r '.movieFile.qualityCutoffNotMet // false' 2>/dev/null)
  if [ "$cutoff" = "true" ]; then
    log "upgrade grab (current file below cutoff) - allowed: $title ($year)"
    exit 0
  fi
  refresh_movie_cache
  [ -r "$CACHE" ] || exit 0
  found=""
  while IFS= read -r b; do
    [ -n "$b" ] || continue
    case "$b" in *"$year"*) ;; *) continue ;; esac
    # shellcheck disable=SC2086
    if tokmatch "$b" $toks; then
      found="$b"
      break
    fi
  done < "$CACHE"
  if [ -n "$found" ]; then
    log "owned movie grabbed: $title ($year) -> $found"
    cancel "$dlid" "movie: $title ($year)"
  elif [ "$DRY" = 1 ]; then
    log "not owned (ok to grab): $title ($year)"
  fi
  exit 0
fi

# ---------------- SONARR (episodes) ----------------
if [ "${sonarr_eventtype:-}" = "Grab" ]; then
  init_app 8989
  series="${sonarr_series_title:-}"
  season="${sonarr_release_seasonnumber:-}"
  epnums="${sonarr_release_episodenumbers:-}"
  dlid="${sonarr_download_id:-}"
  [ -n "$dlid" ] || exit 0
  [ -n "$epnums" ] || exit 0
  case "$season" in ''|*[!0-9]*) exit 0 ;; esac
  stoks=$(sig "$series")
  # shellcheck disable=SC2086
  if [ "$(sigcount $stoks)" -eq 0 ]; then
    log "series title too short to match safely, not judging: $series"
    exit 0
  fi
  label="episode: $series S$season [$epnums]"
  # Sonarr's own folder for the series first, then the first folder under the TV library whose
  # name holds every significant word of the title (copies Sonarr has not scanned yet).
  owned=""
  if [ -n "${sonarr_series_path:-}" ] && episodes_owned "$sonarr_series_path" "$season" "$epnums"; then
    owned="$sonarr_series_path"
  else
    for d in "$DATA/$TV_DIR"/*/; do
      [ -d "$d" ] || continue
      # shellcheck disable=SC2086
      if tokmatch "$(basename "$d" | tr '[:upper:]' '[:lower:]')" $stoks; then
        if episodes_owned "$d" "$season" "$epnums"; then owned="${d%/}"; fi
        break
      fi
    done
  fi
  if [ -n "$owned" ]; then
    log "owned episodes grabbed: $series S$season [$epnums] -> $owned"
    cancel "$dlid" "$label"
  elif [ "$DRY" = 1 ]; then
    log "missing some episodes (ok to grab): $series S$season [$epnums]"
  fi
  exit 0
fi

exit 0
