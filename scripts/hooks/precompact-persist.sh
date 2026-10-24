#!/usr/bin/env bash
# PreCompact hook: fires right before the conversation is compacted. Free-text
# in the conversation is summarized away; only DURABLE artifacts survive. So
# remind to persist residuals, and surface any hygiene drift while there's still
# full context to act on it.
#
# Wired in .claude/settings.json (PreCompact). Never blocks; best-effort.
#
# The two runtimes loading this hook have different stdout contracts:
#   Claude Code — plain stdout is appended to the compaction context; a
#     hookSpecificOutput JSON envelope is schema-rejected for PreCompact.
#   Devin — stdout must be valid pre_compact JSON or EMPTY; plain text fails
#     the parse ("Hook returned invalid pre_compact JSON output"). Devin has
#     no documented context-injection output for this event, so under Devin
#     stdout stays empty and the advisory goes to stderr.
# Devin detection: its hook stdin payload carries the per-turn ids prompt_id
# and turn_id that Claude's payload lacks; AI_AGENT=devin_* is a fallback.
set -euo pipefail

payload="$(cat 2>/dev/null || true)"

devin=0
case "$payload" in
    *'"prompt_id"'* | *'"turn_id"'*) devin=1 ;;
esac
case "${AI_AGENT:-}" in
    devin*) devin=1 ;;
esac

cd "${DEVIN_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}}" 2>/dev/null || exit 0

note() {
    echo "⏳ PreCompact — persist anything that must outlive this context (free text won't):"
    echo "   • residual bugs / next steps → a docs/backlog/ item or a gripe/todo (not the transcript)"
    echo "   • in-flight work → update your section:threads node IN PLACE: edit(kind='memory', id=…, mode='replace') — not a fresh put"

    # Surface hygiene drift so it can be cleaned before context is lost (advisory).
    scripts/memory-lint 2>/dev/null | grep -iE 'issue|DUE' | sed 's/^/   • memory: /' || true
    scripts/backlog-lint 2>/dev/null | grep -v '✓' | head -1 | sed 's/^/   • /' || true
}

if [ "$devin" -eq 1 ]; then
    note >&2
else
    note
fi
exit 0
