#!/usr/bin/env bash
# run_guard.sh JOB [ARGS...]: the cron entry point for every guard script.
#
# Runs scripts/JOB.py (or scripts/JOB.sh) from the stack folder and appends its output to
# logs/JOB.log under a timestamped header. The log is kept under about 5 MB by dropping its
# oldest half. A run that is still going makes the next one skip (no pile-up when a share
# hangs). When the job exits 0 and KUMA_PUSH_<JOB> is set (environment or .env), that Uptime
# Kuma push URL is called, so a monitor goes red when a job fails or stops running.
#
# Plain bash, no bash 4 features: it must run from any cron on any distribution.

set -u

STACK="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
export PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin${PATH:+:$PATH}"
LOG_MAX_BYTES=5242880   # 5 MB

job="${1:-}"
if [[ ! "$job" =~ ^[A-Za-z0-9_-]+$ ]]; then
  echo "usage: run_guard.sh JOB [ARGS...]   (JOB = a script name in scripts/, without .py or .sh)" >&2
  exit 2
fi
shift

env_value() { # KEY -> the process environment wins, then the stack's .env (read, never sourced)
  local key="$1" val=""
  if [[ -n "${!key:-}" ]]; then
    printf '%s' "${!key}"
    return
  fi
  if [[ -r "$STACK/.env" ]]; then
    val="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${key}[[:space:]]*=" "$STACK/.env" | tail -n 1 | cut -d= -f2-)"
    val="${val#"${val%%[![:space:]]*}"}"
    val="${val%"${val##*[![:space:]]}"}"
    if [[ ${#val} -ge 2 && ( ( "${val:0:1}" == '"' && "${val: -1}" == '"' ) || ( "${val:0:1}" == "'" && "${val: -1}" == "'" ) ) ]]; then
      val="${val:1:${#val}-2}"
    fi
  fi
  printf '%s' "$val"
}

cd "$STACK" || exit 2

log_dir="$(env_value LOG_DIR)"
log_dir="${log_dir:-./logs}"
[[ "$log_dir" == /* ]] || log_dir="$STACK/${log_dir#./}"
mkdir -p "$log_dir" || exit 2
log="$log_dir/$job.log"

if [[ -f "scripts/$job.py" ]]; then
  cmd=(python3 "scripts/$job.py")
elif [[ -f "scripts/$job.sh" ]]; then
  cmd=(bash "scripts/$job.sh")
else
  echo "$(date '+%F %T') run_guard: no scripts/$job.py or scripts/$job.sh" >> "$log"
  exit 2
fi

# Keep the newest half when the log is too big (cut at a line boundary).
size=0
if [[ -f "$log" ]]; then
  size=$(wc -c < "$log")
  size="${size//[!0-9]/}"
fi
if [[ "${size:-0}" -gt "$LOG_MAX_BYTES" ]]; then
  tail -c "$(( size / 2 ))" "$log" | tail -n +2 > "$log.tmp.$$" && mv -f "$log.tmp.$$" "$log"
fi

# One run per job at a time (flock is part of util-linux; without it the jobs simply run).
if command -v flock >/dev/null 2>&1; then
  exec 9>"$log_dir/.$job.lock"
  if ! flock -n 9; then
    echo "$(date '+%F %T') $job: previous run still active, skipped" >> "$log"
    exit 0
  fi
fi

start=$(date +%s)
{
  echo "===== $(date '+%F %T') $job $*"
  "${cmd[@]}" "$@"
} >> "$log" 2>&1
rc=$?
echo "----- exit $rc after $(( $(date +%s) - start )) s" >> "$log"

if [[ $rc -eq 0 ]]; then
  push_var="KUMA_PUSH_$(printf '%s' "$job" | tr '[:lower:]' '[:upper:]' | tr '-' '_')"
  push_url="$(env_value "$push_var")"
  if [[ -n "$push_url" ]]; then
    curl -fsS -m 10 "$push_url" >/dev/null 2>&1 || true
  fi
fi
exit "$rc"
