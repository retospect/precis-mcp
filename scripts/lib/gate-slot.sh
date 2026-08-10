# shellcheck shell=bash
# scripts/lib/gate-slot.sh — N-slot admission semaphore for heavyweight gate
# containers (gr202193). Sourced by scripts/test and scripts/ship.
#
# Why: every worktree's gate/test run is a 1-3GB container capped against ONE
# shared Docker Desktop/colima VM (~8GB), not host RAM. With 4+ sibling
# sessions gating at once the VM OOM-kills containers at random (exit 137,
# silent pytest death mid-run, mypy SIGKILL at container-creation) — never a
# real failure, pure capacity. Capping concurrent gates fleet-wide turns that
# churn into a short queue.
#
# Mechanics mirror the ship lock (scripts/ship §3): all worktrees share one
# .git, so mkdir-mutexes on the git common dir are host-global; macOS has no
# flock(1), so atomic mkdir is the lock. This is the counting variant — N
# slot dirs, take any one. Two independent steals for abandoned slots, same
# as the ship lock: holder pid dead on this host → immediate; held >45 min →
# assume crashed/foreign-host (a gate normally finishes well inside that;
# the age is hold time, not queue time).
#
# PRECIS_GATE_SLOTS overrides the cap (default 2 — measured: two concurrent
# full gates fit the ~8GB VM, three don't). Callers must arrange
# `gate_slot_release` on EXIT (idempotent) and around early returns.

GATE_SLOT_DIR=""

gate_slot_acquire() {
    local slots="${PRECIS_GATE_SLOTS:-2}"
    local common i d holder holder_pid waited=0
    common="$(git rev-parse --git-common-dir)"
    while :; do
        for ((i = 0; i < slots; i++)); do
            d="${common}/precis-gate-slot-${i}.lock.d"
            if mkdir "$d" 2>/dev/null; then
                GATE_SLOT_DIR="$d"
                printf '%s pid=%s\n' "$PWD" "$$" >"$d/holder" 2>/dev/null || true
                return 0
            fi
            holder="$(cat "$d/holder" 2>/dev/null || true)"
            holder_pid="$(printf '%s' "$holder" | sed -n 's/.*pid=\([0-9][0-9]*\).*/\1/p')"
            if [[ -n "$holder_pid" ]] && ! kill -0 "$holder_pid" 2>/dev/null; then
                echo "stealing gate slot ${i} — holder is dead: ${holder:-<no holder file>}" >&2
                rm -rf "$d" 2>/dev/null || true
            elif find "$d" -maxdepth 0 -mmin +45 2>/dev/null | grep -q .; then
                echo "stealing gate slot ${i} — held over 45 min by: ${holder:-<no holder file>}" >&2
                rm -rf "$d" 2>/dev/null || true
            else
                continue
            fi
            # Stolen: try to claim it right away (a sibling may win the
            # mkdir race — that's fine, keep scanning).
            if mkdir "$d" 2>/dev/null; then
                GATE_SLOT_DIR="$d"
                printf '%s pid=%s\n' "$PWD" "$$" >"$d/holder" 2>/dev/null || true
                return 0
            fi
        done
        if [[ "$waited" == 0 ]]; then
            echo "waiting for a gate slot (${slots} concurrent gate containers max — shared-VM OOM guard, gr202193)" >&2
            echo "(steals a slot immediately if its holder dies, or after 45 min regardless)" >&2
        fi
        waited=1
        sleep 3
    done
}

gate_slot_release() {
    if [[ -n "${GATE_SLOT_DIR:-}" ]]; then
        rm -rf "$GATE_SLOT_DIR" 2>/dev/null || true
        GATE_SLOT_DIR=""
    fi
}
