#!/usr/bin/env bash
# scripts/lib/pgb-host.sh — resolve the prod pgbouncer address without ever
# committing it.
#
# The repo is PUBLIC and `tests/test_deploy_tree_no_secrets.py` scans the whole
# tree for tailnet addresses, so the coordinate cannot live in a tracked file.
# It already has a home: the gitignored per-cluster overlay
# `deploy/inventory/hosts.yml`, which is the design-of-record source for
# cluster coordinates. Read it from there.
#
# Resolution order:
#   1. $PGB_HOST            — explicit override, always wins
#   2. deploy/inventory/hosts.yml — `postgres_host:` names the node, that
#                             node's `ansible_host:` is the address
#   3. fail loudly          — never silently fall back to a guess, or you get
#                             a confusing connection error instead of a clear
#                             "your overlay is missing" one
#
# Usage:  . "$(dirname "$0")/lib/pgb-host.sh"; PGB_HOST="$(resolve_pgb_host)"

resolve_pgb_host() {
    if [ -n "${PGB_HOST:-}" ]; then
        printf '%s\n' "$PGB_HOST"
        return 0
    fi

    local repo overlay node addr common main
    repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    overlay="$repo/deploy/inventory/hosts.yml"

    # The overlay is gitignored, so it exists only in the primary checkout —
    # never in a worktree. Sessions run in worktrees and CLAUDE.md points at
    # scripts/prod-psql for ad-hoc SQL, so falling back to the main checkout is
    # the common path, not the edge case.
    if [ ! -f "$overlay" ]; then
        common="$(git -C "$repo" rev-parse --git-common-dir 2>/dev/null || true)"
        if [ -n "$common" ]; then
            case "$common" in /*) ;; *) common="$repo/$common" ;; esac
            main="$(cd "$common/.." 2>/dev/null && pwd || true)"
            [ -n "$main" ] && [ -f "$main/deploy/inventory/hosts.yml" ] &&
                overlay="$main/deploy/inventory/hosts.yml"
        fi
    fi

    if [ ! -f "$overlay" ]; then
        echo "pgb-host: no $overlay and \$PGB_HOST unset." >&2
        echo "  This is the gitignored cluster overlay — see docs/runbooks/INDEX.md." >&2
        echo "  Either sync the overlay or run with PGB_HOST=<addr>." >&2
        return 2
    fi

    # `postgres_host: <node>` under all.vars, then that node's ansible_host.
    node="$(awk '/^[[:space:]]*postgres_host:[[:space:]]*/ {
                     gsub(/["\047]/, "", $2); print $2; exit }' "$overlay")"
    if [ -z "$node" ]; then
        echo "pgb-host: $overlay has no 'postgres_host:' key." >&2
        return 2
    fi

    # First `ansible_host:` after the `<node>:` block header.
    addr="$(awk -v want="$node:" '
                $1 == want { found = 1; next }
                found && /^[[:space:]]*ansible_host:[[:space:]]*/ {
                    gsub(/["\047]/, "", $2); print $2; exit }' "$overlay")"
    if [ -z "$addr" ]; then
        echo "pgb-host: no ansible_host for '$node' in $overlay." >&2
        return 2
    fi

    printf '%s\n' "$addr"
}
