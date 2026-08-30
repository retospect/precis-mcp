"""``draft_refresh_scan`` — the scanner half of Part 2
(``docs/backlog/draft-refresh.md``); Part 1 is the job itself
(:mod:`precis.workers.job_types.draft_refresh`).

Fired every ~4h off the ``draft_refresh_scan`` scheduler cadence
(:mod:`precis.workers.scheduler`), fleet-exactly-once via the standing
lease — no host affinity, no eligibility gate, no spend (minting is free;
the LLM spend lives in the job). Per fire: for each **opted-in** draft
(``meta.draft_refresh.enabled``), rank its sections **stalest-first** —
``min(created_at)`` over each section's live *direct paragraph* chunks,
the same clock the spec defines — and mint ONE ``draft_refresh`` job for
the first section, past ``meta.draft_refresh.staleness_days`` (default
14), whose idem_key isn't already claimed. Falling through to the
next-stalest candidate when the stalest one's idem_key is stuck (a
growth-gate refusal or a still-failing job) keeps one wedged section from
blackholing the whole draft — still exactly one mint per draft per fire.

**One partition function, or the clock and the retire set drift apart.**
The section-boundary rule (a section is a ``heading`` chunk's DFS
subtree; only its *direct* ``paragraph`` children are live prose, tables/
figures/terms/nested subsections are preserved) is
:func:`precis.workers.job_types.draft_refresh._split_body_chunks` —
imported here, never duplicated, so the scan's staleness clock and the
job's actual retire set always agree. A section with zero live direct
paragraphs (including the title heading, which has none) is never a
candidate.

Minting reuses the direct-``insert_ref`` pattern
:func:`precis.workers.materialize._mint_jobs` established: parentless
(system-minted background maintenance — no owning todo/build-subject
exists for a scanner-minted refresh), ``idem_key``-guarded existence
check under the same connection as the insert. The idem_key bakes in the
section's current min ``created_at`` date, so a successful rewrite (fresh
``created_at`` on the new paragraphs) naturally re-arms the section for a
future scan, while an unchanged section re-scans to the same key and
dedups silently — "any status" (including a failed or still-running job)
blocks a re-mint, per the spec.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.workers.runner import BatchResult

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

_HANDLER_NAME = "draft_refresh_scan"

#: Mirrors the spec's default (``docs/backlog/draft-refresh.md``): a
#: section is stale once its live paragraphs are older than two weeks.
_DEFAULT_STALENESS_DAYS = 14

#: Same background priority ``materialize.py`` mints under (lower = more
#: urgent under the ``0014`` ordering) — this is unattended maintenance,
#: never ahead of operator-facing work.
_MINT_PRIO = 8

#: Fallback mint cap when the caller passes a non-positive ``batch_size``
#: — defensive only; opted-in drafts are expected to be few in practice.
_DEFAULT_CAP = 50


@dataclass(frozen=True, slots=True)
class _StaleSection:
    """One heading's staleness candidate on one draft: its anchor, its
    depth + reading-order index (the tie-break axis), and the staleness
    clock itself."""

    heading_dc: str
    depth: int
    order: int
    min_created_at: datetime


def _staleness_days(meta: dict[str, Any]) -> int:
    """``meta.draft_refresh.staleness_days``, defaulting (and falling
    back on any garbage value) to :data:`_DEFAULT_STALENESS_DAYS`."""
    sub = meta.get("draft_refresh")
    raw = sub.get("staleness_days") if isinstance(sub, dict) else None
    if raw is None:
        return _DEFAULT_STALENESS_DAYS
    try:
        days = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_STALENESS_DAYS
    return days if days > 0 else _DEFAULT_STALENESS_DAYS


def _opted_in_drafts(store: Store) -> list[tuple[int, str, dict[str, Any]]]:
    """``(ref_id, slug, meta)`` for every live draft carrying
    ``meta.draft_refresh.enabled = true``.

    ``slug`` isn't a ``refs`` column in v2 — it's a correlated subquery
    against ``ref_identifiers`` (``id_kind='cite_key'``), the same shape
    :meth:`~precis.store.Store.resolve_handle` uses."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, "
            "       (SELECT id_value FROM ref_identifiers ri "
            "         WHERE ri.ref_id = r.ref_id AND ri.id_kind = 'cite_key' "
            "         LIMIT 1) AS slug, "
            "       r.meta "
            "  FROM refs r "
            " WHERE r.kind = 'draft' AND r.retired_at IS NULL "
            "   AND r.meta->'draft_refresh'->>'enabled' = 'true' "
            " ORDER BY r.ref_id"
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2] or {})) for r in rows]


