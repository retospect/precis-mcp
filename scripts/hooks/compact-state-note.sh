#!/usr/bin/env bash
# Advisory only: carry decisions and source ranges through compaction.
#
# Devin's pre_compact contract wants stdout EMPTY or valid JSON; plain text
# fails with "Hook returned invalid pre_compact JSON output" (gr514434,
# gr514171). Under Devin the note goes to stderr. Harness detection lives
# in scripts/hooks/_devin-detect.sh (gr515490) — DEVIN_PROJECT_DIR is the
# reliable marker; the live copy of this hook sits in ~/.claude/hooks/, so
# the helper resolves there via the project dir the harness exports for
# every hook spawn.
payload=""
[ -t 0 ] || payload="$(cat 2>/dev/null || true)"

_detect="$(cd "$(dirname "$0")" && pwd)/_devin-detect.sh"
[ -f "$_detect" ] || _detect="${DEVIN_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}/scripts/hooks/_devin-detect.sh"
if [ -f "$_detect" ]; then
    # shellcheck source=/dev/null
    . "$_detect"
else
    hook_is_devin() { [ -n "${DEVIN_PROJECT_DIR:-}" ]; }
fi
devin=0
hook_is_devin "$payload" && devin=1

if [[ "${1:-}" == "--resume" ]]; then
    msg='[compact] Resume from the compacted state note and accepted decisions. Use its document paths and headings/ranges to fetch only missing sections; avoid re-reading whole governing documents. Pre-compact file bodies may be absent, so read any section needed to verify current work.'
else
    msg='[compact] Preserve a compact state-so-far note in the compaction summary: accepted decisions, remaining work, and governing document paths with relevant headings/line ranges. After compaction use that note; fetch only the needed section when context is missing or the source changed, instead of re-reading whole documents. Do not assume pre-compact file bodies remain in context.'
fi

if [ "$devin" -eq 1 ]; then
    printf '%s\n' "$msg" >&2
else
    printf '%s\n' "$msg"
fi
exit 0
