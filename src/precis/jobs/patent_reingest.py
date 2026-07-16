"""One-shot re-ingest pass over already-ingested patents.

Patents ingested **before** the slice-1 claim marker existed
(``chunks.meta.patent_block``) carry no per-claim structure, so the
freedom-to-operate claims digest can't tell their claim blocks from
description (docs/design/patent-authoring-loop.md). Their stored chunks
are also structurally inconsistent per authority — some fuse every claim
into one chunk, some fragment a single claim, some enumerate
"embodiments" — so there is *no* reliable in-DB heuristic to re-mark
them. The only correct fix is to re-run the real ingest: re-fetch the OPS
XML, re-``parse_patent``, and DELETE+re-INSERT the blocks with the
current marker metadata (``ingest_patent(..., force=True)``).

This runner drives that force-reingest over every ``epo_ops`` patent ref
(or a named subset), oldest-first, isolating per-patent failures and
respecting the same rolling 7-day fair-use cap the watch runner uses. It
is an **operator backfill**, invoked once by hand:

    precis jobs reingest-patents --dry-run     # list what would refetch
    precis jobs reingest-patents               # do it

Re-ingest keeps each ref (id, links, tags) and swaps only its meta +
blocks, so the embed / keyword / classify workers re-derive over the
freshly marked blocks. A stub that had no full text at first ingest picks
up its claims here if OPS now serves them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from precis.handlers._patent_ingest import ingest_patent
from precis.handlers._patent_ops import OpsClientProto
from precis.handlers._patent_slug import parse_docdb_id
from precis.jobs.patent_watch import (
    DEFAULT_FAIR_USE_LIMIT_GB,
    _gb_to_bytes,
    compute_rolling_fair_use_bytes,
)

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.store import Store

log = logging.getLogger(__name__)

#: How many patent refs to page through per ``list_refs`` call. The
#: corpus is ~100 patents today; a generous page keeps it one query.
_LIST_PAGE: int = 500


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class ReingestOutcome:
    """One patent's re-ingest outcome."""

    slug: str
    blocks_before: int = 0
    blocks_after: int = 0
    bytes_fetched: int = 0
    error: str | None = None
    skipped_dry_run: bool = False


@dataclass(slots=True)
class ReingestSummary:
    """Aggregate report for a full re-ingest pass."""

    fair_use_bytes_before: int = 0
    fair_use_limit_bytes: int = 0
    paused_global: bool = False
    outcomes: list[ReingestOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Target selection
# ---------------------------------------------------------------------------


def _target_slugs(store: Store, only_slugs: list[str] | None) -> list[str]:
    """The patent slugs to re-ingest, oldest-created first.

    ``only_slugs`` (if given) restricts to that explicit set — an
    operator re-marking one authority's patents, or retrying a handful
    that errored. Otherwise every live ``epo_ops`` patent ref. Ordered
    oldest-first so a partial run (``--limit``) makes steady forward
    progress through the backlog rather than re-touching the newest.
    """
    if only_slugs:
        return list(dict.fromkeys(only_slugs))  # de-dup, order-stable
    slugs: list[str] = []
    offset = 0
    while True:
        refs = store.list_refs(
            kind="patent",
            provider="epo_ops",
            order_by="created_asc",
            limit=_LIST_PAGE,
            offset=offset,
        )
        if not refs:
            break
        slugs.extend(r.slug for r in refs if r.slug)
        if len(refs) < _LIST_PAGE:
            break
        offset += _LIST_PAGE
    return slugs


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_reingest_pass(
    *,
    store: Store,
    ops: OpsClientProto,
    embedder: Embedder | None,
    raw_root: Path,
    only_slugs: list[str] | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    fair_use_limit_gb: float = DEFAULT_FAIR_USE_LIMIT_GB,
) -> ReingestSummary:
    """Force-reingest existing patents so their claims carry markers.

    Args:
        store / ops / embedder / raw_root: as ``ingest_patent``.
        only_slugs: restrict to these slugs; ``None`` → all epo_ops
            patents.
        limit: cap the number attempted this pass (oldest-first);
            overflow is simply left for a follow-up run.
        dry_run: list what would be refetched; fetch nothing, mutate
            nothing.
        fair_use_limit_gb: rolling 7-day OPS fair-use cap. Checked once
            up front — re-ingest updates ``updated_at`` (not
            ``created_at``), so it does not itself inflate the window,
            but we still refuse to start a fresh fetch storm when the
            cluster is already over budget from live ingests.
    """
    summary = ReingestSummary()
    summary.fair_use_limit_bytes = _gb_to_bytes(fair_use_limit_gb)
    summary.fair_use_bytes_before = compute_rolling_fair_use_bytes(store)
    if summary.fair_use_bytes_before >= summary.fair_use_limit_bytes:
        summary.paused_global = True
        log.warning(
            "reingest-patents: paused — rolling 7d fair-use %.2f GiB ≥ limit %.2f GiB",
            summary.fair_use_bytes_before / (1024**3),
            fair_use_limit_gb,
        )
        return summary

    slugs = _target_slugs(store, only_slugs)
    if limit is not None:
        slugs = slugs[:limit]

    for slug in slugs:
        outcome = ReingestOutcome(slug=slug)
        if dry_run:
            try:
                ref = store.get_ref(kind="patent", id=slug)
                outcome.blocks_before = (
                    store.count_blocks(ref.id) if ref is not None else 0
                )
            except Exception:  # pragma: no cover — best-effort count
                outcome.blocks_before = 0
            outcome.skipped_dry_run = True
            summary.outcomes.append(outcome)
            continue
        try:
            ref = store.get_ref(kind="patent", id=slug)
            outcome.blocks_before = store.count_blocks(ref.id) if ref is not None else 0
            # Normalise via the slug parser so an odd stored slug still
            # resolves to a DOCDB id the OPS client accepts.
            docdb = parse_docdb_id(slug)
            result = ingest_patent(
                docdb,
                store=store,
                ops=ops,
                embedder=embedder,
                raw_root=raw_root,
                force=True,
            )
            outcome.blocks_after = result.block_count
            outcome.bytes_fetched = result.bytes_fetched
        except Exception as e:  # isolate per patent
            # One patent's OPS 404 / parse blow-up must not abort the
            # backlog; record and move on (the watch runner does the same).
            # OpsError is an Exception, so this covers auth / quota / 404.
            outcome.error = str(e)
        summary.outcomes.append(outcome)

    return summary
