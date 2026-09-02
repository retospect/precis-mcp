"""Quest loop reconciler — guarantee one live ``quest_tick`` loop per active quest.

:mod:`precis.workers.job_types.quest_tick` is a **coordinator campaign**:
runs indefinitely, event-driven, resting (``Done``) only after a bounded
run of consecutive dry/failed slices, then waits to be re-armed by a fresh
``quest_tick`` job. This module is that autonomy — every worker pass asks,
per active quest, "does it have a live loop, and is a rested one
re-armed?" — idempotent reconciliation, not scheduling.
(:mod:`precis.quest.allocator`'s inline per-pass tick is a distinct,
non-looping shape reserved for the manual ``precis quest run`` one-shot;
running both against the same quest would double-drive it.)

The mint is idempotent via ``idem_key=f"quest_tick:{quest_id}"``:
``JobHandler._lookup_idem`` blocks a re-mint against ANY non-terminal job
(queued / running / waiting_time / waiting_children / …), so a sleeping
coordinator between heartbeats is left alone, while one that reached ``Done``
no longer blocks — the next pass mints a fresh loop.

**Reboot self-heal (reap orphaned loops).** A coordinator slice that dies
mid-run (node reboot, worker restart) leaves its job pinned non-terminal
(`running`, or a `waiting_*` park whose wake_runner also died), so its
`idem_key` keeps blocking a re-mint until the sweeper's ~1h stuck-job
threshold fires. ``reconcile_quest_loops`` closes that gap per-pass: it
cancels a *provably orphaned* loop (non-terminal, `meta.lease_until`
non-null and expired by a grace margin — see below) so
``ensure_quest_loop`` re-mints in the same pass. **Division of labor:** this
reconciler owns quest-coordinator orphans (reacts in ~15 min, one pass); the
sweeper is the general backstop for every other coordinator/`claude_inproc`
orphan (and for a quest loop if the reconciler is disabled).

**Dead-node-pin self-heal (gr292747).** A distinct wedge from the reboot
orphan above: a loop minted *pinned* (``meta.params.target_node`` set — see
:func:`_loop_params`) whose target node dies before the loop is ever
claimed has `meta.lease_until IS NULL`, so it matches neither this
reconciler's own reboot-orphan reap (requires a non-null expired lease) nor
the sweeper's ``_enumerate_dead_node_orphans`` (requires
``executor='ssh_node'`` + ``STATUS:running``; this loop is
`executor='coordinator'`, still `queued`) — nothing ever cancelled it, and
its `idem_key` blocked re-minting indefinitely. This is exactly how spark's
decommission (while still the hardcoded default pin) wedged the whole quest
pipeline for four days. :func:`_reap_dead_node_pinned_loop` closes that gap:
provably-dead-node predicate mirrored from the sweeper, grace margin against
a reboot-window race, cancel + re-mint in the same pass as its sibling.
Default pin is now unset (see :data:`_DEFAULT_NODE_ENV`), so this arm is a
backstop for the opt-in per-quest/env pin, not the common path.

**Grace margin, not the bare ssh_node predicate.** A *coordinator* lease is
5 min (:mod:`precis.workers.executors.coordinator`'s ``_LEASE_MINUTES``,
short vs. an ssh_node lease's ~1h), so a live but slow slice (`big`-tier
review/propose LLM call under spark contention) can outrun the raw lease
window even with the coordinator's own mid-slice renewal (``_LeaseKeepalive``,
gr204309). A loop is reaped only once its lease is stale beyond
``PRECIS_QUEST_LOOP_ORPHAN_GRACE_S`` (default 600s) **AND** it has written no
``chunks`` row more recently than that same grace window (gr204309) — either
signal alone (a fresh chunk, or a lease still inside grace) holds off the
reap. Reap terminalizes to ``STATUS:cancelled``, distinct from a real
``failed`` rest — it recovers only *reboot* orphans, never a loop a real
error rested (RC1, out of scope here).

**Teardown is not purely passive (RC2).** A `dormant`/`abandoned` quest
stops being re-minted here, but this reconciler does not cancel its current
loop directly; :mod:`precis.workers.job_types.quest_tick`'s ``_dispatch``
checks the quest's own liveness (the same ``active_quest_ids`` notion) at
the top of every slice and self-rests (``Done(success=True)``) the moment
the quest is non-active, so an *awaiting* loop also winds down on its next
heartbeat rather than only once its dry-tick budget exhausts.

**Cooldown symmetry + escalation (gr170252).** A dry-budget rest
(``quest_tick``'s ``_max_dry_ticks``) is ``STATUS:succeeded`` — so the
generic "succeeded → re-mint immediately" exemption would spin it forever
(mint → dry ticks → rest → immediate re-mint). Two counters, both stamped by
``quest_tick``, fix it:

1. :func:`_dry_rest_cooldown_active` is :func:`_failed_rest_cooldown_active`'s
   sibling: it reads the same ``quest_tick:<id>`` job history, but keys off
   ``STATUS:succeeded`` *and* ``meta.rest_reason == "dry"`` instead of
   ``STATUS:failed``, applying the identical ``BASE * 2^(n-1)`` escalating
   window. A genuinely productive rest or a punt rest never sets
   ``rest_reason``, so those still re-mint immediately.
2. A ``consecutive_dry_rests`` counter on the *quest* ref
   (``quest_tick``'s ``_register_dry_rest``/``_reset_dry_rest_counter``),
   incremented per dry rest, reset by any frontier improvement or non-dry
   rest. At ``PRECIS_QUEST_DRY_REST_ESCALATE`` (default 3) an operator alert
   fires (``quest:dry-rest/<quest_id>``) and
   :func:`_dry_rest_escalation_active` holds the quest out of re-minting for
   ``PRECIS_QUEST_DRY_REST_ESCALATE_COOLDOWN_S`` (default 24h) since the
   last dry rest — not forever: the quest still ticks at low (~daily)
   cadence, and any tick that resets the counter ends escalation.

**Orphaned pathway stubs.** A `pathway` ref is minted ``meta.status =
"computing"`` at :func:`~precis.quest.compute.dispatch_autocatpath` dispatch
and only ever moves forward — to ``"ready"`` (aggregate job write-back) or
``"superseded"`` (content-key re-dispatch); nothing moves it to
``"failed"``, so a pathway whose whole job tree dies sits `"computing"`
forever (unranked by the frontier, blank on the web pathway page). Each pass
runs :func:`_reconcile_orphaned_pathways` per active quest:
:func:`_pathway_job_tree_state` walks the ``dispatch_autocatpath`` todo/job
tree (``T_agg`` + per-seed children) and classifies it — any live job/todo
leaves it alone; a genuine ``STATUS:failed`` job with no infra-class open
tag stamps the pathway ``"failed"`` (no compute worth re-running); an
all-terminal tree via ``cancelled``/infra-tagged-``failed``/never-minted
(all "wrongfully killed", not a real verdict) gets a bounded re-dispatch
instead, capped at :data:`_MAX_PATHWAY_REDISPATCH_PER_PASS` per quest per
pass. A separate, NOT quest-scoped catch-all
(:func:`_reconcile_stale_computing_pathways`) ages out any `"computing"`
pathway older than a week with no live job anywhere in its tree, active
quest or not. Neither step re-dispatches without the quest's own
``reaction_config`` — a config-less quest's orphans are left for the
catch-all.
"""

