"""``derived_drain`` job_type — a bounded work order draining a SMALL-tier
derived LLM queue (summarize / classify) as a minted job, so derived SMALL work
flows through the job substrate (prio, ``/factory`` console, dispatch
accounting) instead of an invisible standing pass
(``docs/backlog/small-llm-derived-drain-band.md``).

Generalizes ``embed_batch`` one level UP: rather than re-homing the pass logic
into a :class:`~precis.workers.base.WorkerHandler` subclass (summarize/classify
are deliberately *not* WorkerHandlers — they need DB JOINs, doc-card prefetch,
and an outbound LLM call, and already own correct claim/retry/write-back
machinery), this job **wraps** the existing ``run_llm_summarize_pass`` /
``run_classify_pass`` and drives it in a bounded, lease-renewing loop
(``params.limit`` chunks, or until the derived queue is empty).

Placement + cap (Reto's "only melchior, up to 6 at a time, never cloud"):

* ``params.target_node`` **hard-pins** the job to melchior — the only host
  serving the local SMALL model — via ``job_inproc``'s claim-gate node pin
  (``struct_relax`` uses the same field for GPU pinning), NOT the soft 10-min
  ``LLM_AFFINITY_GRACE_MIN`` affinity.
* Concurrency is capped by the ROUTER's local-serving slot (cap 6 on
  ``llm:qwen3.5-9b-q4_k_m``): each ``run_X_pass`` fans out ``params.concurrency``
  concurrent ``Tier.SMALL`` calls; the router admits 6 and ``paused``-backs the
  rest (the pass records-for-retry). So this job **reserves NO executor slot**
  (``REQUIRES = frozenset()``) — reserving the ``llm:`` slot at claim time would
  double-count against those same 6 and starve itself.
* ``Tier.SMALL`` is local-only by construction (the router never routes SMALL to
  a cloud backend); with a cloud-rung-free ``llm.chain.small`` a saturated slot
  is pure backpressure, never spill.

Runs under ``job_inproc`` only (a bounded in-proc drain — minutes, not hours).
Minted by the ``materialize`` cadence's SMALL bands (``workers/materialize.py``)
— dark until those are enabled — but an operator can also
``put(kind='job', job_type='derived_drain', executor='job_inproc',
params={'pass':'classify','limit':32,'target_node':'melchior'})`` directly.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from precis.workers.executors.job_inproc import renew_own_lease
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.store import Store
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

#: The derived SMALL queues this job can drain — the ``params.pass``
#: discriminator. One ``job_type`` for both keeps the registry/console to a
#: single row; the drain loop is identical, only the wrapped ``run_X_pass`` (and
#: its client wiring) differs.
_KNOWN_PASSES = ("llm_summarize", "classify")

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "pass": {"type": "string", "enum": list(_KNOWN_PASSES)},
        "limit": {"type": "integer", "minimum": 1},
        "batch_size": {"type": "integer", "minimum": 1},
        "concurrency": {"type": "integer", "minimum": 1},
        # A claim-gate host pin (job_inproc's node-gate): only the named host
        # claims the job. The materializer sets this to melchior (the local SMALL
        # server); absent ⇒ any job_inproc host may claim (manual/test).
        "target_node": {"type": "string"},
    },
    "required": ["pass"],
    "additionalProperties": False,
}

#: job_inproc only — a bounded in-proc drain, same lane as embed_batch.
COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})

#: Reserve NOTHING at the executor level — the router's local-serving slot caps
#: concurrency per-call (see the module docstring's "reserves NO executor slot").
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Bounded work order draining a SMALL-tier derived LLM queue "
    "(summarize/classify) — up to params.limit chunks; melchior-pinned, "
    "router-capped at 6, never cloud. Minted by the materialize SMALL bands."
)

#: params.limit default — one job drains this many chunks then finalizes, so the
#: minter's next tick re-tops the band. Sized to last ≳ one 300s cadence tick at
#: melchior's SMALL throughput (see the spec's band/limit open question).
_DEFAULT_LIMIT = 500

#: Chunks claimed per wrapped ``run_X_pass`` call inside the drain loop — the
#: pass's own ``batch_size`` (both default 16).
_DEFAULT_BATCH = 16

#: In-pass thread-pool width == the router's cap-6 local slot. A value above the
#: slot cap just eats ``paused`` backoff (harmless, wasteful threads).
_DEFAULT_CONCURRENCY = 6

#: Consecutive fully-unproductive batches (claimed &gt; 0 but ok == 0 — a saturated
#: slot or a model that keeps missing) before the drain bails early instead of
#: busy-spinning through ``limit``. The band + prio bound the damage anyway;
#: this just avoids a hot loop against a wedged/saturated backend.
_MAX_NO_PROGRESS = 3

#: A wrapped ``run_X_pass`` callable: one claim→LLM→write-back cycle, returning
#: at least ``{"claimed", "ok", "failed"}``. Built once per job (client wiring is
#: constructed up front and shared across the drain loop's batches).
_PassRunner = Callable[..., dict[str, Any]]


def _make_summarize_runner() -> _PassRunner:
    """A ``Tier.SMALL`` (local-only) summarize runner — mirrors the client
    ``cli/worker.py`` builds for the standing ``llm_summarize`` pass."""
    from precis.utils.llm.router import DispatchClient, Tier
    from precis.workers.llm_summarize import SUMMARIZER_NAME, run_llm_summarize_pass

    client = DispatchClient(
        tier=Tier.SMALL, source="llm_summarize", log_call=True, log_blobs=False
    )

    def run_once(store: Store, *, batch_size: int, concurrency: int) -> dict[str, Any]:
        return run_llm_summarize_pass(
            store,
            client=client,
            summarizer=SUMMARIZER_NAME,
            batch_size=batch_size,
            concurrency=concurrency,
        )

    return run_once


def _make_classify_runner() -> _PassRunner:
    """A ``Tier.SMALL`` classify runner + optional Tier-2 escalate client —
    mirrors ``cli/worker.py``'s ``classify`` wiring (``PRECIS_CLASSIFY_MODEL`` /
    ``PRECIS_CLASSIFY_ESCALATE_MODEL``). The escalate client MUST be a distinct
    model or it is a no-op re-judge (see ``run_classify_pass``'s docstring)."""
    from precis.utils.llm.router import DispatchClient, Tier
    from precis.workers.classify import run_classify_pass

    client = DispatchClient(
        tier=Tier.SMALL,
        model=os.environ.get("PRECIS_CLASSIFY_MODEL") or None,
        source="classify",
        log_call=True,
        log_blobs=False,
    )
    escalate_model = os.environ.get("PRECIS_CLASSIFY_ESCALATE_MODEL") or None
    escalate_client = (
        DispatchClient(
            tier=Tier.SMALL,
            model=escalate_model,
            source="classify",
            log_call=True,
            log_blobs=False,
        )
        if escalate_model
        else None
    )

    def run_once(store: Store, *, batch_size: int, concurrency: int) -> dict[str, Any]:
        return run_classify_pass(
            store,
            client=client,
            batch_size=batch_size,
            escalate_client=escalate_client,
            concurrency=concurrency,
        )

    return run_once


_RUNNER_FACTORIES: dict[str, Callable[[], _PassRunner]] = {
    "llm_summarize": _make_summarize_runner,
    "classify": _make_classify_runner,
}


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    pass_name = params.get("pass")
    if pass_name not in _RUNNER_FACTORIES:
        ctx.record_failure(
            f"derived_drain: unknown/absent params.pass {pass_name!r}; "
            f"known: {sorted(_RUNNER_FACTORIES)}",
            failure_class="infra",
        )
        return
    limit = int(params.get("limit", _DEFAULT_LIMIT))
    if limit <= 0:
        ctx.record_failure(
            f"derived_drain: params.limit must be positive, got {limit!r}",
            failure_class="infra",
        )
        return
    batch_size = max(1, int(params.get("batch_size", _DEFAULT_BATCH)))
    concurrency = max(1, int(params.get("concurrency", _DEFAULT_CONCURRENCY)))

    run_once = _RUNNER_FACTORIES[pass_name]()
    processed = ok_total = failed_total = 0
    no_progress = 0

    while processed < limit:
        # Renew THIS job's lease at the top of every iteration — a drain slower
        # than the lease window must not get epoch/expiry-reclaimed while we're
        # still working it (a second worker generation would then own it). A
        # lost lease means the new claimant owns the job: stop WITHOUT a terminal
        # status and let it drive (job_inproc's _run_one honors the same signal).
        # Mirrors embed_batch.py.
        if not renew_own_lease(ctx.store, ctx.ref_id, ctx.meta):
            ctx.append_chunk(
                "job_event",
                f"derived_drain[{pass_name}]: lease lost mid-drain after "
                f"{processed} chunk(s) — another worker generation reclaimed "
                "this job; stopping without a terminal status",
            )
            return

        this_batch = min(batch_size, limit - processed)
        res = run_once(ctx.store, batch_size=this_batch, concurrency=concurrency)
        claimed = int(res.get("claimed", 0))
        if claimed == 0:
            break  # the derived queue is empty (or fully leased elsewhere)

        ok = int(res.get("ok", 0))
        ok_total += ok
        failed_total += int(res.get("failed", 0))
        processed += claimed

        # A batch that claimed rows but produced NO summaries/tags is a
        # saturated local slot (every call `paused`) or a persistently-missing
        # model — the passes already record those transiently for retry, so a
        # later job drains them. Don't busy-spin the rest of `limit` against a
        # wedged backend; bail after a few consecutive dry batches.
        if ok == 0:
            no_progress += 1
            if no_progress >= _MAX_NO_PROGRESS:
                ctx.append_chunk(
                    "job_event",
                    f"derived_drain[{pass_name}]: {no_progress} consecutive "
                    "batches made no progress (local slot saturated or model "
                    f"missing) — stopping early at {processed} chunk(s)",
                )
                break
        else:
            no_progress = 0

    ctx.append_chunk(
        "job_summary",
        f"derived_drain[{pass_name}]: drained {processed} chunk(s) "
        f"({ok_total} ok, {failed_total} failed/deferred)",
    )


SPEC = JobTypeSpec(
    name="derived_drain",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
