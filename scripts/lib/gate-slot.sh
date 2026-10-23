# shellcheck shell=bash
# scripts/lib/gate-slot.sh — N-slot admission semaphore for heavyweight gate
# containers (gr202193). Sourced by scripts/test and scripts/ship.
#
# Why: every worktree's gate/test run is a heavyweight container on ONE shared
# Docker VM — OrbStack on this host, 93.2 GiB / 8 CPUs on a 128 GiB box
# (measured 2026-10-21). The cap was set at 2 for the ~8 GB Docker Desktop VM
# (gr202193): with 4+ sibling sessions gating at once the VM OOM-killed
# containers at random (exit 137, silent pytest death mid-run, mypy SIGKILL
# at container-creation) — never a real failure, pure capacity. Capping
# concurrent gates fleet-wide turns that churn into a short queue. A gate now
# peaks ~4.5 GiB and 180-470% CPU (baseline containers ~9 GiB), so three fit
# memory comfortably and CPU is the binding constraint — default 3. The
# remaining OOM risk (one runaway run) is bounded per-container: the gate and
# dev-run services carry a 24 GiB mem_limit in docker/dev/compose.yaml, so a
# runaway OOMs alone (exit 137) and its siblings are untouched; callers name
# that kill via gate_oom_note below.
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
# PRECIS_GATE_SLOTS overrides the cap (default 3 — sized 2026-10-21 for the
# OrbStack VM: ~4.5 GiB peak per gate against 93.2 GiB, and CPU-bound rather
# than memory-bound; the previous 2 was measured on the ~8 GB Docker Desktop
# VM, gr202193). Callers must arrange `gate_slot_release` on EXIT (idempotent)
# and around early returns.
#
# Fairness (gr294498): the slot grab used to be a plain unfair spin-race —
# every waiter re-scanned all slots and `mkdir`'d on each 3s wake-up with no
# arrival order, so a slot freed mid-run went to whichever sibling's `mkdir`
# happened to land first, not to the longest-waiting job. Under sustained
# sibling churn (~8+ worktrees gating) a small 2-file run could lose the race
# for ~45 min straight, only winning once the forced-steal timer fired. Each
# waiter now drops an arrival ticket in a shared queue dir and only enters the
# mkdir race while it is among the oldest PRECIS_GATE_SLOTS outstanding tickets
# — mkdir stays the atomic gate (correctness), the ticket order just decides
# who is allowed to try (fairness). Tickets self-clean: on this host a ticket
# whose pid is dead is pruned at once, and any ticket (foreign host, or a
# waiter -9'd before its trap ran) is reclaimed on the same 45-min age timer as
# the slots themselves, so a leaked ticket can never wedge the queue.

GATE_SLOT_DIR=""
GATE_SLOT_TICKET=""

# shellcheck source=scripts/lib/lock-holder.sh
source "${BASH_SOURCE[0]%/*}/lock-holder.sh"

# Drop waiter tickets whose owner is gone: a dead pid on THIS host (authoritative
# — steal at once), or any ticket past the age timer (a foreign host we can't
# probe, or a local waiter killed before its EXIT trap removed its ticket).
# Ticket name is <epoch>-<pid>-<host>; host may contain dashes, so parse left to
# right rather than trimming from the right.
_gate_queue_prune() { # <qdir> <max_age_min>
    local qdir="$1" max="$2" t base rest pid host me
    me="$(lock_holder_hostname)"
    for t in "$qdir"/*; do
        [[ -e "$t" ]] || continue
        base="${t##*/}"
        rest="${base#*-}"   # <pid>-<host>
        pid="${rest%%-*}"   # <pid>
        host="${rest#*-}"   # <host> (may contain dashes)
        if [[ "$host" == "$me" && "$pid" =~ ^[0-9]+$ ]]; then
            kill -0 "$pid" 2>/dev/null && continue
            rm -f "$t" 2>/dev/null || true
        elif _lock_older_than "$t" "$max"; then
            rm -f "$t" 2>/dev/null || true
        fi
    done
}

