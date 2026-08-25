"""Repair evidence edges that assert support while anchoring no passage.

Design: ``docs/backlog/evidence-edges-assert-support-with-no-passage.md``.
A July batch left 369 evidence edges whose ``meta`` reads, verbatim,
``{"caveats": [], "support": "yes", "source_handle": null}`` with
``src_chunk_id IS NULL`` — an affirmative support verdict for a passage
nobody ever identified. The writing path is fixed; this is a **bounded
backfill over a known cohort**, not a live bug.

**Two cohorts, one repair.** The pass now selects two broken shapes, both
repaired by the same :func:`repair_edge` (which is ``link_id``-driven and does
not care what ``src_chunk_id`` held before):

* **A — anchors no passage** (:func:`select_broken_evidence_edges`, the
  original July batch): ``src_chunk_id IS NULL`` and
  ``meta->'source_handle' = 'null'::jsonb``.
* **B — anchors a passage that cannot be evidence**
  (:func:`select_prose_less_evidence_edges`, gripe 245842): a grounding on a
  paper's title/author front-matter block. Selected by the same clauses except
  the prose test itself, which is a Python predicate
  (:func:`~precis.taproot.grounding.has_grounding_prose`) and so filters in
  Python — which is also why cohort B's ``limit`` applies *after* the filter,
  a prefix of the filtered cohort rather than of the candidate scan.

The two SQL predicates are disjoint on ``src_chunk_id``, so ``--cohort both``
unions them without dedup.

**The pass, in order:**

1. :func:`select_broken_evidence_edges` — the cohort SQL: an evidence edge
   (``establishes``/``corroborates``/``contradicts``) from a live
   paper/patent source, ``src_chunk_id IS NULL`` **and**
   ``meta->'source_handle' = 'null'::jsonb`` (the key is present and
   deliberately empty — the shape that distinguishes this batch), whose
   source still has live body chunks so the passage is actually findable.
   Optionally restricted to hubs a given draft cites (the ``dr42995``
   cohort holds 361 of the 367).
2. :func:`repair_edge` — one edge: read the hub's claim sentence + scope,
   then re-ground it against **only** its already-attached source via
   :func:`~precis.taproot.reground.verify_atoms` with
   ``collect_papers_fn=lambda _s, _h: [source_ref_id]``. A hub's claim is
   just a :class:`~precis.taproot.canon.CanonicalClaim`, which is exactly
   what ``reground`` calls an atom — the library layer needs no migration
   artifact. The verify tier is injectable (``tier=``), so a cohort can be
   re-verified above ``reground``'s default MEDIUM.
3. On success — and only with ``apply=True`` — an in-place
   ``UPDATE links SET src_chunk_id = …, meta = meta || {'source_handle': …}``.

**Why UPDATE and never** :func:`~precis.taproot.hub.attach_evidence`:
``Store.add_link``'s conflict key is ``(src_ref_id, src_chunk_id,
dst_ref_id, dst_chunk_id, relation)``, and the broken row's
``src_chunk_id`` is NULL — so attaching a *grounded* edge inserts a
SECOND row and leaves the broken one live, doubling the defect while
looking fixed. The one existing in-place repair
(``cli/taproot.py::_backfill_grounding`` Part B) has the right write but
the wrong cohort: its candidate SQL requires ``meta->>'source_handle' IS
NOT NULL AND <> 'null'``, which filters this population out by
construction. A ``UniqueViolation`` (a grounded twin already exists) is
reported as :data:`STATUS_DUPLICATE`, never raised.

**The verdict is the part to distrust.** Re-grounding that finds no
supporting passage in a source whose edge says ``support: "yes"`` has not
failed — it has discovered the verdict was empty. That is recorded as
``"verify-rejected"`` and *nothing* is written: neither the edge nor the
claim is touched. There is deliberately **no code path here that writes
``refs.title`` or a ``finding_body`` chunk** — a claim is never edited to
match a source that a passage-less edge merely asserted.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal, cast

from precis.taproot.canon import CanonicalClaim
from precis.taproot.grounding import has_grounding_prose
from precis.taproot.reground import (
    AtomVerifyResult,
    FetchChunksFn,
    PaperChunk,
    VerifyBatchFn,
    _fetch_body_chunks,
    verify_atoms,
    verify_atoms_batch,
)
from precis.utils import handle_registry
from precis.utils.llm.router import Tier

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "BrokenEdge",
    "EdgeRepair",
    "EdgeRepairStatus",
    "repair_edge",
    "select_broken_evidence_edges",
]

#: Candidate passages per atom x paper — mirrors
#: :data:`precis.taproot.reground._DEFAULT_TOP_K`, restated here so this
#: pass's cost shape is tunable (``--top-k``) without re-tuning re-grounding
#: everywhere.
DEFAULT_TOP_K = 6

EdgeRepairStatus = Literal[
    "grounded",
    "duplicate-exists",
    "hub-missing",
    # reground's four named ungrounded reasons, passed through verbatim.
    "no-passage",
    "hearsay-only",
    "verify-rejected",
    "quote-validation-failed",
]

#: Grounded: a verified, quote-validated passage was found. The only
#: status that writes anything.
STATUS_GROUNDED: EdgeRepairStatus = "grounded"
#: A chunk-grounded twin of this edge already exists (``UniqueViolation``
#: on the in-place UPDATE) — reported, never raised. The broken row stays
#: live: deleting it is a separate, human call.
STATUS_DUPLICATE: EdgeRepairStatus = "duplicate-exists"
#: The hub vanished (deleted) between cohort selection and repair —
#: defensive; the cohort SQL only yields live hubs.
STATUS_HUB_MISSING: EdgeRepairStatus = "hub-missing"

#: :mod:`~precis.taproot.reground`'s four ungrounded reasons — the exact
#: taxonomy this backfill needs, so they pass through as statuses rather
#: than being collapsed into one "not repaired". Guarded against (rather
#: than cast blindly) so a future fifth reason surfaces as a loud failure
#: instead of a silently mislabelled row.
_UNGROUNDED_REASONS: frozenset[str] = frozenset(
    {"no-passage", "hearsay-only", "verify-rejected", "quote-validation-failed"}
)


@dataclass(frozen=True)
class BrokenEdge:
    """One row of the broken cohort (:func:`select_broken_evidence_edges`).

    ``source_kind`` is carried from the selection query so
    :func:`repair_edge` can format the grounding handle (``pc<chunk_id>``
    for a paper, the patent code for a patent) without a second lookup.
    """

    link_id: int
    hub_ref_id: int
    source_ref_id: int
    source_kind: str
    relation: str


@dataclass(frozen=True)
class EdgeRepair:
    """One edge's outcome. ``status`` is :data:`STATUS_GROUNDED` (with
    ``chunk_id``/``source_handle``/``quote`` filled) or a named reason
    nothing was written — :data:`STATUS_DUPLICATE`,
    :data:`STATUS_HUB_MISSING`, or one of
    :mod:`~precis.taproot.reground`'s four ungrounded reasons.

    ``applied`` is ``True`` only when the in-place UPDATE actually ran, so
    a dry-run's grounded row is visibly distinct from a written one.
    """

    link_id: int
    hub_ref_id: int
    source_ref_id: int
    status: EdgeRepairStatus
    chunk_id: int | None = None
    source_handle: str | None = None
    quote: str | None = None
    applied: bool = False

    @property
    def grounded(self) -> bool:
        return self.status == STATUS_GROUNDED

    def to_row(self) -> dict[str, Any]:
        """The JSONL row the CLI writes — proposal (dry-run) or record
        (``--apply``); ``reason`` carries the status either way."""
        return {
            "link_id": self.link_id,
            "hub": self.hub_ref_id,
            "source_ref": self.source_ref_id,
            "chunk_id": self.chunk_id,
            "source_handle": self.source_handle,
            "quote": self.quote,
            "reason": self.status,
            "applied": self.applied,
        }


# ── cohort selection ────────────────────────────────────────────────────

#: The broken cohort. ``meta->'source_handle' = 'null'::jsonb`` (jsonb
#: null, not SQL NULL) is the batch's fingerprint: the key was written and
#: left empty, which is why ``_backfill_grounding``'s ``->>'source_handle'
#: IS NOT NULL`` candidate SQL skips every one of them. The ``chunks``
#: EXISTS clause keeps the pass to sources whose text is actually here —
#: the "acquire + ingest first" bucket is not this pass's job.
_BROKEN_EVIDENCE_COHORT_SQL = """
    SELECT l.link_id, l.dst_ref_id, l.src_ref_id, s.kind, l.relation
      FROM links l
      JOIN refs s ON s.ref_id = l.src_ref_id AND s.deleted_at IS NULL
      JOIN refs h ON h.ref_id = l.dst_ref_id AND h.deleted_at IS NULL
     WHERE s.kind IN ('paper', 'patent')
       AND l.relation IN ('establishes', 'corroborates', 'contradicts')
       AND l.src_chunk_id IS NULL
       AND l.meta->'source_handle' = 'null'::jsonb
       AND EXISTS (
             SELECT 1 FROM chunks c
              WHERE c.ref_id = l.src_ref_id
                AND c.ord >= 0
                AND c.retired_at IS NULL
           )
       {draft_clause}
     ORDER BY l.link_id
     {limit_clause}
