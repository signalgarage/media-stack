#!/usr/bin/env bash
# media-stack installer.
#
# Checks this machine, asks a few questions (or reads them from --answers), writes .env,
# creates the folders, starts the containers, wires the apps together (configure.py) and
# installs the guard cron jobs. Safe to run again at any time: it keeps your answers and
# secrets, only fills in what is missing, and never moves, renames or deletes anything in
# your media folder.

set -euo pipefail

STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
ENV_FILE="$STACK/.env"
EXAMPLE="$STACK/.env.example"
NO_VPN_FILES="docker-compose.yml:compose/test-no-vpn.override.yml"
MIN_COMPOSE="2.24"        # !reset in the test override and depends_on restart need it
WAIT_SECS=300             # how long the apps get for their first start
MIN_FREE_GB=1             # below this the install stops; below WARN_FREE_GB it only warns
WARN_FREE_GB=5

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

  --answers FILE     read answers from FILE (KEY=VALUE lines, like .env) instead of asking
  --yes              never ask: use the answers file, environment variables and defaults,
                     and stop with a message when a required answer is missing
  --profiles LIST    optional services, comma separated: plex, monitoring, subtitles
  --no-vpn           TEST INSTALLS ONLY: qBittorrent without the VPN and without a kill switch
  --install-docker   when Docker is missing, offer to install it with Docker's official script
  --skip-cron        do not install the guard cron jobs
  -h, --help         show this help

Answers can also come from environment variables with the same names as in .env.example,
for example: MEDIA_ROOT=/srv/media ./install.sh
Running it again is safe: it keeps your settings and only adds what is missing.
EOF
}

# ---------------------------------------------------------------- output

say()  { printf '%s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
warn() { printf 'WARNING: %s\n' "$*" >&2; }
die()  { printf '\nERROR: %s\n' "$*" >&2; exit 1; }
step() { printf '\n== %s\n' "$*"; }

no_vpn_banner() {
  cat >&2 <<'EOF'

######################################################################
#  TEST MODE (--no-vpn): qBittorrent runs WITHOUT a VPN and WITHOUT  #
#  a kill switch. Its traffic goes out over your normal connection.  #
#  Use this only on a throwaway test machine. Run ./install.sh again #
#  without --no-vpn to put the VPN back.                             #
######################################################################

EOF
}

# ---------------------------------------------------------------- small helpers

trim_to() { # VAR STRING: VAR = STRING without surrounding whitespace
  local __t="$2"
  __t="${__t#"${__t%%[![:space:]]*}"}"
  __t="${__t%"${__t##*[![:space:]]}"}"
  printf -v "$1" '%s' "$__t"
}

unquote_to() { # VAR STRING: VAR = STRING without one pair of matching outer quotes
  local __u="$2"
  if [[ ${#__u} -ge 2 ]]; then
    if [[ "${__u:0:1}" == '"' && "${__u: -1}" == '"' ]] || [[ "${__u:0:1}" == "'" && "${__u: -1}" == "'" ]]; then
      __u="${__u:1:${#__u}-2}"
    fi
  fi
  printf -v "$1" '%s' "$__u"
}

# Values are kept in plain variables with a prefix (no associative arrays):
#   E_<KEY> the existing .env, F_<KEY> the answers file, V_<KEY> the final answers.
has() { local n="$1$2"; [[ -n "${!n+x}" ]]; }
get() { local n="$1$2"; printf '%s' "${!n-}"; }
env_has() { [[ -n "${!1+x}" ]]; }

V_KEYS=""
setv() { # KEY VALUE
  printf -v "V_$1" '%s' "$2"
  case " $V_KEYS " in *" $1 "*) ;; *) V_KEYS="$V_KEYS $1" ;; esac
}

kv_load() { # FILE PREFIX: reads KEY=VALUE lines into <PREFIX><KEY>; never sources the file
  local file="$1" prefix="$2" line key val keys="" re='^[A-Za-z_][A-Za-z0-9_]*$'
  while IFS= read -r line || [[ -n "$line" ]]; do
    line="${line%$'\r'}"
    trim_to line "$line"
    case "$line" in ''|'#'*) continue ;; esac
    line="${line#export }"
    if [[ "$line" != *=* ]]; then continue; fi
    trim_to key "${line%%=*}"
    if [[ ! "$key" =~ $re ]]; then continue; fi
    trim_to val "${line#*=}"
    unquote_to val "$val"
    printf -v "$prefix$key" '%s' "$val"
    case " $keys " in *" $key "*) ;; *) keys="$keys $key" ;; esac
  done < "$file"
  printf -v "${prefix}KEYS" '%s' "${keys# }"
}

env_quote() { # VALUE -> as written into .env (single quotes when it is not a plain word)
  local re='^[A-Za-z0-9_./:,@%+=*-]*$'
  if [[ "$1" =~ $re ]]; then printf '%s' "$1"; else printf "'%s'" "$1"; fi
}

env_file_value() { # KEY -> current value in .env
  local line="" v=""
  if [[ -r "$ENV_FILE" ]]; then
    line="$(grep -E "^[[:space:]]*$1[[:space:]]*=" "$ENV_FILE" | tail -n 1 || true)"
  fi
  trim_to v "${line#*=}"
  unquote_to v "$v"
  printf '%s' "$v"
}

