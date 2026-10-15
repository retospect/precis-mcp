#!/usr/bin/env bash
# scripts/lib/cluster-topology.sh — read `precis_capabilities` out of the
# gitignored overlay (deploy/inventory/group_vars/all/*.yml) and render the
# kind-surface env the web gateway gets from
# roles/precis_web/templates/precis-web.plist.j2 — never a committed literal
# (the repo is public; tests/test_deploy_tree_no_secrets.py gates it).
#
# The session-MCP container (deploy/mcp-http/precis-mcp-http-ensure.sh) is the
# same `route`/`protein`/`pathway` mint surface as precis-web, but its env was
# hand-listed, so put(kind='route', engine='aizynth') was refused there while
# the gateway worked (gr489468). It sources this file from the resolved REPO
# checkout — the installed copy lives outside the repo.
#
# Usage:  . "$(dirname "$0")/lib/cluster-topology.sh"
#   capability_nodes chem_route    → "node-gpu" / "node-a,node-b" ("" if undeclared)
#   topology_surface_env           → KEY=VAL lines mirroring the web plist
#
# Resolution is pgb-host.sh's: $CLUSTER_INVENTORY (tests point it at a
# fixture), else <repo>/deploy/inventory/hosts.yml with the worktree →
# main-checkout fallback. Overlay group_vars are the sibling
# group_vars/all/*.yml next to that hosts.yml. A missing overlay fails the
# callers' render (rc 2) — callers on a SessionStart path must degrade to "no
# env", not error.

_CT_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/lib/pgb-host.sh
. "${_CT_HERE}/pgb-host.sh"

