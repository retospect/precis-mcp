#!/usr/bin/env bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# scripts/lib/overlay-sync.sh — keep the private deploy overlay
# (deploy/inventory, its own git repo) identical to its shared remote BEFORE
# anything renders from it (gr680328).
#
# Why: on 2026-11-14 the controller's overlay copy had forked from the shared
# `deploy-helper` remote weeks earlier — both sides had committed on different
# bases and nothing compared them. A controller-run deploy then rendered the
# pgbouncer userlist and the workers' .pgpass without the precis_system role,
# and the gateway's worker agent lost DB auth (gr678654). The fork surfaced
# only as that outage. This preflight makes every deploy and every host-ops
# play reconcile the two copies first, so the copies cannot drift apart
# unnoticed again.
#
# Contract — overlay_sync_preflight <overlay-dir> [<label>]:
#   • <overlay-dir> is not the top of its own git repo (a plain directory, the
#     documented default for other operators — or a dir merely INSIDE the
#     precis-mcp checkout), or has no `deploy-helper` remote: say so, return 0.
#   • uncommitted tracked changes, or untracked files ansible would load
#     (*.yml, *.yaml, *.json, extensionless; hidden and `~` names skipped):
#     REFUSE, return 1 — they render on this host and reach nobody else.
#   • fetch <remote> <branch> (ssh in batch mode, bounded connect timeout,
#     no terminal prompts). A failed fetch is a WARNING: the relation is then
#     judged against the last fetched remote ref, and nothing is pushed or
#     merged.
#   • identical: say so.  behind: fast-forward the local copy.  ahead: push
#     (fast-forward on the remote; a rejected push refuses).  diverged:
#     REFUSE, return 1, printing the two-sided log command.
#   • PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT=1: report everything, mutate nothing,
#     never refuse (the operator has looked and wants this copy as-is).
# Prints shas, counts, file names and the relation — never file contents.
#
# Env: PRECIS_OVERLAY_REMOTE (default deploy-helper), PRECIS_OVERLAY_BRANCH
# (default main), PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT (alias
# PRECIS_DEPLOY_ALLOW_STALE_OVERLAY), GIT_SSH_COMMAND (honoured when set;
# otherwise `ssh -o BatchMode=yes -o ConnectTimeout=15`).
#
# Operator CLI over the same functions: scripts/overlay-sync. Runbook:
# docs/runbooks/cluster-overlay-sync.md.

_ovs_say() { printf 'overlay-sync: %s\n' "$*"; }
_ovs_warn() { printf 'overlay-sync: WARNING: %s\n' "$*" >&2; }
_ovs_refuse() { printf 'overlay-sync: REFUSE: %s\n' "$*" >&2; }

# git with network, non-interactive: no password prompts (a service account
# or a tty-less deploy must fail fast, not hang), bounded ssh connect.
_ovs_git_net() {
    local dir="$1"; shift
    if [ -n "${GIT_SSH_COMMAND:-}" ]; then
        GIT_TERMINAL_PROMPT=0 git -C "$dir" "$@"
    else
        GIT_TERMINAL_PROMPT=0 GIT_SSH_COMMAND='ssh -o BatchMode=yes -o ConnectTimeout=15' \
            git -C "$dir" "$@"
    fi
}

# Filter stdin paths to the ones ansible's vars loader would read out of a
# group_vars/host_vars tree: *.yml, *.yaml, *.json, or no extension. Any
# hidden component (`.scratch/x.json`, `.DS_Store`) or a `~` backup name is
# skipped — ansible prunes hidden entries at every level of its walk.
_ovs_ansible_loadable() {
    awk '{ if ($0 ~ /(^|\/)\./ || $0 ~ /~$/) next
           n = $0; sub(/.*\//, "", n)
           if (n ~ /\.(yml|yaml|json)$/ || n !~ /\./) print $0 }'
}

