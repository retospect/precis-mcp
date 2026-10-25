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
# Harness detection lives in scripts/hooks/_devin-detect.sh (gr515490):
# DEVIN_PROJECT_DIR is the reliable marker — Devin's payload ids are absent
# for events before the first user prompt (gr514171) and it sets no AI_AGENT.
set -euo pipefail

payload=""
[ -t 0 ] || payload="$(cat 2>/dev/null || true)"

_detect="$(cd "$(dirname "$0")" && pwd)/_devin-detect.sh"
[ -f "$_detect" ] || _detect="${DEVIN_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}/scripts/hooks/_devin-detect.sh"
if [ -f "$_detect" ]; then
    # shellcheck source=/dev/null
    . "$_detect"
else
    # Helper unreachable (a copied-out hook with no project dir exported):
    # fall back to the marker that is always present under Devin.
    hook_is_devin() { [ -n "${DEVIN_PROJECT_DIR:-}" ]; }
    hook_project_dir() { printf '%s' "${DEVIN_PROJECT_DIR:-${CLAUDE_PROJECT_DIR:-}}"; }
fi

devin=0
hook_is_devin "$payload" && devin=1

_proj="$(hook_project_dir)"
cd "${_proj:-$(dirname "$0")/../..}" 2>/dev/null || exit 0

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
