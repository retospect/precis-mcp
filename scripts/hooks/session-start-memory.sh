#!/usr/bin/env bash
# SessionStart hook: print the harness memory index rendered from the graph
# (`precis memory index`), the replacement for MEMORY.md's index role.
# docs/backlog/memory-native-authoring.md.
#
# NOT wired in .claude/settings.json yet: while MEMORY.md still loads through
# the harness this would print the index twice. Wiring is the cutover step.
#
# Budget: memory-lint's preamble budget (8000 tok, ~4 bytes/token, CLAUDE.md +
# index together) minus CLAUDE.md's share, passed as --budget-tok.
#
# DSN: with PRECIS_DATABASE_URL set the CLI uses it; otherwise the index comes
# through scripts/prod-precis (a shell hook cannot reach the session MCP).
# PRECIS_MEMORY_CLI overrides the CLI invocation (default `uv run precis`) —
# the seam the gate test uses to run it inside the test container.
#
# Never blocks a session: any failure prints ONE line naming it and exits 0.
set -uo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
project="${CLAUDE_PROJECT_DIR:-$repo}"

total_tok=8000
claude_bytes=0
if [ -f "$project/CLAUDE.md" ]; then
    claude_bytes="$(wc -c <"$project/CLAUDE.md" | tr -d ' ')"
fi
budget=$((total_tok - claude_bytes / 4))
[ "$budget" -lt 0 ] && budget=0

fail() {
    echo "memory index unavailable: $1"
    exit 0
}

if [ -n "${PRECIS_DATABASE_URL:-}" ]; then
    read -r -a cli <<<"${PRECIS_MEMORY_CLI:-uv run precis}"
else
    cli=("$repo/scripts/prod-precis")
fi

errf="$(mktemp)" || fail "no temp file"
trap 'rm -f "$errf"' EXIT

cd "$repo" || fail "cannot enter repo root"
out="$("${cli[@]}" memory index --budget-tok "$budget" 2>"$errf")"
rc=$?
if [ "$rc" -ne 0 ]; then
    # Last stderr line, connection URLs redacted, capped — one line only.
    reason="$(tail -n 1 "$errf" | sed -E 's#[a-z]+://[^ ]+#<url>#g' | cut -c1-160)"
    fail "precis memory index exited $rc${reason:+ ($reason)}"
fi
if [ -z "$out" ]; then
    fail "precis memory index printed nothing"
fi
printf '%s\n' "$out"
exit 0
