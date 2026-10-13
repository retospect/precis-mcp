#!/usr/bin/env bash
# scripts/lib/node-host.sh — resolve a cluster node's address from the
# gitignored overlay (deploy/inventory/hosts.yml), never from a literal.
# Mirrors scripts/lib/pgb-host.sh, including its worktree → main-checkout
# fallback (the overlay exists only in the primary checkout).
#
# Usage:  . "$(dirname "$0")/lib/node-host.sh"; ADDR="$(resolve_node_host castor)"
#         NODE_HOST_<NAME>=<addr> (upper-cased node name) overrides the lookup.

resolve_node_host() {
    local node="$1" var repo overlay addr common main
    var="NODE_HOST_$(printf '%s' "$node" | tr '[:lower:]-' '[:upper:]_')"
    if [ -n "${!var:-}" ]; then
        printf '%s\n' "${!var}"
        return 0
    fi

    repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
    overlay="$repo/deploy/inventory/hosts.yml"
    if [ ! -f "$overlay" ]; then
        common="$(cd "$repo" && git rev-parse --git-common-dir 2>/dev/null || true)"
        if [ -n "$common" ]; then
            case "$common" in /*) ;; *) common="$repo/$common" ;; esac
            main="$(cd "$common/.." 2>/dev/null && pwd || true)"
            [ -n "$main" ] && [ -f "$main/deploy/inventory/hosts.yml" ] &&
                overlay="$main/deploy/inventory/hosts.yml"
        fi
    fi
    if [ ! -f "$overlay" ]; then
        echo "node-host: no $overlay and \$$var unset (gitignored overlay; docs/runbooks/INDEX.md)." >&2
        return 2
    fi

    addr="$(awk -v want="$node:" '
                $1 == want { found = 1; next }
                found && /^[[:space:]]*ansible_host:[[:space:]]*/ {
                    gsub(/["\047]/, "", $2); print $2; exit }' "$overlay")"
    if [ -z "$addr" ]; then
        echo "node-host: no ansible_host for '$node' in $overlay." >&2
        return 2
    fi
    printf '%s\n' "$addr"
}
