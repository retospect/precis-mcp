#!/usr/bin/env bash
# scripts/lib/node-host.sh — resolve a cluster node's address from the
# gitignored overlay (deploy/inventory/hosts.yml), never from a literal.
# Mirrors scripts/lib/pgb-host.sh, including its worktree → main-checkout
# fallback (the overlay exists only in the primary checkout).
#
# Usage:  . "$(dirname "$0")/lib/node-host.sh"; ADDR="$(resolve_node_host castor)"
#         NODE_HOST_<NAME>=<addr> (upper-cased node name) overrides the lookup.
#         resolve_node_var <node> <key> reads any per-host key (lan_ip, …);
#         its override is NODE_<KEY>_<NAME> (upper-cased).

_NH_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/pgb-host.sh
. "${_NH_HERE}/pgb-host.sh"

resolve_node_var() {
    local node="$1" key="${2:-ansible_host}" var overlay addr
    var="NODE_$(printf '%s_%s' "$key" "$node" | tr '[:lower:]-' '[:upper:]_')"
    if [ -n "${!var:-}" ]; then
        printf '%s\n' "${!var}"
        return 0
    fi

    overlay="$(resolve_overlay 2>/dev/null)" || {
        echo "node-host: no overlay and \$$var unset (gitignored overlay; docs/runbooks/INDEX.md)." >&2
        return 2; }

    # First `<key>:` inside the node's own block — a deeper-indented key on a
    # later host block must not leak into this lookup (same-name keys like
    # lan_ip repeat per host).
    addr="$(awk -v want="$node:" -v k="$key:" '
                $1 == want {
                    match($0, /^[[:space:]]*/); ni = RLENGTH; found = 1; next }
                found {
                    if ($0 ~ /^[[:space:]]*$/) next
                    match($0, /^[[:space:]]*/); ind = RLENGTH
                    if (ind <= ni) found = 0
                    else if ($1 == k) {
                        gsub(/["\047]/, "", $2); print $2; exit } }' "$overlay")"
    if [ -z "$addr" ]; then
        echo "node-host: no $key for '$node' in $overlay." >&2
        return 2
    fi
    printf '%s\n' "$addr"
}

resolve_node_host() {
    local node="$1" var
    var="NODE_HOST_$(printf '%s' "$node" | tr '[:lower:]-' '[:upper:]_')"
    if [ -n "${!var:-}" ]; then
        printf '%s\n' "${!var}"
        return 0
    fi
    resolve_node_var "$node" ansible_host
}
