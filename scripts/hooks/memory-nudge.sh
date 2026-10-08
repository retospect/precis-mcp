#!/usr/bin/env bash
# SessionStart hook: run scripts/memory-lint in the background and print
# nothing. The lint takes ~18 s (one `git merge-base` per sha in every memory
# file), which used to hold up every session start for a nudge that was
# usually silent. Its last result lands in ~/.cache/precis/memory-lint.log;
# /whatneedsdoing still runs the lint itself and reports it.
#
# Wired in .claude/settings.json (SessionStart). Never blocks, never fails.
set -uo pipefail
cd "$(dirname "$0")/../.." || exit 0

log="${XDG_CACHE_HOME:-$HOME/.cache}/precis/memory-lint.log"
mkdir -p "$(dirname "$log")" 2>/dev/null || exit 0
nohup bash -c 'scripts/memory-lint >"$1.tmp" 2>&1; mv -f "$1.tmp" "$1"' _ "$log" \
    >/dev/null 2>&1 </dev/null &
disown 2>/dev/null || true
exit 0
