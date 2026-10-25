#!/usr/bin/env bash
# Advisory only: carry decisions and source ranges through compaction.
#
# Devin's pre_compact contract wants stdout EMPTY or valid JSON; plain text
# fails with "Hook returned invalid pre_compact JSON output" (gr514434,
# gr514171). Detect Devin as precis-mcp's precompact-persist.sh does: its
# hook stdin carries prompt_id/turn_id, which Claude's payload lacks;
# AI_AGENT=devin_* is a fallback. Under Devin the note goes to stderr.
payload=""
[ -t 0 ] || payload="$(cat 2>/dev/null || true)"
devin=0
case "$payload" in
    *'"prompt_id"'* | *'"turn_id"'*) devin=1 ;;
esac
case "${AI_AGENT:-}" in
    devin*) devin=1 ;;
esac
# Auto-compaction before the first prompt carries no prompt_id/turn_id and
# Devin never sets AI_AGENT; DEVIN_PROJECT_DIR is always set (gr514171).
[ -n "${DEVIN_PROJECT_DIR:-}" ] && devin=1

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
