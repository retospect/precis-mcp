#!/usr/bin/env bash
# SessionStart hook: surface memory hygiene + the once/day reconsolidation-due
# signal — but only when ACTIONABLE. Silent on a clean, in-budget index that was
# already reconsolidated today, so it stays low-noise like map-staleness.
#
# Wired in .claude/settings.json (SessionStart). Never blocks: memory lives
# outside the repo, so a missing dir or a slow scan just means no nudge.
set -euo pipefail
cd "$(dirname "$0")/../.."

out="$(scripts/memory-lint 2>/dev/null || true)"
if printf '%s' "$out" | grep -qiE 'issue|DUE|OVER'; then
    echo "🧠 memory-lint (from /whatneedsdoing's hygiene step):"
    printf '%s\n' "$out" | sed 's/^/   /'
fi
exit 0