set_env_key() { # FILE KEY VALUE: replace every KEY= line (or append one), keeping mode 600
  local file="$1" key="$2" val tmp line found=0
  val="$(env_quote "$3")"
  tmp="$(mktemp "$file.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" == "$key="* ]]; then
      printf '%s=%s\n' "$key" "$val"
      found=1
    else
      printf '%s\n' "$line"
    fi
  done < "$file" > "$tmp"
  if [[ $found -eq 0 ]]; then printf '%s=%s\n' "$key" "$val" >> "$tmp"; fi
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

remove_env_key() { # FILE KEY
  local file="$1" key="$2" tmp line
  tmp="$(mktemp "$file.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" != "$key="* ]]; then printf '%s\n' "$line"; fi
  done < "$file" > "$tmp"
  chmod 600 "$tmp"
  mv -f "$tmp" "$file"
}

as_root() { if [[ $EUID -eq 0 ]]; then "$@"; else sudo "$@"; fi; }

read_file() { cat "$1" 2>/dev/null || as_root cat "$1" 2>/dev/null || true; }

version_ge() { # A B -> true when version A >= B (major.minor)
  local a1 a2 b1 b2 rest
  IFS=. read -r a1 a2 rest <<< "$1"
  IFS=. read -r b1 b2 rest <<< "$2"
  a1="${a1//[!0-9]/}"; a2="${a2//[!0-9]/}"; b1="${b1//[!0-9]/}"; b2="${b2//[!0-9]/}"
  a1="${a1:-0}"; a2="${a2:-0}"; b1="${b1:-0}"; b2="${b2:-0}"
  (( 10#$a1 > 10#$b1 || ( 10#$a1 == 10#$b1 && 10#$a2 >= 10#$b2 ) ))
}

in_list() { # WORD LIST... -> true when WORD is one of LIST
  local w="$1" x
  shift
  for x in "$@"; do if [[ "$x" == "$w" ]]; then return 0; fi; done
  return 1
}

profile_on() { [[ ",$(get V_ COMPOSE_PROFILES)," == *",$1,"* ]]; }

# ---------------------------------------------------------------- questions

CAN_PROMPT=0
VERR=""

ask_into() { # VAR QUESTION DEFAULT SECRET(1/0)
  local __reply="" __q="$2"
  if [[ "$4" == 1 ]]; then
    if [[ -n "$3" ]]; then __q="$__q (Enter keeps the current value)"; fi
    IFS= read -rs -p "$__q: " __reply </dev/tty || true
    printf '\n' >/dev/tty
  else
    if [[ -n "$3" ]]; then __q="$__q [$3]"; fi
    IFS= read -r -p "$__q: " __reply </dev/tty || true
  fi
  trim_to __reply "$__reply"
  if [[ -z "$__reply" ]]; then __reply="$3"; fi
  printf -v "$1" '%s' "$__reply"
}

confirm() { # QUESTION -> true for yes (--yes answers yes)
  local r=""
  if [[ $ASSUME_YES -eq 1 ]]; then return 0; fi
  if [[ $CAN_PROMPT -eq 0 ]]; then return 1; fi
  IFS= read -r -p "$1 [y/N] " r </dev/tty || true
  [[ "$r" == [yY] || "$r" == [yY][eE][sS] ]]
}

normalize_to() { # VAR KEY VALUE: tidy an answer before it is checked
  local __n
  trim_to __n "$3"
  case "$2" in
    ALLOWED_TELEGRAM_USER_IDS|COMPOSE_PROFILES|FIREWALL_OUTBOUND_SUBNETS|MOVIE_LIBRARIES|WIREGUARD_ADDRESSES)
      __n="${__n//[[:space:]]/}" ;;
    VPN_SERVICE_PROVIDER|VPN_TYPE|VPN_PORT_FORWARDING)
      __n="$(printf '%s' "$__n" | tr '[:upper:]' '[:lower:]')" ;;
    MEDIA_ROOT)
      while [[ "$__n" == */ && "$__n" != / ]]; do __n="${__n%/}"; done ;;
    DOWNLOADS_DIR|MOVIES_DIR|TV_DIR)
      while [[ "$__n" == /* ]]; do __n="${__n#/}"; done
      while [[ "$__n" == */ ]]; do __n="${__n%/}"; done ;;
  esac
  printf -v "$1" '%s' "$__n"
}

