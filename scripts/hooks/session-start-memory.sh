#!/usr/bin/env bash
# SessionStart hook: print the harness memory index rendered from the graph
# (`precis memory index`), the replacement for MEMORY.md's index role.
# docs/backlog/memory-native-authoring.md.
#
# Wired as a SessionStart hook in .claude/settings.json. It prints only once
# the harness MEMORY.md carries the graph marker (the cutover: MEMORY.md becomes
# a pointer); before that MEMORY.md is the index and printing would double it.
# PRECIS_HARNESS_MEMORY_MD overrides the MEMORY.md path (tests); a missing
# file counts as cut over.
#
# Budget: memory-lint's preamble budget (8000 tok, ~4 bytes/token, CLAUDE.md +
# index together) minus CLAUDE.md's share, passed as --budget-tok.
#
# DSN: with PRECIS_DATABASE_URL set the CLI uses it; otherwise the index comes
# through scripts/prod-precis (a shell hook cannot reach the session MCP).
# PRECIS_MEMORY_CLI overrides the CLI invocation (default `uv run precis`) —
# the seam the gate test uses to run it inside the test container.
#
# Last-good copy: every successful render is saved to $cache. When the graph
# is unreachable the hook prints that copy plus one line naming the failure and
# the copy's age, so a session started during a prod outage still gets an
# index. PRECIS_MEMORY_CACHE overrides the path (tests).
#
# Never blocks a session: any failure exits 0, printing the cached copy and
# one line, or just the one line when no copy exists.
set -uo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
project="${CLAUDE_PROJECT_DIR:-$repo}"
cache="${PRECIS_MEMORY_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/precis/memory-index.md}"

# Same memory-dir derivation as scripts/memory-lint (main checkout root, path
# separators escaped). Same marker string too.
memory_md="${PRECIS_HARNESS_MEMORY_MD:-}"
if [ -z "$memory_md" ]; then
    common="$(git -C "$repo" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
    main_root="$(dirname "${common:-/nonexistent/.git}")"
    memory_md="$HOME/.claude/projects/${main_root//\//-}/memory/MEMORY.md"
fi
if [ -f "$memory_md" ] && ! grep -qF '<!-- memory-index: graph -->' "$memory_md"; then
    exit 0
fi

total_tok=8000
claude_bytes=0
if [ -f "$project/CLAUDE.md" ]; then
    claude_bytes="$(wc -c <"$project/CLAUDE.md" | tr -d ' ')"
fi
budget=$((total_tok - claude_bytes / 4))
[ "$budget" -lt 0 ] && budget=0

fail() {
    if [ -s "$cache" ]; then
        cat "$cache"
        echo "(memory index: $1; showing the cached copy from $(date -u -r "$cache" +%Y-%m-%dT%H:%MZ))"
    else
        echo "memory index unavailable: $1"
    fi
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
# Save the last-good copy; per-process temp + rename, so sessions starting
# together never read a half-written file. A failed save is not an error.
if mkdir -p "$(dirname "$cache")" 2>/dev/null &&
    printf '%s\n' "$out" >"$cache.tmp.$$" 2>/dev/null; then
    mv -f "$cache.tmp.$$" "$cache" 2>/dev/null || rm -f "$cache.tmp.$$"
fi
exit 0
