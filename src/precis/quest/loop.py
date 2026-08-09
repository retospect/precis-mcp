"""Quest loop reconciler — guarantee one live ``quest_tick`` loop per active quest.

The old rung-4d autonomy (:mod:`precis.quest.allocator`) picked one active quest
per worker pass and ran an **inline** ``run_quest_tick`` — a single scored step,
not a loop. :mod:`precis.workers.job_types.quest_tick` replaced that shape: the
tick is now a **coordinator campaign** that runs indefinitely, event-driven, and
rests (``Done``) only after a bounded run of consecutive dry/failed slices —
waiting to be re-armed by a fresh ``quest_tick`` job. The two designs collide if
both run: the allocator's inline tick and a live coordinator loop for the same
quest would double-drive it.

This module is the replacement autonomy: not "which quest ticks next" but
**"does every active quest have a live loop, and is a rested one re-armed?"** —
a much simpler, idempotent reconciliation, run every worker pass.

The mint is idempotent via ``idem_key=f"quest_tick:{quest_id}"``:
``JobHandler._lookup_idem`` blocks a re-mint against ANY non-terminal job
(queued / running / waiting_time / waiting_children / …), so a sleeping
coordinator between heartbeats is correctly left alone, while a coordinator
that reached ``Done`` (terminal) no longer blocks — the next reconcile pass
mints a fresh loop and the quest self-heals.

**Reboot self-heal (reap orphaned loops).** A coordinator slice that dies
mid-run (node reboot, worker restart) leaves its job pinned at a *non-terminal*
status (`running`, or a `waiting_*` park whose wake_runner also died) — nothing
transitions it, so its `idem_key=quest_tick:<id>` keeps blocking a re-mint. The
sweeper eventually fails such a job, but only after the ~1h stuck-job threshold
on its own cadence, so recovery is slow (before this, a manual
``tag(kind='job', …, add=['STATUS:cancelled'])`` was needed). ``reconcile_quest_
loops`` now closes that gap itself: for each active quest it first cancels a
*provably orphaned* loop — non-terminal, `meta.lease_until` non-null and expired
**by a grace margin** (see below) — so ``ensure_quest_loop`` re-mints a fresh
loop in the same pass. **Division of labor:** the reconciler owns
quest-coordinator orphans (it has the quest context and runs every worker pass,
so it reacts in ~15 min); the sweeper stays the general backstop for every other
coordinator/`claude_inproc` orphan (and for a quest loop should the reconciler
be disabled).

**Why a grace margin, not the bare ssh_node predicate.** The claim-side
lease-steal (``claim_executor_jobs(reclaim_stale_running=True)``) treats
`lease < now()` alone as safe because an ssh_node lease is ~1h — longer than any
live dispatch. A *coordinator* lease is only 5 min and is set once at claim, not
renewed mid-slice (:mod:`precis.workers.executors.coordinator`), so a live but
slow ``quest_tick`` slice (its `big`-tier review/propose LLM call under spark
contention) can outlive its lease *while genuinely running*. Cancelling that
would re-mint a second loop while the old slice finishes and re-parks — the very
double-drive this module prevents. So a loop is only reaped once its lease is
stale beyond ``PRECIS_QUEST_LOOP_ORPHAN_GRACE_S`` (default 600 s), which no live
slice reaches. Reap terminalizes to ``STATUS:cancelled`` — distinct from a real
``failed`` rest, so it never feeds the (out-of-scope, RC1) failed-loop re-mint
question: this change only recovers *reboot* orphans, never a loop that a real
error rested.

**Teardown is no longer purely passive (RC2).** A quest that goes
`dormant`/`abandoned` stops being re-minted here; this reconciler still does
NOT cancel its current loop. But :mod:`precis.workers.job_types.quest_tick`'s
``_dispatch`` now checks the quest's own liveness at the top of every
slice — the same ``active_quest_ids`` notion this module uses — and
self-rests (``Done(success=True)``) the moment the quest is non-active, so
an *awaiting* loop also winds down on its next heartbeat rather than only
once its dry-tick budget exhausts.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from precis.quest.allocator import active_quest_ids, cool_stalled
from precis.quest.tick import quest_loop_enabled

log = logging.getLogger(__name__)

#: Default LLM tier for the coordinator loop's review/propose call. Capability tiers + placement chains
#: Phase C retired the location-coupled ``local-big`` tier — a served OSS
#: model still backs ``big`` when the backend/chain routes there.
_DEFAULT_TIER = "big"
#: Default node the coordinator claim pins to (env-overridable per-deploy;
#: a quest's own ``meta.loop.target_node`` wins over both).
_DEFAULT_NODE_ENV = "PRECIS_QUEST_LOOP_NODE"

#: A non-terminal coordinator loop is reaped only once its lease has been
#: expired for at least this many seconds — long enough that a live-but-slow
#: 5-min-lease slice (its LLM review/propose call) is never mistaken for a
#: reboot orphan. See the module docstring for the full rationale.
_ORPHAN_GRACE_ENV = "PRECIS_QUEST_LOOP_ORPHAN_GRACE_S"
_DEFAULT_ORPHAN_GRACE_S = 600

#: RC1 — a loop that rested on real *failure* (STATUS:failed,
#: distinct from a reboot-orphan's ``cancelled`` or a dry/punt/RC2 rest's
#: ``succeeded``) is not re-minted immediately: it backs off for an
#: exponentially-growing window keyed on how many consecutive failed rests
#: precede it, so a permanently-broken quest retries at a 30 min → 6 h cadence
#: instead of every worker pass. ``BASE * 2^(n-1)`` capped at ``MAX``.
_FAIL_BACKOFF_BASE_ENV = "PRECIS_QUEST_LOOP_FAIL_BACKOFF_S"
_DEFAULT_FAIL_BACKOFF_BASE_S = 1800  # 30 min
_FAIL_BACKOFF_MAX_ENV = "PRECIS_QUEST_LOOP_FAIL_BACKOFF_MAX_S"
_DEFAULT_FAIL_BACKOFF_MAX_S = 21600  # 6 h

#: Terminal job statuses — a job carrying one of these no longer blocks a
#: re-mint (mirrors ``JobHandler._lookup_idem``), so it is never an orphan.
_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

#: Parses ``id=N`` out of the ``JobHandler.put`` ack body.
_ID_IN_ACK = re.compile(r"\bid=(\d+)\b")


def _orphan_grace_s() -> int:
    """Grace seconds past lease expiry before a loop is reaped (default 600)."""
    raw = os.environ.get(_ORPHAN_GRACE_ENV)
    if raw is None:
        return _DEFAULT_ORPHAN_GRACE_S
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_ORPHAN_GRACE_S


def _env_int(name: str, default: int) -> int:
    """A non-negative int env override, else ``default`` (bad value → default)."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _failed_rest_cooldown_active(
    store: Any, quest_id: int, *, base_s: int, max_s: int
) -> bool:
    """RC1: is ``quest_id``'s loop inside its failed-rest backoff?

    Reads the quest's ``quest_tick:<id>`` coordinator loops most-recent-first.
    The **most recent** loop's terminal STATUS is the rest-reason discriminator
    the reconciler otherwise ignores:

    - non-terminal (queued/running/waiting_*) → a live loop exists; the idem
      dedup handles it, no cooldown.
    - ``cancelled`` (reboot-orphan reap) or ``succeeded`` (dry / punt / RC2
      self-rest) → re-mint immediately, exactly as before RC1.
    - ``failed`` (``_max_tick_failures`` budget, or a crashed slice) → back off.
      The window is ``min(base_s * 2^(n-1), max_s)`` where ``n`` is the trailing
      run of consecutive ``failed`` terminal loops (the job history *is* the
      counter — nothing stamped). ``True`` iff that window hasn't elapsed since
      the most-recent failure, so the pass skips the re-mint.

    Never raises — a single quest's read failure must not crash the reconcile
    cycle; on error it returns ``False`` (fail open → mint, the pre-RC1
    behavior) rather than silently starving a healthy quest.
    """
    try:
        idem = f"quest_tick:{quest_id}"
        with store.pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT
                    (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                      LIMIT 1) AS status,
                    (SELECT EXTRACT(EPOCH FROM (now() - rt.created_at))::float
                       FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                      LIMIT 1) AS age_s
                  FROM refs r
                 WHERE r.kind = 'job'
                   AND r.deleted_at IS NULL
                   AND r.meta->>'idem_key' = %s
                   AND r.meta->>'executor' = 'coordinator'
                 ORDER BY r.ref_id DESC
                 LIMIT 50
                """,
                (idem,),
            ).fetchall()
        if not rows or rows[0][0] != "failed":
            return False
        failed_age_s = float(rows[0][1] or 0.0)
        # Trailing run of consecutive failed terminal loops = the escalation
        # exponent. A cooldown-spaced permanent break grows this by one each
        # window; a re-mint that finally succeeds makes rows[0] != 'failed', so
        # it resets to 0 by construction.
        n = 0
        for status, _age in rows:
            if status == "failed":
                n += 1
            else:
                break
        window_s = min(base_s * (2 ** min(n - 1, 20)), max_s)
        return failed_age_s < window_s
    except Exception:
        log.exception("_failed_rest_cooldown_active: failed to read quest %s", quest_id)
        return False


def _loop_params(store: Any, quest_id: int) -> tuple[str, str]:
    """Resolve ``(tier, target_node)`` — quest ``meta.loop`` override, else
    the module/env defaults."""
    tier = _DEFAULT_TIER
    target_node = os.environ.get(_DEFAULT_NODE_ENV, "spark")
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        ref = None
    loop_meta = (getattr(ref, "meta", None) or {}).get("loop") if ref else None
    if isinstance(loop_meta, dict):
        tier = str(loop_meta.get("tier") or tier)
        target_node = str(loop_meta.get("target_node") or target_node)
    return tier, target_node


def ensure_quest_loop(
    store: Any, quest_id: int, *, hub: Any = None
) -> tuple[int | None, bool]:
    """Guarantee ``quest_id`` has one live ``quest_tick`` coordinator loop.

    Mints a fresh coordinator job via ``idem_key=f"quest_tick:{quest_id}"`` —
    ``JobHandler``'s idem dedup (any non-terminal status) means a sleeping
    loop is left alone and a rested (terminal) one is re-armed. Returns
    ``(job_id, created)``: ``created=True`` only when this call minted a new
    row; ``False`` when an existing live loop was found instead. Never
    raises — this runs inside a worker pass and a single quest's mint
    failure must not crash the reconcile cycle.
    """
    try:
        from precis.dispatch import Hub
        from precis.handlers.job import JobHandler

        tier, target_node = _loop_params(store, quest_id)
        jobs = JobHandler(hub=hub or Hub(store=store))
        idem = f"quest_tick:{quest_id}"
        resp = jobs.put(
            job_type="quest_tick",
            executor="coordinator",
            parent_id=quest_id,
            idem_key=idem,
            params={"quest_id": quest_id, "tier": tier, "target_node": target_node},
        )
        body = resp.body or ""
        created = body.startswith("created job")
        m = _ID_IN_ACK.search(body)
        job_id = int(m.group(1)) if m is not None else None
        return job_id, created
    except Exception:
        log.exception("ensure_quest_loop: failed to reconcile quest %s", quest_id)
        return None, False


def _reap_orphaned_loop(store: Any, quest_id: int, *, grace_s: int) -> int | None:
    """Cancel ``quest_id``'s coordinator loop iff it is a provable reboot orphan.

    An orphan = the ``idem_key=quest_tick:<id>`` job that is (a) still
    non-terminal — so it blocks a re-mint — yet (b) has a ``meta.lease_until``
    that is non-null and expired by more than ``grace_s`` (no live executor
    could still be holding it). Such a job's slice died mid-run and nothing
    else will transition it promptly. We terminalize it to ``STATUS:cancelled``
    (not ``failed``: a reboot is not a real error) so the next
    ``ensure_quest_loop`` re-mints a fresh loop this same pass.

    Race- and re-run-safe: the candidate is re-checked ``FOR UPDATE`` inside
    the write tx, and a bare-``queued`` re-mint (null lease) or a live-lease
    loop can never match. Returns the cancelled job id, or ``None`` when there
    is nothing to reap. Never raises — a single quest's reap failure must not
    crash the reconcile cycle.
    """
    try:
        from precis.store.types import Tag

        idem = f"quest_tick:{quest_id}"
        with store.tx() as conn:
            row = conn.execute(
                """
                SELECT r.ref_id
                  FROM refs r
                 WHERE r.kind = 'job'
                   AND r.deleted_at IS NULL
                   AND r.meta->>'idem_key' = %s
                   AND r.meta->>'executor' = 'coordinator'
                   AND (r.meta->>'lease_until') IS NOT NULL
                   AND (r.meta->>'lease_until')::timestamptz < now() - %s::interval
                   AND NOT EXISTS (
                         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                          WHERE rt.ref_id = r.ref_id
                            AND t.namespace = 'STATUS'
                            AND t.value = ANY(%s)
                       )
                 ORDER BY r.ref_id DESC
                 LIMIT 1
                   FOR UPDATE OF r SKIP LOCKED
                """,
                (idem, f"{grace_s} seconds", list(_TERMINAL_STATUSES)),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row[0])
            # Replace whatever non-terminal STATUS:* it carries with
            # cancelled, in one shot (replace_prefix), then mark *why* with a
            # searchable open tag — distinct from the sweeper's
            # ``swept:claim-orphaned`` so the two recovery paths stay legible.
            store.add_tag(
                job_id,
                Tag.closed("STATUS", "cancelled"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            store.add_tag(
                job_id,
                Tag.open("reaped:reboot-orphan"),
                set_by="system",
                conn=conn,
            )
            store.append_event(
                job_id,
                source="quest-loop-reconcile",
                event="loop-reaped",
                payload={"quest_id": quest_id, "cause": "reboot-orphan"},
                conn=conn,
            )
        log.info(
            "reconcile_quest_loops: reaped orphaned loop %d for quest %s "
            "(expired lease, no live executor)",
            job_id,
            quest_id,
        )
        return job_id
    except Exception:
        log.exception("_reap_orphaned_loop: failed to reap quest %s", quest_id)
        return None


def reconcile_quest_loops(
    store: Any, *, enabled: bool | None = None, hub: Any = None
) -> dict[str, Any]:
    """One reconcile pass: cool the cold, then ensure a loop for each active quest.

    Gated on ``PRECIS_QUEST_LOOP_ENABLED`` unless ``enabled`` overrides. Cooling
    runs first so a quest that just went cold this pass isn't handed a fresh
    loop in the same cycle. For each remaining active quest a *reap* step runs
    before the ensure: a reboot-orphaned loop (non-terminal, lease provably
    expired) is cancelled so its idem no longer blocks the re-mint below, and
    the quest self-heals in this pass. A quest whose most-recent loop rested
    ``failed`` (RC1) is instead held out of the re-mint for an
    escalating cooldown (:func:`_failed_rest_cooldown_active`). Returns a summary
    dict: ``cooled`` (quests cooled to dormant), ``reaped`` (orphaned loops
    cancelled this pass), ``backoff`` (active quests whose re-mint was skipped
    this pass because a failed rest is still cooling down), ``ensured`` (active
    quests confirmed to have a live loop, minted or pre-existing), ``minted``
    (of those, how many were freshly created).
    """
    on = quest_loop_enabled() if enabled is None else enabled
    if not on:
        return {
            "enabled": False,
            "cooled": 0,
            "reaped": 0,
            "backoff": 0,
            "ensured": 0,
            "minted": 0,
        }

    cooled = cool_stalled(store)
    grace_s = _orphan_grace_s()
    base_s = _env_int(_FAIL_BACKOFF_BASE_ENV, _DEFAULT_FAIL_BACKOFF_BASE_S)
    max_s = _env_int(_FAIL_BACKOFF_MAX_ENV, _DEFAULT_FAIL_BACKOFF_MAX_S)
    reaped = backoff = ensured = minted = 0
    for qid in active_quest_ids(store):
        # A reboot-orphan reap terminalizes to ``cancelled`` and re-mints in this
        # same pass — it is never a failed rest, so the RC1 backoff can't apply.
        # A loop that rested ``failed`` (and wasn't reaped) waits out its
        # escalating cooldown before the re-mint below.
        if _reap_orphaned_loop(store, qid, grace_s=grace_s) is not None:
            reaped += 1
        elif _failed_rest_cooldown_active(store, qid, base_s=base_s, max_s=max_s):
            backoff += 1
            continue
        job_id, created = ensure_quest_loop(store, qid, hub=hub)
        if job_id is not None:
            ensured += 1
            if created:
                minted += 1
    return {
        "enabled": True,
        "cooled": len(cooled),
        "reaped": reaped,
        "backoff": backoff,
        "ensured": ensured,
        "minted": minted,
    }


__all__ = [
    "ensure_quest_loop",
    "reconcile_quest_loops",
]  # _reap_orphaned_loop is internal
