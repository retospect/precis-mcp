#!/usr/bin/env bash
# scripts/lib/deploy-state.sh — where the "what is the cluster running" marker
# lives, for scripts/deploy (writer) and scripts/ship (reader).
#
# The bug this fixes: the marker used to be `<worktree>/.deploy-state`, i.e.
# per-worktree. But the fact it records — the sha the FLEET is running — is
# global. Ship one tree, deploy from another, and the first tree's marker never
# moves, so its lag report counts from a sha that was deployed long ago. Seen
# 2026-09-08: ship announced "39 commit(s) on main not yet deployed (oldest 57h
# ago)" while every host's venv actually had a sha only 3 commits behind main.
# An over-reported lag is not harmless — that line is what you read to decide
# whether to deploy, so it manufactures urgency and trains you to ignore it.
#
# Fix: keep it in the git COMMON dir, which every worktree of the repo shares.
# Any worktree's deploy is then visible to every worktree's ship. It also sits
# outside the working tree, so it needs no .gitignore entry and cannot be
# swept by a `git clean`.
#
# gr332009 (2026-09-09): two more honesty rules.
#   • No legacy fallback. The read path used to fall back to the old
#     per-worktree `.deploy-state` when the shared marker was absent — which
#     resurrected week-old shas and fabricated a 118-commit lag the day after
#     a real deploy. An absent marker now means "no successful deploy on
#     record", and ship says exactly that instead of counting.
#   • Attempt stamp. scripts/deploy writes `<target-sha> <epoch>` to the
#     attempt path the moment it starts touching hosts; a green deploy
#     replaces it with the success marker and removes the stamp. A stamp with
#     no newer marker = the last deploy never recorded success (died red on a
#     task, or is still running) — the fleet's sha is UNKNOWN, and ship
#     reports that. (Trigger case: the balthazar sandbox podman-pull residual
#     kept every deploy red at its tail, so the success marker was never
#     written and lag reports counted from ancient per-worktree markers.)
#
# Usage:  . "$(dirname "$0")/lib/deploy-state.sh"
#         path="$(deploy_state_path "$REPO_ROOT")"

# Echo the shared success-marker path. Falls back to a repo-root path when git
# is unavailable (e.g. a tarball checkout) so callers always get one.
deploy_state_path() {
    local root="${1:-$PWD}" common
    common="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null || true)"
    if [[ -z "$common" ]]; then
        printf '%s\n' "${root}/.deploy-state"
        return 0
    fi
    case "$common" in /*) ;; *) common="${root}/${common}" ;; esac
    printf '%s\n' "${common}/precis-deploy-state"
}

# Echo the shared attempt-stamp path (same location rules as the marker).
deploy_attempt_path() {
    local root="${1:-$PWD}" common
    common="$(git -C "$root" rev-parse --git-common-dir 2>/dev/null || true)"
    if [[ -z "$common" ]]; then
        printf '%s\n' "${root}/.deploy-attempt"
        return 0
    fi
    case "$common" in /*) ;; *) common="${root}/${common}" ;; esac
    printf '%s\n' "${common}/precis-deploy-attempt"
}

# Echo the success-marker path iff it exists. No legacy fallback (gr332009):
# a missing shared marker means "no successful deploy on record", not "go
# find an older file to count from".
deploy_state_read_path() {
    local root="${1:-$PWD}" shared
    shared="$(deploy_state_path "$root")"
    if [[ -f "$shared" ]]; then
        printf '%s\n' "$shared"
    fi
}
