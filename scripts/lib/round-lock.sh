# shellcheck shell=bash
# Release lifecycle only. Main ships do not take this lock. fd 9 stays open
# in the shell: flock attaches to its shared open-file description, so the
# Python child's exit does not unlock it; shell exit/crash does. Never unlink
# the lock file (that would let another inode admit a second holder).
acquire_round_lock() {
    local common
    common="$(git rev-parse --path-format=absolute --git-common-dir)" || return 1
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
    common="$(git rev-parse --path-format=absolute --git-common-dir)" || return 1
    if ! python3 "$reader" "$common/precis-round/round.json" publication
    then
        release_round_lock
        printf 'main gate keeps its local pin; global gated publication deferred while a release is recorded\n' >&2
        return 1
    fi
}

# A retained attempt owns the release even after its controller exits.
release_journal_clear() {
    local common reader
    reader="$(dirname "${BASH_SOURCE[0]}")/round_state.py"
    common="$(git rev-parse --path-format=absolute --git-common-dir)" || return 1
    python3 "$reader" "$common/precis-round/round.json" journal
}