from __future__ import annotations

import logging
import os
import re
from typing import TYPE_CHECKING, Any

from precis.handlers._job_bubble import INFRA_FAILURE_TAGS
from precis.quest.allocator import active_quest_ids, cool_stalled
from precis.quest.compute import (
    _TIER_NEB,
    _candidate_struct_ids,
    _quest_reaction_config,
    dispatch_autocatpath,
)
from precis.quest.tick import quest_loop_enabled
from precis.utils.env import env_int
from precis.workers.nursery import DEAD_WORKER_SILENCE_MIN, WORKER_CONTINUOUS_PROCESSES

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: Default LLM tier for the coordinator loop's review/propose call. Capability tiers + placement chains
#: Phase C retired the location-coupled ``local-big`` tier — a served OSS
#: model still backs ``big`` when the backend/chain routes there.
_DEFAULT_TIER = "big"
#: Node to pin the coordinator claim to — unset by default (gr292747): a
#: loop with no ``target_node`` in its params is claimable by any ``system``
#: worker (``workers/executors/coordinator.py::_claim_jobs``'s node-pin
#: semantics — absent means unrestricted). The pin exists only for a
#: coordinator that genuinely needs a node-local resource (e.g. a box-local
#: OSS model); the earlier hardcoded ``"spark"`` default assumed that box was
#: permanent infrastructure, and it wasn't — spark's decommission left every
#: freshly-minted loop pinned to a node that no longer existed, and nothing
#: reaped a never-claimed (``lease_until IS NULL``) pinned loop, wedging the
#: quest pipeline for four days (gr292747; see
#: :func:`_reap_dead_node_pinned_loop` for the fix). Env-overridable per-deploy
#: (empty string reads as unset, same as absent); a quest's own
#: ``meta.loop.target_node`` wins over both when truthy.
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

#: gr170252 — consecutive *dry rests* (not dry ticks) before a quest is
#: treated as stuck on missing input: re-minting backs off to a long cooldown
#: and an operator alert fires. Same env var ``quest_tick._register_dry_rest``
#: reads, so the escalate-and-cool-down line never disagrees.
_DRY_REST_ESCALATE_ENV = "PRECIS_QUEST_DRY_REST_ESCALATE"
_DEFAULT_DRY_REST_ESCALATE = 3

#: gr170252 review finding #3 — an escalated quest (``consecutive_dry_rests``
#: at/above threshold) is held out of re-minting for this long since its most
#: recent dry rest, not forever. A permanent skip would be unrecoverable: every
#: counter-reset path (frontier improvement, a non-dry rest) lives inside
#: ``quest_tick``'s own tick, and no tick can ever run again once re-minting
#: stops — so the quest would be locked out for good the moment it escalates,
#: contradicting the alert text's promised recovery paths. A long (default
#: 24 h) cooldown instead lets the quest keep ticking at a low daily cadence;
#: an eventual tick that sees frontier improvement or a non-dry rest resets
#: the counter via ``quest_tick``'s existing reset paths, ending escalation
#: naturally. Same recency source as :func:`_dry_rest_cooldown_active`
#: (``STATUS:succeeded`` + ``meta.rest_reason == "dry"`` job recency).
_DRY_REST_ESCALATE_COOLDOWN_ENV = "PRECIS_QUEST_DRY_REST_ESCALATE_COOLDOWN_S"
_DRY_REST_ESCALATED_COOLDOWN_S = 86400  # 24 h

#: Terminal job statuses — a job carrying one of these no longer blocks a
#: re-mint (mirrors ``JobHandler._lookup_idem``), so it is never an orphan.
_TERMINAL_STATUSES = ("succeeded", "failed", "cancelled")

#: Parses ``id=N`` out of the ``JobHandler.put`` ack body.
_ID_IN_ACK = re.compile(r"\bid=(\d+)\b")

#: Open tags on a ``STATUS:failed`` job that mark it infra-class — the
#: compute never really ran, so the failure says nothing about the material —
#: rather than a genuine content-class result. :data:`INFRA_FAILURE_TAGS`
#: (``precis.handlers._job_bubble``) is the bounded-retry classification the
#: rest of the codebase already uses; ``reaped:dead-node-orphan``
#: (``workers.sweeper``'s ``ssh_node``-specific dead-node reap — exactly the
#: executor `autocatpath_seed`/`autocatpath_aggregate` run under) is an
#: equally wrongful-kill signal that constant doesn't (yet) carry — filed as
#: a gripe rather than widened at the source from here.
_PATHWAY_WRONGFUL_KILL_TAGS = INFRA_FAILURE_TAGS | frozenset(
    {"reaped:dead-node-orphan"}
)

#: Bounded per-quest-per-pass re-dispatch budget for
#: :func:`_reconcile_orphaned_pathways` — a systemic outage (a GPU node down
#: for hours) must not re-mint every wrongfully-killed tree in the corpus in
#: one worker pass; each active quest gets a small, renewable budget instead,
#: with the remainder simply waiting for the next pass (or, eventually, the
#: age-gated catch-all).
_MAX_PATHWAY_REDISPATCH_PER_PASS = 3

#: :func:`_reconcile_stale_computing_pathways` catch-all knobs — how stale a
#: still-``computing`` pathway with no live compute must be before it's
#: given up on, and how many it stamps per pass (draining a large historical
#: backlog gradually rather than in one write burst).
_PATHWAY_ORPHAN_MAX_AGE_DAYS = 7
_PATHWAY_ORPHAN_CATCHALL_LIMIT = 50