check_subdir() { # a folder below MEDIA_ROOT, given relative
  local re='^[A-Za-z0-9_][A-Za-z0-9._ -]*(/[A-Za-z0-9_][A-Za-z0-9._ -]*)*$'
  if [[ ! "$1" =~ $re ]]; then
    VERR="use a folder name below the media folder (letters, digits, . _ - and spaces), for example movies"
  elif [[ "/$1/" == */../* || "/$1/" == */./* ]]; then
    VERR="'.' and '..' are not allowed"
  fi
}

check_media_root() {
  local v="$1"
  if [[ "$v" != /* ]]; then VERR="must be an absolute path, for example /srv/media"; return 0; fi
  case "$v" in
    *:*|*'$'*|*\\*|*'"'*|*'`'*|*,*) VERR="must not contain : , \$ \\ \" or \`"; return 0 ;;
    /|/bin|/boot|/dev|/etc|/home|/lib|/lib64|/opt|/proc|/root|/run|/sbin|/srv|/sys|/tmp|/usr|/var|/var/lib|/mnt|/media)
      VERR="pick a dedicated folder below $v, not $v itself"; return 0 ;;
    /proc/*|/sys/*|/dev/*|/etc/*|/usr/*|/boot/*|/var/lib/docker*)
      VERR="$v is a system location"; return 0 ;;
  esac
  if [[ "$v" == "$STACK" || "$STACK" == "$v"/* ]]; then
    VERR="the media folder must not contain this stack folder"
  fi
}

check_libraries() {
  local item code folder re='^[a-z]{2}$' seen=","
  if [[ -z "$1" ]]; then return 0; fi
  IFS=, read -r -a items <<< "$1"
  for item in "${items[@]}"; do
    code="${item%%=*}"; folder="${item#*=}"
    if [[ "$item" != *=* || ! "$code" =~ $re ]]; then
      VERR="use <language code>=<folder>, for example fr=movies-fr,es=movies-es"; return 0
    fi
    check_subdir "$folder"
    if [[ -n "$VERR" ]]; then VERR="$folder: $VERR"; return 0; fi
    if [[ "$seen" == *",$code,"* ]]; then VERR="language $code is listed twice"; return 0; fi
    seen="$seen$code,"
  done
}

check_cidrs() {
  local item re='^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})/([0-9]{1,2})$' i
  IFS=, read -r -a items <<< "$1"
  for item in "${items[@]}"; do
    if [[ ! "$item" =~ $re ]]; then VERR="use networks such as 192.0.2.0/24, comma separated"; return 0; fi
    for i in 1 2 3 4; do
      if (( 10#${BASH_REMATCH[$i]} > 255 )); then VERR="$item is not a valid network"; return 0; fi
    done
    if (( 10#${BASH_REMATCH[5]} > 32 )); then VERR="$item: the prefix must be 0-32"; return 0; fi
  done
}

check_tz() {
  local re='^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$'
  if [[ ! "$1" =~ $re ]]; then VERR="use a zone name such as Etc/UTC"; return 0; fi
  if [[ -d /usr/share/zoneinfo && ! -f "/usr/share/zoneinfo/$1" ]]; then
    VERR="unknown time zone $1 (see timedatectl list-timezones)"
  fi
}

check_profiles() {
  local p
  if [[ -z "$1" ]]; then return 0; fi
  IFS=, read -r -a items <<< "$1"
  for p in "${items[@]}"; do
    case "$p" in plex|monitoring|subtitles) ;; *) VERR="unknown profile '$p' (use plex, monitoring, subtitles)"; return 0 ;; esac
  done
}

validate() { # KEY VALUE -> true when acceptable; VERR says why not
  local k="$1" v="$2" re
  local items=()
  VERR=""
  case "$v" in *$'\n'*|*"'"*) VERR="quotes and line breaks cannot be stored in .env"; return 1 ;; esac
  case "$k" in
    MEDIA_ROOT) check_media_root "$v" ;;
    DOWNLOADS_DIR|MOVIES_DIR|TV_DIR) check_subdir "$v" ;;
    MOVIE_LIBRARIES) check_libraries "$v" ;;
    COMPOSE_PROFILES) check_profiles "$v" ;;
    VPN_SERVICE_PROVIDER)
      re='^[a-z0-9][a-z0-9 ._-]*$'
      [[ "$v" =~ $re ]] || VERR="use the provider name as gluetun spells it, for example mullvad" ;;
    VPN_TYPE) [[ "$v" == wireguard || "$v" == openvpn ]] || VERR="wireguard or openvpn" ;;
    WIREGUARD_PRIVATE_KEY|WIREGUARD_PRESHARED_KEY)
      re='^[A-Za-z0-9+/]{43}=$'
      [[ -z "$v" && "$k" == WIREGUARD_PRESHARED_KEY ]] || [[ "$v" =~ $re ]] \
        || VERR="a WireGuard key is 44 characters ending in =" ;;
    WIREGUARD_ADDRESSES)
      re='^[0-9A-Fa-f:.]+/[0-9]{1,3}(,[0-9A-Fa-f:.]+/[0-9]{1,3})*$'
      [[ -z "$v" || "$v" =~ $re ]] || VERR="an address with its prefix, for example 192.0.2.10/32" ;;
    OPENVPN_USER|OPENVPN_PASSWORD) [[ -n "$v" ]] || VERR="required for OpenVPN" ;;
    SERVER_COUNTRIES)
      re='^[A-Za-z][A-Za-z .-]*(,[A-Za-z][A-Za-z .-]*)*$'
      [[ -z "$v" || "$v" =~ $re ]] || VERR="country names separated by commas" ;;
    VPN_PORT_FORWARDING) [[ "$v" == on || "$v" == off ]] || VERR="on or off" ;;
    FIREWALL_OUTBOUND_SUBNETS) if [[ -n "$v" ]]; then check_cidrs "$v"; fi ;;
    TELEGRAM_BOT_TOKEN)
      re='^[0-9]{5,}:[A-Za-z0-9_-]{30,}$'
      [[ "$v" =~ $re ]] || VERR="a bot token looks like 123456789:AA... (Telegram's @BotFather creates it)" ;;
    ALLOWED_TELEGRAM_USER_IDS)
      re='^[0-9]{3,}(,[0-9]{3,})*$'
      [[ "$v" =~ $re ]] || VERR="Telegram user ids are numbers; separate several with commas" ;;
    PLEX_URL)
      re='^https?://[^/[:space:]]+/?$'
      [[ -z "$v" || "$v" =~ $re ]] || VERR="like http://192.0.2.10:32400" ;;
    PLEX_TOKEN)
      re='^[A-Za-z0-9_-]{8,}$'
      [[ -z "$v" || "$v" =~ $re ]] || VERR="a Plex token is a string of letters and digits" ;;
    PLEX_CLAIM)
      re='^claim-[A-Za-z0-9_-]+$'
      [[ -z "$v" || "$v" =~ $re ]] || VERR="a claim code starts with claim-" ;;
    TZ) check_tz "$v" ;;
    PUID|PGID)
      re='^[0-9]+$'
      [[ "$v" =~ $re ]] || VERR="a number (see: id)" ;;
    WEBHOOK_SECRET)
      re='^[A-Za-z0-9_-]{16,}$'
      [[ "$v" =~ $re ]] || VERR="at least 16 letters or digits" ;;
    QBIT_PASSWORD)
      if [[ ${#v} -lt 6 ]]; then
        VERR="at least 6 characters"
      elif [[ "$v" == *[[:space:]\"\\]* ]]; then
        VERR="no spaces, quotes or backslashes"
      fi ;;
  esac
  [[ -z "$VERR" ]]
}

resolve() { # KEY QUESTION DEFAULT REQUIRED(1/0) SECRET(1/0)
  local key="$1" question="$2" def="$3" required="$4" secret="$5" val="" src="" tries=0 bad_existing=""
  if [[ "$ENV_MODE" == keep ]] && has E_ "$key"; then
    val="$(get E_ "$key")"
    if [[ -n "$val" || "$required" == 0 ]]; then
      if validate "$key" "$val"; then setv "$key" "$val"; return 0; fi
      bad_existing="$VERR"
      warn "$key in .env is not valid ($VERR)"
    fi
  fi
  if has F_ "$key"; then
    val="$(get F_ "$key")"; src="answers file"
  elif [[ "$key" != COMPOSE_PROFILES ]] && env_has "$key"; then
    val="${!key}"; src="environment"
  fi
  if [[ -n "$src" ]]; then
    normalize_to val "$key" "$val"
    if validate "$key" "$val" && { [[ -n "$val" ]] || [[ "$required" == 0 ]]; }; then
      setv "$key" "$val"
      if [[ "$secret" == 0 ]]; then info "$key = ${val:-(empty)} (from the $src)"; fi
      return 0
    fi
    if [[ $CAN_PROMPT -eq 0 ]]; then die "$key from the $src is not usable: ${VERR:-an answer is required}"; fi
    warn "$key from the $src is not usable: ${VERR:-an answer is required}"
  fi
  if [[ "$ENV_MODE" == rewrite ]] && has E_ "$key"; then def="$(get E_ "$key")"; fi
  if [[ $CAN_PROMPT -eq 0 ]]; then
    if [[ -n "$bad_existing" ]]; then
      die "$key in .env is not valid ($bad_existing): correct it in .env or pass it in the answers file"
    fi
    if [[ -z "$def" && "$required" == 1 ]]; then
      die "no answer for $key: add $key=... to the answers file (--answers) or set it in the environment"
    fi
    validate "$key" "$def" || die "the default for $key is not usable: $VERR"
    setv "$key" "$def"
    return 0
  fi
  while :; do
    ask_into val "$question" "$def" "$secret"
    normalize_to val "$key" "$val"
    if [[ -z "$val" && "$required" == 1 ]]; then
      tries=$((tries + 1))
      if [[ $tries -ge 5 ]]; then die "no answer for $key"; fi
      warn "an answer is required"
      continue
    fi
    if validate "$key" "$val"; then setv "$key" "$val"; return 0; fi
    warn "$VERR"
  done
}

detect_tz() {
  local tz=""
  if [[ -r /etc/timezone ]]; then tz="$(head -n 1 /etc/timezone | tr -d '[:space:]' || true)"; fi
  if [[ -z "$tz" ]] && command -v timedatectl >/dev/null 2>&1; then
    tz="$(timedatectl show -p Timezone --value 2>/dev/null || true)"
  fi
  if [[ -z "$tz" && -L /etc/localtime ]]; then
    tz="$(readlink /etc/localtime | sed -n 's#.*/zoneinfo/##p' || true)"
  fi
  VERR=""
  check_tz "${tz:-Etc/UTC}"
  if [[ -n "$VERR" ]]; then tz=""; fi
  printf '%s' "${tz:-Etc/UTC}"
}

default_dev() { ip -4 route show default 2>/dev/null | awk '{for (i = 1; i < NF; i++) if ($i == "dev") {print $(i + 1); exit}}' || true; }

detect_lan_cidr() {
  local dev addr
  command -v ip >/dev/null 2>&1 || return 0
  dev="$(default_dev)"
  if [[ -z "$dev" ]]; then return 0; fi
  addr="$(ip -4 -o addr show dev "$dev" 2>/dev/null | awk '{print $4; exit}' || true)"
  if [[ -z "$addr" ]]; then return 0; fi
  python3 -c 'import ipaddress, sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$addr" 2>/dev/null || true
}

detect_host_ip() {
  local dev addr=""
  if command -v ip >/dev/null 2>&1; then
    dev="$(default_dev)"
    if [[ -n "$dev" ]]; then
      addr="$(ip -4 -o addr show dev "$dev" 2>/dev/null | awk '{print $4; exit}' || true)"
    fi
  fi
  addr="${addr%%/*}"
  printf '%s' "${addr:-localhost}"
}

# ---------------------------------------------------------------- 1. checks

DOCKER=(docker)

install_docker() {
  say "Docker is not installed."
  if ! confirm "Download Docker's official install script from https://get.docker.com and run it with sudo now?"; then
    die "Docker is needed. Install Docker Engine (https://docs.docker.com/engine/install/) and run ./install.sh again."
  fi
  local tmp
  tmp="$(mktemp)"
  curl -fsSL https://get.docker.com -o "$tmp" || die "could not download the Docker install script"
  as_root sh "$tmp" || die "the Docker install script failed (see above)"
  rm -f "$tmp"
  as_root usermod -aG docker "$(id -un)" 2>/dev/null || true
  info "Docker installed. Log out and in again later so your user can run docker without sudo."
}

check_machine() {
  step "Checking this machine"
  [[ "$(uname -s)" == Linux ]] || die "media-stack runs on Linux (with Docker); this is $(uname -s)."
  (( BASH_VERSINFO[0] >= 4 )) || die "bash 4 or newer is needed (this is $BASH_VERSION)."
  command -v curl >/dev/null 2>&1 || die "curl is missing; install it with your package manager."
  command -v python3 >/dev/null 2>&1 || die "python3 is missing; install Python 3.9 or newer."
  python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
    || die "Python 3.9 or newer is needed ($(python3 --version 2>&1))."
  [[ -r "$EXAMPLE" ]] || die ".env.example is missing from $STACK; is this a complete copy of media-stack?"
  if [[ $EUID -eq 0 ]]; then
    warn "running as root: the guard jobs will run as root. Running as your normal user is better."
  fi

  if ! command -v docker >/dev/null 2>&1; then
    if [[ $INSTALL_DOCKER -eq 1 ]]; then
      install_docker
    else
      die "Docker is not installed. Install Docker Engine (https://docs.docker.com/engine/install/) or run ./install.sh --install-docker"
    fi
  fi
  if docker info >/dev/null 2>&1; then
    DOCKER=(docker)
  elif [[ $EUID -ne 0 ]] && { sudo -n docker info >/dev/null 2>&1 || { [[ $CAN_PROMPT -eq 1 ]] && sudo docker info >/dev/null 2>&1; }; }; then
    DOCKER=(sudo docker)
    warn "your user cannot use Docker directly, so sudo is used for docker commands."
    info "To fix it: sudo usermod -aG docker $(id -un)   (then log out and in again)"
  else
    die "Docker is installed but not usable. Is it running? Try: sudo systemctl start docker"
  fi
  local ver
  ver="$("${DOCKER[@]}" compose version --short 2>/dev/null || true)"
  [[ -n "$ver" ]] || die "Docker Compose v2 (the 'docker compose' plugin) is missing: https://docs.docker.com/compose/install/linux/"
  ver="${ver#v}"
  version_ge "$ver" "$MIN_COMPOSE" || die "Docker Compose $ver is too old; $MIN_COMPOSE or newer is needed."
  info "docker: ok (compose $ver${DOCKER[0]:+, via ${DOCKER[*]}})"

  if [[ $SKIP_CRON -eq 0 ]] && ! command -v crontab >/dev/null 2>&1; then
    SKIP_CRON=1
    warn "no crontab command: the guard jobs will not be installed (install cron, then run: python3 configure.py --only cron)"
  fi
  if [[ $NO_VPN -eq 0 && ! -c /dev/net/tun ]]; then
    die "/dev/net/tun is missing; the VPN container needs it. Load it with: sudo modprobe tun (and add 'tun' to /etc/modules-load.d/ so it stays)"
  fi
  local free_kb
  free_kb="$(df -Pk "$STACK" | awk 'NR == 2 {print $4}' || true)"
  if [[ -n "$free_kb" ]]; then
    if (( free_kb < MIN_FREE_GB * 1024 * 1024 )); then
      die "only $((free_kb / 1024)) MB free where $STACK lives; at least ${MIN_FREE_GB} GB is needed."
    elif (( free_kb < WARN_FREE_GB * 1024 * 1024 )); then
      warn "only $((free_kb / 1024 / 1024)) GB free where $STACK lives; the app databases grow over time."
    fi
  fi
}

# ---------------------------------------------------------------- 2. answers

ENV_MODE=new

choose_env_mode() {
  E_KEYS=""
  if [[ ! -e "$ENV_FILE" ]]; then ENV_MODE=new; return 0; fi
  [[ -r "$ENV_FILE" ]] || die ".env exists but you cannot read it (fix with: sudo chown $(id -u) $ENV_FILE)"
  kv_load "$ENV_FILE" E_
  ENV_MODE=keep
  if [[ $CAN_PROMPT -eq 1 ]]; then
    local r=""
    say ".env already exists."
    IFS= read -r -p "Keep it and only fill in what is missing (k), or answer every question again (r)? [K/r] " r </dev/tty || true
    case "$r" in r|R|rewrite) ENV_MODE=rewrite ;; esac
  fi
  info "existing .env: $ENV_MODE"
}

gen_hex() { python3 -c 'import secrets; print(secrets.token_hex(24))'; }
gen_password() {
  python3 -c 'import secrets, string; a = string.ascii_letters + string.digits; print("".join(secrets.choice(a) for _ in range(20)))'
}

generated() { # KEY GENERATOR: keep an existing value, else the answers file, else a new one
  local key="$1" val
  val="$(get E_ "$key")"
  if [[ -n "$val" ]] && validate "$key" "$val"; then setv "$key" "$val"; return 0; fi
  if has F_ "$key"; then
    val="$(get F_ "$key")"
    validate "$key" "$val" || die "$key from the answers file is not usable: $VERR"
    setv "$key" "$val"
    return 0
  fi
  setv "$key" "$("$2")"
  info "$key generated"
}

check_layout() { # the three main folders must differ and must not sit inside each other
  local a b
  for a in DOWNLOADS_DIR MOVIES_DIR TV_DIR; do
    for b in DOWNLOADS_DIR MOVIES_DIR TV_DIR; do
      if [[ "$a" != "$b" ]]; then
        if [[ "$(get V_ "$a")" == "$(get V_ "$b")" || "$(get V_ "$a")/" == "$(get V_ "$b")/"* ]]; then
          die "$a and $b must be separate folders (now $(get V_ "$a") and $(get V_ "$b"))"
        fi
      fi
    done
  done
}

collect_answers() {
  step "Settings"
  if [[ $PROFILES_GIVEN -eq 1 ]]; then
    normalize_to PROFILES_ARG COMPOSE_PROFILES "$PROFILES_ARG"
    validate COMPOSE_PROFILES "$PROFILES_ARG" || die "--profiles: $VERR"
    setv COMPOSE_PROFILES "$PROFILES_ARG"
  else
    resolve COMPOSE_PROFILES "Optional services: plex, monitoring, subtitles (comma separated, Enter = none)" "" 0 0
  fi

  resolve MEDIA_ROOT "Media folder (downloads and libraries; the apps see it as /data)" /srv/media 1 0
  resolve DOWNLOADS_DIR "Downloads folder inside it" downloads 1 0
  resolve MOVIES_DIR "Movies folder inside it" movies 1 0
  resolve TV_DIR "TV folder inside it" tv 1 0
  check_layout
  if [[ $CAN_PROMPT -eq 1 ]] && ! has V_ MOVIE_LIBRARIES; then
    info "Optional extra movie libraries by original language, as <language code>=<folder>,"
    info "for example fr=movies-fr,es=movies-es (folders inside the media folder)."
  fi
  resolve MOVIE_LIBRARIES "Extra movie libraries (Enter = none)" "" 0 0

  if [[ $NO_VPN -eq 1 ]]; then
    setv VPN_MODE off
    setv COMPOSE_FILE "$NO_VPN_FILES"
  else
    if [[ "$(get E_ VPN_MODE)" == off ]]; then
      info "this .env was a test install without VPN: switching the VPN back on"
      ENV_DROP_KEYS="COMPOSE_FILE"
      # The VPN answers in a test install are only the template's examples: ask for real ones.
      unset E_VPN_SERVICE_PROVIDER E_VPN_TYPE E_WIREGUARD_PRIVATE_KEY E_WIREGUARD_ADDRESSES \
            E_WIREGUARD_PRESHARED_KEY E_OPENVPN_USER E_OPENVPN_PASSWORD E_SERVER_COUNTRIES
    fi
    setv VPN_MODE on
    if [[ $CAN_PROMPT -eq 1 ]]; then
      info "VPN: provider names and the settings each one needs:"
      info "https://github.com/qdm12/gluetun-wiki/tree/main/setup/providers"
    fi
    resolve VPN_SERVICE_PROVIDER "VPN provider (as gluetun names it)" "" 1 0
    resolve VPN_TYPE "VPN type (wireguard or openvpn)" wireguard 1 0
    if [[ "$(get V_ VPN_TYPE)" == wireguard ]]; then
      resolve WIREGUARD_PRIVATE_KEY "WireGuard private key" "" 1 1
      resolve WIREGUARD_ADDRESSES "WireGuard address (Enter if your provider's page says it is not needed)" "" 0 0
      resolve WIREGUARD_PRESHARED_KEY "WireGuard preshared key (usually none: Enter)" "" 0 1
    else
      resolve OPENVPN_USER "OpenVPN user name" "" 1 1
      resolve OPENVPN_PASSWORD "OpenVPN password" "" 1 1
    fi
    resolve SERVER_COUNTRIES "VPN server countries (comma separated, Enter = any)" "" 0 0
    resolve VPN_PORT_FORWARDING "Port forwarding: on only if your provider supports it" off 1 0
    local lan
    lan="$(detect_lan_cidr)"
    resolve FIREWALL_OUTBOUND_SUBNETS "Your LAN, so the qBittorrent web UI stays reachable${lan:+ (detected $lan)}" "$lan" 0 0
    if [[ -z "$(get V_ FIREWALL_OUTBOUND_SUBNETS)" ]]; then
      warn "no LAN set: the qBittorrent web UI will only answer on this machine."
    fi
  fi

  resolve TELEGRAM_BOT_TOKEN "Telegram bot token (create a bot with Telegram's @BotFather)" "" 1 1
  resolve ALLOWED_TELEGRAM_USER_IDS "Telegram user id(s) allowed to use the bot (numbers, comma separated)" "" 1 0

  if profile_on plex; then
    setv PLEX_URL "http://plex:32400"
    info "PLEX_URL = http://plex:32400 (the Plex container of this stack)"
    if has F_ PLEX_CLAIM || env_has PLEX_CLAIM; then resolve PLEX_CLAIM "Plex claim code" "" 0 0; fi
  else
    if [[ "$(get E_ PLEX_URL)" == "http://plex:32400" ]]; then
      unset E_PLEX_URL    # the stack's own Plex is no longer enabled: ask again
    fi
    resolve PLEX_URL "Existing Plex server URL, for example http://192.0.2.10:32400 (Enter = no Plex)" "" 0 0
    if [[ -n "$(get V_ PLEX_URL)" ]]; then
      resolve PLEX_TOKEN "Plex token (optional: lets the apps refresh Plex and set up Jellyseerr)" "" 0 1
    fi
  fi

  resolve TZ "Time zone" "$(detect_tz)" 1 0
  resolve PUID "User id the apps run as" "${SUDO_UID:-$(id -u)}" 1 0
  resolve PGID "Group id the apps run as" "${SUDO_GID:-$(id -g)}" 1 0

  generated WEBHOOK_SECRET gen_hex
  generated QBIT_PASSWORD gen_password
}

# ---------------------------------------------------------------- 3. .env

ENV_DROP_KEYS=""

write_env() {
  step "Writing .env"
  umask 077
  local tmp line key key_re='^([A-Za-z_][A-Za-z0-9_]*)=' template_keys=" " extra=""
  if [[ "$ENV_MODE" == keep ]]; then
    for key in $V_KEYS; do
      if ! has E_ "$key" || [[ "$(get E_ "$key")" != "$(get V_ "$key")" ]]; then
        set_env_key "$ENV_FILE" "$key" "$(get V_ "$key")"
      fi
    done
    while IFS= read -r line || [[ -n "$line" ]]; do
      if [[ "$line" =~ $key_re ]]; then
        key="${BASH_REMATCH[1]}"
        if ! has E_ "$key" && ! has V_ "$key"; then printf '%s\n' "$line" >> "$ENV_FILE"; fi
      fi
    done < "$EXAMPLE"
    for key in $ENV_DROP_KEYS; do remove_env_key "$ENV_FILE" "$key"; done
    chmod 600 "$ENV_FILE"
    info "kept your .env and filled in what was missing"
    umask 022
    return 0
  fi
  if [[ "$ENV_MODE" == rewrite ]]; then
    cp -p "$ENV_FILE" "$ENV_FILE.bak-$(date +%Y%m%d-%H%M%S)"
    info "previous .env saved as .env.bak-<date>"
  fi
  tmp="$(mktemp "$STACK/.env.XXXXXX")"
  while IFS= read -r line || [[ -n "$line" ]]; do
    if [[ "$line" =~ $key_re ]]; then
      key="${BASH_REMATCH[1]}"
      template_keys="$template_keys$key "
      if has V_ "$key"; then
        printf '%s=%s\n' "$key" "$(env_quote "$(get V_ "$key")")"
      elif has E_ "$key"; then
        printf '%s=%s\n' "$key" "$(env_quote "$(get E_ "$key")")"
      else
        printf '%s\n' "$line"
      fi
    else
      printf '%s\n' "$line"
    fi
  done < "$EXAMPLE" > "$tmp"
  for key in $V_KEYS $E_KEYS; do
    if [[ "$template_keys" == *" $key "* || " $extra " == *" $key "* || " $ENV_DROP_KEYS " == *" $key "* ]]; then
      continue
    fi
    extra="$extra $key"
  done
  if [[ -n "$extra" ]]; then
    {
      printf '\n# ---- Other settings -------------------------------------------------------\n'
      if [[ " $extra " == *" COMPOSE_FILE "* ]]; then
        printf '# COMPOSE_FILE with the test override = TEST MODE without VPN (install.sh --no-vpn).\n'
      fi
      for key in $extra; do
        if has V_ "$key"; then
          printf '%s=%s\n' "$key" "$(env_quote "$(get V_ "$key")")"
        else
          printf '%s=%s\n' "$key" "$(env_quote "$(get E_ "$key")")"
        fi
      done
    } >> "$tmp"
  fi
  chmod 600 "$tmp"
  mv -f "$tmp" "$ENV_FILE"
  umask 022
  info "wrote $ENV_FILE (readable by you only)"
}

# ---------------------------------------------------------------- 4. folders

CREATED=()

make_dir() { # PATH: create it and any missing parents, remembering what was created here
  local path="$1" p missing=()
  p="$path"
  while [[ ! -e "$p" ]]; do
    missing=("$p" ${missing[@]+"${missing[@]}"})
    p="$(dirname "$p")"
  done
  [[ -d "$p" ]] || die "$p exists but is not a folder"
  for p in ${missing[@]+"${missing[@]}"}; do
    if ! mkdir "$p" 2>/dev/null; then
      as_root mkdir "$p" || die "cannot create $p"
    fi
    CREATED+=("$p")
  done
}

own_dir() { # PATH: give it to PUID:PGID when this script created it or it is empty
  local path="$1" owner want
  want="$(get V_ PUID):$(get V_ PGID)"
  owner="$(stat -c '%u:%g' "$path" 2>/dev/null || true)"
  if [[ "$owner" == "$want" ]]; then return 0; fi
  if in_list "$path" ${CREATED[@]+"${CREATED[@]}"} || [[ -z "$(ls -A "$path" 2>/dev/null || true)" ]]; then
    chown -R "$want" "$path" 2>/dev/null || as_root chown -R "$want" "$path" || warn "could not change the owner of $path"
  fi
}

create_folders() {
  step "Folders"
  local root dl mv tv lib folder libs=() d all=()
  root="$(get V_ MEDIA_ROOT)"
  dl="$root/$(get V_ DOWNLOADS_DIR)"
  mv="$root/$(get V_ MOVIES_DIR)"
  tv="$root/$(get V_ TV_DIR)"
  if [[ -n "$(get V_ MOVIE_LIBRARIES)" ]]; then
    IFS=, read -r -a libs <<< "$(get V_ MOVIE_LIBRARIES)"
  fi
  all=("$root" "$dl" "$dl/movies" "$dl/tv" "$mv" "$tv")
  for lib in ${libs[@]+"${libs[@]}"}; do
    folder="${lib#*=}"
    all+=("$root/$folder")
  done
  for d in "${all[@]}"; do make_dir "$d"; done
  # Only folders this script made (or empty ones) change owner; existing media is never touched.
  for d in ${CREATED[@]+"${CREATED[@]}"} "${all[@]}"; do own_dir "$d"; done
  info "media folder: $root"

  local apps=(gluetun qbittorrent radarr sonarr prowlarr jellyseerr)
  if profile_on plex; then apps+=(plex); fi
  if profile_on monitoring; then apps+=(uptime-kuma tautulli); fi
  if profile_on subtitles; then apps+=(bazarr); fi
  local app state logs backups
  for app in "${apps[@]}"; do mkdir -p "$STACK/config/$app"; done
  state="$(env_file_value STATE_DIR)"; state="${state:-./state}"
  logs="$(env_file_value LOG_DIR)"; logs="${logs:-./logs}"
  backups="$(env_file_value BACKUP_DIR)"; backups="${backups:-./backups}"
  for d in "$state" "$state/confirms" "$logs" "$backups"; do
    if [[ "$d" != /* ]]; then d="$STACK/${d#./}"; fi
    mkdir -p "$d"
  done
  chmod +x "$STACK"/*.sh "$STACK"/scripts/*.sh "$STACK"/scripts/*/*.sh "$STACK"/tools/*.sh 2>/dev/null || true
  info "app settings: $STACK/config/<app>"

  # Hardlinks (instant, no extra space) only work inside one filesystem.
  local first="" dev
  for d in "$dl" "$mv" "$tv"; do
    dev="$(stat -c %d "$d" 2>/dev/null || true)"
    if [[ -z "$first" ]]; then first="$dev"; continue; fi
    if [[ -n "$dev" && "$dev" != "$first" ]]; then
      warn "downloads, movies and tv are not all on the same filesystem: imports will be copies, not hardlinks."
      info "Keep them on one disk (or one share) under $root to get instant, space-free imports."
      break
    fi
  done
}

# ---------------------------------------------------------------- 5. containers

compose() { (cd "$STACK" && "${DOCKER[@]}" compose "$@"); }

isolate_env() { # compose must read .env, not variables that happen to be set in this shell
  local k
  for k in $(sed -n 's/^[[:space:]]*\([A-Za-z_][A-Za-z0-9_]*\)[[:space:]]*=.*/\1/p' "$ENV_FILE") \
           COMPOSE_FILE COMPOSE_PROFILES COMPOSE_PROJECT_NAME COMPOSE_ENV_FILES; do
    unset "$k" 2>/dev/null || true
  done
}

app_url() { # APP -> its address as seen from this machine
  local key="$1" def="$2" v
  v="$(env_file_value "$key")"
  v="${v:-$def}"
  printf '%s' "${v%/}"
}

http_up() { # URL -> true when anything answers (even 401/403)
  local code
  code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' "$1" 2>/dev/null || true)"
  [[ -n "$code" && "$code" != 000 ]]
}

app_ready() {
  case "$1" in
    radarr) [[ -s "$STACK/config/radarr/config.xml" ]] && http_up "$(app_url RADARR_URL http://localhost:7878)/ping" ;;
    sonarr) [[ -s "$STACK/config/sonarr/config.xml" ]] && http_up "$(app_url SONARR_URL http://localhost:8989)/ping" ;;
    prowlarr) [[ -s "$STACK/config/prowlarr/config.xml" ]] && http_up "$(app_url PROWLARR_URL http://localhost:9696)/ping" ;;
    jellyseerr) [[ -s "$STACK/config/jellyseerr/settings.json" ]] \
                  && http_up "$(app_url JELLYSEERR_URL http://localhost:5055)/api/v1/status" ;;
    bazarr) [[ -s "$STACK/config/bazarr/config/config.yaml" ]] && http_up "$(app_url BAZARR_URL http://localhost:6767)/" ;;
    qbittorrent) http_up "$(app_url QBIT_URL http://localhost:8080)/api/v2/app/version" ;;
    *) return 0 ;;
  esac
}