"""

#: Restrict to hubs a draft cites outbound — the same ``cites`` edge
#: ``handlers/draft.py::sync_draft_links`` writes per citing passage, and
#: the same shape ``nanopub/overview.py::draft_cited_hub_ids`` reads. The
#: broken batch is concentrated in one draft's cohort, so the first real
#: run wants to be scoped to it.
_DRAFT_CLAUSE = """
       AND EXISTS (
             SELECT 1 FROM links dl
              WHERE dl.src_ref_id = %(draft)s
                AND dl.relation = 'cites'
                AND dl.dst_ref_id = l.dst_ref_id
           )
"""


#: Cohort B — the edge names a chunk, but that chunk cannot be evidence:
#: a title/author front-matter block (gripe 245842), or a row that is no longer
#: live. Selected the same way as cohort A except the prose test itself, which
#: is a Python predicate and not expressible in SQL — so this SQL returns the
#: chunk text and :func:`select_prose_less_evidence_edges` filters in Python.
#:
#: The join is a LEFT join **on purpose**: an edge whose ``src_chunk_id`` points
#: at a retired or deleted row would otherwise fall between the two cohorts —
#: cohort A wants ``src_chunk_id IS NULL`` and an inner join here would drop it
#: silently — leaving an edge anchored on text no reader can reach with nothing
#: that ever selects it. A missing row yields ``text = NULL``, which fails the
#: prose test, so it lands in this cohort and gets re-grounded.
_PROSE_LESS_COHORT_SQL = """
    SELECT l.link_id, l.dst_ref_id, l.src_ref_id, s.kind, l.relation, c.text
      FROM links l
      JOIN refs s ON s.ref_id = l.src_ref_id AND s.deleted_at IS NULL
      JOIN refs h ON h.ref_id = l.dst_ref_id AND h.deleted_at IS NULL
      LEFT JOIN chunks c
             ON c.chunk_id = l.src_chunk_id AND c.retired_at IS NULL
     WHERE s.kind IN ('paper', 'patent')
       AND l.relation IN ('establishes', 'corroborates', 'contradicts')
       AND l.src_chunk_id IS NOT NULL
       {draft_clause}
     ORDER BY l.link_id
