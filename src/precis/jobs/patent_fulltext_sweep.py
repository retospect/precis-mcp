"""Deferred full-text retry runner for the ``patent`` kind.

Designed for one pass per scheduled tick — invoked by launchd / cron
(or an ad-hoc CLI run):

    precis jobs sweep-patent-fulltext

Some OPS publications serve biblio + abstract but 404 on the
description / claims endpoints when we first ingest them (recent US
/ CN applications that haven't been fully indexed yet). At ingest
time we stash an ``awaiting-fulltext`` open tag and a
``fulltext_retry_at`` ISO timestamp in ``refs.meta``; this runner
polls for due refs and retries the missing endpoints.

One pass:

1. Fair-use pre-check over the rolling 7-day window (shared with
   ``patent_watch.py``). Paused runs mutate no state.
2. ``SELECT`` patents tagged ``awaiting-fulltext`` where
   ``meta->>'fulltext_retry_at'`` is past. Hard ``limit`` caps
   how many we attempt per pass.
3. For each due ref:

   * **Give-up check** — if the publication is older than
     :data:`FULLTEXT_GIVEUP_DAYS`, swap the tag for
     ``fulltext-unavailable`` and move on. EPO rarely back-fills
     after six months.
   * **Retry** — fetch description + claims. On success: parse,
     embed, insert the new blocks, flip ``has_description`` /
     ``has_claims`` in meta, drop the awaiting tag. On 404:
     bump ``fulltext_retry_count`` and recompute
     ``fulltext_retry_at`` via exponential backoff.
   * **Other OPS errors** — log, skip, leave retry bookkeeping
     untouched so the next pass tries again.

Per-ref failures are isolated: an OPS outage on one ref logs and
moves on to the next. The runner returns a structured summary so
the CLI entry point can print a concise report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from precis.handlers._patent_ingest import (
    AWAITING_FULLTEXT_TAG,
    FULLTEXT_GIVEUP_DAYS,
    FULLTEXT_UNAVAILABLE_TAG,
    next_fulltext_retry_at,
)
from precis.handlers._patent_ops import OpsClientProto, OpsError, OpsNotFound
from precis.handlers._patent_slug import parse_docdb_id
from precis.handlers._patent_xml import parse_patent
from precis.ingest import ParsedBlock, classify_density, fill_embeddings
from precis.jobs.patent_watch import (
    DEFAULT_FAIR_USE_LIMIT_GB,
    _gb_to_bytes,
    compute_rolling_fair_use_bytes,
)
from precis.store import Tag
from precis.store.types import BlockInsert

if TYPE_CHECKING:
    from precis.embedder import Embedder
    from precis.store import Store

log = logging.getLogger(__name__)

#: Cap on how many patents one pass attempts. Bounds OPS quota use
#: and keeps the runner responsive; overflow re-surfaces next pass.
DEFAULT_SWEEP_LIMIT: int = 50


# ---------------------------------------------------------------------------
# Result types — what the CLI prints / what tests assert against
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class FulltextSweepOutcome:
    """One patent's outcome from this pass."""

    slug: str
    succeeded: bool = False  # full text now ingested
    still_pending: bool = False  # retry bookkeeping bumped
    given_up: bool = False  # swapped to fulltext-unavailable
    blocks_added: int = 0
    bytes_fetched: int = 0
    error: str | None = None
    skipped_dry_run: bool = False


@dataclass(slots=True)
class FulltextSweepSummary:
    """Aggregate report for a full sweep pass."""

    fair_use_bytes_before: int = 0
    fair_use_limit_bytes: int = 0
    paused_global: bool = False
    outcomes: list[FulltextSweepOutcome] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Due-ref lookup
# ---------------------------------------------------------------------------


