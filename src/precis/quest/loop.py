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

**Teardown is deferred (out of scope for v1).** A quest that goes
`dormant`/`abandoned` simply stops being re-minted here; its current loop is
NOT cancelled — it rests on its own via the dry-tick budget in
:mod:`precis.workers.job_types.quest_tick` (the loop naturally winds down once
the quest stops producing new work) rather than being torn down actively.
"""

from __future__ import annotations

import logging
import os
import re
from typing import Any

from precis.quest.allocator import active_quest_ids, cool_stalled
from precis.quest.tick import quest_loop_enabled

log = logging.getLogger(__name__)

#: Default LLM tier for the coordinator loop's review/propose call.
_DEFAULT_TIER = "local-big"
#: Default node the coordinator claim pins to (env-overridable per-deploy;
#: a quest's own ``meta.loop.target_node`` wins over both).
_DEFAULT_NODE_ENV = "PRECIS_QUEST_LOOP_NODE"

#: Parses ``id=N`` out of the ``JobHandler.put`` ack body.
_ID_IN_ACK = re.compile(r"\bid=(\d+)\b")


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


def reconcile_quest_loops(
    store: Any, *, enabled: bool | None = None, hub: Any = None
) -> dict[str, Any]:
    """One reconcile pass: cool the cold, then ensure a loop for each active quest.

    Gated on ``PRECIS_QUEST_LOOP_ENABLED`` unless ``enabled`` overrides. Cooling
    runs first so a quest that just went cold this pass isn't handed a fresh
    loop in the same cycle. Returns a summary dict: ``cooled`` (quests cooled
    to dormant), ``ensured`` (active quests confirmed to have a live loop,
    minted or pre-existing), ``minted`` (of those, how many were freshly
    created this pass).
    """
    on = quest_loop_enabled() if enabled is None else enabled
    if not on:
        return {"enabled": False, "cooled": 0, "ensured": 0, "minted": 0}

    cooled = cool_stalled(store)
    ensured = minted = 0
    for qid in active_quest_ids(store):
        job_id, created = ensure_quest_loop(store, qid, hub=hub)
        if job_id is not None:
            ensured += 1
            if created:
                minted += 1
    return {
        "enabled": True,
        "cooled": len(cooled),
        "ensured": ensured,
        "minted": minted,
    }


__all__ = ["ensure_quest_loop", "reconcile_quest_loops"]