# True when <dir> is the TOP of its own git work tree. A gitignored plain
# directory inside the precis-mcp checkout resolves to the checkout's top, not
# to itself, so this also keeps every function here off the code repo.
overlay_sync_is_repo() {
    local dir="$1" top real
    [ -d "$dir" ] || return 1
    top="$(git -C "$dir" rev-parse --show-toplevel 2>/dev/null)" || return 1
    real="$(cd "$dir" 2>/dev/null && pwd -P)" || return 1
    top="$(cd "$top" 2>/dev/null && pwd -P)" || return 1
    [ "$real" = "$top" ]
}

# `<ahead> <behind>` of HEAD relative to <remote>/<branch> (space separated),
# or empty when either side is missing.
overlay_sync_counts() {
    local dir="$1" remote="$2" branch="$3" local_sha remote_sha counts
    local_sha="$(git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null)" || return 0
    remote_sha="$(git -C "$dir" rev-parse --verify -q "refs/remotes/$remote/$branch" 2>/dev/null)" || return 0
    counts="$(git -C "$dir" rev-list --left-right --count "$local_sha...$remote_sha" 2>/dev/null)" || return 0
    # rev-list prints the two counts tab-separated; normalise to one space.
    printf '%s %s\n' "${counts%%[[:space:]]*}" "${counts##*[[:space:]]}"
}

# Uncommitted state ansible would render: `git status --porcelain` for tracked
# files plus `?? <path>` for loadable untracked files. Empty when clean.
overlay_sync_dirty() {
    local dir="$1" tracked untracked
    tracked="$(git --no-optional-locks -C "$dir" status --porcelain --untracked-files=no 2>/dev/null)"
    untracked="$(git --no-optional-locks -C "$dir" ls-files --others --exclude-standard 2>/dev/null \
        | _ovs_ansible_loadable | sed 's/^/?? /')"
    if [ -n "$tracked" ] && [ -n "$untracked" ]; then
        printf '%s\n%s\n' "$tracked" "$untracked"
    elif [ -n "$tracked$untracked" ]; then
        printf '%s\n' "$tracked$untracked"
    fi
}