check_names_free() { # a container with one of our names from another project would block `up`
  local name project mine
  mine="$(env_file_value COMPOSE_PROJECT_NAME)"
  mine="${mine:-media-stack}"
  for name in "$@"; do
    project="$("${DOCKER[@]}" inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "$name" 2>/dev/null || true)"
    if [[ -n "$("${DOCKER[@]}" ps -aq -f "name=^${name}$" 2>/dev/null || true)" && "$project" != "$mine" ]]; then
      die "a container named '$name' already exists and is not part of this stack. Stop and remove or rename it, then run ./install.sh again."
    fi
  done
}

plex_claimed() {
  local prefs="$STACK/config/plex/Library/Application Support/Plex Media Server/Preferences.xml"
  read_file "$prefs" | grep -q 'PlexOnlineToken="[^"]'
}

plex_claim_prompt() {
  if plex_claimed || [[ -n "$(env_file_value PLEX_CLAIM)" ]]; then return 0; fi
  if [[ $CAN_PROMPT -eq 0 ]]; then
    info "Plex will start unclaimed; claim it later from its web page (http://<this server>:32400/web)."
    return 0
  fi
  info "Plex: open https://www.plex.tv/claim in a browser, copy the code (it is valid for 4 minutes)"
  info "and paste it here, or press Enter to claim the server later from its web page."
  local claim=""
  ask_into claim "Plex claim code" "" 0
  if [[ -n "$claim" ]]; then
    if validate PLEX_CLAIM "$claim"; then set_env_key "$ENV_FILE" PLEX_CLAIM "$claim"; else warn "$VERR; skipped"; fi
  fi
}