def _list_due(store: Store, *, now: datetime, limit: int) -> list[tuple[int, str]]:
    """Return ``(ref_id, slug)`` for every awaiting-fulltext ref whose
    retry timestamp is past.

    The query joins ``refs`` against ``ref_open_tags`` and compares
    the ISO timestamp in ``meta->>'fulltext_retry_at'`` against
    ``now`` at the SQL boundary so we don't materialise every awaiting
    ref client-side. Ordering by ``retry_at`` ensures the oldest
    backlog clears first.
    """
    sql = """
        SELECT r.id, r.slug
        FROM   refs r
        JOIN   ref_open_tags t ON t.ref_id = r.id
        WHERE  r.kind = 'patent'
          AND  r.deleted_at IS NULL
          AND  t.value = %s
          AND  (r.meta->>'fulltext_retry_at') IS NOT NULL
          AND  (r.meta->>'fulltext_retry_at')::timestamptz <= %s
        ORDER  BY (r.meta->>'fulltext_retry_at')::timestamptz ASC
        LIMIT  %s
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, (AWAITING_FULLTEXT_TAG, now, limit)).fetchall()
    return [(int(r[0]), str(r[1])) for r in rows]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_fulltext_sweep(
    *,
    store: Store,
    ops: OpsClientProto,
    embedder: Embedder | None,
    raw_root: Path,
    limit: int = DEFAULT_SWEEP_LIMIT,
    dry_run: bool = False,
    fair_use_limit_gb: float = DEFAULT_FAIR_USE_LIMIT_GB,
    now: datetime | None = None,
) -> FulltextSweepSummary:
    """Run one sweep pass over every due awaiting-fulltext patent.

    Args:
        store, ops, embedder, raw_root: same as ``ingest_patent``.
        limit: max refs per pass — caps OPS quota use. Overflow
            re-surfaces on the next pass (their retry timestamps
            are still past).
        dry_run: enumerate due refs but mutate nothing. Useful for
            ``sweep-patent-fulltext --dry-run``.
        fair_use_limit_gb: rolling 7-day cap, shared with
            ``patent_watch``.
        now: injectable clock for tests. Defaults to
            ``datetime.now(tz=utc)``.

    Returns:
        ``FulltextSweepSummary`` listing every ref attempted and the
        per-ref outcome. Per-ref failures are captured in
        ``FulltextSweepOutcome.error`` rather than propagated.
    """
    if now is None:
        now = datetime.now(UTC)

    summary = FulltextSweepSummary(
        fair_use_limit_bytes=_gb_to_bytes(fair_use_limit_gb),
    )

    # Global fair-use pre-check. One query regardless of how many
    # refs are due.
    summary.fair_use_bytes_before = compute_rolling_fair_use_bytes(store)
    if summary.fair_use_bytes_before >= summary.fair_use_limit_bytes:
        summary.paused_global = True
        log.warning(
            "patent_fulltext_sweep: paused - rolling 7d fair-use "
            "%.2f GiB ≥ limit %.2f GiB",
            summary.fair_use_bytes_before / (1024**3),
            fair_use_limit_gb,
        )
        return summary

    due = _list_due(store, now=now, limit=limit)

    for ref_id, slug in due:
        outcome = _retry_one_ref(
            store=store,
            ops=ops,
            embedder=embedder,
            raw_root=raw_root,
            ref_id=ref_id,
            slug=slug,
            now=now,
            dry_run=dry_run,
        )
        summary.outcomes.append(outcome)

    return summary


# ---------------------------------------------------------------------------
# Per-ref processing
# ---------------------------------------------------------------------------


def _retry_one_ref(
    *,
    store: Store,
    ops: OpsClientProto,
    embedder: Embedder | None,
    raw_root: Path,
    ref_id: int,
    slug: str,
    now: datetime,
    dry_run: bool,
) -> FulltextSweepOutcome:
    """Retry one awaiting-fulltext patent. Catches all OPS errors."""
    outcome = FulltextSweepOutcome(slug=slug)

    ref = store.get_ref(kind="patent", id=slug)
    if ref is None:
        # Race with soft-delete; skip silently.
        outcome.error = "ref disappeared"
        return outcome
    meta = ref.meta or {}
    retry_count = int(meta.get("fulltext_retry_count", 0) or 0)
    pub_date_raw = meta.get("publication_date")

    # Give-up check. Patents older than FULLTEXT_GIVEUP_DAYS past
    # their publication date rarely get back-filled; swap the tag
    # and stop polling so we don't burn quota on dead rows.
    if _should_give_up(pub_date_raw=pub_date_raw, now=now):
        if dry_run:
            outcome.skipped_dry_run = True
            outcome.given_up = True
            log.info(
                "patent_fulltext_sweep[%s]: dry-run - would give up (%s)",
                slug,
                pub_date_raw,
            )
            return outcome
        _mark_unavailable(store, ref_id=ref_id)
        outcome.given_up = True
        log.info(
            "patent_fulltext_sweep[%s]: gave up - published %s", slug, pub_date_raw
        )
        return outcome

    if dry_run:
        outcome.skipped_dry_run = True
        log.info("patent_fulltext_sweep[%s]: dry-run - would retry", slug)
        return outcome

    # Retry the two full-text endpoints.
    description_xml: bytes = b""
    claims_xml: bytes = b""
    try:
        description_xml = ops.description(slug)
    except OpsNotFound:
        description_xml = b""
    except OpsError as e:
        outcome.error = f"description OPS error: {e}"
        log.warning("patent_fulltext_sweep[%s]: %s", slug, outcome.error)
        return outcome

    try:
        claims_xml = ops.claims(slug)
    except OpsNotFound:
        claims_xml = b""
    except OpsError as e:
        outcome.error = f"claims OPS error: {e}"
        log.warning("patent_fulltext_sweep[%s]: %s", slug, outcome.error)
        return outcome

    outcome.bytes_fetched = len(description_xml) + len(claims_xml)

    # If OPS still 404s on both, bump the retry schedule and move on.
    if not description_xml and not claims_xml:
        next_count = retry_count + 1
        next_at = next_fulltext_retry_at(now=now, retry_count=next_count)
        store.update_ref(
            ref_id=ref_id,
            meta_patch={
                "fulltext_retry_at": next_at.isoformat(),
                "fulltext_retry_count": next_count,
            },
        )
        outcome.still_pending = True
        log.info(
            "patent_fulltext_sweep[%s]: still 404 (attempt #%d, next %s)",
            slug,
            next_count,
            next_at.date().isoformat(),
        )
        return outcome

    # At least one endpoint returned real XML. Parse, embed, persist.
    # Disk cache lands next to the biblio file from the original ingest.
    parsed_id = parse_docdb_id(slug)
    cc, num, kind_full = parsed_id.disk_subpath
    disk_dir = raw_root / cc / num / kind_full
    if description_xml:
        _write_xml_atomic(disk_dir / "description.xml", description_xml)
    if claims_xml:
        _write_xml_atomic(disk_dir / "claims.xml", claims_xml)

    parsed = parse_patent(
        description_xml=description_xml or None,
        claims_xml=claims_xml or None,
    )

    seeds: list[ParsedBlock] = []
    for txt in parsed.description_paragraphs:
        seeds.append(
            ParsedBlock(text=txt, embedding=None, density=classify_density(txt))
        )
    for txt in parsed.claim_texts:
        seeds.append(
            ParsedBlock(text=txt, embedding=None, density=classify_density(txt))
        )
    if embedder is not None and seeds:
        seeds = fill_embeddings(seeds, embedder=embedder)

    # Block positions follow the existing block count so a partial
    # earlier ingest (unlikely, but defensive) doesn't collide.
    offset = store.count_blocks(ref_id)
    inserts = [
        BlockInsert(
            pos=offset + i,
            text=b.text,
            embedding=b.embedding,
            density=b.density,
            token_count=len(b.text.split()),
        )
        for i, b in enumerate(seeds)
    ]
    if inserts:
        store.insert_blocks(ref_id, inserts)

    # Update meta: flip has_description / has_claims, clear retry
    # bookkeeping. Accumulate fair_use_bytes so the rolling-window
    # query sees the additional fetch.
    prior_bytes = int(meta.get("fair_use_bytes", 0) or 0)
    meta_patch: dict[str, Any] = {
        "fair_use_bytes": prior_bytes + outcome.bytes_fetched,
    }
    if description_xml:
        meta_patch["has_description"] = True
    if claims_xml:
        meta_patch["has_claims"] = True
    # Clear retry bookkeeping only when BOTH endpoints are now
    # available. Otherwise schedule one more retry for the
    # still-missing endpoint.
    if description_xml and claims_xml:
        meta_patch["fulltext_retry_at"] = None
        meta_patch["fulltext_retry_count"] = None
    else:
        next_count = retry_count + 1
        next_at = next_fulltext_retry_at(now=now, retry_count=next_count)
        meta_patch["fulltext_retry_at"] = next_at.isoformat()
        meta_patch["fulltext_retry_count"] = next_count
    store.update_ref(ref_id=ref_id, meta_patch=meta_patch)

    # Drop the awaiting tag only once both endpoints are in. Partial
    # success (one endpoint back, the other still 404-ing) stays in
    # the awaiting cohort so the runner keeps trying.
    if description_xml and claims_xml:
        try:
            store.remove_tag(ref_id, Tag.open(AWAITING_FULLTEXT_TAG))
        except Exception:
            # Best-effort — a failed tag removal shouldn't leave the
            # blocks unindexed.
            log.warning(
                "patent_fulltext_sweep[%s]: failed to remove awaiting tag", slug
            )
        outcome.succeeded = True
    else:
        outcome.still_pending = True

    outcome.blocks_added = len(inserts)
    log.info(
        "patent_fulltext_sweep[%s]: %s - +%d blocks",
        slug,
        "done" if outcome.succeeded else "partial",
        outcome.blocks_added,
    )
    return outcome


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _should_give_up(*, pub_date_raw: Any, now: datetime) -> bool:
    """Return True when the patent is older than the give-up window.

    ``pub_date_raw`` may be missing, malformed, or in either
    ``YYYY-MM-DD`` or ``YYYY-MM`` shape (matching what the biblio
    parser normalises). Unparseable dates fall back to "don't give
    up" so we don't silently drop patents with odd meta.
    """
    if not isinstance(pub_date_raw, str) or not pub_date_raw:
        return False
    # Accept the two shapes the biblio parser emits.
    raw = pub_date_raw
    if len(raw) == 7:  # YYYY-MM
        raw = f"{raw}-01"
    try:
        pub = datetime.fromisoformat(raw).replace(tzinfo=UTC)
    except ValueError:
        return False
    return (now - pub) > timedelta(days=FULLTEXT_GIVEUP_DAYS)


def _mark_unavailable(store: Store, *, ref_id: int) -> None:
    """Swap the awaiting tag for the terminal unavailable tag."""
    try:
        store.remove_tag(ref_id, Tag.open(AWAITING_FULLTEXT_TAG))
    except Exception:
        log.warning("patent_fulltext_sweep[%s]: could not remove awaiting tag", ref_id)
    try:
        store.add_tag(ref_id, Tag.open(FULLTEXT_UNAVAILABLE_TAG), set_by="system")
    except Exception:
        log.warning(
            "patent_fulltext_sweep[%s]: could not apply unavailable tag", ref_id
        )
    # Clear retry bookkeeping so listings don't advertise a ghost date.
    store.update_ref(
        ref_id=ref_id,
        meta_patch={
            "fulltext_retry_at": None,
            "fulltext_retry_count": None,
        },
    )


def _write_xml_atomic(target: Path, xml: bytes) -> None:
    """tmp-file + rename — parents created on demand."""
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_bytes(xml)
    tmp.replace(target)


__all__ = [
    "DEFAULT_SWEEP_LIMIT",
    "FulltextSweepOutcome",
    "FulltextSweepSummary",
    "run_fulltext_sweep",
]
