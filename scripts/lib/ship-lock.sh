# shellcheck shell=bash
# scripts/lib/ship-lock.sh — per-WORKTREE "one scripts/ship at a time here"
# guard (gr335894). Sourced by scripts/ship only.
#
# Incident: a long `--impacted` full-gate ship and a later `--quick` ship ran
# concurrently from the SAME worktree. scripts/ship's repo-wide squash lock
# (mkdir-mutex under --git-common-dir, see scripts/ship §3) only serialises
# the final sync→squash→push section, and is taken well after the WIP commit
# — so two ships in one worktree could still race each other's `git add -A
# && git commit`, sync merge, and branch reset before either ever touched
# that lock. The quick ship's squash landed a tree that predated a sibling
# worktree's already-merged change, silently reverting 45 lines with no
# conflict and no red gate (the squash's `TREE=$(git rev-parse
# HEAD^{tree})` was read after a concurrent reset had rewritten the branch
# out from under it).
#
# This lock is keyed off `git rev-parse --absolute-git-dir`, which is unique
# PER LINKED WORKTREE (unlike --git-common-dir, shared by every worktree) —
# so it catches exactly the same-worktree double-run the squash lock can't,
# without serialising unrelated worktrees against each other. It is taken
# immediately at run start (right after argument parsing, before the first
# git write) and held for the whole run, released on EXIT via a trap the
# caller arms.
#
# Unlike the squash lock, this one REFUSES rather than waits: two unrelated
# ships queuing for the squash lock both deserve to land eventually, but two
# ships racing the same branch in the same worktree is never a case where
# the second one should proceed — it should stop, and the operator decides
# whether to wait for the first or kill it. Stale-lock detection still
# applies (a crashed ship must not wedge every future one): reuses
# lock_holder_reclaim_reason from lock-holder.sh, so a dead holder pid is
# stolen immediately and the run proceeds normally.

SHIP_WORKTREE_LOCKDIR=""

# _release_worktree_ship_lock — idempotent; removes the lock dir only if this
# process is still the recorded holder (a sibling that stole the lock after
# some pathological delay must not have its fresh lock deleted by our late
# exit — same ownership-checked-release rule as the squash lock and the gate
# slot semaphore).
_release_worktree_ship_lock() {
    [[ -n "$SHIP_WORKTREE_LOCKDIR" ]] || return 0
    local holder_pid
    holder_pid="$(lock_holder_pid "$SHIP_WORKTREE_LOCKDIR")"
    if [[ "$holder_pid" == "$$" ]]; then
        rm -rf "$SHIP_WORKTREE_LOCKDIR" 2>/dev/null || true
    fi
    return 0
}

# acquire_worktree_ship_lock <worktree-dir>
#
# On success: creates the lock, stamps it with pid/host/start-time, and
# returns 0. The caller must arm `trap _release_worktree_ship_lock EXIT` (or
# fold it into a combined trap alongside any other lock's release) — this
# function does not touch `trap` itself, so it composes cleanly with a lock
# acquired later in the same script.
#
# On failure (another live ship holds it): prints the refusal message to
# stderr and returns 1. Never loops or waits — requires lock_holder_* from
# lock-holder.sh to already be sourced.
acquire_worktree_ship_lock() {
    local worktree="$1" gitdir holder holder_pid holder_started reason
    gitdir="$(git -C "$worktree" rev-parse --absolute-git-dir 2>/dev/null)" \
        || { printf '\n\033[31m✖ could not resolve --absolute-git-dir for %s — refusing to ship without the worktree lock.\033[0m\n' "$worktree" >&2; return 1; }
    SHIP_WORKTREE_LOCKDIR="${gitdir}/precis-ship-worktree.lock.d"

    if ! mkdir "$SHIP_WORKTREE_LOCKDIR" 2>/dev/null; then
        if reason="$(lock_holder_reclaim_reason "$SHIP_WORKTREE_LOCKDIR" 30)"; then
            holder="$(cat "$SHIP_WORKTREE_LOCKDIR/holder" 2>/dev/null || true)"
            printf '\n\033[33m⚠ stale per-worktree ship lock (%s: %s) — stealing it.\033[0m\n' \
                "$reason" "${holder:-<no holder file>}" >&2
            rm -rf "$SHIP_WORKTREE_LOCKDIR"
            if ! mkdir "$SHIP_WORKTREE_LOCKDIR" 2>/dev/null; then
                printf '\n\033[31m✖ could not acquire the per-worktree ship lock after stealing it — refusing.\033[0m\n' >&2
                SHIP_WORKTREE_LOCKDIR=""
                return 1
            fi
        else
            holder="$(cat "$SHIP_WORKTREE_LOCKDIR/holder" 2>/dev/null || true)"
            holder_pid="$(lock_holder_pid "$SHIP_WORKTREE_LOCKDIR")"
            holder_started="$(sed -n 's/.*started=\([^[:space:]]*\).*/\1/p' <<< "$holder")"
            printf '\n\033[31m✖ ship already running in this worktree, pid %s, started %s — refusing.\033[0m\n' \
                "${holder_pid:-?}" "${holder_started:-unknown}" >&2
            SHIP_WORKTREE_LOCKDIR=""
            return 1
        fi
    fi

    lock_holder_write "$SHIP_WORKTREE_LOCKDIR"
    printf 'started=%s\n' "$(date +%H:%M)" >> "$SHIP_WORKTREE_LOCKDIR/holder" 2>/dev/null || true
    return 0
}
