# shellcheck shell=bash
# scripts/lib/lock-holder.sh — who holds a mkdir-mutex, and when it may be
# stolen. Sourced by scripts/ship (the ship lock) and scripts/lib/gate-slot.sh
# (the N-slot gate semaphore).
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
# a ~8GB VM (the OOM churn of gr202193).
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

# Stamp ownership. Best-effort: a lock with an unwritable holder file is
# reclaimable on age rather than wedged forever.
lock_holder_write() { # <lockdir>
    printf '%s pid=%s host=%s\n' "$PWD" "$$" "$(lock_holder_hostname)" \
        >"$1/holder" 2>/dev/null || true
}

lock_holder_pid() { # <lockdir>
    sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p' "$1/holder" 2>/dev/null || true
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
        if ! kill -0 "$pid" 2>/dev/null; then
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
