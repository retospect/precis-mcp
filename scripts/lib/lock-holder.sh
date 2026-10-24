# shellcheck shell=bash
# scripts/lib/lock-holder.sh — who holds a mkdir-mutex, when it may be
# stolen, how to steal it atomically, and how to release only your own.
# Sourced by scripts/ship (the ship lock), scripts/lib/ship-lock.sh (the
# per-worktree ship lock), scripts/lib/gate-slot.sh (the N-slot gate
# semaphore) and scripts/deploy (the deploy lock).
#
# Both locks had the same defect, written independently — hence one shared
# rule here rather than a third copy. Each checked "holder pid dead → steal"
# and then, as a documented *fallback for when the pid check cannot decide*,
# "held over N min → steal". But the dead-pid branch consumes the only case
# where the pid check fails, so the age branch was reached exactly when the
# holder was alive and parseable. Every hold longer than N minutes was stolen
# from a live process — and `ship --mutate` plus a full gate routinely runs
# 70+ minutes. That re-opened the shared-index clobber the ship mutex exists
# to prevent, and let a third gate container into a 2-slot semaphore sized to
# the ~8 GB Docker Desktop VM the fleet ran then (the OOM churn of gr202193;
# on the 93.2 GiB OrbStack VM the default is 3 — 2026-10-21).
#
# The condition the comments described was never expressible, because a
# holder file recorded only `<dir> pid=<n>`: pids are host-local, so a pid
# that is not running *here* means "dead" for a local holder and "unknowable"
# for a foreign one, and nothing distinguished the two. Holder files now carry
# `host=`, which makes the two paths decidable:
#
#   same host      → the pid check is authoritative. Dead: steal at once.
#                    Alive: WAIT, however long it has been held.
#   other host     → our pid namespace says nothing about theirs. Age is the
#                    only available signal, so the timer applies.
#   no usable pid  → crashed before writing a holder, or corrupt. Age applies.
#
# Legacy holder files (written before `host=`) are treated as local: a live
# pid is never stolen. That is the conservative direction — the cost is that a
# foreign stale lock whose recorded pid collides with some live local pid
# waits instead of being stolen, which needs a pid collision across hosts AND
# a crashed foreign ship, and self-resolves as holders carry `host=`.

lock_holder_hostname() {
    hostname -s 2>/dev/null || echo unknown
}

# Does pid $1 exist, whoever owns it? `kill -0` alone answers "may I signal
# it": for a live pid owned by another user it fails with EPERM, which a bare
# `kill -0` check reads as dead — and then steals a live holder's lock. The
# deploy controller runs deploys as more than one user (scripts/deploy's
# deploy/user split). EPERM means the process exists; only ESRCH means it is
# gone.
lock_pid_alive() {
    local err
    if err="$(export LC_ALL=C; kill -0 "$1" 2>&1)"; then return 0; fi
    [[ "$err" == *"not permitted"* ]]
}

# Stamp ownership. Best-effort: a lock with an unwritable holder file is
# reclaimable on age rather than wedged forever.
lock_holder_write() { # <lockdir>
    printf '%s pid=%s host=%s\n' "$PWD" "$$" "$(lock_holder_hostname)" \
        >"$1/holder" 2>/dev/null || true
}

lock_holder_pid() { # <lockdir>
    local pid
    pid="$(sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' "$1/holder" 2>/dev/null || true)"
    if [[ -z "$pid" ]]; then
        # scripts/deploy's pre-holder-format lock wrote a bare `pid` file.
        # Its numeric pid is the same witness — without this a LIVE legacy
        # holder reads "unparseable" and gets stolen on the age timer.
        pid="$(sed -n 's/^\([0-9][0-9]*\).*/\1/p' "$1/pid" 2>/dev/null || true)"
    fi
    printf '%s\n' "$pid"
}

lock_holder_host() { # <lockdir>
    sed -n 's/.*host=\([^[:space:]][^[:space:]]*\).*/\1/p' "$1/holder" 2>/dev/null || true
}

_lock_older_than() { # <lockdir> <minutes>
    find "$1" -maxdepth 0 -mmin "+$2" 2>/dev/null | grep -q .
}

