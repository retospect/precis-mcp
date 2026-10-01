#!/usr/bin/env bash
# scripts/lib/env-pointers.sh — the two moving refs that say where a commit
# has got to, for scripts/ship (writer of `gated`) and scripts/deploy (writer
# of `prod`).
#
#   main   landed, possibly untested          moved by a land
#   gated  last sha the full gate passed      moved by scripts/ship, on green
#   prod   what the cluster runs              moved by scripts/deploy, on success
#
# Both are branches on origin that nobody commits to: a script picks a commit
# already on main and moves the name to it. They exist because the facts they
# record used to be local files — `.ship-sha` in ONE worktree and the
# deploy-state marker in ONE machine's git dir — so no other session, and
# nobody looking at GitHub, could tell which sha was gated or deployed.
# Those files stay: they are the guards' inputs (the unconsumed-pin check, the
# rollback guard). The refs are the published copy.
#
# Fast-forward only. A plain push refuses a non-fast-forward, which is the
# rule wanted: a stale gate cannot drag `gated` backward, and `prod` goes
# backward only on a deliberate `scripts/deploy --force-rollback`. `prod` may
# sit AHEAD of `gated` — that is /qgo, which deploys ungated — and the gap is
# the visible sign the cluster is running code no gate has passed.
#
# Best-effort by contract: moving a pointer never fails a gate that passed or
# a deploy that converged. Every failure is one warning line on stderr.
#
# PRECIS_ENV_POINTERS=0 turns both off (throwaway repos, rigs).
#
# Usage:  . "$(dirname "$0")/lib/env-pointers.sh"
#         env_pointer_move "$REPO_ROOT" prod "$sha"          # fast-forward only
#         env_pointer_move "$REPO_ROOT" prod "$sha" force    # deliberate rollback
#         env_prod_checkout_sync "$REPO_ROOT" "$sha"         # the served clone

# Move origin's <branch> to <sha>. Returns 0 when the ref now names <sha>,
# 1 otherwise (skipped, no origin, unknown sha, push refused). Never exits.
env_pointer_move() {
    local root="$1" branch="$2" sha="$3" mode="${4:-}" spec
    [[ "${PRECIS_ENV_POINTERS:-1}" != 0 ]] || return 1
    [[ -n "$root" && -n "$branch" && "$sha" =~ ^[0-9a-f]{40}$ ]] || return 1
    git -C "$root" remote get-url origin >/dev/null 2>&1 || return 1
    git -C "$root" cat-file -e "${sha}^{commit}" 2>/dev/null || {
        printf 'warning: %s pointer not moved — %s is not a commit in this checkout\n' "$branch" "${sha:0:8}" >&2
        return 1
    }
    spec="${sha}:refs/heads/${branch}"
    [[ "$mode" == force ]] && spec="+${spec}"
    if git -C "$root" push -q origin "$spec" >/dev/null 2>&1; then
        return 0
    fi
    printf 'warning: %s pointer not moved to %s — the push was refused (not a fast-forward, or origin unreachable); the local marker is still correct\n' "$branch" "${sha:0:8}" >&2
    return 1
}

# Echo the directory of the prod clone: PRECIS_PROD_CHECKOUT, else the main
# checkout's path with `-prod` appended (a sibling directory).
env_prod_checkout_dir() {
    local root="${1:-$PWD}" common
    if [[ -n "${PRECIS_PROD_CHECKOUT:-}" ]]; then
        printf '%s\n' "$PRECIS_PROD_CHECKOUT"
        return 0
    fi
    common="$(git -C "$root" rev-parse --path-format=absolute --git-common-dir 2>/dev/null || true)"
    [[ -n "$common" ]] || return 1
    printf '%s-prod\n' "$(dirname "$common")"
}

# Put the prod clone on <sha>, detached. The clone is what the shared session
# MCP serves (its ensure script mounts it), so "the session server runs what
# the cluster runs" holds only while every deploy moves it. A PLAIN clone, not
# a `git worktree`: the server's checkout watchdog reads .git/HEAD inside a
# container, where a worktree's .git pointer leads nowhere.
#
# No clone on this machine -> nothing to do, quietly (returns 1): most
# machines that deploy do not host the session server. A clone with local
# edits is left alone and warned about — it is nobody's working tree, so
# edits there mean something is wrong, and discarding them is not this
# function's call. Never exits.
env_prod_checkout_sync() {
    local root="$1" sha="$2" dir
    [[ "${PRECIS_ENV_POINTERS:-1}" != 0 ]] || return 1
    [[ "$sha" =~ ^[0-9a-f]{40}$ ]] || return 1
    dir="$(env_prod_checkout_dir "$root")" || return 1
    [[ -f "${dir}/.git/HEAD" ]] || return 1
    if ! git -C "$dir" cat-file -e "${sha}^{commit}" 2>/dev/null; then
        git -C "$dir" fetch -q origin >/dev/null 2>&1 || true
    fi
    if git -C "$dir" checkout -q --detach "$sha" >/dev/null 2>&1; then
        return 0
    fi
    printf 'warning: prod clone %s not moved to %s — fetch failed or the clone has local edits; the session server keeps serving its old sha\n' "$dir" "${sha:0:8}" >&2
    return 1
}
