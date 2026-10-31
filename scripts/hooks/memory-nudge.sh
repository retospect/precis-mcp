#!/usr/bin/env bash
# SessionStart hook: run scripts/memory-lint in the background and print
# nothing. The lint takes ~18 s (one `git merge-base` per sha in every memory
# file), which used to hold up every session start for a nudge that was
# usually silent. Its last result lands in ~/.cache/precis/memory-lint.log;
# /whatneedsdoing still runs the lint itself and reports it.
#
# Throttled (gr541365): with dozens of sessions starting together, one lint
# per session was a fleet-wide pile of git work on top of everything else
# SessionStart does. A launch takes an atomic claim (a directory, `mkdir`) and
# a later start inside MEMORY_NUDGE_SECS (default 600) launches nothing — the
# log from that run is current enough for a nudge. The claim also covers a
# lint still running, which the log (written on completion) cannot.
#
# Wired in .claude/settings.json (SessionStart). Never blocks, never fails.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

log="${XDG_CACHE_HOME:-$HOME/.cache}/precis/memory-lint.log"
mkdir -p "$(dirname "$log")" 2>/dev/null || exit 0

# The claim is a directory: `mkdir` is atomic, so of a burst of starts that all
# pass the age check exactly one wins it and launches. A claim older than the
# window is cleared first (rmdir races are harmless: one mkdir still wins).
claim="$log.started.d"
secs="${MEMORY_NUDGE_SECS:-600}"
case "$secs" in '' | *[!0-9]*) secs=600 ;; esac
mins=$(((secs + 59) / 60))
if [ "$mins" -gt 0 ] && [ -d "$claim" ] && [ -z "$(find "$claim" -maxdepth 0 -mmin "-$mins" 2>/dev/null)" ]; then
    rmdir "$claim" 2>/dev/null || true
fi
[ "$mins" -eq 0 ] && rmdir "$claim" 2>/dev/null
mkdir "$claim" 2>/dev/null || exit 0

nohup bash -c 'scripts/memory-lint >"$1.tmp" 2>&1; mv -f "$1.tmp" "$1"' _ "$log" \
    >/dev/null 2>&1 </dev/null &
disown 2>/dev/null || true
exit 0
