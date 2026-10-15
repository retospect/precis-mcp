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

    local overlay node addr
    overlay="$(resolve_overlay)" || return 2

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

# A scalar `all.vars` value — `postgres_host`, `nfs_server`, `shared_mount`,
# the role-named aliases — read out of the overlay. Same loose lookup shape
# as the postgres_host line in resolve_pgb_host: the first `<name>: <value>`
# anywhere in hosts.yml wins (in practice these keys live only in all.vars).
resolve_inventory_var() {
    local name="$1" overlay val
    overlay="$(resolve_overlay)" || return 2
    val="$(awk -v k="${name}:" '
                $1 == k { gsub(/["\047]/, "", $2); print $2; exit }' "$overlay")"
    if [ -z "$val" ]; then
        echo "inventory: no '${name}:' var in $overlay." >&2
        return 2
    fi
    printf '%s\n' "$val"
}

# Host names of one inventory group (`compute`, `linux`, `vllm_serving`, …),
# one per line, in file order. Empty output when the group is absent or has
# `hosts: {}` — callers decide whether that is an error, so this stays quiet.
inventory_group_hosts() {
    local group="$1" overlay
    overlay="$(resolve_overlay)" || return 2
    awk -v want="${group}:" '
        done { next }
        $1 == want {
            match($0, /^[[:space:]]*/); gind = RLENGTH
            ing = 1; next }
        ing {
            if ($0 ~ /^[[:space:]]*($|#)/) next
            match($0, /^[[:space:]]*/); ind = RLENGTH
            if (ind <= gind) { ing = 0; done = 1; next }
            if ($1 == "hosts:") { hosts = 1; hind = ind; next }
            if (ind <= hind) hosts = 0
            if (hosts && ind > hind) {
                if (hent == 0) hent = ind
                if (ind == hent) { n = $1; sub(/:$/, "", n); print n } } }' \
        "$overlay"
}

# Every inventory host as "<name> <ansible_host>" lines, in file order, once
# each: a `<name>:` key whose block carries an `ansible_host:`.
list_cluster_hosts() {
    local overlay
    overlay="$(resolve_overlay)" || return 2
    awk '
        /^[[:space:]]*[A-Za-z0-9_.-]+:[[:space:]]*$/ {
            key = $1; sub(/:$/, "", key); next }
        /^[[:space:]]*ansible_host:[[:space:]]*/ && key != "" {
            addr = $2; gsub(/["\047]/, "", addr)
            if (!(key in seen)) { seen[key] = 1; print key, addr }
            key = "" }' "$overlay"
}

# Path of the gitignored overlay `deploy/inventory/hosts.yml`, or fail loudly.
# $CLUSTER_INVENTORY overrides (tests point it at a fixture).
resolve_overlay() {
    local repo overlay common main
    if [ -n "${CLUSTER_INVENTORY:-}" ]; then
        printf '%s\n' "$CLUSTER_INVENTORY"
        return 0
    fi
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
    printf '%s\n' "$overlay"
}