def _created_at_map(store: Store, ref_id: int) -> dict[int, datetime]:
    """``chunk_id -> created_at`` for every live chunk of ``ref_id``.

    ``reading_order`` itself doesn't carry ``created_at`` (it's built for
    the reader/editor path, not the staleness clock), so this is one
    extra per-draft query — cheap, since opted-in drafts are few and each
    is scanned at most once per ~4h fire."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, created_at FROM chunks "
            " WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def _stale_sections(store: Store, ref_id: int) -> list[_StaleSection]:
    """Every heading candidate in ``ref_id`` with at least one live direct
    paragraph, **stalest-first**: oldest clock wins, ties broken by deeper
    heading depth first, then earlier reading order. ``[]`` when no heading
    has any live direct paragraph.

    Walks :meth:`~precis.store.Store.reading_order` once (DFS pre-order):
    for each ``heading`` row, its subtree is every immediately-following
    row whose ``depth`` exceeds the heading's own, up to the next
    same-or-shallower row — identical to the true parent-link subtree
    ``draft_subtree_chunk_ids``/``_scope_chunks`` resolve, since
    ``reading_order`` places a subtree contiguously right after its root.
    :func:`~precis.workers.job_types.draft_refresh._split_body_chunks`
    then partitions it exactly as the job does.

    Returning the WHOLE ranked list (not just the single stalest) lets the
    caller fall through past a section whose idem_key is stuck (a
    growth-gate refusal or a FAILED job pins the same key forever) to the
    next-stalest candidate, instead of a stuck section blackholing the
    whole draft."""
    from precis.workers.job_types.draft_refresh import _split_body_chunks

    rows = store.drafts.reading_order(ref_id)
    created = _created_at_map(store, ref_id)
    candidates: list[_StaleSection] = []
    for i, c in enumerate(rows):
        if c.chunk_kind != "heading":
            continue
        j = i + 1
        subtree = []
        while j < len(rows) and rows[j].depth > c.depth:
            subtree.append(rows[j])
            j += 1
        retire_targets, _preserved = _split_body_chunks(subtree)
        if not retire_targets:
            continue  # no live direct paragraphs — never a candidate
        times = [created[t.chunk_id] for t in retire_targets if t.chunk_id in created]
        if not times:
            continue
        candidates.append(
            _StaleSection(
                heading_dc=c.dc,
                depth=c.depth,
                order=i,
                min_created_at=min(times),
            )
        )
    candidates.sort(key=lambda s: (s.min_created_at, -s.depth, s.order))
    return candidates


def _stalest_section(store: Store, ref_id: int) -> _StaleSection | None:
    """The single stalest heading in ``ref_id`` — the first element of
    :func:`_stale_sections`, or ``None`` when there are no candidates.
    Kept as a thin convenience wrapper (unit-tested selection/tie-break
    surface); :func:`run_draft_refresh_scan` itself walks the full ranked
    list so a stuck section can't blackhole the draft."""
    sections = _stale_sections(store, ref_id)
    return sections[0] if sections else None


def _mint(store: Store, *, slug: str, section: _StaleSection) -> bool:
    """Mint ONE ``draft_refresh`` job for ``section``. Returns ``True``
    iff a new job was inserted, ``False`` when its ``idem_key`` already
    exists (any status — that's the dedup working, not an error)."""
    from precis.store.types import Tag

    scope = section.heading_dc
    idem_key = f"draft_refresh:{slug}:{scope}:{section.min_created_at:%Y-%m-%d}"
    with store.pool.connection() as conn:
        existing = conn.execute(
            "SELECT 1 FROM refs WHERE kind = 'job' AND retired_at IS NULL "
            "AND meta->>'idem_key' = %s LIMIT 1",
            (idem_key,),
        ).fetchone()
        if existing is not None:
            conn.commit()
            return False
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title=f"draft_refresh ({slug}:{scope})",
            meta={
                "job_type": "draft_refresh",
                "executor": "claude_inproc",
                "params": {"draft": slug, "scope": scope},
                "idem_key": idem_key,
            },
            prio=_MINT_PRIO,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "queued"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        conn.commit()
    log.info(
        "draft_refresh_scan: minted draft_refresh job for %s:%s (stale since %s)",
        slug,
        scope,
        section.min_created_at,
    )
    return True


def run_draft_refresh_scan(store: Store, batch_size: int) -> BatchResult:
    """One scan tick: mint at most ``batch_size`` ``draft_refresh`` jobs —
    still exactly ONE per opted-in draft per fire, same return shape as the
    other folded cadences (:func:`precis.workers.materialize.run_materialize_pass`)
    so the scheduler wrapper logs it identically.

    Per draft: walk its over-threshold sections stalest-first
    (:func:`_stale_sections`) and mint the FIRST one whose idem_key doesn't
    already exist, then stop. A section whose idem_key is stuck (a
    growth-gate refusal or a still-failing job pins the same key until the
    chunks change) is skipped in favour of the next-stalest over-threshold
    candidate, rather than blackholing the whole draft — the list is sorted
    stalest-first, so the moment a candidate is no longer past the
    threshold every later one is even fresher and the walk stops."""
    cap = batch_size if batch_size and batch_size > 0 else _DEFAULT_CAP
    now = datetime.now(UTC)
    minted = 0
    failed = 0
    for ref_id, slug, meta in _opted_in_drafts(store):
        if minted >= cap:
            break
        try:
            threshold = timedelta(days=_staleness_days(meta))
            for section in _stale_sections(store, ref_id):
                if now - section.min_created_at <= threshold:
                    break  # stalest-first: nothing further qualifies either
                if _mint(store, slug=slug, section=section):
                    minted += 1
                    break
                # idem_key already exists (stuck job) — try the next-stalest
                # over-threshold candidate instead of giving up on the draft.
        except Exception:
            failed += 1
            log.exception("draft_refresh_scan: scan failed for draft ref %s", ref_id)
    return BatchResult(handler=_HANDLER_NAME, claimed=minted, ok=minted, failed=failed)


__all__ = ["run_draft_refresh_scan"]
