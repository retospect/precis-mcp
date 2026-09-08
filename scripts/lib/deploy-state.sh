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
# Usage:  . "$(dirname "$0")/lib/deploy-state.sh"
#         path="$(deploy_state_path "$REPO_ROOT")"

# Echo the shared marker path. Falls back to the legacy per-worktree location
# when git is unavailable (e.g. a tarball checkout) so callers always get one.
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

# Echo the marker path that actually EXISTS, preferring the shared one.
# Lets a ship still read a pre-migration marker written by an older deploy,
# so the lag report does not go silent for one cycle after this lands.
deploy_state_read_path() {
    local root="${1:-$PWD}" shared legacy
    shared="$(deploy_state_path "$root")"
    legacy="${root}/.deploy-state"
    if [[ -f "$shared" ]]; then
        printf '%s\n' "$shared"
    elif [[ -f "$legacy" ]]; then
        printf '%s\n' "$legacy"
    fi
}