# Decide whether a held lock may be reclaimed. Prints a reason phrase for the
# caller's "stealing …" line and returns 0 to steal, 1 to keep waiting.
lock_holder_reclaim_reason() { # <lockdir> <max_age_min>
    local dir="$1" max="$2" pid host me
    pid="$(lock_holder_pid "$dir")"
    host="$(lock_holder_host "$dir")"
    me="$(lock_holder_hostname)"

    if [[ -n "$host" && "$host" != "$me" ]]; then
        if _lock_older_than "$dir" "$max"; then
            printf 'held over %s min from another host (%s), pid unknowable here' \
                "$max" "$host"
            return 0
        fi
        return 1
    fi

    if [[ -n "$pid" ]]; then
        if ! lock_pid_alive "$pid"; then
            printf 'holder is dead (pid %s not running)' "$pid"
            return 0
        fi
        # Alive on this host. Deliberately ignores the age timer: a long hold
        # is a slow gate, not an abandoned lock.
        return 1
    fi

    if _lock_older_than "$dir" "$max"; then
        printf 'held over %s min with no parseable holder' "$max"
        return 0
    fi
    return 1
}

# _lock_put_back <aside> <lockdir> — rename a moved-aside lock dir back onto
# its path, but ONLY while the path is free. `mv src dst` onto an existing
# directory does not fail — it nests src inside dst — so when a racing mkdir
# takes the path in the microsecond window between the free check and the
# move, undo the nesting (pull ourselves back out of the new holder's dir).
# Returns 0 iff <aside> was restored to <lockdir>; 1 means it still sits at
# its aside path.
_lock_put_back() { # <aside> <lockdir>
    local aside="$1" dir="$2" inner="$2/${1##*/}"
    [[ -e "$dir" || -L "$dir" ]] && return 1
    mv "$aside" "$dir" 2>/dev/null || true
    [[ -d "$inner" ]] && mv "$inner" "$aside" 2>/dev/null || true
    [[ ! -d "$aside" ]]
}

# lock_steal <lockdir> <max_age_min> — remove a held lock dir, but only the
# exact directory instance the caller inspected, and only if it is STILL
# reclaimable. The naive `rm -rf "$dir"` between a staleness read and the
# next `mkdir` is the deploy-concurrency race: two waiters both read "holder
# A dead", B's steal+acquire lands first, and C's `rm -rf` then deletes B's
# LIVE lock on stale information — two processes now hold the mutex (the
# concurrent-deploy mechanism of docs/backlog/deploy-concurrency.md).
#
# rename(2) is the atomicity primitive: `mv` pins THE directory that was at
# the path or fails outright (a second stealer finds no path to move). The
# pinned dir is then re-judged under the same reclaim rule — if it is not
# reclaimable after all, the dir at the path was swapped for a live
# holder's between our check and the mv, and it is put back rather than
# destroyed.
#
# Residual, documented: if a third party's mkdir lands in the ~ms window
# between our mv-aside and the restore, the live lock survives displaced at
# <dir>.steal.<pid> (never deleted) — leaked, not corrupted. The stray-sweep
# below restores or reaps such leftovers on later steal attempts.
#
# Returns 0 iff this process destroyed a genuinely reclaimable dir.
lock_steal() { # <lockdir> <max_age_min>
    local dir="$1" max="$2" aside="${1%/}.steal.$$" stray
    # A stealer that died between its mv and the re-judge leaves the dir it
    # pinned under <dir>.steal.<pid>: restore a live one to the free path,
    # reap one that is reclaimable anyway. Never destroys a live lock —
    # the re-judge decides on the stray's own holder file.
    for stray in "${dir}.steal."*; do
        [[ -d "$stray" ]] || continue
        if lock_holder_reclaim_reason "$stray" "$max" >/dev/null 2>&1; then
            rm -rf "$stray" 2>/dev/null || true
        else
            _lock_put_back "$stray" "$dir" || true
        fi
    done
    mv "$dir" "$aside" 2>/dev/null || return 1
    if lock_holder_reclaim_reason "$aside" "$max" >/dev/null 2>&1; then
        rm -rf "$aside" 2>/dev/null || true
        return 0
    fi
    _lock_put_back "$aside" "$dir" \
        || printf 'WARNING: %s was a live lock, not a stale one, and a new holder took the path before it could be restored — it survives displaced at %s\n' \
            "$dir" "$aside" >&2
    return 1
}

# lock_release <lockdir> — release the lock ONLY on a positive ownership
# match (recorded holder pid == $$). A missing/unparseable holder is
# ambiguous — ours with a failed best-effort write, or a sibling mid-acquire
# that has not written its holder yet — and deleting a sibling's live lock
# has no recovery, while leaking ours self-heals via the pid-dead/age
# steals above (gr202363). Idempotent; safe to arm before acquiring.
lock_release() { # <lockdir>
    if [[ "$(lock_holder_pid "$1")" == "$$" ]]; then
        rm -rf "$1" 2>/dev/null || true
    fi
    return 0
}
