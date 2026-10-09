"""``conflict_sweep`` job_type — one named claim hub hunts its opposition.

**Thin on purpose.** The mechanism is :func:`precis.workers.conflict_search.
sweep_one_hub` (negate → ANN over claim + negated paraphrases →
``paper_rank``-budgeted verify with the small-voice floor → ``disputes``
edge on a confirmed contradicts verdict → ``meta.conflict_search`` ledger
stamp). This module only reads ``params``, builds the embedder, calls it
once, and writes the job summary — the same shape ``reground_claim.py``
has over ``hub_refine``. There is exactly one sweep implementation; the
standing ``conflict_search`` pass (the retro backfill) and this job are
two doors onto it.

Who mints it: :func:`precis.workers.conflict_search.enqueue_conflict_sweep`
— from ``taproot.hub.mint_hub`` (``reason='mint'``, so a new claim is
swept promptly) and from the nanopub approve surface (``refresh=True``
when the ledger is missing or stale). A hand ``put(kind='job',
job_type='conflict_sweep', params={'hub_id': <fi id>})`` works too.

Outcomes: a hub that is not claimable (already swept at the current
version without ``refresh``, leased by an in-flight sweep, or not a live
claim hub) is a clean no-op SUCCEEDED with a summary saying so; a negate
dispatch failure is an ``infra`` failure (the lease was cleared, nothing
stamped — retry re-runs it); a completed sweep is SUCCEEDED with the
counts in ``meta``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from precis.workers.job_types import JobTypeSpec

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "hub_id": {"type": "integer"},  # the claim hub's ref_id (fi<id>)
        # Sweep even when the ledger is already at the current version
        # (the approve-time freshness re-sweep). The ledger still skips
        # every chunk already verified.
        "refresh": {"type": "boolean"},
    },
    "required": ["hub_id"],
    "additionalProperties": False,
}


def _build_embedder(store: Store) -> Any:
    """Real embedder for the discovery step — same construction
    ``taproot_backfill`` / ``reground_claim`` use, since this job
    dispatches in-worker off its own hub rather than the server's runtime."""
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


def _dispatch(ctx: Any, spec: Any) -> None:
    """Plugin dispatcher invoked by ``claude_inproc`` for a claimed job.
    ``ctx`` is a :class:`~precis.workers.executors._context.DispatchContext`."""
    from precis.workers.conflict_search import sweep_one_hub

    params = (ctx.meta or {}).get("params") or {}
    try:
        raw_hub = params.get("hub_id")
        if not isinstance(raw_hub, (int, str)):
            raise TypeError("hub_id missing")
        hub_id = int(raw_hub)
    except (TypeError, ValueError):
        ctx.record_failure(
            "conflict_sweep: params.hub_id (a claim hub ref_id) is required"
        )
        return
    refresh = bool(params.get("refresh"))

    try:
        embedder = _build_embedder(ctx.store)
    except Exception as exc:
        ctx.record_failure(
            f"conflict_sweep: no embedder configured (PRECIS_EMBEDDER_URL): {exc}",
            failure_class="infra",
        )
        return

    outcome = sweep_one_hub(
        ctx.store, embedder=embedder, hub_ref_id=hub_id, refresh=refresh
    )
    if outcome is None:
        ctx.append_chunk(
            "job_summary",
            f"conflict sweep — fi{hub_id} not claimable: already swept at the "
            "current version, leased by an in-flight sweep, or not a live claim "
            "hub (no-op)",
        )
        ctx.set_meta(swept=False, skipped=True)
        return

    ctx.set_meta(
        swept=outcome.swept,
        candidates_checked=outcome.checked,
        disputes_filed=outcome.disputes_filed,
        llm_errors=outcome.llm_errors,
        skipped_covered=outcome.skipped_covered,
    )
    if outcome.vanished:
        ctx.record_failure(f"conflict_sweep: fi{hub_id} vanished mid-sweep")
        return
    if not outcome.swept:
        # Only reachable through a negate dispatch failure — the lease was
        # cleared and nothing stamped, so a retry re-runs the whole sweep.
        ctx.record_failure(
            f"conflict_sweep: negate LLM unavailable for fi{hub_id} — nothing "
            "stamped; retry this job",
            failure_class="infra",
        )
        return
    summary = (
        f"conflict sweep — fi{hub_id}: {outcome.checked} candidate(s) verified, "
        f"{outcome.disputes_filed} disputes filed, "
        f"{outcome.skipped_covered} already covered"
    )
    if outcome.llm_errors:
        summary += f", {outcome.llm_errors} verify call(s) failed (not covered)"
    if refresh:
        summary += " [refresh]"
    ctx.append_chunk("job_summary", summary)


SPEC = JobTypeSpec(
    name="conflict_sweep",
    params_schema=_PARAMS_SCHEMA,
    compatible_executors=frozenset({"claude_inproc"}),
    requires=frozenset(),
    description=(
        "Sweep one claim hub for opposing corpus passages (negated-paraphrase "
        "ANN, paper_rank-budgeted verify), file disputes edges, stamp the "
        "meta.conflict_search coverage ledger. Minted at claim mint and by the "
        "approve page's freshness check."
    ),
    dispatch=_dispatch,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = ["SPEC", "load"]