# Every `precis_capabilities` entry as "cap<TAB>node1,node2" lines. Handles
# both YAML list spellings used in the overlay (`cap: [a, b]` and the
# `cap:` + indented `- item` block form); a cap repeated across files takes
# the later file's list, matching how ansible's group_vars merge reads.
_ct_capabilities() {
    local overlay gvars
    overlay="$(resolve_overlay)" || return 2
    gvars="$(dirname "$overlay")/group_vars/all"
    if [ ! -d "$gvars" ]; then
        echo "cluster-topology: no $gvars (overlay group_vars)" >&2
        return 2
    fi
    awk '
        FNR == 1 { inmap = 0; cap = "" }
        /^[[:space:]]*precis_capabilities:[[:space:]]*$/ {
            inmap = 1
            match($0, /^[[:space:]]*/); mi = RLENGTH
            next
        }
        !inmap || /^[[:space:]]*($|#)/ { next }
        {
            match($0, /^[[:space:]]*/); ind = RLENGTH
            if (ind <= mi) { inmap = 0; next }
            line = $0
            sub(/#.*$/, "", line)
            if (cap != "" && line ~ /^[[:space:]]*-[[:space:]]/) {
                v = line
                sub(/^[[:space:]]*-[[:space:]]*/, "", v)
                gsub(/["\047[:space:]]/, "", v)
                if (v != "") caps[cap] = caps[cap] (caps[cap] == "" ? "" : ",") v
                next
            }
            if (line ~ /^[[:space:]]*[A-Za-z0-9_]+:([[:space:]]|$)/) {
                key = line
                sub(/^[[:space:]]*/, "", key); sub(/:.*/, "", key)
                rest = line
                sub(/^[[:space:]]*[A-Za-z0-9_]+:[[:space:]]*/, "", rest)
                gsub(/^[[:space:]]+|[[:space:]]+$/, "", rest)
                if (rest ~ /^\[/) {
                    gsub(/[\[\]"'\''[:space:]]/, "", rest)
                    caps[key] = rest
                    cap = ""
                } else {
                    caps[key] = rest
                    cap = (rest == "" ? key : "")
                }
                next
            }
            cap = ""
        }
        END { for (k in caps) if (caps[k] != "") printf "%s\t%s\n", k, caps[k] }
    ' "$gvars"/*.yml
}

# Comma-joined node list for one capability, "" when undeclared.
capability_nodes() {
    _ct_capabilities | awk -F '\t' -v c="$1" '$1 == c { print $2; exit }'
}

# A plain `name: scalar` group_vars value — tracked deploy/group_vars/all.yml
# first, then the overlay's group_vars/all/*.yml (later wins, matching
# inventory-beats-playbook precedence). For the optional tunables the plist
# renders `{% if var is defined %}`; topology lists go through
# _ct_capabilities instead.
topology_var() {
    local name="$1" repo overlay gvars f v val
    repo="$(cd "${_CT_HERE}/../.." && pwd)"
    v=""
    for f in "$repo/deploy/group_vars/all.yml"; do
        [ -f "$f" ] || continue
        val="$(awk -v k="$name:" '
            $1 == k { line = $0
                      sub(/^[^:]*:[[:space:]]*/, "", line)
                      sub(/[[:space:]]*#.*$/, "", line)
                      gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
                      gsub(/^["\047]|["\047]$/, "", line)
                      print line }' "$f" | tail -1)"
        [ -n "$val" ] && v="$val"
    done
    if overlay="$(resolve_overlay 2>/dev/null)"; then
        gvars="$(dirname "$overlay")/group_vars/all"
        for f in "$gvars"/*.yml; do
            [ -f "$f" ] || continue
            val="$(awk -v k="$name:" '
                $1 == k { line = $0
                          sub(/^[^:]*:[[:space:]]*/, "", line)
                          sub(/[[:space:]]*#.*$/, "", line)
                          gsub(/^[[:space:]]+|[[:space:]]+$/, "", line)
                          gsub(/^["\047]|["\047]$/, "", line)
                          print line }' "$f" | tail -1)"
            [ -n "$val" ] && v="$val"
        done
    fi
    printf '%s\n' "$v"
}

# KEY=VAL lines mirroring roles/precis_web/templates/precis-web.plist.j2's
# topology block. A non-empty `<cap>_plugin` list stands in for
# `inventory_hostname in <cap>_plugin`: this container only exists to serve
# the kind surface, so a declared surface anywhere un-darks it here. A
# declared plugin with no compute node still un-darks the kind — put() then
# reports "needs a route node", exactly like the gateway.
topology_surface_env() {
    local map chem_plugin chem_route bio_plugin bio_fold acap_plugin acap dft v
    map="$(_ct_capabilities)" || return 2

    chem_plugin="$(printf '%s\n' "$map" | awk -F '\t' '$1 == "chem_plugin" { print $2; exit }')"
    chem_route="$(printf '%s\n' "$map" | awk -F '\t' '$1 == "chem_route" { print $2; exit }')"
    bio_plugin="$(printf '%s\n' "$map" | awk -F '\t' '$1 == "bio_plugin" { print $2; exit }')"
    bio_fold="$(printf '%s\n' "$map" | awk -F '\t' '$1 == "bio_fold" { print $2; exit }')"
    acap_plugin="$(printf '%s\n' "$map" | awk -F '\t' '$1 == "autocatpath_plugin" { print $2; exit }')"
    acap="$(printf '%s\n' "$map" | awk -F '\t' '$1 == "autocatpath" { print $2; exit }')"
    dft="$(printf '%s\n' "$map" | awk -F '\t' '$1 == "dft" { print $2; exit }')"

    if [ -n "$acap_plugin" ] && [ -n "$acap" ]; then
        printf 'PRECIS_AUTOCATPATH_ROUTE_NODE=%s\n' "$acap"
        v="$(topology_var precis_autocatpath_verify_wall_seconds)"
        [ -n "$v" ] && printf 'PRECIS_AUTOCATPATH_VERIFY_WALL_SECONDS=%s\n' "$v"
        v="$(topology_var precis_autocatpath_seed_cpuset)"
        [ -n "$v" ] && printf 'PRECIS_AUTOCATPATH_SEED_CPUSET=%s\n' "$v"
    fi
    if [ -n "$chem_plugin" ]; then
        printf 'PRECIS_CHEM_ENABLED=1\n'
        [ -n "$chem_route" ] && printf 'PRECIS_CHEM_ROUTE_NODE=%s\n' "${chem_route%%,*}"
    fi
    if [ -n "$bio_plugin" ]; then
        printf 'PRECIS_BIO_ENABLED=1\n'
        [ -n "$bio_fold" ] && printf 'PRECIS_FOLD_NODE=%s\n' "${bio_fold%%,*}"
    fi
    [ -n "$dft" ] && printf 'PRECIS_DFT_NODE=%s\n' "${dft%%,*}"
    return 0
}
