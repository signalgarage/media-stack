#!/usr/bin/env bash
# privacy_scan.sh: fail when the tree holds something that must never be published.
#
# Usage: bash tools/privacy_scan.sh [DIR]     (default: the repository this script lives in)
# Exit:  0 clean, 1 findings (one "file:line: what" line each), 2 usage error.
#
# Why: the project is published from a working private setup, and a review by eye misses things
# a scan does not. CI runs this on every push. It fails on:
#   - hex strings of 32 or more characters (API keys, webhook secrets) and Telegram bot tokens;
#     a line under tests/ that carries the comment "privacy-scan: fake" may hold a made-up value
#     of these two kinds, nothing else
#   - IPv4 addresses other than 0.0.0.0, 127.0.0.1 and the documentation ranges 192.0.2.0/24,
#     198.51.100.0/24 and 203.0.113.0/24 (private and CGNAT addresses are named as such)
#   - e-mail addresses other than GitHub's users.noreply.github.com
#   - Uptime Kuma push URLs that carry a token, magnet links, references to torrent or NZB files
#   - files over 1 MiB, and files of the types torrent, nzb, mkv, mp4, conf, ovpn, key and pem
# Skipped: .git, virtualenvs and tool caches anywhere; the runtime folders state, config, logs
# and backups at the top; .env files git does not track (install.sh writes the real secrets
# there on a live install, and .gitignore keeps them out of the repository; a committed one is
# scanned like any other file).
# Findings name the file, the line and the kind of problem, never the value itself, so a CI log
# does not republish what was found.
#
# Portable on purpose: bash 3.2 with BSD grep (macOS) and bash 5 with GNU grep (CI).

set -u
# Byte semantics everywhere: in a UTF-8 locale GNU grep treats a file with one invalid byte as
# binary, and -I would then skip it without a word.
export LC_ALL=C

if [ "$#" -gt 1 ] || [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    echo "usage: bash tools/privacy_scan.sh [DIR]" >&2
    exit 2
fi
root=${1:-"$(dirname "${BASH_SOURCE[0]}")/.."}
if ! cd "$root" 2>/dev/null; then
    echo "privacy_scan: cannot open the folder $root" >&2
    exit 2
fi

FAKE_MARK="privacy-scan: fake"
MAX_BYTES=1048576
findings=0
last_key=""

report() {   # report FILE LINE WHAT (LINE may be empty for a whole-file finding)
    local key="$1:$2:$3"
    [ "$key" = "$last_key" ] && return 0     # one line per file, line and kind of problem
    last_key=$key
    if [ -n "$2" ]; then
        printf '%s:%s: %s\n' "$1" "$2" "$3"
    else
        printf '%s: %s\n' "$1" "$3"
    fi
    findings=$((findings + 1))
}

walk() {     # walk [find tests]: the tree's files outside the skipped folders, NUL separated
    find . \( -name .git -o -name .venv -o -name venv -o -name __pycache__ -o -name .ruff_cache \
              -o -name .pytest_cache -o -name .mypy_cache -o -name node_modules \) -prune \
        -o \( -path ./state -o -path ./config -o -path ./logs -o -path ./backups \) -prune \
        -o -type f "$@" -print0
}

tracked() {  # true when the folder is a git checkout that tracks the file (a committed .env is scanned)
    [ -e .git ] && git ls-files --error-unmatch -- "$1" >/dev/null 2>&1
}

files=()
while IFS= read -r -d '' f; do
    f=${f#./}
    case "${f##*/}" in
        .env.example) ;;
        .env|.env.*) tracked "$f" || continue ;;
    esac
    files+=("$f")
done < <(walk)

if [ "${#files[@]}" -eq 0 ]; then
    echo "privacy_scan: no files to scan"
    exit 0
fi

scan() {     # scan GREP-ARGS...: "file:line:text" for each matching line of every text file
    printf '%s\0' "${files[@]}" | xargs -0 grep -nIH "$@" -- 2>/dev/null
}