def _orphan_grace_s() -> int:
    """Grace seconds past lease expiry before a loop is reaped (default 600)."""
    raw = os.environ.get(_ORPHAN_GRACE_ENV)
    if raw is None:
        return _DEFAULT_ORPHAN_GRACE_S
    try:
        return max(0, int(raw))
    except ValueError:
        return _DEFAULT_ORPHAN_GRACE_S


def _failed_rest_cooldown_active(
    store: Store, quest_id: int, *, base_s: int, max_s: int
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
                   AND r.retired_at IS NULL
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


def _dry_rest_cooldown_active(
    store: Store, quest_id: int, *, base_s: int, max_s: int
) -> bool:
    """gr170252 cooldown symmetry: is ``quest_id``'s loop inside a *dry-rest*
    backoff?

    Sibling of :func:`_failed_rest_cooldown_active`, same job history and
    same ``BASE * 2^(n-1)`` (capped at ``max_s``) escalation, but keyed on
    ``STATUS:succeeded`` *and* ``meta.rest_reason == "dry"`` (stamped by
    ``quest_tick``'s ``_phase_tick`` exactly when it rests via the
    ``_max_dry_ticks`` budget — see that module) instead of ``STATUS:failed``.
    A dry rest is a *successful* tick outcome, so without this the plain
    ``_failed_rest_cooldown_active`` "succeeded → re-mint immediately"
    exemption re-armed it on the very next reconcile pass — the cooldown
    asymmetry behind the bug. A productive rest or a punt rest never sets
    ``rest_reason``, so those are unaffected and still re-mint immediately.

    Never raises — fail-open (``False``) on a read error, same as its
    sibling.
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
                      LIMIT 1) AS age_s,
                    r.meta->>'rest_reason' AS rest_reason
                  FROM refs r
                 WHERE r.kind = 'job'
                   AND r.retired_at IS NULL
                   AND r.meta->>'idem_key' = %s
                   AND r.meta->>'executor' = 'coordinator'
                 ORDER BY r.ref_id DESC
                 LIMIT 50
                """,
                (idem,),
            ).fetchall()
        if not rows or rows[0][0] != "succeeded" or rows[0][2] != "dry":
            return False
        dry_age_s = float(rows[0][1] or 0.0)
        # Trailing run of consecutive dry rests = the escalation exponent —
        # same construction as _failed_rest_cooldown_active's failed-run count.
        n = 0
        for status, _age, reason in rows:
            if status == "succeeded" and reason == "dry":
                n += 1
            else:
                break
        window_s = min(base_s * (2 ** min(n - 1, 20)), max_s)
        return dry_age_s < window_s
    except Exception:
        log.exception("_dry_rest_cooldown_active: failed to read quest %s", quest_id)
        return False


def _dry_rest_escalation_active(
    store: Store, quest_id: int, *, threshold: int, cooldown_s: int
) -> bool:
    """gr170252 escalation gate: is ``quest_id`` still inside its escalated
    dry-rest cooldown?

    Reads ``consecutive_dry_rests`` off the *quest* ref's own ``meta`` —
    ``quest_tick``'s ``_register_dry_rest`` bumps it on each dry rest and
    ``_reset_dry_rest_counter`` zeroes it on any frontier improvement or
    non-dry rest (see that module). Below ``threshold``, this is a no-op
    (``False``).

    At/above ``threshold``, ``quest_tick`` has already raised the
    ``quest:dry-rest/<quest_id>`` alert — but re-minting is skipped only for
    ``cooldown_s`` since the most recent dry rest, not forever: every
    counter-reset path lives inside a running tick, so a permanent skip would
    make recovery unreachable. This reads the exact same recency source as
    :func:`_dry_rest_cooldown_active` (the most-recent ``quest_tick:<id>``
    coordinator loop's ``STATUS:succeeded`` + ``meta.rest_reason == "dry"``)
    so the two never disagree about what counts as "the last dry rest". Once
    ``cooldown_s`` has elapsed, this returns ``False`` and the pass proceeds
    to the ordinary reap/cooldown/mint path below — by then the much shorter
    RC1/dry-rest cooldown has certainly also elapsed, so the mint proceeds and
    the quest gets one more daily-cadence tick to observe recovery.

    Never raises — fail-open (``False``, i.e. keep re-minting) on a read
    error, so a single quest's read failure can't starve the whole pass.
    """
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        log.exception("_dry_rest_escalation_active: failed to read quest %s", quest_id)
        return False
    if ref is None:
        return False
    n = int((ref.meta or {}).get("consecutive_dry_rests", 0) or 0)
    if n < threshold:
        return False
    try:
        idem = f"quest_tick:{quest_id}"
        with store.pool.connection() as conn:
            row = conn.execute(
                """
                SELECT
                    (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                      LIMIT 1) AS status,
                    (SELECT EXTRACT(EPOCH FROM (now() - rt.created_at))::float
                       FROM ref_tags rt JOIN tags t USING (tag_id)
                      WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS'
                      LIMIT 1) AS age_s,
                    r.meta->>'rest_reason' AS rest_reason
                  FROM refs r
                 WHERE r.kind = 'job'
                   AND r.retired_at IS NULL
                   AND r.meta->>'idem_key' = %s
                   AND r.meta->>'executor' = 'coordinator'
                 ORDER BY r.ref_id DESC
                 LIMIT 1
                """,
                (idem,),
            ).fetchone()
        if row is None or row[0] != "succeeded" or row[2] != "dry":
            # No dry-rest recency to key off — shouldn't happen once
            # escalated, but fail toward re-minting rather than a lockout.
            return False
        dry_age_s = float(row[1] or 0.0)
        return dry_age_s < cooldown_s
    except Exception:
        log.exception(
            "_dry_rest_escalation_active: failed to read recency for quest %s",
            quest_id,
        )
        return False


def _loop_params(store: Store, quest_id: int) -> tuple[str, str | None]:
    """Resolve ``(tier, target_node)`` — quest ``meta.loop`` override, else
    the module/env defaults.

    ``target_node`` is unset (``None``) by default (gr292747) — an empty or
    absent :data:`_DEFAULT_NODE_ENV` is not a pin. A quest's own
    ``meta.loop.target_node``, when truthy, wins over the env default; a
    falsy override (``""``/``None``/missing key) leaves the env value in
    place rather than clobbering it with a bogus unset.
    """
    tier = _DEFAULT_TIER
    target_node: str | None = os.environ.get(_DEFAULT_NODE_ENV) or None
    try:
        ref = store.get_ref(kind="quest", id=quest_id)
    except Exception:
        ref = None
    loop_meta = (getattr(ref, "meta", None) or {}).get("loop") if ref else None
    if isinstance(loop_meta, dict):
        tier = str(loop_meta.get("tier") or tier)
        override_node = loop_meta.get("target_node")
        if override_node:
            target_node = str(override_node)
    return tier, target_node


