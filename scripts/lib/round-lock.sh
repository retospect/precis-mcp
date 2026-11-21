# shellcheck shell=bash
# bash 3.2 floor (stock macOS): no declare -A, mapfile, ${x,,}, &>, |&, coproc, globstar — tests/test_bash32_gotchas.py
# Release lifecycle only. Main ships do not take this lock. fd 9 stays open
# in the shell: flock attaches to its shared open-file description, so the
# Python child's exit does not unlock it; shell exit/crash does. Never unlink
# the lock file (that would let another inode admit a second holder).
#
# The lock file and precis-round/round.json live in the git common dir, which
# scripts/round (run in the host checkout) shares with every linked worktree.
# A standalone clone (a factory attempt) has a common dir of its own, so there
# the lock would exclude nothing and round.json would read as absent: a
# release fix would pass the "deployment journal owns the head" check.
# PRECIS_ROUND_STATE_ROOT names the directory to use instead (the host
# checkout's git common dir); unset or empty, the common dir is used as before.
# round_state_root fails (rc 1) when the override is not a directory, rather
# than falling back to a clone-local path that coordinates nothing. A factory
# attempt clone (<host>/.claude/worktrees/slice-td<N>[-land]-j<M>) run without
# the override derives the host's common dir from its own path, or fails too.
_round_attempt_host() { # -> host root on stdout; rc 1 when not an attempt clone
    local top base parent
    top="$(git rev-parse --show-toplevel 2>/dev/null)" || return 1
    [[ -d "$top/.git" ]] || return 1
    base="${top##*/}"
    [[ "$base" =~ ^slice-td[0-9]+(-land)?-j[0-9]+$ ]] || return 1
    parent="${top%/*}"
    [[ "$parent" == */.claude/worktrees ]] || return 1
    printf '%s\n' "${parent%/.claude/worktrees}"
}

round_state_root() {
    local host common
    if [[ -n "${PRECIS_ROUND_STATE_ROOT:-}" ]]; then
        if [[ ! -d "$PRECIS_ROUND_STATE_ROOT" ]]; then
            printf 'PRECIS_ROUND_STATE_ROOT=%s is not an existing directory — create it or unset the variable\n' \
                "$PRECIS_ROUND_STATE_ROOT" >&2
            return 1
        fi
        printf '%s\n' "$PRECIS_ROUND_STATE_ROOT"
        return 0
    fi
    if host="$(_round_attempt_host)"; then
        if [[ -n "$host" && -e "$host/.git" ]] \
            && common="$(git -C "$host" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)" \
            && [[ -d "$common" ]]; then
            printf '%s\n' "$common"
            return 0
        fi
        printf "this is an attempt clone and its host checkout's git dir cannot be derived from its path (host root '%s') — set PRECIS_ROUND_STATE_ROOT to the host's git common dir\n" \
            "$host" >&2
        return 1
    fi
    git rev-parse --path-format=absolute --git-common-dir
}

acquire_round_lock() {
    local common
    common="$(round_state_root)" || return 1
    exec 9>"${common}/precis-round-lifecycle.lock"
    python3 -c 'import fcntl; fcntl.flock(9, fcntl.LOCK_EX | fcntl.LOCK_NB)' 2>/dev/null || {
        exec 9>&-
        printf 'release lifecycle busy — a cut, deploy, abandon or release fix is running; retry after it completes\n' >&2
        return 1
    }
}

release_round_lock() { exec 9>&-; }

# Main's gate keeps its local pin, but a release owns the global warrant.
# Nonblocking: this runs AFTER the ship lock is released. Atomic with cut;
# an unreadable state is a refusal to publish, never permission to race.
acquire_main_gated_publication() {
    local common reader
    reader="$(dirname "${BASH_SOURCE[0]}")/round_state.py"
    acquire_round_lock || return 1
    common="$(round_state_root)" || return 1
    if ! python3 "$reader" "$common/precis-round/round.json" publication
    then
        release_round_lock
        printf 'main gate keeps its local pin; global gated publication deferred while a release is recorded\n' >&2
        return 1
    fi
}

# A retained attempt owns the release even after its controller exits.
# $1 names the release branch the caller is about to mutate — only that
# record's pending cut or live deploy journal blocks; a different record's
# journal does not.
release_journal_clear() {
    local common reader
    reader="$(dirname "${BASH_SOURCE[0]}")/round_state.py"
    common="$(round_state_root)" || return 1
    python3 "$reader" "$common/precis-round/round.json" journal "${1:-}"
}