overlay_sync_preflight() {
    local dir="$1" label="${2:-overlay}"
    local remote="${PRECIS_OVERLAY_REMOTE:-deploy-helper}" branch="${PRECIS_OVERLAY_BRANCH:-main}"
    # PRECIS_DEPLOY_ALLOW_STALE_OVERLAY is the name the first (die-only) guard
    # in scripts/deploy shipped with on 2026-11-14; both spellings override.
    local allow="${PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT:-${PRECIS_DEPLOY_ALLOW_STALE_OVERLAY:-}}"
    local fetched=1 dirty counts ahead behind local_sha remote_sha

    if ! overlay_sync_is_repo "$dir"; then
        _ovs_say "$label at $dir is not its own git repo — nothing to sync (docs/runbooks/cluster-overlay-sync.md)"
        return 0
    fi
    if ! git -C "$dir" remote get-url "$remote" >/dev/null 2>&1; then
        _ovs_say "$label has no '$remote' remote — nothing to sync"
        return 0
    fi

    dirty="$(overlay_sync_dirty "$dir")"
    if [ -n "$dirty" ]; then
        if [ -n "$allow" ]; then
            _ovs_warn "$label at $dir has uncommitted changes (PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT set — rendering them as-is):"
            printf '%s\n' "$dirty" | sed 's/^/        /' >&2
        else
            _ovs_refuse "$label at $dir has uncommitted changes — they would render here and reach nobody else:"
            printf '%s\n' "$dirty" | sed 's/^/        /' >&2
            printf '        commit and share them:  scripts/overlay-sync commit -m "<what changed>" && scripts/overlay-sync push\n' >&2
            printf '        or render this copy once as-is:  PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT=1\n' >&2
            return 1
        fi
    fi

    if ! _ovs_git_net "$dir" fetch -q "$remote" "$branch"; then
        fetched=0
        _ovs_warn "could not fetch $remote/$branch — judging against the last fetched state; nothing will be pushed or merged"
    fi

    local_sha="$(git -C "$dir" rev-parse --verify -q HEAD 2>/dev/null)" || local_sha=""
    remote_sha="$(git -C "$dir" rev-parse --verify -q "refs/remotes/$remote/$branch" 2>/dev/null)" || remote_sha=""
    if [ -z "$local_sha" ] || [ -z "$remote_sha" ]; then
        _ovs_warn "$label: no commit on one side (local ${local_sha:-none}, $remote/$branch ${remote_sha:-never fetched}) — cannot judge the relation; using this copy as-is"
        return 0
    fi
    counts="$(overlay_sync_counts "$dir" "$remote" "$branch")"
    ahead="${counts%% *}"; behind="${counts##* }"
    if [ -z "$ahead" ] || [ -z "$behind" ]; then
        _ovs_warn "$label: could not count commits between ${local_sha:0:9} and $remote/$branch ${remote_sha:0:9} — using this copy as-is"
        return 0
    fi

    if [ "$ahead" = 0 ] && [ "$behind" = 0 ]; then
        _ovs_say "$label in sync with $remote/$branch (${local_sha:0:9})"
        return 0
    fi
    if [ "$ahead" != 0 ] && [ "$behind" != 0 ]; then
        if [ -n "$allow" ]; then
            _ovs_warn "$label DIVERGED from $remote/$branch ($ahead local-only, $behind remote-only commits; PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT set — rendering this copy as-is)"
            return 0
        fi
        _ovs_refuse "$label at $dir has DIVERGED from $remote/$branch: $ahead commit(s) only here, $behind only there (the 2026-11-14 outage shape, gr680328)."
        printf '        see both sides:  scripts/overlay-sync log\n' >&2
        printf '        reconcile per docs/runbooks/cluster-overlay-sync.md, then re-run; or render this copy once as-is: PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT=1\n' >&2
        return 1
    fi
    if [ "$behind" != 0 ]; then
        if [ -n "$allow" ]; then
            _ovs_warn "$label is $behind commit(s) behind $remote/$branch (PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT set — not fast-forwarding)"
            return 0
        fi
        if [ "$fetched" = 0 ]; then
            _ovs_warn "$label is $behind commit(s) behind the last fetched $remote/$branch; remote unreachable, so not fast-forwarding — rendering this copy as-is"
            return 0
        fi
        if git -C "$dir" merge -q --ff-only "$remote_sha" >/dev/null; then
            _ovs_say "$label fast-forwarded ${local_sha:0:9} → ${remote_sha:0:9} ($behind commit(s) from $remote/$branch)"
            return 0
        fi
        _ovs_refuse "$label could not fast-forward to $remote/$branch ${remote_sha:0:9} (git merge --ff-only failed)"
        return 1
    fi
    # ahead only: the remote lags this copy — share it, fast-forward only.
    if [ -n "$allow" ]; then
        _ovs_warn "$label is $ahead commit(s) ahead of $remote/$branch (PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT set — not pushing)"
        return 0
    fi
    if [ "$fetched" = 0 ]; then
        _ovs_warn "$label is $ahead commit(s) ahead of the last fetched $remote/$branch; remote unreachable, so not pushing — rendering this copy as-is"
        return 0
    fi
    # git's own stderr stays visible: a rejected push names its reason.
    if _ovs_git_net "$dir" push -q "$remote" "HEAD:refs/heads/$branch" >/dev/null; then
        _ovs_say "$label pushed $ahead commit(s) to $remote/$branch (${remote_sha:0:9} → ${local_sha:0:9})"
        return 0
    fi
    _ovs_refuse "$label is $ahead commit(s) ahead of $remote/$branch and the push was rejected — the remote's work tree is probably dirty (docs/runbooks/cluster-overlay-sync.md, 'remote rejected')."
    printf '        or render this copy once as-is: PRECIS_DEPLOY_ALLOW_OVERLAY_DRIFT=1\n' >&2
    return 1
}