gluetun_health() { "${DOCKER[@]}" inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{end}}' gluetun 2>/dev/null || true; }

up_services() { # SERVICES...: `compose up -d`, giving a slow VPN connection a second chance
  if compose up -d "$@"; then return 0; fi
  if [[ "$(env_file_value VPN_MODE)" != off ]] && in_list qbittorrent "$@"; then
    info "waiting up to 2 minutes for the VPN (gluetun) to report healthy..."
    local i
    for i in $(seq 1 24); do
      if [[ "$(gluetun_health)" == healthy ]]; then break; fi
      sleep 5
    done
    if [[ "$(gluetun_health)" == healthy ]] && compose up -d "$@"; then return 0; fi
    die "the VPN container (gluetun) is not healthy, so qBittorrent cannot start. Check the VPN settings in .env and the log: ${DOCKER[*]} logs gluetun"
  fi
  die "docker compose up failed (see above). Fix the cause and run ./install.sh again."
}

wait_for_apps() {
  local wanted=(qbittorrent radarr sonarr prowlarr jellyseerr) pending=() app start now last=0
  if profile_on subtitles; then wanted+=(bazarr); fi
  start="$(date +%s)"
  info "waiting for the apps to finish their first start (up to $((WAIT_SECS / 60)) minutes)..."
  while :; do
    pending=()
    for app in "${wanted[@]}"; do
      if ! app_ready "$app"; then pending+=("$app"); fi
    done
    if [[ ${#pending[@]} -eq 0 ]]; then info "all apps answer"; return 0; fi
    now="$(date +%s)"
    if (( now - start >= WAIT_SECS )); then
      die "still not answering after $((WAIT_SECS / 60)) minutes: ${pending[*]}. Look at: ${DOCKER[*]} logs <name>, then run ./install.sh again."
    fi
    if (( now - last >= 30 )); then
      info "$((now - start)) s: waiting for ${pending[*]}"
      last="$now"
    fi
    sleep 5
  done
}

store_key() { # KEY VALUE: save a value read from an app's own files into .env
  if [[ -z "$2" ]]; then warn "could not read $1 from the app's config folder"; return 0; fi
  if [[ "$(env_file_value "$1")" != "$2" ]]; then
    set_env_key "$ENV_FILE" "$1" "$2"
    info "$1 saved to .env"
  fi
}

harvest_keys() {
  local key
  key="$(read_file "$STACK/config/radarr/config.xml" | sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' | head -n 1)"
  store_key RADARR_API_KEY "$key"
  key="$(read_file "$STACK/config/sonarr/config.xml" | sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' | head -n 1)"
  store_key SONARR_API_KEY "$key"
  key="$(read_file "$STACK/config/prowlarr/config.xml" | sed -n 's:.*<ApiKey>\([^<]*\)</ApiKey>.*:\1:p' | head -n 1)"
  store_key PROWLARR_API_KEY "$key"
  key="$(read_file "$STACK/config/jellyseerr/settings.json" \
         | python3 -c 'import json, sys; print(json.load(sys.stdin)["main"]["apiKey"])' 2>/dev/null || true)"
  store_key JELLYSEERR_API_KEY "$key"
  if profile_on subtitles; then
    # The first "apikey:" inside the top-level "auth:" section of Bazarr's config.yaml.
    key="$(read_file "$STACK/config/bazarr/config/config.yaml" | awk '
      /^[^[:space:]#][^:]*:/ { section = $1 }
      section == "auth:" && $1 == "apikey:" { gsub(/["'\'']/, "", $2); print $2; exit }' || true)"
    store_key BAZARR_API_KEY "$key"
  fi
  if profile_on plex && [[ -z "$(env_file_value PLEX_TOKEN)" ]]; then
    key="$(read_file "$STACK/config/plex/Library/Application Support/Plex Media Server/Preferences.xml" \
           | sed -n 's/.*PlexOnlineToken="\([^"]*\)".*/\1/p' | head -n 1)"
    if [[ -n "$key" ]]; then store_key PLEX_TOKEN "$key"; fi
  fi
}

start_stack() {
  step "Containers"
  isolate_env
  local services=() s list
  list="$(compose config --services)" || die "docker compose cannot read the configuration (see above); check .env"
  while IFS= read -r s; do
    if [[ -n "$s" && "$s" != moviebot ]]; then services+=("$s"); fi
  done <<< "$list"
  [[ ${#services[@]} -gt 0 ]] || die "docker compose found no services (is docker-compose.yml intact?)"
  check_names_free "${services[@]}" moviebot
  # Only images that are not here yet: re-running the installer never updates the apps.
  info "downloading missing images (only the first run takes a while)..."
  compose pull --policy missing "${services[@]}" \
    || die "could not download the images (see above); check the internet connection and run ./install.sh again."
  if in_list plex "${services[@]}"; then plex_claim_prompt; fi
  isolate_env
  up_services "${services[@]}"
  wait_for_apps
  harvest_keys
  info "starting the Telegram bot..."
  isolate_env
  compose up -d --build moviebot || die "the bot (moviebot) could not be built or started (see above)."
  sleep 5
  local restarts
  restarts="$("${DOCKER[@]}" inspect -f '{{.RestartCount}}' moviebot 2>/dev/null || echo 0)"
  if [[ "$("${DOCKER[@]}" inspect -f '{{.State.Running}}' moviebot 2>/dev/null || true)" != true || "${restarts:-0}" != 0 ]]; then
    warn "the bot is not running steadily; check TELEGRAM_BOT_TOKEN and ALLOWED_TELEGRAM_USER_IDS, and: ${DOCKER[*]} logs moviebot"
  fi
}

configure_apps() {
  step "Connecting the apps (configure.py)"
  python3 "$STACK/configure.py" --only qbit,radarr,sonarr,prowlarr \
    || die "configure.py could not finish (see the lines above). Fix the cause and run ./install.sh again; it is safe to repeat."
  python3 "$STACK/configure.py" --only jellyseerr || warn "Jellyseerr is not fully set up yet (see above)."
  isolate_env
  compose up -d || die "docker compose up failed (see above)."
}

install_cron() {
  step "Guard jobs (cron)"
  if [[ $SKIP_CRON -eq 1 ]]; then
    info "skipped. Install them later with: python3 configure.py --only cron"
    return 0
  fi
  python3 "$STACK/configure.py" --only cron || die "could not install the cron jobs (see above)."
}

# ---------------------------------------------------------------- 7. summary

jellyseerr_initialized() {
  curl -fsS -m 10 "$(app_url JELLYSEERR_URL http://localhost:5055)/api/v1/settings/public" 2>/dev/null \
    | python3 -c 'import json, sys; sys.exit(0 if json.load(sys.stdin).get("initialized") else 1)' 2>/dev/null
}

summary() {
  local ip
  ip="$(detect_host_ip)"
  step "Done"
  printf '  %-13s http://%s:%s\n' "qBittorrent" "$ip" 8080 Radarr "$ip" 7878 Sonarr "$ip" 8989 \
    Prowlarr "$ip" 9696 Jellyseerr "$ip" 5055
  if profile_on plex; then printf '  %-13s http://%s:%s/web\n' Plex "$ip" 32400; fi
  if profile_on monitoring; then printf '  %-13s http://%s:%s\n' "Uptime Kuma" "$ip" 3001 Tautulli "$ip" 8181; fi
  if profile_on subtitles; then printf '  %-13s http://%s:%s\n' Bazarr "$ip" 6767; fi
  say ""
  say "Next steps"
  info "- qBittorrent login: user $(env_file_value QBIT_USERNAME), password: QBIT_PASSWORD in .env"
  info "- Radarr, Sonarr and Prowlarr ask you to create a login the first time you open them."
  info "- Add your indexers in Prowlarr; tag the ones behind Cloudflare with 'flaresolverr'."
  info "- Send /start to your Telegram bot."
  if ! jellyseerr_initialized; then
    info "- Jellyseerr: open it once, sign in and finish its wizard, then run:"
    info "    python3 configure.py --only jellyseerr"
  fi
  if profile_on plex && ! plex_claimed; then
    info "- Plex is not claimed yet: open http://$ip:32400/web and sign in, then run ./install.sh again."
  fi
  if profile_on subtitles; then
    info "- Bazarr: connect it to Sonarr (sonarr:8989) and Radarr (radarr:7878) in its settings;"
    info "  the API keys are in .env."
  fi
  info "- Logs of the guard jobs: $STACK/logs/<job>.log; app settings: $STACK/config/"
  info "- Bot and guard state: $STACK/state/"
  info "- Run ./install.sh again at any time: it keeps your settings and only adds what is missing."
  if [[ "$(env_file_value VPN_MODE)" == off ]]; then no_vpn_banner; fi
}

# ---------------------------------------------------------------- main

ASSUME_YES=0
ANSWERS_FILE=""
PROFILES_ARG=""
PROFILES_GIVEN=0
NO_VPN=0
INSTALL_DOCKER=0
SKIP_CRON=0

parse_args() {
  while [[ $# -gt 0 ]]; do
    case "$1" in
      --answers) [[ $# -ge 2 ]] || die "--answers needs a file"; ANSWERS_FILE="$2"; shift 2 ;;
      --answers=*) ANSWERS_FILE="${1#*=}"; shift ;;
      --yes|-y) ASSUME_YES=1; shift ;;
      --profiles) [[ $# -ge 2 ]] || die "--profiles needs a list"; PROFILES_ARG="$2"; PROFILES_GIVEN=1; shift 2 ;;
      --profiles=*) PROFILES_ARG="${1#*=}"; PROFILES_GIVEN=1; shift ;;
      --no-vpn) NO_VPN=1; shift ;;
      --install-docker) INSTALL_DOCKER=1; shift ;;
      --skip-cron) SKIP_CRON=1; shift ;;
      -h|--help) usage; exit 0 ;;
      *) die "unknown option: $1 (see ./install.sh --help)" ;;
    esac
  done
}

main() {
  parse_args "$@"
  say "media-stack installer ($STACK)"
  if [[ $NO_VPN -eq 1 ]]; then no_vpn_banner; fi
  if [[ $ASSUME_YES -eq 1 ]]; then
    CAN_PROMPT=0
  elif { : </dev/tty; } 2>/dev/null; then
    CAN_PROMPT=1
  else
    die "there is no terminal to ask questions on: run with --yes (and --answers FILE)"
  fi
  if [[ -n "$ANSWERS_FILE" ]]; then
    [[ -r "$ANSWERS_FILE" ]] || die "cannot read the answers file $ANSWERS_FILE"
    kv_load "$ANSWERS_FILE" F_
  fi
  check_machine
  choose_env_mode
  collect_answers
  write_env
  create_folders
  start_stack
  configure_apps
  install_cron
  summary
}

if [[ "${BASH_SOURCE[0]}" == "$0" ]]; then
  main "$@"
fi