"""


def select_prose_less_evidence_edges(
    store: Store, *, draft_ref_id: int | None = None, limit: int | None = None
) -> list[BrokenEdge]:
    """Cohort B: evidence edges grounded on a chunk that asserts nothing.

    The mirror of :func:`select_broken_evidence_edges`. Cohort A's edge names
    no passage at all; this one names a passage that cannot support anything —
    a paper's title/author front-matter block, or a chunk row that is no longer
    live (retired by a re-chunk, or deleted). Both are repaired identically by
    :func:`repair_edge`, which is ``link_id``-driven and does not care whether
    ``src_chunk_id`` was NULL or merely wrong.

    ``limit`` is applied in **Python**, after the prose filter, so a limited
    run is a stable prefix of the *filtered* cohort rather than of the
    candidate scan (SQL cannot evaluate
    :func:`~precis.taproot.grounding.has_grounding_prose`). Cohort A limits in
    SQL because its predicate is fully expressible there.
    """
    params: dict[str, Any] = {}
    draft_clause = ""
    if draft_ref_id is not None:
        draft_clause = _DRAFT_CLAUSE
        params["draft"] = draft_ref_id
    sql = _PROSE_LESS_COHORT_SQL.format(draft_clause=draft_clause)
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    out = [
        BrokenEdge(
            link_id=int(r[0]),
            hub_ref_id=int(r[1]),
            source_ref_id=int(r[2]),
            source_kind=str(r[3]),
            relation=str(r[4]),
        )
        for r in rows
        if not has_grounding_prose(str(r[5] or ""))
    ]
    return out[:limit] if limit is not None else out


def select_broken_evidence_edges(
    store: Store, *, draft_ref_id: int | None = None, limit: int | None = None
) -> list[BrokenEdge]:
    """The repairable cohort, oldest ``link_id`` first.

    ``draft_ref_id`` scopes to hubs that draft cites; ``limit`` caps the
    batch (so the first real run can be small). Both are applied in SQL,
    so a limited run is a stable prefix of the full cohort.
    """
    params: dict[str, Any] = {}
    draft_clause = ""
    if draft_ref_id is not None:
        draft_clause = _DRAFT_CLAUSE
        params["draft"] = draft_ref_id
    limit_clause = ""
    if limit is not None:
        limit_clause = "LIMIT %(limit)s"
        params["limit"] = limit
    sql = _BROKEN_EVIDENCE_COHORT_SQL.format(
        draft_clause=draft_clause, limit_clause=limit_clause
    )
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
    return [
        BrokenEdge(
            link_id=int(r[0]),
            hub_ref_id=int(r[1]),
            source_ref_id=int(r[2]),
            source_kind=str(r[3]),
            relation=str(r[4]),
        )
        for r in rows
    ]


# ── per-edge repair ─────────────────────────────────────────────────────


def _fetch_hub_claim(store: Store, hub_ref_id: int) -> CanonicalClaim | None:
    """The hub's claim as re-grounding's own atom shape — ``refs.title``
    (the claim sentence) + ``meta.scope``, exactly what
    ``workers/hub_refine.py::_fetch_hub_info`` reads for its own verify
    step. ``None`` when the hub is gone.

    READ-ONLY, by design: this pass never writes a claim sentence back
    (module docstring).
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT title, meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (hub_ref_id,),
        ).fetchone()
    if row is None:
        return None
    meta = dict(row[1] or {})
    scope = {str(k): str(v) for k, v in (meta.get("scope") or {}).items()}
    return CanonicalClaim(sentence=str(row[0] or "").strip(), scope=scope)


