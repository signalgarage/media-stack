#!/usr/bin/env bash
# Stops media-stack and removes its guard jobs from your crontab.
#
# By default it keeps everything you would need to come back with ./install.sh: your media,
# .env, the app settings in config/, state/, logs/ and backups/. --purge also deletes those
# from THIS folder. Nothing outside this folder is ever deleted, and your media never is.

set -euo pipefail

STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$STACK/.env"
MARK_BEGIN="# >>> media-stack >>>"
MARK_END="# <<< media-stack <<<"

usage() {
  cat <<'EOF'
Usage: ./uninstall.sh [--yes] [--remove-images] [--purge]

  (no option)       stop and remove the containers and their network, remove the cron jobs
  --remove-images   also delete the images the stack downloaded or built
  --purge           also delete .env, config/, state/, logs/ and backups/ in this folder
                    (your media folder is never touched)
  --yes             do not ask for confirmation
  -h, --help        show this help
EOF
}

say()  { printf '%s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

ASSUME_YES=0
REMOVE_IMAGES=0
PURGE=0
while [[ $# -gt 0 ]]; do
  case "$1" in
    --yes|-y) ASSUME_YES=1 ;;
    --remove-images) REMOVE_IMAGES=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown option: $1 (see ./uninstall.sh --help)" ;;
  esac
  shift
done

confirm() { # PROMPT ACCEPTED-ANSWER...: true when the reply is one of the accepted answers
  local r="" prompt="$1" a
  shift
  if [[ $ASSUME_YES -eq 1 ]]; then return 0; fi
  { : </dev/tty; } 2>/dev/null || die "no terminal to confirm on: add --yes"
  IFS= read -r -p "$prompt " r </dev/tty || true
  for a in "$@"; do if [[ "$r" == "$a" ]]; then return 0; fi; done
  return 1
}

env_value() { # KEY -> value from .env (read, never sourced)
  local v=""
  if [[ -r "$ENV_FILE" ]]; then
    v="$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE" | tail -n 1 | cut -d= -f2- || true)"
  fi
  v="${v#"${v%%[![:space:]]*}"}"
  v="${v%"${v##*[![:space:]]}"}"
  v="${v#[\"\']}"
  v="${v%[\"\']}"
  printf '%s' "$v"
}

[[ -f "$STACK/docker-compose.yml" ]] || die "run this from the media-stack folder (docker-compose.yml not found)"

say "This stops media-stack in $STACK and removes its cron jobs."
if [[ $REMOVE_IMAGES -eq 1 ]]; then say "It also deletes the stack's Docker images."; fi
if [[ $PURGE -eq 1 ]]; then
  say "--purge: it also DELETES .env, config/ (all app settings), state/, logs/ and backups/ here."
fi
say "Your media folder is not touched."
if [[ $PURGE -eq 1 ]]; then
  confirm "Type 'delete' to continue:" delete || die "cancelled"
else
  confirm "Continue? [y/N]" y Y yes YES || die "cancelled"
fi

# ---- cron block
if command -v crontab >/dev/null 2>&1; then
  current="$(crontab -l 2>/dev/null || true)"
  if grep -qxF "$MARK_BEGIN" <<< "$current"; then
    printf '%s\n' "$current" | awk -v b="$MARK_BEGIN" -v e="$MARK_END" '
      $0 == b { skip = 1; next }
      $0 == e { skip = 0; next }
      !skip' | crontab -
    say "cron: media-stack jobs removed"
  else
    say "cron: no media-stack jobs installed"
  fi
fi

# ---- containers
DOCKER=(docker)
if command -v docker >/dev/null 2>&1; then
  if ! docker info >/dev/null 2>&1; then DOCKER=(sudo docker); fi
  # Compose has to read .env, not variables that happen to be set in this shell.
  unset COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME
  args=(--profile plex --profile monitoring --profile subtitles)
  if [[ ! -f "$ENV_FILE" ]]; then args+=(--env-file "$STACK/.env.example"); fi
  down=(down --remove-orphans)
  if [[ $REMOVE_IMAGES -eq 1 ]]; then down+=(--rmi all); fi
  if (cd "$STACK" && "${DOCKER[@]}" compose "${args[@]}" "${down[@]}"); then
    say "containers: stopped and removed"
  else
    warn "docker compose down reported an error (see above)"
  fi
else
  warn "docker not found: no containers to stop"
fi

# ---- purge (only inside this folder)
inside_stack() { # PATH -> true when PATH resolves to a folder strictly inside the stack folder
  local real
  real="$(cd "$1" 2>/dev/null && pwd -P)" || return 1
  [[ "$real" == "$STACK"/* ]]
}

remove_dir() { # PATH
  local d="$1"
  if [[ ! -e "$d" ]]; then return 0; fi
  if ! inside_stack "$d"; then
    say "kept (outside this folder): $d"
    return 0
  fi
  if ! rm -rf -- "$d" 2>/dev/null; then
    # Some apps write their settings as root inside the container.
    sudo rm -rf -- "$d" || { warn "could not delete $d"; return 0; }
  fi
  say "deleted: $d"
}

if [[ $PURGE -eq 1 ]]; then
  for key_default in STATE_DIR:./state LOG_DIR:./logs BACKUP_DIR:./backups; do
    key="${key_default%%:*}"
    d="$(env_value "$key")"
    d="${d:-${key_default#*:}}"
    if [[ "$d" != /* ]]; then d="$STACK/${d#./}"; fi
    remove_dir "$d"
  done
  remove_dir "$STACK/config"
  rm -f -- "$STACK/scripts/arr/arr.env"
  for f in "$STACK"/.env "$STACK"/.env.bak-*; do
    if [[ -f "$f" ]]; then rm -f -- "$f" && say "deleted: $f"; fi
  done
fi

say "Done. To install again: ./install.sh"