each_hit() { # read "file:line:text" lines into $hit_file $hit_line $hit_text
    IFS= read -r hit || return 1
    hit_file=${hit%%:*}
    hit=${hit#*:}
    hit_line=${hit%%:*}
    hit_text=${hit#*:}
}

fake_ok() {  # true for a line under tests/ that declares its values made up
    case "$hit_file" in
        tests/*) case "$hit_text" in *"$FAKE_MARK"*) return 0 ;; esac ;;
    esac
    return 1
}

ip_problem() {   # prints what is wrong with an IPv4 address; nothing when it is allowed
    local IFS=. a b c d o
    read -r a b c d <<< "$1"
    for o in "$a" "$b" "$c" "$d"; do
        [ "$o" -le 255 ] 2>/dev/null || return 0      # 999.1.2.3 is a version number, not an address
    done
    case "$1" in 0.0.0.0|127.0.0.1) return 0 ;; esac
    if [ "$a" -eq 192 ] && [ "$b" -eq 0 ] && [ "$c" -eq 2 ]; then return 0; fi
    if [ "$a" -eq 198 ] && [ "$b" -eq 51 ] && [ "$c" -eq 100 ]; then return 0; fi
    if [ "$a" -eq 203 ] && [ "$b" -eq 0 ] && [ "$c" -eq 113 ]; then return 0; fi
    if [ "$a" -eq 10 ] || { [ "$a" -eq 172 ] && [ "$b" -ge 16 ] && [ "$b" -le 31 ]; } \
            || { [ "$a" -eq 192 ] && [ "$b" -eq 168 ]; }; then
        echo "private IPv4 address"
    elif [ "$a" -eq 100 ] && [ "$b" -ge 64 ] && [ "$b" -le 127 ]; then
        echo "CGNAT IPv4 address"
    else
        echo "IPv4 address (only 0.0.0.0, 127.0.0.1 and the documentation ranges are allowed)"
    fi
}

# ---- whole files: forbidden types and size
while IFS= read -r f; do
    report "$f" "" "forbidden file type"
done < <(printf '%s\n' "${files[@]}" | grep -Ei '\.(torrent|nzb|mkv|mp4|conf|ovpn|key|pem)$')

while IFS= read -r -d '' f; do
    f=${f#./}
    case "${f##*/}" in .env|.env.*) [ "${f##*/}" = ".env.example" ] || tracked "$f" || continue ;; esac
    report "$f" "" "file larger than 1 MiB"
done < <(walk -size +"${MAX_BYTES}"c)

# ---- secrets
while each_hit; do
    fake_ok || report "$hit_file" "$hit_line" "hex string of 32+ characters (an API key or secret?)"
done < <(scan -E -e '[0-9A-Fa-f]{32,}')

while each_hit; do
    fake_ok || report "$hit_file" "$hit_line" "Telegram bot token"
done < <(scan -E -e '[0-9]{6,}:[A-Za-z0-9_-]{30,}')

while each_hit; do
    report "$hit_file" "$hit_line" "Uptime Kuma push URL with a token"
done < <(scan -E -e 'api/push/[A-Za-z0-9_-]{6,}')

# ---- media and download references
while each_hit; do
    report "$hit_file" "$hit_line" "magnet link"
done < <(scan -Ei -e 'magnet:\?')

while each_hit; do
    report "$hit_file" "$hit_line" "reference to a torrent or NZB file"
done < <(scan -Ei -e '\.(torrent|nzb)([^A-Za-z0-9_]|$)')   # not code such as qbit.torrent_info()

# ---- e-mail addresses
email_re='[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}'
while each_hit; do
    rest=$hit_text
    while [[ $rest =~ $email_re ]]; do
        m=${BASH_REMATCH[0]}
        case "$m" in
            *@users.noreply.github.com) ;;
            *) report "$hit_file" "$hit_line" "e-mail address" ;;
        esac
        rest=${rest#*"$m"}
    done
done < <(scan -E -e "$email_re")

# ---- IPv4 addresses: four numbers, not part of a longer dotted or alphanumeric word
ip_re='(^|[^0-9A-Za-z.])([0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3})($|[^0-9A-Za-z.]|\.($|[^0-9A-Za-z]))'
while each_hit; do
    rest=$hit_text
    while [[ $rest =~ $ip_re ]]; do
        problem=$(ip_problem "${BASH_REMATCH[2]}")
        [ -n "$problem" ] && report "$hit_file" "$hit_line" "$problem"
        rest=${rest#*"${BASH_REMATCH[0]}"}
    done
done < <(scan -E -e '[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}')

if [ "$findings" -gt 0 ]; then
    echo "privacy_scan: $findings finding(s); nothing above may be published" >&2
    exit 1
fi
echo "privacy_scan: clean (${#files[@]} files)"
exit 0