def _apply_grounding(
    store: Store, link_id: int, chunk_id: int, source_handle: str
) -> bool:
    """The in-place repair — ``False`` on ``UniqueViolation`` (a grounded
    twin already holds this ``(src_ref, src_chunk, dst_ref, dst_chunk,
    relation)`` tuple), ``True`` when the row was updated.

    Deliberately a bare UPDATE on the ORIGINAL ``link_id``, not an
    ``attach_evidence`` call — see the module docstring for why attaching
    would double the defect rather than fix it. ``meta ||`` patches the
    one key and preserves the verdict's ``support``/``caveats``.
    """
    from psycopg.errors import UniqueViolation

    try:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE links SET src_chunk_id = %s, "
                "meta = meta || jsonb_build_object('source_handle', %s::text) "
                "WHERE link_id = %s",
                (chunk_id, source_handle, link_id),
            )
    except UniqueViolation:
        log.info(
            "taproot-repair-evidence: link %s already has a chunk-grounded "
            "twin at chunk %s — left as-is",
            link_id,
            chunk_id,
        )
        return False
    return True


def _tiered_verify_batch_fn(tier: Tier) -> VerifyBatchFn:
    """:func:`~precis.taproot.reground.verify_atoms_batch` bound to one
    tier — the per-call tier seam ``verify_atoms``'s ``verify_batch_fn``
    parameter exists to provide. A cohort whose verdicts were written by
    something that read nothing is worth re-verifying above MEDIUM."""

    def _fn(
        atoms: Sequence[CanonicalClaim], passages: Sequence[PaperChunk]
    ) -> list[AtomVerifyResult]:
        return verify_atoms_batch(atoms, passages, tier=tier)

    return _fn


