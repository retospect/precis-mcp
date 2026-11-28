#!/usr/bin/env bash
# SessionStart hook: print a one-line memory summary from the graph
# (`precis memory index --for-hook`): "N live threads (+M older)" plus the
# command that shows the list. The thread list itself moved behind that
# on-demand command — hook output is injected once and then carried on every
# later turn, so it pays for a pointer, not a roster (gr499042; the list
# returned via `precis memory index`, recall via search(kind='memory')).
# me690866.
#
# Wired as a SessionStart hook in .claude/settings.json. It prints only once
# the harness MEMORY.md carries the graph marker (the cutover: MEMORY.md becomes
# a pointer); before that MEMORY.md is the index and printing would double it.
# PRECIS_HARNESS_MEMORY_MD overrides the MEMORY.md path (tests); a missing
# file counts as cut over.
#
# DSN: with PRECIS_DATABASE_URL set the CLI uses it; otherwise the index comes
# through scripts/prod-precis (a shell hook cannot reach the session MCP).
# PRECIS_MEMORY_CLI overrides the CLI invocation (default `uv run precis`) —
# the seam the gate test uses to run it inside the test container.
#
# Last-good copy: every successful render is saved to $cache. When the graph
# is unreachable the hook prints that copy plus one line naming the failure and
# the copy's age, so a session started during a prod outage still gets the
# summary. PRECIS_MEMORY_CACHE overrides the path (tests).
#
# No node export here (Reto 2026-11-28): `--export-dir` read every node body
# one round trip at a time — ~37 s of a ~38 s hook over the prod tunnel. The
# node cache scripts/memory-lint, docs-index and gripe-closed-citations read is
# refreshed on demand:
#   scripts/prod-precis memory index --export-dir ~/.cache/precis/memory-nodes
#
# Gripe line: after a good render the hook appends one `gripes: N open · M
# older than 3d · oldest grX (D days)` line (`precis gripes --summary`, the
# same numbers as get(kind='gripe', id='/open')). It is part of the saved
# copy, which is where scripts/inflight reads it from. A failed gripe call
# drops the line and nothing else.
#
# Outage file: when the graph is reachable and <memory dir>/outage-memory.md
# (beside MEMORY.md) holds entries (`## <UTC stamp> <session>` headings,
# written during an outage), one extra line asks the session to integrate them
# and empty the file. Print only; not part of the cached copy. Flow:
# docs/runbooks/memory-restore.md.
#
# Never blocks a session: any failure exits 0, printing the cached copy and
# one line, or just the one line when no copy exists.
set -uo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
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

# gr499042: the hook's output is injected once and carried every later
# turn — the live-threads list cost ~185M tok/day of carry at 20 lines,
# ~7 after the first cut. --for-hook prints counts + the command instead.

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
# Both CLI calls below are reads whose stderr is discarded, so prod-precis's
# `git fetch origin prod` + stale-tree check (which only WARNS for a read) is
# pure latency here — ~5 s a call, and a ref-lock contender across dozens of
# sessions starting together (gr541365). Skip it; writes keep their guard.
export PRECIS_ALLOW_STALE_PROD_TREE=1
out="$("${cli[@]}" memory index --for-hook 2>"$errf")"
rc=$?
if [ "$rc" -ne 0 ]; then
    # Last stderr line, connection URLs redacted, capped — one line only.
    reason="$(tail -n 1 "$errf" | sed -E 's#[a-z]+://[^ ]+#<url>#g' | cut -c1-160)"
    fail "precis memory index exited $rc${reason:+ ($reason)}"
fi
if [ -z "$out" ]; then
    fail "precis memory index printed nothing"
fi
gripes="$("${cli[@]}" gripes --summary 2>/dev/null | tail -n 1)" || gripes=""
case "$gripes" in
"gripes: "*) out="$out"$'\n'"$gripes" ;;
esac
printf '%s\n' "$out"
outage_md="$(dirname "$memory_md")/outage-memory.md"
if [ -s "$outage_md" ]; then
    n="$(grep -c '^## ' "$outage_md" 2>/dev/null)"
    if [ "${n:-0}" -gt 0 ]; then
        echo "outage-memory.md has $n entries — integrate them into the graph (remember skill), then empty the file"
    fi
fi
# Save the last-good copy; per-process temp + rename, so sessions starting
# together never read a half-written file. A failed save is not an error.
if mkdir -p "$(dirname "$cache")" 2>/dev/null &&
    printf '%s\n' "$out" >"$cache.tmp.$$" 2>/dev/null; then
    mv -f "$cache.tmp.$$" "$cache" 2>/dev/null || rm -f "$cache.tmp.$$"
fi
exit 0
