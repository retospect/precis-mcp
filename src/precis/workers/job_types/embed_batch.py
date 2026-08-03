"""``embed_batch`` job_type — a bounded work order draining the derived
embed queue (§F, ``docs/proposals/cluster-scheduling.md`` §F).

ADR 0007 is respected: the *fine-grained* queue stays the derived
``chunks``/``chunk_embeddings`` predicate with its own per-chunk
``chunk_claims`` lease (:class:`~precis.workers.embed.EmbedHandler`,
:mod:`precis.workers.base`) — this job_type is a *coarse* work order on
top of it, not a new queue. It shares the SAME ``claim_batch`` /
``process_batch`` / ``write_ok`` / ``write_failed`` machinery the (now
manual-only, §F cycle b) ``embed`` pass uses, so an ``embed_batch`` job
and a manual ``--only embed`` run can still coexist without conflict —
the chunk-level lease dedupes their claims.

Runs under the ``job_inproc`` executor only: a bounded (minutes, not
hours) in-process loop, ``params.limit`` chunks or until the derived queue
is empty, whichever comes first. ``requires={'embedder': 1}`` (via the
``embed_batch`` :class:`~precis.workers.registry.ServiceSpec` +
``effective_requires``, mirroring how ``struct_relax``/``fold`` derive
``{'gpu': 1}``) reserves an embedder slot at claim time.

Minted by the ``materialize`` scheduler cadence
(:mod:`precis.workers.materialize`) — **default-ON as of §F cycle b**
(``PRECIS_MATERIALIZE_EMBED=0`` is the opt-out) — but nothing stops an
operator or another job_type from ``put(kind='job', job_type=
'embed_batch', executor='job_inproc', ...)`` directly.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.embedder import EmbedderUnavailable
from precis.workers.embed import EmbedHandler, resolve_embedder, unembedded_chunk_count
from precis.workers.executors.job_inproc import renew_own_lease
from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.workers.executors._context import DispatchContext

log = logging.getLogger(__name__)

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "limit": {"type": "integer", "minimum": 1},
        "embedder": {"type": "string"},
    },
    "additionalProperties": False,
}

#: job_inproc only — the whole point (bounded in-proc work + slot
#: reservation via respect_reserve) requires that lane.
COMPATIBLE_EXECUTORS = frozenset({"job_inproc"})

#: Executor-capability layer (job_inproc's PROVIDES set) — job_inproc
#: provides nothing special and embed_batch needs nothing from it. The
#: REAL gate is the separate resource_slots reservation
#: (ServiceSpec.requires={'embedder'} on the registry row, consumed via
#: effective_requires at claim time) — see the module docstring.
REQUIRES: frozenset[str] = frozenset()

DESCRIPTION = (
    "Bounded work order draining the derived embed queue (ADR 0007) — up "
    "to params.limit chunks, or until the queue empties, whichever first."
)

#: params.limit default — mirrors the master proposal's "mint the next
#: 5000" shape at a more conservative size; PRECIS_EMBED_BATCH_MAX_JOBS ×
#: this is the materializer's per-tick mint ceiling.
_DEFAULT_LIMIT = 2000

#: Chunks claimed per handler.claim_batch() call inside the loop — mirrors
#: the worker CLI's --batch-size default (cli/worker.py).
_MICRO_BATCH = 32


def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
    params = dict(ctx.meta.get("params") or {})
    limit = int(params.get("limit", _DEFAULT_LIMIT))
    if limit <= 0:
        ctx.record_failure(
            f"embed_batch: params.limit must be positive, got {limit!r}",
            failure_class="infra",
        )
        return
    # §F cycle b fix: an EXPLICIT params.embedder is an override; absent
    # (the materializer-minted default) must fall through to
    # ``resolve_embedder``'s own ``name or cfg.embedder`` — passing the
    # bare string ``"bge-m3"`` here (the old default) shadowed
    # ``PrecisConfig.embedder`` unconditionally, so a prod job on
    # ``embedder='remote'`` built a LOCAL ``BgeM3Embedder`` (a torch
    # import the worker venv doesn't even have — see the module
    # docstring's [embed] extra note) instead of the configured
    # ``RemoteEmbedder`` client.
    embedder_name = params.get("embedder")
    if embedder_name is not None:
        embedder_name = str(embedder_name)

    try:
        embedder = resolve_embedder(name=embedder_name, dim=ctx.store.embedding_dim())
    except ValueError as exc:
        ctx.record_failure(f"embed_batch: {exc}", failure_class="infra")
        return

    handler = EmbedHandler(embedder)
    processed = ok_total = failed_total = 0

    while processed < limit:
        # job_inproc's lease (PRECIS_JOB_INPROC_LEASE_S, default 1800s)
        # must outlive this WHOLE drain, not just one micro-batch — renew
        # it every iteration so a batch slower than that window doesn't
        # get epoch/expiry-reclaimed while we're still working it (which
        # would let a second worker claim + dispatch the same job,
        # double-using the capacity-1 embedder slot). A lost lease means
        # another worker generation now owns this job: stop immediately
        # and leave it WITHOUT a terminal status — the new owner drives it
        # (job_inproc's ``_run_one`` also skips its happy-path finalize on
        # the same signal; see ``renew_own_lease``).
        if not renew_own_lease(ctx.store, ctx.ref_id, ctx.meta):
            ctx.append_chunk(
                "job_event",
                f"embed_batch: lease lost mid-drain after {processed} "
                "chunk(s) — another worker generation reclaimed this job; "
                "stopping without a terminal status",
            )
            return
        batch_limit = min(_MICRO_BATCH, limit - processed)
        with ctx.store.pool.connection() as conn:
            rows = handler.claim_batch(conn, limit=batch_limit)
            conn.commit()
        if not rows:
            break  # the derived queue is empty (or fully leased elsewhere)

        try:
            results = handler.process_batch(rows)
        except EmbedderUnavailable as exc:
            # Release these claims so they're immediately re-claimable —
            # same discipline as run_handler_once's deferral — then fail
            # the JOB (not the chunks): the materializer's cooldown (§F
            # cycle a, workers/materialize.py) prevents a mint-fail loop.
            # Do NOT retry/sleep here — a bounded in-proc job on this lane
            # must not block the pass rotation.
            with ctx.store.pool.connection() as conn:
                handler.release_claims(conn, [r.chunk_id for r in rows])
                conn.commit()
            ctx.record_failure(
                f"embed_batch: embedder unreachable after {processed} "
                f"chunk(s) embedded: {exc}",
                failure_class="infra",
            )
            return

        with ctx.store.pool.connection() as conn:
            for row, payload in zip(rows, results, strict=True):
                if isinstance(payload, Exception):
                    handler.write_failed(conn, row.chunk_id, repr(payload))
                    failed_total += 1
                else:
                    handler.write_ok(conn, row.chunk_id, payload)
                    ok_total += 1
            handler.release_claims(conn, [r.chunk_id for r in rows])
            conn.commit()
        processed += len(rows)

    with ctx.store.pool.connection() as conn:
        remaining = unembedded_chunk_count(conn)
    ctx.append_chunk(
        "job_summary",
        f"embedded {ok_total} chunk(s) ({failed_total} failed) — "
        f"queue_remaining≈{remaining}",
    )


def _run(*_a: Any, **_k: Any) -> Any:
    raise NotImplementedError("embed_batch runs via dispatch(), not run()")


SPEC = JobTypeSpec(
    name="embed_batch",
    params_schema=PARAMS_SCHEMA,
    compatible_executors=COMPATIBLE_EXECUTORS,
    requires=REQUIRES,
    description=DESCRIPTION,
    run=_run,
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