def repair_edge(
    store: Store,
    hub_ref_id: int,
    source_ref_id: int,
    link_id: int,
    *,
    source_kind: str = "paper",
    apply: bool = False,
    tier: Tier = Tier.MEDIUM,
    top_k: int = DEFAULT_TOP_K,
    verify_batch_fn: VerifyBatchFn | None = None,
    fetch_body_chunks_fn: FetchChunksFn | None = None,
) -> EdgeRepair:
    """Re-ground ONE broken evidence edge and (with ``apply=True``) repair
    it in place.

    The hub's claim is re-verified against **only** ``source_ref_id`` —
    the source the edge already names — via
    :func:`~precis.taproot.reground.verify_atoms`, so a passage is found
    in the paper this edge asserts, never in some better-matching other
    paper. Every anti-hallucination guard re-grounding carries applies
    unchanged: hearsay sections excluded, the model's quote re-checked in
    code against the claimed chunk and for uniqueness across the paper.

    ``apply=False`` (the default) computes the proposal and writes
    NOTHING. ``apply=True`` writes only on :data:`STATUS_GROUNDED`, and
    only the two columns of the one ``link_id`` passed in.

    Raises whatever ``verify_batch_fn`` raises — by default
    :class:`~precis.taproot.reground.RegroundingUnavailable` on a dead or
    persistently-malformed dispatch. A caller looping over a cohort should
    catch that per edge: "the model never ran" must never be recorded as
    "no passage supports this".
    """
    claim = _fetch_hub_claim(store, hub_ref_id)
    if claim is None:
        return EdgeRepair(
            link_id=link_id,
            hub_ref_id=hub_ref_id,
            source_ref_id=source_ref_id,
            status=STATUS_HUB_MISSING,
        )

    verify_fn: VerifyBatchFn = verify_batch_fn or _tiered_verify_batch_fn(tier)
    result = verify_atoms(
        store,
        hub_ref_id,
        [claim],
        top_k=top_k,
        verify_batch_fn=verify_fn,
        fetch_body_chunks_fn=fetch_body_chunks_fn or _fetch_body_chunks,
        # The seam this whole pass turns on: search ONLY the source this
        # edge already asserts, not the hub's other provenance.
        collect_papers_fn=lambda _s, _h: [source_ref_id],
    )
    grounding = result.atoms[0]
    if not grounding.grounded:
        # A reason outside the four is an invariant break, not a verdict:
        # `reason is None` means verify never ran (a hub with no candidate
        # papers — impossible here, collect_papers_fn always yields one).
        # Raise rather than record a support judgment nothing produced.
        if grounding.reason not in _UNGROUNDED_REASONS:
            raise ValueError(
                f"re-grounding returned reason {grounding.reason!r} for link "
                f"{link_id} — expected one of {sorted(_UNGROUNDED_REASONS)}"
            )
        return EdgeRepair(
            link_id=link_id,
            hub_ref_id=hub_ref_id,
            source_ref_id=source_ref_id,
            status=cast("EdgeRepairStatus", grounding.reason),
        )

    record = grounding.records[0]
    handle = handle_registry.try_format(source_kind, record.chunk_id, chunk=True)
    if handle is None:  # pragma: no cover — cohort is paper/patent only
        raise ValueError(
            f"no chunk handle code for source kind {source_kind!r} "
            f"(link {link_id}) — evidence sources are paper/patent"
        )

    if not apply:
        return EdgeRepair(
            link_id=link_id,
            hub_ref_id=hub_ref_id,
            source_ref_id=source_ref_id,
            status=STATUS_GROUNDED,
            chunk_id=record.chunk_id,
            source_handle=handle,
            quote=record.quote,
        )

    written = _apply_grounding(store, link_id, record.chunk_id, handle)
    return EdgeRepair(
        link_id=link_id,
        hub_ref_id=hub_ref_id,
        source_ref_id=source_ref_id,
        status=STATUS_GROUNDED if written else STATUS_DUPLICATE,
        chunk_id=record.chunk_id,
        source_handle=handle,
        quote=record.quote,
        applied=written,
    )
