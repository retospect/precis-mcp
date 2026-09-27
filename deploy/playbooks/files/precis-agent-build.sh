#!/usr/bin/env bash
# Run the agent-image build under a PROGRESS watchdog, leaving a durable
# trail on the host — so a stalled build is distinguishable from a merely
# slow one without a live operator inside the colima VM.
#
# WHY THIS EXISTS. Three builds on 2026-09-26, same host, same playbook task:
# 3684s, 92s, 7329s. All three ENDED GREEN, and playbook 33's diagnostics
# only fire on a terminal failure — so every one of them captured nothing,
# and the only evidence anyone has is host-side CPU/network/disk, which is
# flat during a build that then completes normally (that inference was wrong
# twice in one session). A slow success has to leave evidence too.
# See docs/backlog/agent-image-build-stalls-on-mirror-fallback.md.
#
# WHY A SCRIPT, NOT INLINE PLAYBOOK SHELL. Portability is the whole problem:
# the gateway is macOS, where `timeout(1)` does not exist and `stat` is BSD,
# while the inference node is Linux. That is also why playbook 33 reaches for
# ansible's own async/poll rather than `timeout` for its outer ceiling. A
# file can be unit-tested locally against fake binaries; a Jinja-templated
# heredoc cannot.
#
# WHAT THE WATCHDOG ADDS OVER THE ASYNC CEILING. `async: 3600` fires on total
# elapsed, so a legitimate cold build (43 min, measured) and a wedged
# registry fetch are indistinguishable until it trips. Silence is the real
# signal: with --progress=plain BuildKit emits a timestamped line per step and
# streams each step's own output, so a build that has written nothing for
# STALL_SEC is wedged whatever its total elapsed. A false kill is cheap — the
# caller's retries/until ladder just runs another attempt.
#
# CAVEAT, PRE-EXISTING: killing the build kills the docker CLI, not the
# daemon-side build — playbook 33's own async comment already notes that a
# killed-but-alive buildx keeps going behind ansible's back. The watchdog
# neither causes nor worsens that; the retry re-attaches to whatever layers
# the daemon finished meanwhile.
#
# Usage: precis-agent-build.sh <log-path> <stall-seconds> <cmd> [args...]
#
# PRECIS_AGENT_BUILD_POLL_SEC overrides the 15s watchdog poll interval (tests
# drive it down; there is no reason to change it in production).
#
# Stdout is two machine-readable lines (BUILD_ELAPSED_SEC, and BUILD_STALLED
# when the watchdog fired); the build's own output goes to <log-path>, which
# survives an outer async kill. Exit status is the command's own, except a
# watchdog kill which exits 124 (the conventional timeout status) so the
# caller can tell "stalled" from "the build genuinely failed".
set -uo pipefail

if [ "$#" -lt 3 ]; then
    echo "usage: $0 <log-path> <stall-seconds> <cmd> [args...]" >&2
    exit 2
fi

LOG="$1"; shift
STALL_SEC="$1"; shift

# GNU (Linux inference) first, BSD (macOS gateway) second — and the ORDER is
# load-bearing, not stylistic. GNU `stat -f` is not "BSD format string", it is
# `--file-system`, and with an unsupported format it prints `?` and exits 0;
# trying BSD first therefore SUCCEEDS on Linux with a non-numeric answer, the
# arithmetic below fails, and `set -u` kills the watchdog subshell silently —
# a watchdog that never fires and never says so. BSD stat has no `-c` at all,
# so it errors out cleanly and falls through. The digit check is the belt to
# that braces: anything non-numeric, or both stats failing, yields `now`, i.e.
# fails OPEN. Returning 0 would read as "last progress at the epoch" and turn
# every build into an instant phantom stall.
_mtime() {
    local m
    m="$(stat -c %Y "$1" 2>/dev/null || stat -f %m "$1" 2>/dev/null)"
    case "$m" in
        "" | *[!0-9]*) date +%s ;;
        *) printf '%s\n' "$m" ;;
    esac
}

mkdir -p "$(dirname "$LOG")" 2>/dev/null || true
: > "$LOG"
STALL_FLAG="${LOG}.stalled"
rm -f "$STALL_FLAG"

start="$(date +%s)"
echo "precis-agent-build: start $(date -u +%Y-%m-%dT%H:%M:%SZ) stall_ceiling=${STALL_SEC}s cmd=$*" >> "$LOG"

"$@" >> "$LOG" 2>&1 &
build_pid=$!

# Poll the log's mtime, not the process: a wedged build is alive and burning
# no CPU, so liveness says nothing — that is precisely what made the
# host-side signals useless. The kill notice written below lands after the
# decision, so it cannot perturb it.
poll_sec="${PRECIS_AGENT_BUILD_POLL_SEC:-15}"
(
    while kill -0 "$build_pid" 2>/dev/null; do
        sleep "$poll_sec"
        now="$(date +%s)"
        quiet="$(( now - $(_mtime "$LOG") ))"
        if [ "$quiet" -ge "$STALL_SEC" ]; then
            echo "precis-agent-build: WATCHDOG — no build output for ${quiet}s (ceiling ${STALL_SEC}s), killing pid ${build_pid}. The last step above is where it wedged." >> "$LOG"
            : > "$STALL_FLAG"
            kill -TERM "$build_pid" 2>/dev/null
            # Escalate only if TERM was ignored — polling beats a flat sleep
            # so a well-behaved build exits the watchdog in ~1s, not 10.
            for _ in 1 2 3 4 5 6 7 8 9 10; do
                kill -0 "$build_pid" 2>/dev/null || break
                sleep 1
            done
            kill -KILL "$build_pid" 2>/dev/null
            break
        fi
    done
) &
watch_pid=$!

wait "$build_pid"
rc=$?
kill "$watch_pid" 2>/dev/null
wait "$watch_pid" 2>/dev/null

elapsed="$(( $(date +%s) - start ))"
echo "precis-agent-build: done rc=${rc} elapsed=${elapsed}s" >> "$LOG"
echo "BUILD_ELAPSED_SEC=${elapsed}"

if [ -f "$STALL_FLAG" ]; then
    rm -f "$STALL_FLAG"
    echo "BUILD_STALLED=1"
    exit 124
fi
exit "$rc"