gate_slot_acquire() {
    local slots="${PRECIS_GATE_SLOTS:-3}"
    local common i d holder reason host name my_rank depth other seen_self
    local waited=0 last_rank=-1
    common="$(git rev-parse --git-common-dir)"
    host="$(lock_holder_hostname)"
    local qdir="${common}/precis-gate-queue.d"
    mkdir -p "$qdir" 2>/dev/null || true
    # Arrival ticket: epoch-seconds first so a lexical sort is chronological,
    # pid to break within-second ties. Written once, on first entry; removed on
    # a successful acquire and by gate_slot_release on an abandoned wait.
    # Second granularity is ample here — the starvation this fixes was a fresh
    # sibling beating a 40-min waiter, a ~2400s gap.
    GATE_SLOT_TICKET="${qdir}/$(date +%s)-$$-${host}"
    name="${GATE_SLOT_TICKET##*/}"
    : >"$GATE_SLOT_TICKET" 2>/dev/null || true
    while :; do
        _gate_queue_prune "$qdir" 45
        # Rank = how many live tickets sit ahead of ours (oldest first); depth =
        # total waiting. Only the oldest `slots` waiters enter the mkdir race,
        # so a freed slot goes to the longest-queued job. If our own ticket is
        # missing (an unwritable queue dir), fall back to always-eligible rather
        # than starve ourselves.
        my_rank=0
        depth=0
        seen_self=0
        while IFS= read -r other; do
            depth=$((depth + 1))
            if [[ "$other" == "$name" ]]; then
                seen_self=1
                continue
            fi
            [[ "$seen_self" == 0 ]] && my_rank=$((my_rank + 1))
        done < <(ls -1 "$qdir" 2>/dev/null | sort)
        [[ "$seen_self" == 0 ]] && my_rank=0
        if ((my_rank < slots)); then
            for ((i = 0; i < slots; i++)); do
                d="${common}/precis-gate-slot-${i}.lock.d"
                if mkdir "$d" 2>/dev/null; then
                    GATE_SLOT_DIR="$d"
                    lock_holder_write "$d"
                    rm -f "$GATE_SLOT_TICKET" 2>/dev/null || true
                    GATE_SLOT_TICKET=""
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
                    rm -f "$GATE_SLOT_TICKET" 2>/dev/null || true
                    GATE_SLOT_TICKET=""
                    return 0
                fi
            done
        fi
        if [[ "$waited" == 0 ]]; then
            echo "waiting for a gate slot (${slots} concurrent gate containers max — shared-VM OOM guard, gr202193)" >&2
            echo "(steals a slot immediately if its holder dies; a live holder on this host is waited out however long its gate takes)" >&2
        fi
        # Surface queue position so a waiter can tell "next up" from "deep in a
        # churny queue" (gr294498). Re-announced only when our rank changes.
        if ((my_rank != last_rank)); then
            if ((my_rank < slots)); then
                echo "gate slot: next up (position $((my_rank + 1)) of ${depth} waiting, ${slots} slots) — trying each freed slot" >&2
            else
                echo "gate slot: waiting at position $((my_rank + 1)) of ${depth} (${slots} slots ahead are busy)" >&2
            fi
            last_rank="$my_rank"
        fi
        waited=1
        sleep 3
    done
}

gate_slot_release() {
    # Drop our arrival ticket if we exit still waiting (never acquired). The
    # ticket name carries our own pid, so removing it is unambiguously safe;
    # an acquire already cleared it. A -9 skips this — the queue prune reaps
    # the leaked ticket by dead-pid/age (gr294498).
    if [[ -n "${GATE_SLOT_TICKET:-}" ]]; then
        rm -f "$GATE_SLOT_TICKET" 2>/dev/null || true
        GATE_SLOT_TICKET=""
    fi
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

# The other half of the OOM guard (2026-10-21): the semaphore bounds how many
# gates run at once; the 24 GiB mem_limit on the precis-dev/precis-gate
# services (docker/dev/compose.yaml) bounds ONE runaway, so it dies inside
# its own cgroup instead of the VM killing a random sibling's gate. A cgroup
# OOM surfaces as exit 137 (128+SIGKILL) — historically a silent pytest death
# or a mystery red gate. Callers pass the container's exit code through here
# so every 137 is named as the kill it almost always is.
gate_oom_note() { # <exit code> <what died>
    [[ "${1:-0}" == 137 ]] || return 0
    # ✖ + indented continuation: the shape ship's quiet epilogue surfaces.
    printf '\033[31m✖ %s exited 137 (SIGKILL) — almost always the OOM killer on its mem_limit (24 GiB, docker/dev/compose.yaml), not a verdict on the diff.\n  The kill is per-container now: siblings keep running. Re-run to rule out a flake; if it repeats, bisect the memory hog.\033[0m\n' "${2:-the gate container}" >&2
    return 0
}
