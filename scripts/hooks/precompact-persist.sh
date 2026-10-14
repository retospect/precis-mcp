#!/usr/bin/env bash
# PreCompact hook: fires right before the conversation is compacted. Free-text
# in the conversation is summarized away; only DURABLE artifacts survive. So
# remind to persist residuals, and surface any hygiene drift while there's still
# full context to act on it.
#
# Wired in .claude/settings.json (PreCompact). Never blocks; best-effort.
set -euo pipefail
cd "${CLAUDE_PROJECT_DIR:-$(dirname "$0")/../..}" 2>/dev/null || exit 0

echo "⏳ PreCompact — persist anything that must outlive this context (free text won't):"
echo "   • residual bugs / next steps → a docs/backlog/ item or a gripe/todo (not the transcript)"
echo "   • in-flight work → update your section:threads node IN PLACE: edit(kind='memory', id=…, mode='replace') — not a fresh put"

# Surface hygiene drift so it can be cleaned before context is lost (advisory).
scripts/memory-lint 2>/dev/null | grep -iE 'issue|DUE' | sed 's/^/   • memory: /' || true
scripts/backlog-lint 2>/dev/null | grep -v '✓' | head -1 | sed 's/^/   • /' || true
exit 0