def ensure_quest_loop(
    store: Store, quest_id: int, *, hub: Any = None
) -> tuple[int | None, bool]:
    """Guarantee ``quest_id`` has one live ``quest_tick`` coordinator loop.

    Mints a fresh coordinator job via ``idem_key=f"quest_tick:{quest_id}"`` —
    ``JobHandler``'s idem dedup (any non-terminal status) means a sleeping
    loop is left alone and a rested (terminal) one is re-armed. Returns
    ``(job_id, created)``: ``created=True`` only when this call minted a new
    row; ``False`` when an existing live loop was found instead. Never
    raises — this runs inside a worker pass and a single quest's mint
    failure must not crash the reconcile cycle.

    ``target_node`` is OMITTED from ``params`` (not set to ``null``) when
    :func:`_loop_params` resolves it unset — absence, not a null-ish key, is
    the shape ``workers/executors/coordinator.py::_claim_jobs`` reads as
    "claimable by any system worker" (gr292747; see the module docstring).
    """
    try:
        from precis.dispatch import Hub
        from precis.handlers.job import JobHandler

        tier, target_node = _loop_params(store, quest_id)
        jobs = JobHandler(hub=hub or Hub(store=store))
        idem = f"quest_tick:{quest_id}"
        params: dict[str, Any] = {"quest_id": quest_id, "tier": tier}
        if target_node:
            params["target_node"] = target_node
        resp = jobs.put(
            job_type="quest_tick",
            executor="coordinator",
            parent_id=quest_id,
            idem_key=idem,
            params=params,
        )
        body = resp.body or ""
        created = body.startswith("created job")
        m = _ID_IN_ACK.search(body)
        job_id = int(m.group(1)) if m is not None else None
        return job_id, created
    except Exception:
        log.exception("ensure_quest_loop: failed to reconcile quest %s", quest_id)
        return None, False


