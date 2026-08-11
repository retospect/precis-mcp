"""``taproot_backfill`` job_type — convert a draft scope's ``[pc]``/``[pa]``
legacy paper citations into living ``[fi<id>]`` claim-hub cites.

The cluster-side replacement for the (removed) synchronous
``edit(kind='draft', taproot=True)`` MCP door: the *no LLM runs in the MCP
process* principle means the canonicalizer cascade (extract → block →
judge → place, ``precis.taproot.backfill``) can only run on a worker.
``put(kind='job', job_type='taproot_backfill', params={'scope': '<slug|dc<id>>'})``
queues it; ``claude_inproc`` on the melchior agent worker runs this
dispatcher directly (a plugin ``dispatch``, no claude subprocess — same
shape as ``draft_export``).

Serial, one chunk at a time, and **checkpointed**: each processed chunk's id
is appended to ``refs.meta.done_chunk_ids`` as it finishes, so a re-claimed
(resumed) job skips the chunks it already converted rather than re-running
the (idempotent but not free) cascade on them. A single chunk's cascade
failure is isolated — recorded as a ``job_event`` and skipped — so one bad
chunk never fails the whole scope.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.workers.job_types import JobTypeSpec

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "scope": {"type": "string"},  # a draft slug, or a dc<id> handle
        "ref_level": {"type": "boolean"},
    },
    "required": ["scope"],
    "additionalProperties": False,
}


def _build_embedder(store: Any) -> Any:
    """Build a real embedder for the cascade's ANN convergence step —
    mirroring :func:`precis.runtime.factory.build_runtime`'s construction,
    since this job dispatches in-worker, off its own hub, not the server's.
    Raises (``ValueError``) when misconfigured, e.g. ``embedder='remote'``
    with no ``PRECIS_EMBEDDER_URL`` — the caller turns that into a clean
    job failure rather than a crash."""
    from precis.config import load_config
    from precis.embedder import make_embedder

    cfg = load_config()
    return make_embedder(
        cfg.embedder,
        dim=store.embedding_dim(),
        url=cfg.embedder_url,
        timeout=cfg.embedder_timeout,
        max_retries=cfg.embedder_max_retries,
    )


def _render_chunk_summary(result: Any) -> str | None:
    """One line summarising a single chunk's ``ChunkBackfill.plans`` —
    mirrors the old ``DraftHandler._render_backfill`` per-chunk rendering
    (shipped 184432bd, removed with the sync door). ``None`` when the chunk
    had no cite-groups (nothing to report)."""
    if not result.plans:
        return None
    counts: dict[str, int] = {}
    for p in result.plans:
        counts[p.action] = counts.get(p.action, 0) + 1
    summary = ", ".join(f"{n} {a}" for a, n in sorted(counts.items()))
    if result.n_ungrounded:
        summary += f", {result.n_ungrounded} ref-level/ungrounded"
    return f"dc{result.chunk_id}: {summary}"


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job.
    ``ctx`` is a :class:`~precis.workers.executors._context.DispatchContext`."""
    from precis.errors import BadInput, NotFound
    from precis.handlers.draft import DraftHandler
    from precis.taproot.backfill import apply_chunk
    from precis.utils import draft_regex

    params = (ctx.meta or {}).get("params") or {}
    scope = str(params.get("scope") or "").strip()
    ref_level = bool(params.get("ref_level"))
    if not scope:
        ctx.record_failure("taproot_backfill: params.scope is required")
        return

    try:
        embedder = _build_embedder(ctx.store)
    except Exception as exc:
        ctx.record_failure(
            f"taproot_backfill: no embedder configured (PRECIS_EMBEDDER_URL): {exc}"
        )
        return

    from precis.dispatch import Hub

    draft_handler = DraftHandler(hub=Hub(store=ctx.store, embedder=embedder))

    try:
        pairs, where = draft_handler._scope_chunks(scope, allow_all=False)
    except (NotFound, BadInput) as exc:
        ctx.record_failure(f"taproot_backfill: {exc}")
        return

    done_ids: list[int] = list((ctx.meta or {}).get("done_chunk_ids") or [])
    done_set = set(done_ids)

    n_scanned = 0
    n_converted = 0
    n_failed = 0
    n_ungrounded = 0

    for _slug, c in pairs:
        if c.chunk_kind in draft_regex.DERIVED_KINDS:
            continue  # table/figure: derived text, no citations
        if c.chunk_id in done_set:
            continue  # checkpoint: already converted by a prior run
        n_scanned += 1
        try:
            result = apply_chunk(
                ctx.store,
                embedder,
                draft_handler,
                c.chunk_id,
                set_by="agent",
                ref_level=ref_level,
            )
        except Exception as exc:
            n_failed += 1
            ctx.append_chunk("job_event", f"dc{c.chunk_id}: FAILED — {exc}")
        else:
            line = _render_chunk_summary(result)
            if line is not None:
                ctx.append_chunk("job_event", line)
            if result.rewritten_text is not None:
                n_converted += 1
            n_ungrounded += result.n_ungrounded

        done_ids.append(c.chunk_id)
        done_set.add(c.chunk_id)
        ctx.set_meta(done_chunk_ids=list(done_ids))

    summary = (
        f"taproot backfill — {where}: {n_scanned} scanned, "
        f"{n_converted} converted, {n_failed} failed"
    )
    if n_ungrounded:
        summary += f", {n_ungrounded} ref-level/ungrounded"
    ctx.append_chunk("job_summary", summary)
    ctx.set_meta(scanned=n_scanned, converted=n_converted, failed=n_failed)


SPEC = JobTypeSpec(
    name="taproot_backfill",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),
    description=(
        "Convert a draft scope's [pc]/[pa] cites into [fi] claim-hub cites "
        "(LLM cascade; serial, checkpointed)."
    ),
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
