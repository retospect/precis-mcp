#!/usr/bin/env bash
# SessionStart hook: print a one-line memory summary from the graph
# (`precis memory index --for-hook`): "N live threads (+M older)" plus the
# command that shows the list. The thread list itself moved behind that
# on-demand command — hook output is injected once and then carried on every
# later turn, so it pays for a pointer, not a roster (gr499042; the list
# returned via `precis memory index`, recall via search(kind='memory')).
# docs/backlog/memory-native-authoring.md.
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
# Node cache: the same CLI call passes `--export-dir "$nodes"`, which writes each
# topic node's body to $nodes/<handle>.md (`# title`, blank line, body; swapped
# in whole by the CLI). scripts/memory-lint lints those bodies in graph mode.
# Default ~/.cache/precis/memory-nodes; PRECIS_MEMORY_NODES overrides (tests).
# A failed render leaves the previous set as it was; a failed export prints one
# stderr line and does not change the index output or the exit code.
#
# Gripe line: after a good render the hook appends one `gripes: N open · M
# older than 3d · oldest grX (D days)` line (`precis gripes --summary`, the
# same numbers as get(kind='gripe', id='/open')). It is part of the saved
# copy, which is where scripts/inflight reads it from. A failed gripe call
# drops the line and nothing else.
#
# Rotation: before the CLI swaps the node set, the previous one is copied to
# $nodes.snap.$$; only when the new export succeeded (rc 0, no export-failure
# line, a non-empty set) is that copy renamed to $nodes.<UTC YYYYMMDDTHHMMSSZ>
# and all but the newest 7 such dirs deleted — a failed or empty export never
# pushes good copies out. At most one rotation per PRECIS_MEMORY_ROTATE_SECS
# (default 21600) so seven dirs span days, not seven session starts. Only
# names matching $nodes.2* are pruned; the CLI's .old.<pid>/.tmp.<pid> debris
# is left alone. Restore: docs/runbooks/memory-restore.md.
#
# Never blocks a session: any failure exits 0, printing the cached copy and
# one line, or just the one line when no copy exists.
set -uo pipefail

repo="$(cd "$(dirname "$0")/../.." && pwd)"
cache="${PRECIS_MEMORY_CACHE:-${XDG_CACHE_HOME:-$HOME/.cache}/precis/memory-index.md}"
nodes="${PRECIS_MEMORY_NODES:-${XDG_CACHE_HOME:-$HOME/.cache}/precis/memory-nodes}"

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
snap=""
trap 'rm -f "$errf"; [ -z "$snap" ] || rm -rf "$snap"' EXIT

# Copy (not move) the current set aside: readers keep a complete $nodes at
# every instant, and a failed run just drops the copy.
if [ -d "$nodes" ] && [ -n "$(ls -A "$nodes" 2>/dev/null)" ]; then
    snap="$nodes.snap.$$"
    cp -R "$nodes" "$snap" 2>/dev/null || {
        rm -rf "$snap"
        snap=""
    }
fi

rotate_nodes() {
    [ -n "$snap" ] || return 0
    grep -qF 'memory index: node export' "$errf" && return 0
    [ -s "$nodes/_sections.tsv" ] || return 0
    local mins=$(((${PRECIS_MEMORY_ROTATE_SECS:-21600} + 59) / 60))
    if [ "$mins" -gt 0 ] && [ -n "$(find "$(dirname "$nodes")" -maxdepth 1 \
        -name "$(basename "$nodes").2*" -mmin "-$mins" 2>/dev/null | head -n 1)" ]; then
        return 0
    fi
    local dest="$nodes.$(date -u +%Y%m%dT%H%M%SZ)"
    [ ! -e "$dest" ] || return 0
    mv "$snap" "$dest" 2>/dev/null || return 0
    snap=""
    # Newest 7 stay (UTC stamps sort lexically); older ones go.
    local d
    for d in $(ls -d "$nodes".2* 2>/dev/null | sort -r | tail -n +8); do
        rm -rf "$d"
    done
}

cd "$repo" || fail "cannot enter repo root"
out="$("${cli[@]}" memory index --for-hook --export-dir "$nodes" 2>"$errf")"
rc=$?
if [ "$rc" -ne 0 ]; then
    # Last stderr line, connection URLs redacted, capped — one line only.
    reason="$(tail -n 1 "$errf" | sed -E 's#[a-z]+://[^ ]+#<url>#g' | cut -c1-160)"
    fail "precis memory index exited $rc${reason:+ ($reason)}"
fi
if [ -z "$out" ]; then
    fail "precis memory index printed nothing"
fi
rotate_nodes
gripes="$("${cli[@]}" gripes --summary 2>/dev/null | tail -n 1)" || gripes=""
case "$gripes" in
"gripes: "*) out="$out"$'\n'"$gripes" ;;
esac
printf '%s\n' "$out"
# A successful render can still carry the CLI's one-line export failure.
grep -F 'memory index: node export' "$errf" | tail -n 1 | cut -c1-300 >&2
# Save the last-good copy; per-process temp + rename, so sessions starting
# together never read a half-written file. A failed save is not an error.
if mkdir -p "$(dirname "$cache")" 2>/dev/null &&
    printf '%s\n' "$out" >"$cache.tmp.$$" 2>/dev/null; then
    mv -f "$cache.tmp.$$" "$cache" 2>/dev/null || rm -f "$cache.tmp.$$"
fi
exit 0