def _reap_orphaned_loop(store: Store, quest_id: int, *, grace_s: int) -> int | None:
    """Cancel ``quest_id``'s coordinator loop iff it is a provable reboot orphan.

    An orphan = the ``idem_key=quest_tick:<id>`` job that is (a) still
    non-terminal — so it blocks a re-mint — AND (b) has a ``meta.lease_until``
    that is non-null and expired by more than ``grace_s`` — AND (c) has no
    ``chunks`` row for it created within that same ``grace_s`` window.

    Both (b) and (c) are required (gr204309). Before this fix the lease-expiry
    arm alone was treated as "no live executor could still be holding it" —
    that claim was false: :mod:`precis.workers.executors.coordinator` set
    ``meta.lease_until`` once at claim and never renewed it mid-slice, so a
    genuinely long-running (tens-of-minutes) slice would read as expired while
    still doing real work, and got cancelled out from under itself (prod: job
    204379 wrote a chunk 37 minutes after its lease had "expired", then was
    reaped anyway; 328 quest_tick jobs for quest 164903 mint→claim→reap→re-mint
    spun this way since 2026-07-24). The coordinator now renews its own lease
    mid-slice (``_LeaseKeepalive``), which should keep (b) from firing on a
    live slice going forward — but (c) stays as defense in depth against any
    *other* non-renewing executor a future ``idem_key=quest_tick:*`` coordinator
    might run under: a chunk written inside the grace window is direct evidence
    the slice is alive regardless of what its lease says, so it is never
    cancelled out from under itself. Such a job's slice died mid-run and
    nothing else will transition it promptly. We terminalize it to
    ``STATUS:cancelled`` (not ``failed``: a reboot is not a real error) so the
    next ``ensure_quest_loop`` re-mints a fresh loop this same pass.

    Race- and re-run-safe: the candidate is re-checked ``FOR UPDATE`` inside
    the write tx, and a bare-``queued`` re-mint (null lease), a live-lease
    loop, or a loop with a recent chunk can never match. Returns the cancelled
    job id, or ``None`` when there is nothing to reap. Never raises — a single
    quest's reap failure must not crash the reconcile cycle.
    """
    try:
        from precis.store.types import Tag

        idem = f"quest_tick:{quest_id}"
        grace_interval = f"{grace_s} seconds"
        with store.tx() as conn:
            row = conn.execute(
                """
                SELECT r.ref_id, r.meta->>'lease_until' AS lease_until,
                       (SELECT MAX(c.created_at) FROM chunks c
                         WHERE c.ref_id = r.ref_id) AS last_chunk_at
                  FROM refs r
                 WHERE r.kind = 'job'
                   AND r.retired_at IS NULL
                   AND r.meta->>'idem_key' = %s
                   AND r.meta->>'executor' = 'coordinator'
                   AND (r.meta->>'lease_until') IS NOT NULL
                   AND (r.meta->>'lease_until')::timestamptz < now() - %s::interval
                   AND NOT EXISTS (
                         SELECT 1 FROM chunks c
                          WHERE c.ref_id = r.ref_id
                            AND c.created_at > now() - %s::interval
                       )
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
                (
                    idem,
                    grace_interval,
                    grace_interval,
                    list(_TERMINAL_STATUSES),
                ),
            ).fetchone()
            if row is None:
                return None
            job_id = int(row[0])
            lease_until = row[1]
            last_chunk_at = row[2]
            last_chunk_iso = last_chunk_at.isoformat() if last_chunk_at else None
            reap_note = (
                f"reaped: lease_until={lease_until} expired beyond the "
                f"{grace_s}s grace window; last chunk "
                f"{'at ' + last_chunk_iso if last_chunk_iso else 'never written'}"
            )
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
            store.update_ref(job_id, meta_patch={"reap_note": reap_note}, conn=conn)
            store.append_event(
                job_id,
                source="quest-loop-reconcile",
                event="loop-reaped",
                payload={
                    "quest_id": quest_id,
                    "cause": "reboot-orphan",
                    "lease_until": lease_until,
                    "last_chunk_at": last_chunk_iso,
                },
                conn=conn,
            )
        log.info(
            "reconcile_quest_loops: reaped orphaned loop %d for quest %s "
            "(expired lease beyond grace, no recent chunk)",
            job_id,
            quest_id,
        )
        return job_id
    except Exception:
        log.exception("_reap_orphaned_loop: failed to reap quest %s", quest_id)
        return None


def _reap_dead_node_pinned_loop(
    store: Store, quest_id: int, *, grace_s: int
) -> int | None:
    """Cancel ``quest_id``'s coordinator loop iff it is pinned to a dead node
    and was never claimed (gr292747).

    **Division of labor with** :func:`_reap_orphaned_loop`: that reaper
    handles a loop that WAS claimed and then died mid-run
    (``meta.lease_until`` non-null, expired) — a coordinator's own lease. This
    reaper handles the other half: a loop that was minted pinned to a
    ``target_node`` (``meta.params.target_node`` — see :func:`_loop_params`)
    and never claimed at all (``lease_until IS NULL``, so no coordinator ever
    took the lease). A never-claimed pinned loop is invisible to every other
    self-heal path: ``_reap_orphaned_loop`` requires a non-null expired lease
    and skips it; :mod:`workers.sweeper`'s ``_enumerate_dead_node_orphans``
    only matches ``executor='ssh_node'`` + ``STATUS:running`` and skips it
    too (this loop is ``executor='coordinator'``, still ``queued``) — so
    nothing ever cancelled it, and its ``idem_key=quest_tick:<id>`` blocked
    ``ensure_quest_loop`` from re-minting forever. This is exactly what
    happened prod-wide when spark was decommissioned while still the
    hardcoded default pin: every loop minted against it wedged for four days
    until this reaper shipped.

    A candidate must satisfy ALL of:

    1. still non-terminal (the idem-blocking condition — same
       ``NOT EXISTS`` STATUS check as :func:`_reap_orphaned_loop`);
    2. ``meta.lease_until IS NULL`` — never claimed; a claimed loop is the
       other reaper's business, not this one's;
    3. ``meta.params.target_node`` is set — an unpinned loop is claimable by
       any ``system`` worker and can never wedge this way;
    4. that target node is *provably dead* — mirrors
       :mod:`workers.sweeper`'s ``_enumerate_dead_node_orphans`` predicate
       exactly: no ``worker_logs`` row for either continuous daemon
       (:data:`~precis.workers.nursery.WORKER_CONTINUOUS_PROCESSES`) within
       :data:`~precis.workers.nursery.DEAD_WORKER_SILENCE_MIN`, AND no fresh
       (< 3 min) ``host_heartbeat`` row — the host itself looks down, not
       just one wedged process;
    5. ``r.created_at`` older than ``grace_s`` — a freshly-minted pinned loop
       during a node reboot window is left alone rather than raced.

    Terminalizes to ``STATUS:cancelled`` (never claimed, so never actually
    ran — not a real failure) tagged ``reaped:dead-node-pin``, deliberately
    distinct from ``reaped:reboot-orphan`` (this module's other arm),
    ``reaped:dead-node-orphan`` (the sweeper's ``ssh_node`` arm) and
    ``swept:claim-orphaned`` (the sweeper's generic stuck-job backstop) so
    the recovery paths stay legible in the tag history. Re-checked ``FOR
    UPDATE`` inside the write tx, so a claim or a re-mint racing this can
    never be reaped out from under itself. Returns the cancelled job id, or
    ``None`` when there is nothing to reap. Never raises — a single quest's
    reap failure must not crash the reconcile cycle.
    """
    try:
        from precis.store.types import Tag

        idem = f"quest_tick:{quest_id}"
        grace_interval = f"{grace_s} seconds"
        with store.tx() as conn:
            row = conn.execute(
                """
                SELECT r.ref_id, r.meta->'params'->>'target_node' AS target_node
                  FROM refs r
                 WHERE r.kind = 'job'
                   AND r.retired_at IS NULL
                   AND r.meta->>'idem_key' = %(idem)s
                   AND r.meta->>'executor' = 'coordinator'
                   AND (r.meta->>'lease_until') IS NULL
                   AND (r.meta->'params'->>'target_node') IS NOT NULL
                   AND r.created_at < now() - %(grace)s::interval
                   AND NOT EXISTS (
                         SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                          WHERE rt.ref_id = r.ref_id
                            AND t.namespace = 'STATUS'
                            AND t.value = ANY(%(terminal)s)
                       )
                   AND NOT EXISTS (
                         SELECT 1 FROM worker_logs wl
                          WHERE wl.host = r.meta->'params'->>'target_node'
                            AND wl.process = ANY(%(procs)s)
                            AND wl.ts > now() - %(silence)s::interval
                       )
                   AND NOT EXISTS (
                         SELECT 1 FROM host_heartbeat hh
                          WHERE hh.host = r.meta->'params'->>'target_node'
                            AND hh.ts > now() - interval '3 minutes'
                       )
                 ORDER BY r.ref_id DESC
                 LIMIT 1
                   FOR UPDATE OF r SKIP LOCKED
                """,
                {
                    "idem": idem,
                    "grace": grace_interval,
                    "terminal": list(_TERMINAL_STATUSES),
                    "procs": list(WORKER_CONTINUOUS_PROCESSES),
                    "silence": f"{DEAD_WORKER_SILENCE_MIN} minutes",
                },
            ).fetchone()
            if row is None:
                return None
            job_id = int(row[0])
            target_node = row[1]
            reap_note = (
                f"reaped: pinned to dead node {target_node!r}, never claimed "
                f"(lease_until null), minted more than {grace_s}s ago"
            )
            # Same replace_prefix + searchable open-tag shape as
            # _reap_orphaned_loop, distinct tag value.
            store.add_tag(
                job_id,
                Tag.closed("STATUS", "cancelled"),
                set_by="system",
                replace_prefix=True,
                conn=conn,
            )
            store.add_tag(
                job_id,
                Tag.open("reaped:dead-node-pin"),
                set_by="system",
                conn=conn,
            )
            store.update_ref(job_id, meta_patch={"reap_note": reap_note}, conn=conn)
            store.append_event(
                job_id,
                source="quest-loop-reconcile",
                event="loop-reaped",
                payload={
                    "quest_id": quest_id,
                    "cause": "dead-node-pin",
                    "target_node": target_node,
                },
                conn=conn,
            )
        log.info(
            "reconcile_quest_loops: reaped dead-node-pinned loop %d for quest %s "
            "(target_node=%s never claimed, host provably dead)",
            job_id,
            quest_id,
            target_node,
        )
        return job_id
    except Exception:
        log.exception("_reap_dead_node_pinned_loop: failed to reap quest %s", quest_id)
        return None


def _pathway_job_tree_state(store: Store, pathway_id: int) -> tuple[str, str | None]:
    """Classify a ``computing`` `pathway`'s compute tree.

    Walks the :func:`~precis.quest.compute.dispatch_autocatpath` todo tree
    this pathway's mint ensures — ``T_agg`` (the ``kind='todo'`` ref whose
    ``meta.params.pathway_ref_id`` names this pathway) plus its per-seed
    child todos — and every ``kind='job'`` ref parented directly on each of
    those todos (both ``autocatpath_seed`` and ``autocatpath_aggregate`` jobs
    carry the same ``meta.params.pathway_ref_id`` provenance stamp, but the
    todo-tree walk is what lets a todo with NO job at all — a dispatch that
    crashed before ``jobs.put`` landed, see ``compute._seed_todo_handled`` —
    read distinctly from one whose job(s) actually ran). Returns
    ``(state, reason)`` where ``state`` is one of:

    * ``"in_flight"`` — some job is still queued/running/waiting_*, or an
      open SEED todo has no job yet, or ``T_agg`` itself has no seed
      children at all (ambiguous: could be a crashed dispatch, or one that
      started this same tick) — never touched here. A job-less ``T_agg``
      that DOES have seed children is NOT this case: per
      :func:`~precis.quest.compute.dispatch_autocatpath`'s own docstring, it
      never gets a job of its own until every seed todo resolves and the
      ordinary dispatch worker notices — an expected, not stuck, state — so
      it's simply skipped rather than treated as evidence of anything.
    * ``"failed"`` — at least one job terminalized ``STATUS:failed`` WITHOUT
      an infra-class open tag (:data:`_PATHWAY_WRONGFUL_KILL_TAGS`) — a
      genuine compute/content failure; ``reason`` is a short human string.
    * ``"wrongful_kill"`` — every todo in the tree is resolved (closed, or
      carries only ``cancelled``/infra-tagged-``failed`` jobs) and none
      genuinely failed — re-dispatch-eligible.
    * ``"unknown"`` — no ``T_agg`` todo found at all (a dispatch that died
      before minting even the aggregate todo, or an ad-hoc pathway with no
      compute tree) — left alone here; the module-level catch-all
      (:func:`_reconcile_stale_computing_pathways`, todo-tree-agnostic) is
      the backstop for a truly abandoned mint once it's stale enough.

    Never raises — a read failure returns ``("unknown", None)``, i.e. fail
    toward "leave it alone".
    """
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                """
                WITH agg AS (
                  SELECT ref_id FROM refs
                   WHERE kind = 'todo' AND retired_at IS NULL
                     AND meta->'params'->>'pathway_ref_id' = %(pid)s
                ),
                tree AS (
                  SELECT ref_id, TRUE AS is_root FROM agg
                  UNION ALL
                  SELECT t.ref_id, FALSE FROM refs t JOIN agg a ON t.parent_id = a.ref_id
                   WHERE t.kind = 'todo' AND t.retired_at IS NULL
                )
                SELECT
                  tr.is_root,
                  COALESCE(
                    (SELECT tg.value FROM ref_tags rt JOIN tags tg
                        ON tg.tag_id = rt.tag_id
                      WHERE rt.ref_id = tr.ref_id AND tg.namespace = 'STATUS'
                      LIMIT 1),
                    'open'
                  ) AS todo_status,
                  j.ref_id AS job_id,
                  (SELECT tg2.value FROM ref_tags rt2 JOIN tags tg2
                      ON tg2.tag_id = rt2.tag_id
                    WHERE rt2.ref_id = j.ref_id AND tg2.namespace = 'STATUS'
                    LIMIT 1) AS job_status,
                  EXISTS (
                    SELECT 1 FROM ref_tags rt3 JOIN tags tg3
                        ON tg3.tag_id = rt3.tag_id
                     WHERE rt3.ref_id = j.ref_id AND tg3.namespace = 'OPEN'
                       AND tg3.value = ANY(%(wrongful_tags)s)
                  ) AS job_wrongful
                  FROM tree tr
                  LEFT JOIN refs j
                    ON j.parent_id = tr.ref_id AND j.kind = 'job'
                       AND j.retired_at IS NULL
                """,
                {
                    "pid": str(pathway_id),
                    "wrongful_tags": sorted(_PATHWAY_WRONGFUL_KILL_TAGS),
                },
            ).fetchall()
    except Exception:
        log.exception(
            "_pathway_job_tree_state: failed to read tree for pathway %s", pathway_id
        )
        return "unknown", None

    if not rows:
        return "unknown", None

    # T_agg alone with no seed children at all is a DIFFERENT case from
    # T_agg alone-and-jobless WITH seed children under it: the latter is the
    # expected "every seed resolved, aggregate not minted yet" pause
    # (skip); the former means dispatch died before minting even the first
    # seed — as ambiguous as a jobless seed todo, so it reads the same way
    # (in-flight, not "unknown" — this candidate never even started).
    has_children = any(not is_root for is_root, *_rest in rows)

    any_terminal_bad = False
    for is_root, todo_status, job_id, job_status, job_wrongful in rows:
        if todo_status in ("done", "won't-do"):
            continue
        if job_id is None:
            if is_root and has_children:
                # T_agg never gets a job of its own until every seed todo
                # resolves (dispatch_autocatpath's docstring) — expected,
                # not evidence either way; skip rather than short-circuit.
                continue
            # A seed todo with no job at all, OR a T_agg with no seed
            # children ever minted — both ambiguous (crashed dispatch, or
            # one that started this same tick) — read conservatively as
            # in-flight; the age-gated catch-all is the backstop for a
            # truly abandoned mint, not this walk.
            return "in_flight", None
        if job_status not in _TERMINAL_STATUSES:
            return "in_flight", None
        if job_status == "succeeded":
            continue
        if job_status == "failed" and not job_wrongful:
            return "failed", "seed jobs failed"
        any_terminal_bad = True
    if any_terminal_bad:
        return "wrongful_kill", None
    return "unknown", None


def _reconcile_orphaned_pathways(
    store: Store, quest_id: int, *, hub: Any
) -> tuple[int, int]:
    """Resolve this quest's dead ``computing`` `pathway` stubs — stamp a
    genuine failure, or re-dispatch a wrongfully-killed tree — rather than
    leaving them stuck forever (see the module docstring's "Orphaned pathway
    stubs" section).

    For every candidate `structure` this quest serves
    (:func:`~precis.quest.compute._candidate_struct_ids`), finds its still-
    ``computing`` `pathway` refs (``meta.candidate_ref`` = the structure,
    ``meta.status = 'computing'``) and classifies each one's job tree via
    :func:`_pathway_job_tree_state`:

    * ``"in_flight"``/``"unknown"`` — left alone.
    * ``"failed"`` — stamped ``meta.status = 'failed'`` +
      ``meta.failed_reason`` directly.
    * ``"wrongful_kill"`` — re-dispatched via
      :func:`~precis.quest.compute.dispatch_autocatpath` (idempotent — the
      quest's own ``reaction_config`` at the pathway's own ``meta.tier``),
      bounded to :data:`_MAX_PATHWAY_REDISPATCH_PER_PASS` per quest per pass.
      Skipped entirely (no stamp, no re-dispatch) when the quest carries no
      ``reaction_config`` — nothing to re-dispatch against; the catch-all
      still eventually reclaims it.

    Returns ``(failed, redispatched)`` counts for this quest. Never raises —
    a single candidate's/pathway's failure is logged and skipped, never
    aborts the reconcile pass.
    """
    reaction = _quest_reaction_config(store, quest_id)
    failed = redispatched = 0
    for sid in _candidate_struct_ids(store, quest_id):
        try:
            with store.pool.connection() as conn:
                rows = conn.execute(
                    """
                    SELECT ref_id, meta FROM refs
                     WHERE kind = 'pathway' AND retired_at IS NULL
                       AND meta->>'candidate_ref' = %(sid)s
                       AND meta->>'status' = 'computing'
                    """,
                    {"sid": str(sid)},
                ).fetchall()
        except Exception:
            log.exception(
                "_reconcile_orphaned_pathways: failed to list pathways for "
                "candidate %s (quest %s)",
                sid,
                quest_id,
            )
            continue
        for raw_id, raw_meta in rows:
            pathway_id = int(raw_id)
            pmeta = dict(raw_meta or {})
            state, reason = _pathway_job_tree_state(store, pathway_id)
            if state == "failed":
                try:
                    store.stamp_ref_meta(
                        pathway_id,
                        {
                            "status": "failed",
                            "failed_reason": reason or "seed jobs failed",
                        },
                    )
                    failed += 1
                    log.info(
                        "reconcile_quest_loops: pathway %d (quest %s, "
                        "candidate %s) stamped failed: %s",
                        pathway_id,
                        quest_id,
                        sid,
                        reason,
                    )
                except Exception:
                    log.exception(
                        "_reconcile_orphaned_pathways: failed to stamp "
                        "pathway %d failed",
                        pathway_id,
                    )
            elif state == "wrongful_kill":
                if redispatched >= _MAX_PATHWAY_REDISPATCH_PER_PASS or reaction is None:
                    continue
                try:
                    tier = pmeta.get("tier") or _TIER_NEB
                    dispatch_autocatpath(store, sid, reaction, hub=hub, tier=tier)
                    redispatched += 1
                    log.info(
                        "reconcile_quest_loops: pathway %d (quest %s, "
                        "candidate %s) re-dispatched (wrongful kill)",
                        pathway_id,
                        quest_id,
                        sid,
                    )
                except Exception:
                    log.exception(
                        "_reconcile_orphaned_pathways: re-dispatch failed for "
                        "pathway %d (candidate %s)",
                        pathway_id,
                        sid,
                    )
    return failed, redispatched


def _reconcile_stale_computing_pathways(store: Store) -> int:
    """Module-level catch-all (NOT quest-scoped): age out a ``pathway`` ref
    still ``status='computing'`` after :data:`_PATHWAY_ORPHAN_MAX_AGE_DAYS`
    with no non-terminal job anywhere in its tree.

    Complements :func:`_reconcile_orphaned_pathways`, which only ever sees a
    pathway whose quest is still active — this backstop also reclaims stubs
    whose quest has since gone dormant/abandoned (so it never reaches the
    per-quest sweep again) and drains the historical backlog that predates
    this reconciler, without re-running month-old reaction configs. Bounded
    to :data:`_PATHWAY_ORPHAN_CATCHALL_LIMIT` per pass so a large backlog
    drains gradually rather than in one write burst.

    Deliberately job-only (not the todo-tree walk
    :func:`_pathway_job_tree_state` does): "nothing is running" is knowable
    from the job rows alone, regardless of whether the todo tree still
    resolves at all.

    Returns the number stamped ``failed``. Never raises.
    """
    try:
        with store.tx() as conn:
            rows = conn.execute(
                """
                SELECT p.ref_id FROM refs p
                 WHERE p.kind = 'pathway' AND p.retired_at IS NULL
                   AND p.meta->>'status' = 'computing'
                   AND p.updated_at < now() - %(max_age)s::interval
                   AND NOT EXISTS (
                         SELECT 1 FROM refs j
                          WHERE j.kind = 'job' AND j.retired_at IS NULL
                            AND j.meta->'params'->>'pathway_ref_id' = p.ref_id::text
                            AND EXISTS (
                                  SELECT 1 FROM ref_tags rt JOIN tags t
                                      ON t.tag_id = rt.tag_id
                                   WHERE rt.ref_id = j.ref_id
                                     AND t.namespace = 'STATUS'
                                     AND t.value != ALL(%(terminal)s)
                                )
                       )
                 ORDER BY p.ref_id
                 LIMIT %(limit)s
                   FOR UPDATE OF p SKIP LOCKED
                """,
                {
                    "max_age": f"{_PATHWAY_ORPHAN_MAX_AGE_DAYS} days",
                    "terminal": list(_TERMINAL_STATUSES),
                    "limit": _PATHWAY_ORPHAN_CATCHALL_LIMIT,
                },
            ).fetchall()
            n = 0
            for (raw_id,) in rows:
                store.stamp_ref_meta(
                    int(raw_id),
                    {
                        "status": "failed",
                        "failed_reason": "orphaned (no live compute)",
                    },
                    conn=conn,
                )
                n += 1
        if n:
            log.info(
                "reconcile_quest_loops: catch-all stamped %d stale computing "
                "pathway(s) failed",
                n,
            )
        return n
    except Exception:
        log.exception("_reconcile_stale_computing_pathways: sweep failed")
        return 0


def reconcile_quest_loops(
    store: Store, *, enabled: bool | None = None, hub: Any = None
) -> dict[str, Any]:
    """One reconcile pass: cool the cold, then ensure a loop for each active quest.

    Gated on ``PRECIS_QUEST_LOOP_ENABLED`` unless ``enabled`` overrides. Cooling
    runs first so a quest that just went cold this pass isn't handed a fresh
    loop in the same cycle. For each remaining active quest an *escalation*
    check runs first: a quest whose ``consecutive_dry_rests`` counter has
    crossed ``PRECIS_QUEST_DRY_REST_ESCALATE`` (gr170252 —
    :func:`_dry_rest_escalation_active`) is skipped for a long cooldown
    (``PRECIS_QUEST_DRY_REST_ESCALATE_COOLDOWN_S``, default 24 h) since its
    most recent dry rest — no reap/cooldown/mint while the cooldown holds, and
    an alert is already open on it — but this is not permanent: once the
    cooldown elapses re-minting resumes at that long cadence, giving the quest
    a tick that can observe recovery and reset the counter. Otherwise a *reap* step runs
    before the ensure, trying both arms: a reboot-orphaned loop (non-terminal,
    lease provably expired — :func:`_reap_orphaned_loop`) is cancelled first;
    if that finds nothing, a never-claimed loop pinned to a provably dead
    node (:func:`_reap_dead_node_pinned_loop`, gr292747) is tried next — either
    way its idem no longer blocks the re-mint below, and the quest self-heals
    in this pass. A quest whose most-recent loop rested
    ``failed`` (RC1) or rested ``succeeded`` with ``rest_reason: "dry"``
    (gr170252's cooldown symmetry) is instead held out of the re-mint for an
    escalating cooldown (:func:`_failed_rest_cooldown_active` /
    :func:`_dry_rest_cooldown_active`). Returns a summary dict: ``cooled``
    (quests cooled to dormant), ``escalated`` (active quests skipped this
    pass because they're past the dry-rest-stuck threshold and still inside
    the escalated cooldown), ``reaped``
    (orphaned loops cancelled this pass, either arm), ``backoff`` (active quests whose
    re-mint was skipped this pass because a failed or dry rest is still
    cooling down), ``ensured`` (active quests confirmed to have a live loop,
    minted or pre-existing), ``minted`` (of those, how many were freshly
    created), ``pathways_failed`` (``computing`` `pathway` stubs stamped
    ``failed`` this pass — genuine content failures plus the age-gated
    catch-all, see the module docstring's "Orphaned pathway stubs" section),
    ``pathways_redispatched`` (wrongfully-killed trees re-dispatched instead).
    """
    on = quest_loop_enabled() if enabled is None else enabled
    if not on:
        # The age-gated stub catch-all still runs with the loop OFF — an
        # ops-disabled loop is exactly when dead ``computing`` stubs pile
        # up unnoticed, and terminalizing them mints no new work.
        return {
            "enabled": False,
            "cooled": 0,
            "escalated": 0,
            "reaped": 0,
            "backoff": 0,
            "ensured": 0,
            "minted": 0,
            "pathways_failed": _reconcile_stale_computing_pathways(store),
            "pathways_redispatched": 0,
        }

    cooled = cool_stalled(store)
    grace_s = _orphan_grace_s()
    base_s = env_int(_FAIL_BACKOFF_BASE_ENV, _DEFAULT_FAIL_BACKOFF_BASE_S, lo=0)
    max_s = env_int(_FAIL_BACKOFF_MAX_ENV, _DEFAULT_FAIL_BACKOFF_MAX_S, lo=0)
    dry_threshold = env_int(_DRY_REST_ESCALATE_ENV, _DEFAULT_DRY_REST_ESCALATE, lo=0)
    escalate_cooldown_s = env_int(
        _DRY_REST_ESCALATE_COOLDOWN_ENV, _DRY_REST_ESCALATED_COOLDOWN_S, lo=0
    )
    escalated = reaped = backoff = ensured = minted = 0
    pathways_failed = pathways_redispatched = 0
    for qid in active_quest_ids(store):
        # Independent of the tick-loop reap/cooldown/mint state below — runs
        # for every active quest regardless of where it lands in that state
        # machine (see the module docstring's "Orphaned pathway stubs"
        # section).
        pf, pr = _reconcile_orphaned_pathways(store, qid, hub=hub)
        pathways_failed += pf
        pathways_redispatched += pr
        # gr170252: a quest stuck on missing input is held out of the mint —
        # no reap, no ordinary cooldown — while its escalated cooldown holds.
        # Checked before everything else so an orphan reap can't accidentally
        # re-arm it. Not permanent: once the cooldown elapses this returns
        # False and the quest falls through to the ordinary reap/mint path,
        # giving it a tick that can observe recovery.
        if _dry_rest_escalation_active(
            store, qid, threshold=dry_threshold, cooldown_s=escalate_cooldown_s
        ):
            escalated += 1
            continue
        # A reboot-orphan reap terminalizes to ``cancelled`` and re-mints in this
        # same pass — it is never a failed/dry rest, so neither cooldown applies.
        # The dead-node-pin arm (gr292747) only runs when the reboot-orphan arm
        # found nothing — the two predicates are mutually exclusive (lease_until
        # non-null vs. null) but checking both cheaply covers whichever wedge this
        # loop actually hit. A loop that rested ``failed`` or dry (and wasn't
        # reaped by either arm) waits out its escalating cooldown before the
        # re-mint below.
        reaped_job = _reap_orphaned_loop(store, qid, grace_s=grace_s)
        if reaped_job is None:
            reaped_job = _reap_dead_node_pinned_loop(store, qid, grace_s=grace_s)
        if reaped_job is not None:
            reaped += 1
        elif _failed_rest_cooldown_active(
            store, qid, base_s=base_s, max_s=max_s
        ) or _dry_rest_cooldown_active(store, qid, base_s=base_s, max_s=max_s):
            backoff += 1
            continue
        job_id, created = ensure_quest_loop(store, qid, hub=hub)
        if job_id is not None:
            ensured += 1
            if created:
                minted += 1
    # Module-level, NOT quest-scoped — also reclaims a stub whose quest went
    # dormant/abandoned before the per-quest sweep above ever caught it.
    pathways_failed += _reconcile_stale_computing_pathways(store)
    return {
        "enabled": True,
        "cooled": len(cooled),
        "escalated": escalated,
        "reaped": reaped,
        "backoff": backoff,
        "ensured": ensured,
        "minted": minted,
        "pathways_failed": pathways_failed,
        "pathways_redispatched": pathways_redispatched,
    }


__all__ = [
    "ensure_quest_loop",
    "reconcile_quest_loops",
]  # _reap_orphaned_loop is internal
