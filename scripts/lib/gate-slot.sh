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
# peaks ~4.5 GiB and 180-470% CPU (baseline containers ~9 GiB), so the cap is
# derived from the VM's memory minus a baseline reserve, bounded by half its
# cores — gate_slot_default below; the flat 3 it replaces was the same sizing
# done once by hand (gr514466). The
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
# PRECIS_GATE_SLOTS overrides the cap; unset, gate_slot_default derives it
# from the VM the containers run on (gr514466 — the flat 3 starved small runs
# behind ~25 sharing agents). Callers must arrange `gate_slot_release` on
# EXIT (idempotent) and around early returns.
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

# Age of a lock dir from its mtime, for the waiting message (gr513773).
# `stat -f %m` is macOS/BSD, `stat -c %Y` is GNU — the first to succeed wins,
# matching the find -mmin portability constraint of _lock_older_than.
_gate_dir_age() { # <dir> -> "45s" | "12m" | "3h" | "?"
    local mt now s
    mt="$(stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo "")"
    [[ "$mt" =~ ^[0-9]+$ ]] || { printf '?'; return; }
    now="$(date +%s)"
    s=$((now - mt))
    if ((s < 120)); then
        printf '%ss' "$s"
    elif ((s < 7200)); then
        printf '%sm' "$((s / 60))"
    else
        printf '%sh' "$((s / 3600))"
    fi
}

# One line per slot for the waiting message (gr513773): free, or who holds it
# — the holder file's leading path (the worktree root scripts/test|ship ran
# from, so its basename is the tree/agent name), the holder pid and host, how
# long it has been held, and whether that pid is still alive on this host.
# A waiter can then tell a genuinely running gate from a leaked slot before
# the pid-dead steal notices it.
_gate_slot_holders() { # <common> <slots>
    local common="$1" slots="$2" i d holder tree pid host age state
    for ((i = 0; i < slots; i++)); do
        d="${common}/precis-gate-slot-${i}.lock.d"
        if [[ ! -d "$d" ]]; then
            printf '  gate slot %s: free\n' "$i" >&2
            continue
        fi
        pid="$(lock_holder_pid "$d")"
        host="$(lock_holder_host "$d")"
        age="$(_gate_dir_age "$d")"
        if [[ -z "$pid" ]]; then
            printf '  gate slot %s: held %s — no usable holder file\n' "$i" "$age" >&2
            continue
        fi
        holder="$(cat "$d/holder" 2>/dev/null || true)"
        tree="${holder%% pid=*}"
        tree="${tree##*/}"
        state="dead"
        lock_pid_alive "$pid" && state="alive"
        printf '  gate slot %s: held %s by %s (pid %s on %s, %s)\n' \
            "$i" "$age" "${tree:-?}" "$pid" "${host:-?}" "$state" >&2
    done
}

# Drop waiter tickets whose owner is gone: a dead pid on THIS host (authoritative
# — steal at once), or any ticket past the age timer (a foreign host we can't
# probe, or a local waiter killed before its EXIT trap removed its ticket).
# Ticket name is <epoch>-<pid>-<host>; host may contain dashes, so parse left to
# right rather than trimming from the right.
# The default cap (gr514466), sized on the Docker VM's own totals — that's
# the pool the gate containers share, and on this host it is much smaller
# than the host's (OrbStack: 47 GiB / 12 CPUs against 192 GiB). `docker info`
# reports VM totals on every runtime; the host probes (sysctl on macOS,
# /proc/meminfo + nproc on Linux) are only the no-daemon fallback. Sizing:
# a run peaks ~4.5 GiB against a 16 GiB reserve for the baseline containers,
# CPU-capped at half the VM's cores; never below 1. Every probe is a command
# call so tests can stub them as shell functions.
_gate_slot_capacity() { # prints "<mem_bytes> <ncpu>"
    local out kb
    if out="$(docker info --format '{{.MemTotal}} {{.NCPU}}' 2>/dev/null)" \
        && [[ "$out" =~ ^[0-9]+\ [0-9]+$ ]]; then
        printf '%s\n' "$out"
        return 0
    fi
    if [[ "$(uname -s)" == "Darwin" ]]; then
        printf '%s %s\n' \
            "$(sysctl -n hw.memsize 2>/dev/null || echo 0)" \
            "$(sysctl -n hw.ncpu 2>/dev/null || echo 0)"
    else
        kb="$(awk '/^MemTotal:/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
        printf '%s %s\n' "$((kb * 1024))" "$(nproc 2>/dev/null || echo 0)"
    fi
}

gate_slot_default() { # the derived default cap
    local mem=0 ncpu=0 mem_slots cpu_slots slots
    read -r mem ncpu <<<"$(_gate_slot_capacity)"
    [[ "$mem" =~ ^[0-9]+$ ]] || mem=0
    [[ "$ncpu" =~ ^[0-9]+$ ]] || ncpu=0
    mem_slots=$(( (mem - 17179869184) / 4831838208 )) # (mem - 16 GiB) / 4.5 GiB
    cpu_slots=$((ncpu / 2))
    slots=$((mem_slots < cpu_slots ? mem_slots : cpu_slots))
    ((slots < 1)) && slots=1
    printf '%s\n' "$slots"
}

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
    local slots="${PRECIS_GATE_SLOTS:-$(gate_slot_default)}"
    local common i d holder reason host name my_rank depth other seen_self
    local waited=0 last_rank=-1 last_announce=0
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
                # lock_steal re-judges the exact dir it removes, so a slot
                # re-acquired between our read and the steal is never rm'd
                # on stale information.
                if reason="$(lock_holder_reclaim_reason "$d" 45)" \
                    && lock_steal "$d" 45; then
                    echo "stole gate slot ${i} — ${reason}: ${holder:-<no holder file>}" >&2
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
        # churny queue" (gr294498), plus one line per slot naming the holding
        # tree/agent, the holder pid, the hold's age and its liveness
        # (gr513773). Re-announced when our rank changes and every ~60 s — the
        # ages advance and a constant-rank wait otherwise prints nothing while
        # the status stream buries the first "waiting" reason.
        if ((my_rank != last_rank || SECONDS - last_announce >= 60)); then
            if ((my_rank < slots)); then
                echo "gate slot: next up (position $((my_rank + 1)) of ${depth} waiting, ${slots} slots) — trying each freed slot" >&2
            else
                echo "gate slot: waiting at position $((my_rank + 1)) of ${depth} (${slots} slots ahead are busy)" >&2
            fi
            _gate_slot_holders "$common" "$slots"
            last_rank="$my_rank"
            last_announce=$SECONDS
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
        # lock_release removes ONLY on a positive ownership match (holder
        # pid == $$): a missing/unparseable holder is ambiguous — ours with
        # a failed best-effort write, or a sibling mid-acquire that hasn't
        # written its holder yet — and deleting a sibling's live slot has
        # no recovery, while leaking ours self-heals via the pid-dead/45-min
        # steals (gr202363).
        lock_release "$GATE_SLOT_DIR"
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
