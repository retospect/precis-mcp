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
# slot dirs, take any one. Reclaim of an abandoned slot is the shared rule in
# lock-holder.sh: on this host the holder pid decides (dead → steal at once,
# alive → wait however long its gate runs), and the 45-minute timer applies
# only where the pid cannot decide — a foreign host's slot, or a holder file
# that was never written. The timer used to fire on live local holders too,
# which let a third gate into a 2-slot semaphore and caused the very OOM
# churn this guard exists to stop. gate_slot_release is ownership-
# checked (holder pid == $$) before it rm -rf's, same fix as the ship lock
# (gr202363) — otherwise a release firing after a sibling steals our slot
# (>45-min hold, or a late EXIT trap) deletes THEIR fresh slot instead.
#
# PRECIS_GATE_SLOTS overrides the cap (default 2 — measured: two concurrent
# full gates fit the ~8GB VM, three don't). Callers must arrange
# `gate_slot_release` on EXIT (idempotent) and around early returns.

GATE_SLOT_DIR=""

# shellcheck source=scripts/lib/lock-holder.sh
source "${BASH_SOURCE[0]%/*}/lock-holder.sh"

gate_slot_acquire() {
    local slots="${PRECIS_GATE_SLOTS:-2}"
    local common i d holder reason waited=0
    common="$(git rev-parse --git-common-dir)"
    while :; do
        for ((i = 0; i < slots; i++)); do
            d="${common}/precis-gate-slot-${i}.lock.d"
            if mkdir "$d" 2>/dev/null; then
                GATE_SLOT_DIR="$d"
                lock_holder_write "$d"
                return 0
            fi
            holder="$(cat "$d/holder" 2>/dev/null || true)"
            if reason="$(lock_holder_reclaim_reason "$d" 45)"; then
                echo "stealing gate slot ${i} — ${reason}: ${holder:-<no holder file>}" >&2
                rm -rf "$d" 2>/dev/null || true
            else
                continue
            fi
            # Stolen: try to claim it right away (a sibling may win the
            # mkdir race — that's fine, keep scanning).
            if mkdir "$d" 2>/dev/null; then
                GATE_SLOT_DIR="$d"
                lock_holder_write "$d"
                return 0
            fi
        done
        if [[ "$waited" == 0 ]]; then
            echo "waiting for a gate slot (${slots} concurrent gate containers max — shared-VM OOM guard, gr202193)" >&2
            echo "(steals a slot immediately if its holder dies; a live holder on this host is waited out however long its gate takes)" >&2
        fi
        waited=1
        sleep 3
    done
}

gate_slot_release() {
    if [[ -n "${GATE_SLOT_DIR:-}" ]]; then
        local holder_pid
        holder_pid="$(lock_holder_pid "$GATE_SLOT_DIR")"
        # Remove ONLY on a positive ownership match (holder pid == $$). A
        # missing/unparseable holder is ambiguous — ours with a failed
        # best-effort write, or a sibling mid-steal that hasn't written its
        # holder yet — and deleting a sibling's live slot has no recovery,
        # while leaking ours self-heals via the pid-dead/45-min steals
        # (gr202363).
        if [[ "$holder_pid" == "$$" ]]; then
            rm -rf "$GATE_SLOT_DIR" 2>/dev/null || true
        fi
        GATE_SLOT_DIR=""
    fi
}
