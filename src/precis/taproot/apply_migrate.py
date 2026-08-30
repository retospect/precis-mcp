"""Taproot atomic-claims migration — Phase 2 (apply), the quiet-window
write pass over ``docs/backlog/taproot-atomic-claims.md``'s Strategy.
:mod:`precis.taproot.migrate` (Phase 0/1) is strictly read-only; this
module is the one place that pass's dry-run outcomes turn into writes —
one hub per transaction, resumable, via :func:`apply_dry_run` (per-verdict
routing: its own docstring).

**Quiet window (an operator step, not code)**: pause ``hub_refine``/
``chase_trigger`` and avoid 02:00-03:30 UTC (nightly backup + caspar's
reboot) before ``precis taproot-migrate apply``
(``docs/backlog/taproot-atomic-claims.md`` §"Quiet window definition").
:func:`apply_dry_run` has no opinion on *when*; it only assumes nothing
else mutates the same hubs concurrently.

**Split verdict: the original hub becomes the compound** (minting a *new*
compound would never converge with the legacy hub's own ``pub_id``,
duplicating it forever). Each atom runs the same ``block -> dedup_judge ->
place`` cascade :mod:`precis.taproot.backfill` uses, mints/converges with
**no** evidence edge at placement time, and links
``atom --conjunct-of--> original hub``. A hub's paper provenance re-points
in two different ways:

* **Inbound evidence** (:data:`~precis.taproot.hub.HUB_ROLES`) — every
  live edge is re-pointed via the **add-first invariant**
  (``docs/backlog/taproot-reground-add-first-invariant.md``): verify each
  atom against the edge's grounding passage, add-and-read-back-confirm
  before ever pruning, never prune with zero confirmed replacement adds.
  A *verified*, per-atom re-point, never a blanket copy.
* **Outbound lineage** (``hub --derived-from--> paper``) — copied to
  *every* placed atom, unconditionally: ``derived-from`` asserts
  derivation ("descends from"), not evidential support for a specific
  sentence, so there is nothing per-atom to verify against. The original
  hub keeps its own links too (:attr:`ApplyReport.lineage_copied`).

A hub can carry either shape, both, or neither; an atom that ends the
transaction with no paper reference of its own (every evidence edge
failed verification, no lineage to fall back on) is counted in
:attr:`ApplyReport.atoms_unreferenced` — a visible gap, not fatal.

**Never re-uses :func:`precis.taproot.hub.apply_placement`** for the atom
placement itself, despite reusing everything else that module offers:
``apply_placement`` *always* writes an evidence edge to the paper it's
given (no "mint but don't attach" mode) — backwards from this migration's
need, where the atom's hub must exist *before* the re-point step can ask
which existing edge to verify against. :func:`_place_atom` is the
structural-only analogue; evidence attach happens later, once
verification has named which atom earns which edge.

**Two network-bearing stages never share a transaction with a write**:
the placement cascade (LLM) and ``extract_verify_fn`` (LLM) both run
*before* :func:`apply_dry_run` opens its one ``store.tx()`` per hub — same
pool-exhaustion-deadlock reasoning as
:func:`~precis.taproot.hub.attach_evidence`'s retraction check.

**Atom re-grounding** (``docs/backlog/taproot-atom-regrounding.md``, "no
source, no atom"): :mod:`precis.taproot.reground`'s CLI stage runs
*before* apply and writes an optional ``row["grounding"]`` key (this
module never imports :mod:`precis.taproot.reground` — one-directional,
see :func:`_parse_grounding`). When present:

* An atom marked ungrounded on a hub WITH candidate source papers is
  **withheld** (:func:`_withheld_atoms`) — no placement, no hub, no
  ``conjunct-of`` link; counted in
  :attr:`ApplyReport.atoms_withheld_ungrounded`/``atoms_withheld_reasons``.
* A hanging hub (no candidate papers, or no ``"grounding"`` key at all)
  keeps today's behavior — atoms may place hanging (lineage-only).
* ``grounding["error"]`` (regrounding itself raised) is a **third**
  shape, distinct from present/missing: withholds *every* atom on the
  row (``"reground-error"``), never falls back to the permissive
  missing-key default.
* Every withheld atom is a partial failure, not a zero-child split — no
  stamp (so a retry isn't locked out) plus a needs_review filing.
* A grounded atom's evidence edge's ``meta.source_handle`` is upgraded to
  the grounded record's own chunk anchor when the re-pointed edge shares
  that record's paper (:func:`_grounded_chunk_anchors`) — quote/snip
  storage stays out of the DB (open design call,
  ``claim-publication-nanopub-ots.md``); a grounding record's quote lives
  only in the CLI's JSONL run artifact.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.handlers._link_tag_ops import validate_relation
from precis.store.types import ActorSlug
from precis.taproot.canon import (
    Candidate,
    CanonicalClaim,
    Placement,
    Verdict,
    block,
    dedup_judge,
    merge_confirm,
    place,
)
from precis.taproot.directed import QualifyResult, qualify_claim
from precis.taproot.hub import (
    EVIDENCE_SRC_KINDS,
    HUB_ROLES,
    attach_evidence,
    link_claims,
    mint_hub,
    run_retraction_checks,
)

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

__all__ = [
    "ApplyReport",
    "HubApplyOutcome",
    "NeedsReviewFn",
    "apply_dry_run",
]

#: The Phase-2 idempotency stamp key. Mirrors
#: :mod:`precis.taproot.migrate`'s private ``_DECOMPOSED_AT_META_KEY`` —
#: that module only *reads* this key (to shrink the candidate pool once
#: apply starts landing stamps); this module is the one that writes it, so
#: it needs its own copy of the literal, not an import of a private name.
_DECOMPOSED_AT_META_KEY = "taproot_decomposed_at"

BlockFn = Callable[[CanonicalClaim, Any, Any], list[Candidate]]
JudgeFn = Callable[[str, str], Verdict]
MergeConfirmFn = Callable[[str, str], Verdict]
#: The evidence re-point's one-way claim-vs-evidence check — production
#: default is :func:`~precis.taproot.directed.qualify_claim` (BIG tier).
#: Injectable so tests run with a deterministic stub and no LLM call.
VerifyFn = Callable[[str, str], QualifyResult]
NowFn = Callable[[], datetime]
#: ``(hub_ref_id, reason, detail)`` -> anything. The needs_review filing
#: door, same shape/spirit as :func:`~precis.taproot.hub.apply_placement`'s
#: ``todo_fn`` (default ``None`` degrades to a log warning, never a
#: silently-dropped review) but keyed by hub + free-text reason rather than
#: ``(CanonicalClaim, Placement)`` — this module's needs_review cases (an
#: unverified evidence edge, a no-claim hub with evidence, a would-strand
#: hub) don't all carry a claim/placement pair the way a single-claim
#: canonicalizer outcome does.
NeedsReviewFn = Callable[[int, str, dict[str, Any]], Any]


def _utcnow() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True)
class HubApplyOutcome:
    """One hub's :func:`apply_dry_run` result — the per-hub row
    :attr:`ApplyReport.hubs` collects. ``action`` is one of the
    :class:`ApplyReport` counter names (``"stamped_passthrough"``,
    ``"split_applied"``, ``"skipped_already_stamped"``,
    ``"skipped_verdict"``, ``"no_claim_needs_review"``,
    ``"no_claim_unevidenced"``, or ``"error"`` for a hub this pass
    couldn't process at all — see ``detail``)."""

    hub_ref_id: int
    action: str
    detail: str = ""


@dataclass(frozen=True)
class ApplyReport:
    """The full result of one :func:`apply_dry_run` call — counts per
    action plus :attr:`hubs`, one row per input outcome, for a human (or a
    re-run) to see exactly what happened to which hub.

    Every counter here is a plain total over the ``outcomes`` passed in,
    not a nested breakdown — :attr:`hubs` carries the per-hub detail a
    caller can filter for a specific action or ``partial_failures`` root
    cause.
    """

    stamped_passthrough: int = 0
    split_applied: int = 0
    atoms_placed: int = 0
    atoms_needs_review: int = 0
    edges_repointed: int = 0
    edges_kept_needs_review: int = 0
    #: Total atom -> paper ``derived-from`` links written across every
    #: split hub — the blanket lineage copy (module docstring), one
    #: write per (lineage link, placed atom) pair.
    lineage_copied: int = 0
    #: Placed atoms whose hub carried paper provenance (either shape)
    #: but which ended their hub's transaction with zero direct paper
    #: links of their own — the silent gap the evidence re-point's
    #: verified-only semantics can leave when a hub had no
    #: ``derived-from`` lineage to fall back on. Visible, not fatal:
    #: never aborts the hub, never blocks the stamp.
    atoms_unreferenced: int = 0
    #: Atoms withheld from placement entirely because a *regrounded* row
    #: (``row["grounding"]``, :mod:`precis.taproot.reground`) marked them
    #: ungrounded on a hub that has candidate source papers — "no source,
    #: no atom" (``docs/backlog/taproot-atom-regrounding.md``). Zero on any
    #: row without a ``"grounding"`` key (plain, un-regrounded dry-run
    #: input) or on a hanging hub (no candidate papers at all) — see
    #: :func:`_withheld_atoms`. A withheld atom mints no hub, gets no
    #: ``conjunct-of`` link, and files a needs_review the same way an
    #: unrepointed evidence edge does.
    atoms_withheld_ungrounded: int = 0
    #: ``reason -> count`` breakdown of every :attr:`atoms_withheld_ungrounded`
    #: atom (``"no-passage"``/``"hearsay-only"``/``"verify-rejected"``) —
    #: the run-artifact-level detail :attr:`hubs`' per-hub strings also
    #: carry, aggregated here for a batch-level summary.
    atoms_withheld_reasons: dict[str, int] = field(default_factory=dict)
    skipped_already_stamped: int = 0
    skipped_verdict: int = 0
    no_claim_needs_review: int = 0
    no_claim_unevidenced: int = 0
    #: Sub-hub-granular failures that didn't necessarily abort the whole
    #: hub: one failed evidence add, an unparseable split extraction, a
    #: hub gone missing/deleted since the dry-run, or a whole-hub abort
    #: triggered by the would-strand-to-zero backstop (see
    #: :func:`apply_dry_run`'s module docstring).
    partial_failures: int = 0
    hubs: list[HubApplyOutcome] = field(default_factory=list)


# ── module-local read helpers ─────────────────────────────────────────────
#
# Each of these duplicates a query shape that already exists, privately, in
# hub.py/seniority.py. Deliberate, not an oversight: hub.py's own
# ``_is_compound_hub`` docstring documents this exact seam (each caller
# keeps a connection-agnostic copy of a small predicate rather than the
# modules sharing one) as the established precedent this module follows.


def _hub_meta(store: Store, hub_ref_id: int) -> dict[str, Any] | None:
    """``refs.meta`` for a live ``hub_ref_id``, or ``None`` if it's gone
    (deleted, or never existed) since the dry-run ran."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s AND deleted_at IS NULL",
            (hub_ref_id,),
        ).fetchone()
    return dict(row[0] or {}) if row is not None else None


def _is_compound(store: Store, ref_id: int, *, conn: Any) -> bool:
    """Module-local copy of :func:`precis.taproot.hub._is_compound_hub`
    (private there) — true iff ``ref_id`` carries a live inbound
    ``conjunct-of`` edge from a live ``finding``."""
    row = conn.execute(
        """
        SELECT 1
          FROM links l
          JOIN refs a ON a.ref_id = l.src_ref_id
         WHERE l.dst_ref_id = %s
           AND l.relation = 'conjunct-of'
           AND a.kind = 'finding'
           AND a.deleted_at IS NULL
         LIMIT 1
        """,
        (ref_id,),
    ).fetchone()
    return row is not None


@dataclass(frozen=True)
class _EvidenceEdge:
    """One raw ``paper -> hub`` evidence edge — one row per *edge*, not
    deduped by paper (unlike
    :func:`precis.taproot.seniority.derive_evidence`, which dedups for the
    seniority split): the re-point step below adds/prunes each edge on its
    own, so two edges from the same paper at two different passages must
    stay distinguishable."""

    paper_ref_id: int
    src_chunk_id: int | None
    src_ord: int | None
    relation: str
    meta: dict[str, Any]


def _fetch_evidence_edges(store: Store, hub_ref_id: int) -> list[_EvidenceEdge]:
    """Every live evidence edge landing on ``hub_ref_id`` — module-local
    copy of :func:`precis.taproot.seniority._fetch_evidence_rows`'s query
    shape, extended to also project the edge's chunk ``ord`` (needed by
    :func:`~precis.taproot.hub.attach_evidence`/
    :meth:`~precis.store.Store.remove_link`, which take a ``pos``/``ord``,
    not a raw ``chunk_id``). Excludes a hub<->hub ``contradicts`` link the
    same way: a ``finding`` source is never in
    :data:`~precis.taproot.hub.EVIDENCE_SRC_KINDS`.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.src_ref_id, l.src_chunk_id, sc.ord, l.relation, l.meta
              FROM links l
              JOIN refs p ON p.ref_id = l.src_ref_id
              LEFT JOIN chunks sc ON sc.chunk_id = l.src_chunk_id
             WHERE l.dst_ref_id = %(hub)s
               AND l.relation = ANY(%(roles)s)
               AND p.kind = ANY(%(kinds)s)
               AND p.deleted_at IS NULL
            """,
            {
                "hub": hub_ref_id,
                "roles": list(HUB_ROLES),
                "kinds": list(EVIDENCE_SRC_KINDS),
            },
        ).fetchall()
    return [
        _EvidenceEdge(
            paper_ref_id=int(r[0]),
            src_chunk_id=int(r[1]) if r[1] is not None else None,
            src_ord=int(r[2]) if r[2] is not None else None,
            relation=str(r[3]),
            meta=dict(r[4] or {}),
        )
        for r in rows
    ]


def _passage_text(store: Store, edge: _EvidenceEdge) -> str | None:
    """Best-effort grounding-passage text for one evidence edge — the
    read-side mirror of :func:`precis.taproot.hub._grounding_chunk_ord`,
    returning the chunk's *text* (``extract_verify_fn`` needs a passage to
    argue against, not a pointer).

    Two grounding forms: ``links.src_chunk_id`` already resolved at write
    time, or ``meta['source_handle']`` (a ``pc<chunk_id>`` handle or
    ``slug~ord`` chase pointer) when unset. ``None`` when neither resolves
    to a live body chunk of this edge's own paper — the caller then
    verifies against ``""``, which ``extract_verify_fn`` already treats as
    unsupported (safe degrade: kept + needs_review, never guessed at).
    """
    if edge.src_chunk_id is not None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE chunk_id = %s AND retired_at IS NULL",
                (edge.src_chunk_id,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    handle = edge.meta.get("source_handle")
    if not handle or not isinstance(handle, str):
        return None

    candidate: int | None = None
    try:
        resolved = store.resolve_handle(handle)
    except Exception:  # defensive — a malformed handle never raises
        resolved = None
    if resolved is not None and getattr(resolved, "chunk_id", None) is not None:
        if resolved.ref_id != edge.paper_ref_id or resolved.chunk_ord is None:
            return None
        candidate = resolved.chunk_ord
    else:
        _, sep, tail = handle.rpartition("~")
        if not sep:
            return None
        try:
            candidate = int(tail)
        except ValueError:
            return None

    if candidate is None or candidate < 0:
        return None
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND ord = %s "
            "AND retired_at IS NULL AND ord >= 0",
            (edge.paper_ref_id, candidate),
        ).fetchone()
    return str(row[0]) if row is not None else None


@dataclass(frozen=True)
class _LineageLink:
    """One raw ``hub -> paper`` outbound ``derived-from`` lineage link —
    the shape (b) provenance :func:`_fetch_evidence_edges` never reads
    (module docstring). Unlike :class:`_EvidenceEdge` this is never
    re-pointed/pruned; it is copied blanket onto every placed atom, so
    there is no ``relation`` field to carry (always ``'derived-from'``)
    and no verification-relevant fields either."""

    paper_ref_id: int
    dst_chunk_id: int | None
    dst_ord: int | None


def _fetch_lineage_links(store: Store, hub_ref_id: int) -> list[_LineageLink]:
    """Every live outbound ``derived-from`` lineage link from
    ``hub_ref_id`` to a paper-shaped ref — the src/dst-flipped mirror of
    :func:`_fetch_evidence_edges`'s query shape: there the paper is the
    edge's source and the hub the destination (evidence flows paper ->
    hub); here the hub is the source and the paper the destination
    (lineage flows hub -> paper). Projects the destination chunk's
    ``ord`` (needed to call :func:`~precis.store.Store.add_link` with
    ``dst_pos``, not a raw ``chunk_id``) the same way
    :func:`_fetch_evidence_edges` projects the source chunk's.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.dst_ref_id, l.dst_chunk_id, dc.ord
              FROM links l
              JOIN refs p ON p.ref_id = l.dst_ref_id
              LEFT JOIN chunks dc ON dc.chunk_id = l.dst_chunk_id
             WHERE l.src_ref_id = %(hub)s
               AND l.relation = 'derived-from'
               AND p.kind = ANY(%(kinds)s)
               AND p.deleted_at IS NULL
            """,
            {"hub": hub_ref_id, "kinds": list(EVIDENCE_SRC_KINDS)},
        ).fetchall()
    return [
        _LineageLink(
            paper_ref_id=int(r[0]),
            dst_chunk_id=int(r[1]) if r[1] is not None else None,
            dst_ord=int(r[2]) if r[2] is not None else None,
        )
        for r in rows
    ]


def _atom_paper_ref_count(conn: Any, atom_hub_id: int) -> int:
    """How many direct paper references ``atom_hub_id`` carries right
    now, on the same (possibly not-yet-committed) ``conn`` an in-flight
    split transaction is using — the sum of its live inbound evidence
    edges (:data:`HUB_ROLES`) and its live outbound ``derived-from``
    lineage links. Feeds :attr:`ApplyReport.atoms_unreferenced`: zero
    here, on a hub that had provenance to begin with, is the silent gap
    a verified-only evidence re-point can leave when there was no
    lineage link to fall back on."""
    row = conn.execute(
        """
        SELECT
            (SELECT count(*)
               FROM links l JOIN refs p ON p.ref_id = l.src_ref_id
              WHERE l.dst_ref_id = %(id)s
                AND l.relation = ANY(%(roles)s)
                AND p.kind = ANY(%(kinds)s)
                AND p.deleted_at IS NULL)
          + (SELECT count(*)
               FROM links l JOIN refs p ON p.ref_id = l.dst_ref_id
              WHERE l.src_ref_id = %(id)s
                AND l.relation = 'derived-from'
                AND p.kind = ANY(%(kinds)s)
                AND p.deleted_at IS NULL)
        """,
        {
            "id": atom_hub_id,
            "roles": list(HUB_ROLES),
            "kinds": list(EVIDENCE_SRC_KINDS),
        },
    ).fetchone()
    return int(row[0]) if row is not None else 0


def _live_evidence_count(store: Store, conn: Any, hub_ref_ids: list[int]) -> int:
    """Total live evidence edges landing on any of ``hub_ref_ids`` — the
    add-first invariant's post-write backstop
    (``docs/backlog/taproot-reground-add-first-invariant.md`` step 3):
    "re-check count(live evidence edges) > 0" after the transaction's adds
    and prunes are staged, still inside the same connection so it sees
    them. Runs against the SAME ``conn`` the writes used (not a fresh
    ``store.pool.connection()``) — a not-yet-committed transaction is only
    visible to its own connection."""
    row = conn.execute(
        """
        SELECT count(*)
          FROM links l
          JOIN refs p ON p.ref_id = l.src_ref_id
         WHERE l.dst_ref_id = ANY(%(ids)s)
           AND l.relation = ANY(%(roles)s)
           AND p.kind = ANY(%(kinds)s)
           AND p.deleted_at IS NULL
        """,
        {
            "ids": hub_ref_ids,
            "roles": list(HUB_ROLES),
            "kinds": list(EVIDENCE_SRC_KINDS),
        },
    ).fetchone()
    return int(row[0]) if row is not None else 0


# ── parsing the dry-run JSONL rows ────────────────────────────────────────


def _parse_claim(d: dict[str, Any]) -> CanonicalClaim:
    return CanonicalClaim(
        sentence=str(d.get("sentence") or ""), scope=dict(d.get("scope") or {})
    )


def _parse_atoms(extraction: dict[str, Any] | None) -> list[CanonicalClaim]:
    """The atom sentences+scopes out of one row's ``extraction`` dict —
    the same shape :func:`precis.taproot.migrate.dump_outcomes_jsonl`
    serializes (``_extraction_to_dict``, private there; this is this
    module's own deserializer for that JSON shape, not an import of the
    private encoder's inverse)."""
    if not extraction:
        return []
    return [_parse_claim(a) for a in extraction.get("atoms") or []]


def _parse_grounding(row: dict[str, Any]) -> dict[str, Any] | None:
    """The optional ``"grounding"`` key a *regrounded* JSONL row carries —
    the shape :mod:`precis.taproot.reground`'s CLI stage writes, or the
    ``{"error": "<message>"}`` sentinel when re-grounding itself raised
    (module docstring's "Atom re-grounding" section covers both shapes and
    the missing-key-vs-error distinction). Read back as a plain dict —
    this module never imports :mod:`precis.taproot.reground`
    (one-directional).
    """
    grounding = row.get("grounding")
    return grounding if isinstance(grounding, dict) else None


def _withheld_atoms(
    atoms: list[CanonicalClaim], grounding: dict[str, Any] | None
) -> dict[int, str]:
    """``atom index -> withholding reason`` for every atom this hub's
    ``grounding`` marks ungrounded, on a hub WITH candidate source papers
    (module docstring's atom-regrounding rules; a hanging hub's atoms are
    never withheld).

    A ``grounding["error"]`` sentinel (:func:`_parse_grounding`) withholds
    **every** atom unconditionally (``"reground-error"``), regardless of
    ``paper_ref_ids``. Otherwise empty when ``grounding`` is absent or the
    hub is hanging.
    """
    if not grounding:
        return {}
    error = grounding.get("error")
    if isinstance(error, str) and error:
        return {i: "reground-error" for i in range(len(atoms))}
    if not (grounding.get("paper_ref_ids") or []):
        return {}
    withheld: dict[int, str] = {}
    for i, entry in enumerate(grounding.get("atoms") or []):
        if i >= len(atoms) or not isinstance(entry, dict) or entry.get("grounded"):
            continue
        reason = entry.get("reason")
        withheld[i] = (
            reason if isinstance(reason, str) and reason else "verify-rejected"
        )
    return withheld


def _grounded_chunk_anchors(
    atoms: list[CanonicalClaim], grounding: dict[str, Any] | None
) -> dict[tuple[int, int], int]:
    """``(atom_index, paper_ref_id) -> chunk_id`` out of a grounding row's
    per-atom ``records`` — the grounded-chunk-anchor upgrade the evidence
    re-point step below applies: when it repoints an existing edge from
    paper P onto an atom that re-grounding also found a P-sourced
    :class:`~precis.taproot.reground.GroundedRecord` for, the new edge's
    ``meta.source_handle`` points at *that* record's chunk rather than
    whatever the original edge's meta carried. An atom with no matching
    entry here (no ``"grounding"`` key, a hanging hub, or simply no record
    for that particular paper) falls back to the edge's own pre-existing
    meta, exactly as before re-grounding existed."""
    anchors: dict[tuple[int, int], int] = {}
    if not grounding:
        return anchors
    for i, entry in enumerate(grounding.get("atoms") or []):
        if i >= len(atoms) or not isinstance(entry, dict):
            continue
        for rec in entry.get("records") or []:
            if not isinstance(rec, dict):
                continue
            paper_ref_id = rec.get("paper_ref_id")
            chunk_id = rec.get("chunk_id")
            if isinstance(paper_ref_id, int) and isinstance(chunk_id, int):
                anchors[(i, paper_ref_id)] = chunk_id
    return anchors


# ── atom placement — structural only, no evidence edge ────────────────────


def _run_cascade(
    claim: CanonicalClaim,
    store: Store,
    embedder: Any,
    *,
    block_fn: BlockFn,
    judge_fn: JudgeFn,
    merge_confirm_fn: MergeConfirmFn,
) -> Placement:
    """The ``block -> dedup_judge -> place`` tail for one atom — mirrors
    :mod:`precis.taproot.backfill`'s private ``_place_one`` closure (not
    importable from here). No writes; every call here is a read (``block``)
    or an LLM dispatch (``judge_fn``, and ``merge_confirm_fn`` on a
    low-confidence ``same``) — this must run before the caller ever opens
    a transaction (module docstring)."""
    candidates = block_fn(claim, store, embedder)
    judged = [(cand, judge_fn(claim.sentence, cand.claim)) for cand in candidates]
    return place(claim, judged, merge_confirm_fn=merge_confirm_fn)


def _place_atom(
    store: Store,
    claim: CanonicalClaim,
    placement: Placement,
    *,
    set_by: ActorSlug,
    file_review: Callable[[str], None],
    conn: Any,
) -> int | None:
    """Mint-or-converge one atom hub with **no** evidence edge — the atom
    counterpart of :func:`precis.taproot.hub._apply_compound_placement`'s
    mint-without-evidence shape. Evidence is attached entirely by the
    separate re-point step in :func:`apply_dry_run`, never at placement
    time.

    * ``"attach"`` onto a hub that turns out to be a **compound**
      downgrades to needs_review (mirrors
      :func:`~precis.taproot.hub.apply_placement`'s own compound
      downgrade) — an atom must never become one of a compound's evidence
      holders without first being one of its ``conjunct-of`` atoms.
    * ``"new"``/``"new_contradicts"`` mints via
      :func:`~precis.taproot.hub.mint_hub` (no ``paper_ref_id``);
      ``new_contradicts`` also links the fresh hub ``contradicts`` its
      opposite-claim candidate.
    * ``"needs_review"`` files the review and mints nothing.

    Returns the atom's hub ref_id, or ``None`` on any needs_review path.
    """
    action = placement.action
    if action == "attach":
        hub_ref_id = placement.hub_ref_id
        if hub_ref_id is None:
            raise BadInput("attach placement has no hub_ref_id")
        if _is_compound(store, hub_ref_id, conn=conn):
            file_review(
                f"atom {claim.sentence!r} placed 'attach' onto compound "
                f"hub_ref_id={hub_ref_id} — downgraded to needs_review "
                "(an atom never attaches onto another compound)"
            )
            return None
        return hub_ref_id

    if action in ("new", "new_contradicts"):
        hub_id = mint_hub(store, claim, set_by=set_by, conn=conn)
        if action == "new_contradicts":
            if placement.contradicts_hub_ref_id is None:
                raise BadInput(
                    "new_contradicts placement has no contradicts_hub_ref_id"
                )
            store.add_link(
                src_ref_id=hub_id,
                dst_ref_id=placement.contradicts_hub_ref_id,
                relation=validate_relation("contradicts", store=store),
                set_by=set_by,
                conn=conn,
            )
        return hub_id

    if action == "needs_review":
        file_review(f"atom {claim.sentence!r} needs_review: {placement.reason}")
        return None

    raise BadInput(f"unknown placement action: {action!r}")  # pragma: no cover


# ── the apply pass ─────────────────────────────────────────────────────────


def apply_dry_run(
    store: Store,
    outcomes: list[dict[str, Any]],
    *,
    extract_verify_fn: VerifyFn = qualify_claim,
    now_fn: NowFn = _utcnow,
    embedder: Any = None,
    block_fn: BlockFn = block,
    judge_fn: JudgeFn = dedup_judge,
    merge_confirm_fn: MergeConfirmFn = merge_confirm,
    todo_fn: NeedsReviewFn | None = None,
    set_by: ActorSlug = "agent",
) -> ApplyReport:
    """Phase 2: apply a Phase-1 dry-run report's outcomes to production
    hubs — one transaction per hub, resumable (module docstring).

    ``outcomes`` is the already-``json.loads``'d row list from
    :func:`precis.taproot.migrate.dump_outcomes_jsonl`'s JSONL (one dict
    per line — the CLI reads/parses/filters the file before calling this;
    this function makes no assumption about ordering or which subset it
    was handed). Each row's ``"hub"`` key is the hub's ``ref_id``.

    Per hub:

    1. Skip (``skipped_already_stamped``) if ``meta.taproot_decomposed_at``
       is already set, or (partial failure) if the hub is gone (deleted
       since the dry-run ran).
    2. ``"pass-through"`` -> stamp only.
    3. ``"no-claim"`` -> needs_review if the hub has evidence, else counted
       and left untouched.
    4. ``"lossy"``/``"nested"``/``"error"``/anything unrecognized ->
       counted, untouched (``skipped_verdict``).
    5. ``"split"`` -> the atom cascade + evidence re-point (see the module
       docstring); a hub whose split extraction didn't actually carry
       ``>=2`` atoms is a partial failure (the dry-run/apply contract
       broke somewhere upstream), not a silent no-op.

    ``extract_verify_fn``/``block_fn``/``judge_fn``/``merge_confirm_fn``
    default to the real implementations; each is injectable for a fully
    offline test. ``now_fn`` is the stamp's clock (injectable). ``embedder``
    threads to ``block_fn`` unchanged (``None`` is a legal degrade for a
    faked ``block_fn``, same convention as :mod:`precis.taproot.backfill`).
    """

    def _needs_review(hub_ref_id: int, reason: str, detail: dict[str, Any]) -> None:
        if todo_fn is not None:
            todo_fn(hub_ref_id, reason, detail)
        else:
            log.warning(
                "taproot-apply: needs_review for hub_ref_id=%s: %s (%s)",
                hub_ref_id,
                reason,
                detail,
            )

    stamped_passthrough = 0
    split_applied = 0
    atoms_placed = 0
    atoms_needs_review = 0
    edges_repointed = 0
    edges_kept_needs_review = 0
    lineage_copied = 0
    atoms_unreferenced = 0
    atoms_withheld_ungrounded = 0
    atoms_withheld_reasons: dict[str, int] = {}
    skipped_already_stamped = 0
    skipped_verdict = 0
    no_claim_needs_review = 0
    no_claim_unevidenced = 0
    partial_failures = 0
    hub_rows: list[HubApplyOutcome] = []

    for row in outcomes:
        hub_ref_id = int(row["hub"])
        verdict = row.get("verdict")

        meta = _hub_meta(store, hub_ref_id)
        if meta is None:
            log.warning(
                "taproot-apply: hub_ref_id=%s not found (deleted since the "
                "dry-run?) — skipping",
                hub_ref_id,
            )
            partial_failures += 1
            hub_rows.append(
                HubApplyOutcome(hub_ref_id, "error", "hub not found/deleted")
            )
            continue
        if _DECOMPOSED_AT_META_KEY in meta:
            skipped_already_stamped += 1
            hub_rows.append(HubApplyOutcome(hub_ref_id, "skipped_already_stamped"))
            continue

        if verdict == "pass-through":
            with store.tx() as c:
                store.update_ref(
                    hub_ref_id,
                    meta_patch={_DECOMPOSED_AT_META_KEY: now_fn().isoformat()},
                    conn=c,
                )
            stamped_passthrough += 1
            hub_rows.append(HubApplyOutcome(hub_ref_id, "stamped_passthrough"))
            continue

        if verdict == "no-claim":
            # Only verdict shapes that can ever touch evidence pay the
            # extra round trip (P2-13-ish 1,346-hub scale note): a
            # pass-through/lossy/nested/error hub never reads its edges.
            edges = _fetch_evidence_edges(store, hub_ref_id)
            if edges:
                _needs_review(
                    hub_ref_id,
                    "no-claim verdict on a hub carrying live evidence edges",
                    {"n_edges": len(edges)},
                )
                no_claim_needs_review += 1
                hub_rows.append(HubApplyOutcome(hub_ref_id, "no_claim_needs_review"))
            else:
                no_claim_unevidenced += 1
                hub_rows.append(HubApplyOutcome(hub_ref_id, "no_claim_unevidenced"))
            continue

        if verdict != "split":
            # "lossy" / "nested" / "error" / anything this build doesn't
            # recognize — never stamp a still-possibly-compound hub
            # (docs/backlog/taproot-atomic-claims.md P2-12 invariant).
            skipped_verdict += 1
            hub_rows.append(
                HubApplyOutcome(hub_ref_id, "skipped_verdict", detail=str(verdict))
            )
            continue

        # verdict == "split"
        edges = _fetch_evidence_edges(store, hub_ref_id)
        lineage_links = _fetch_lineage_links(store, hub_ref_id)
        atoms = _parse_atoms(row.get("extraction"))
        grounding = _parse_grounding(row)
        withheld_reasons_by_idx = _withheld_atoms(atoms, grounding)
        grounded_chunk_anchors = _grounded_chunk_anchors(atoms, grounding)
        if len(atoms) < 2:
            log.warning(
                "taproot-apply: hub_ref_id=%s verdict='split' but its "
                "extraction carries <2 atoms — treating as a partial "
                "failure rather than silently skipping",
                hub_ref_id,
            )
            partial_failures += 1
            hub_rows.append(
                HubApplyOutcome(
                    hub_ref_id, "error", "split verdict with fewer than 2 atoms"
                )
            )
            continue

        if len(withheld_reasons_by_idx) == len(atoms):
            # Every atom withheld ungrounded (either every atom individually
            # failed re-grounding on a papered hub, or the whole hub carries
            # a re-grounding error sentinel — _withheld_atoms) -- a
            # zero-child split. Mirrors the <2-atoms guard above: partial
            # failure, no stamp, so a re-run (after the doer-paper hunt
            # lands better evidence, or re-grounding is re-run) picks this
            # hub back up rather than skipped_already_stamped locking it
            # out forever. Unlike that guard, this DOES file a needs_review
            # -- a hub that re-grounding entirely rejected (or couldn't
            # check) is worth a human look, not just a silent retry.
            reason_counts: dict[str, int] = {}
            for reason in withheld_reasons_by_idx.values():
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            log.warning(
                "taproot-apply: hub_ref_id=%s verdict='split' but every "
                "atom was withheld ungrounded (%s) -- treating as a "
                "partial failure, no stamp, rather than a zero-child split "
                "(retryable on a re-run)",
                hub_ref_id,
                reason_counts,
            )
            partial_failures += 1
            hub_rows.append(
                HubApplyOutcome(
                    hub_ref_id,
                    "error",
                    f"split verdict with every atom withheld ungrounded: {reason_counts}",
                )
            )
            _needs_review(
                hub_ref_id,
                "every atom withheld ungrounded -- hub not stamped, retryable",
                {"reasons": reason_counts},
            )
            continue

        # ── Phase A: network/read work, no open transaction ─────────────
        # A withheld (ungrounded, on a papered hub) atom never runs the
        # placement cascade at all — cheaper (no wasted dedup_judge/
        # merge_confirm dispatch) and correct (there is nothing to place).
        placements: list[Placement | None] = [
            None
            if i in withheld_reasons_by_idx
            else _run_cascade(
                atom,
                store,
                embedder,
                block_fn=block_fn,
                judge_fn=judge_fn,
                merge_confirm_fn=merge_confirm_fn,
            )
            for i, atom in enumerate(atoms)
        ]
        passages = [_passage_text(store, e) for e in edges]
        # edge index -> atom indices whose sentence the passage supports.
        verified_atoms: list[list[int]] = []
        for passage in passages:
            hits = [
                a_idx
                for a_idx, atom in enumerate(atoms)
                if extract_verify_fn(atom.sentence, passage or "").supported
            ]
            verified_atoms.append(hits)

        # ── Phase B: one transaction, structural + evidence writes only ─
        hub_partial_failures = 0
        hub_atoms_placed = 0
        hub_atoms_review = 0
        hub_edges_repointed = 0
        hub_edges_review = 0
        hub_lineage_copied = 0
        hub_atoms_unreferenced = 0
        hub_atoms_withheld = 0
        hub_withheld_reasons: dict[str, int] = {}
        aborted = False
        pending_checks: list[int] = []
        # needs_review filings are collected here, never fired immediately —
        # a later write in this SAME hub can still raise and roll the whole
        # transaction back, and `todo_fn` commits its own separate
        # transaction the moment it's called (chase.py's "side-effect for a
        # human" pattern). Firing eagerly would leave a real todo on disk
        # for a hub whose atoms/edges never actually landed, so a re-run
        # (which re-derives the identical decision from scratch, the stamp
        # never having been written) files a DUPLICATE. Flushed only after
        # the `with store.tx()` below commits; on rollback, discarded —
        # the re-run re-derives them for free.
        pending_reviews: list[tuple[str, dict[str, Any]]] = []

        def _atom_review(
            msg: str, *, _reviews: list[tuple[str, dict[str, Any]]] = pending_reviews
        ) -> None:
            _reviews.append((msg, {}))

        try:
            with store.tx() as c:
                atom_hub_ids: list[int | None] = []
                for i, (atom, placement) in enumerate(
                    zip(atoms, placements, strict=True)
                ):
                    if placement is None:
                        # Withheld: re-grounding found this atom ungrounded
                        # on a hub that has candidate source papers — "no
                        # source, no atom" (docs/backlog/
                        # taproot-atom-regrounding.md). No hub minted, no
                        # conjunct-of link, no cascade ever ran for it.
                        atom_hub_ids.append(None)
                        reason = withheld_reasons_by_idx[i]
                        hub_atoms_withheld += 1
                        hub_withheld_reasons[reason] = (
                            hub_withheld_reasons.get(reason, 0) + 1
                        )
                        pending_reviews.append(
                            (
                                f"atom withheld — ungrounded ({reason})",
                                {"atom": atom.sentence, "reason": reason},
                            )
                        )
                        continue
                    hub_id = _place_atom(
                        store,
                        atom,
                        placement,
                        set_by=set_by,
                        file_review=_atom_review,
                        conn=c,
                    )
                    atom_hub_ids.append(hub_id)
                    if hub_id is None:
                        hub_atoms_review += 1
                        continue
                    hub_atoms_placed += 1
                    link_claims(
                        store,
                        from_hub_ref_id=hub_id,
                        to_hub_ref_id=hub_ref_id,
                        relation="conjunct-of",
                        set_by=set_by,
                        conn=c,
                    )

                # Outbound lineage — blanket copy onto every placed atom
                # (module docstring: 'derived-from' asserts derivation, not
                # evidential support, so there is nothing per-atom to
                # verify). `store.add_link` is idempotent on the unique
                # endpoint tuple, so this is safe to re-derive if this hub's
                # transaction is ever retried from scratch.
                for lineage in lineage_links:
                    for atom_hub_id in atom_hub_ids:
                        if atom_hub_id is None:
                            continue
                        store.add_link(
                            src_ref_id=atom_hub_id,
                            dst_ref_id=lineage.paper_ref_id,
                            relation="derived-from",
                            dst_pos=lineage.dst_ord,
                            set_by=set_by,
                            conn=c,
                        )
                        hub_lineage_copied += 1

                for e_idx, edge in enumerate(edges):
                    # (atom_index, atom_hub_id) pairs, not bare hub ids —
                    # the atom index is needed below to look up this
                    # edge's paper in grounded_chunk_anchors (the
                    # chunk-anchor upgrade re-grounding contributes).
                    candidates: list[tuple[int, int]] = [
                        (a, hid)
                        for a in verified_atoms[e_idx]
                        if (hid := atom_hub_ids[a]) is not None
                    ]
                    candidate_hub_ids: list[int] = [hid for _a, hid in candidates]
                    if not candidate_hub_ids:
                        hub_edges_review += 1
                        # Distinguish WHY nothing landed: an atom that
                        # verified against this passage but was WITHHELD
                        # (re-grounding rejected it) reads very differently
                        # in the review queue than "genuinely nothing
                        # verified" — the former means "the extractor's
                        # sentence matched this passage but the atom itself
                        # didn't check out", the latter means "this edge's
                        # passage doesn't support any atom at all".
                        verified_idxs = verified_atoms[e_idx]
                        withheld_only = bool(verified_idxs) and all(
                            a in withheld_reasons_by_idx for a in verified_idxs
                        )
                        if withheld_only:
                            pending_reviews.append(
                                (
                                    "the only atom(s) that verified against "
                                    "this evidence edge were withheld as "
                                    "ungrounded — edge kept on the original "
                                    "hub",
                                    {
                                        "paper_ref_id": edge.paper_ref_id,
                                        "relation": edge.relation,
                                        "withheld_reasons": [
                                            withheld_reasons_by_idx[a]
                                            for a in verified_idxs
                                        ],
                                    },
                                )
                            )
                        else:
                            pending_reviews.append(
                                (
                                    "no atom verified against an existing evidence "
                                    "edge — kept on the original hub",
                                    {
                                        "paper_ref_id": edge.paper_ref_id,
                                        "relation": edge.relation,
                                    },
                                )
                            )
                        continue

                    # Add-first: attach to every verified atom, read back
                    # each add's commit before ever touching the original.
                    confirmed: list[int] = []
                    for atom_idx, atom_hub_id in candidates:
                        # Grounded-chunk-anchor upgrade: when re-grounding
                        # (precis.taproot.reground) also produced a
                        # GroundedRecord for this exact (atom, paper) pair,
                        # anchor the new edge at THAT chunk rather than
                        # whatever the original edge's meta carried — the
                        # module docstring's "grounded atoms place as
                        # today PLUS their evidence edge uses the grounded
                        # chunk anchor" rule. No matching record (no
                        # grounding data, or none for this paper) falls
                        # back to the edge's own meta unchanged.
                        anchor_chunk_id = grounded_chunk_anchors.get(
                            (atom_idx, edge.paper_ref_id)
                        )
                        atom_edge_meta = (
                            {**edge.meta, "source_handle": f"pc{anchor_chunk_id}"}
                            if anchor_chunk_id is not None
                            else edge.meta
                        )
                        try:
                            with c.transaction():  # savepoint — isolate one add
                                attach_evidence(
                                    store,
                                    hub_ref_id=atom_hub_id,
                                    paper_ref_id=edge.paper_ref_id,
                                    role=edge.relation,
                                    meta=atom_edge_meta,
                                    set_by=set_by,
                                    conn=c,
                                    pending_checks=pending_checks,
                                )
                        except Exception:
                            log.warning(
                                "taproot-apply: evidence add failed "
                                "(hub_ref_id=%s paper_ref_id=%s -> atom "
                                "hub_ref_id=%s)",
                                hub_ref_id,
                                edge.paper_ref_id,
                                atom_hub_id,
                                exc_info=True,
                            )
                            hub_partial_failures += 1
                            continue
                        # Confirm "a live link now exists" — NOT that its
                        # src_chunk_id exactly matches the original edge's.
                        # attach_evidence re-derives grounding from
                        # meta['source_handle'] at write time and can
                        # legitimately degrade to a ref-level edge (e.g. the
                        # grounding chunk was retired since the original
                        # edge was written) even on a fully successful add —
                        # an exact chunk-id match would misread that as a
                        # failure and needlessly keep the original edge
                        # (issue: over-strict confirm).
                        confirm_row = c.execute(
                            "SELECT 1 FROM links WHERE src_ref_id = %s "
                            "AND dst_ref_id = %s AND relation = %s",
                            (edge.paper_ref_id, atom_hub_id, edge.relation),
                        ).fetchone()
                        if confirm_row is not None:
                            confirmed.append(atom_hub_id)
                        else:  # pragma: no cover — defensive, add didn't raise
                            hub_partial_failures += 1

                    if not confirmed:
                        # Every add failed — never prune. File review
                        # rather than silently leaving the edge in place
                        # unexplained.
                        hub_edges_review += 1
                        pending_reviews.append(
                            (
                                "atom evidence add(s) failed to commit — edge "
                                "kept on the original hub",
                                {
                                    "paper_ref_id": edge.paper_ref_id,
                                    "relation": edge.relation,
                                },
                            )
                        )
                        continue

                    # >=1 confirmed replacement — safe to prune the original.
                    store.remove_link(
                        src_ref_id=edge.paper_ref_id,
                        dst_ref_id=hub_ref_id,
                        relation=edge.relation,
                        src_pos=edge.src_ord,
                        conn=c,
                    )
                    hub_edges_repointed += 1

                # The silent gap the owner cares about: a hub that HAD paper
                # provenance (either shape) but a placed atom that ended
                # this transaction with no direct paper reference of its
                # own — every evidence edge failed verification for it and
                # there was no lineage link to fall back on. Visible, never
                # fatal: doesn't abort the hub or block the stamp.
                if edges or lineage_links:
                    for atom_hub_id in atom_hub_ids:
                        if atom_hub_id is None:
                            continue
                        if _atom_paper_ref_count(c, atom_hub_id) == 0:
                            hub_atoms_unreferenced += 1

                # Add-first invariant's post-write backstop (step 3): total
                # live evidence across (original hub + every atom hub) must
                # never land at zero when this hub started with evidence.
                if edges:
                    all_ids = [hub_ref_id] + [h for h in atom_hub_ids if h is not None]
                    if _live_evidence_count(store, c, all_ids) == 0:
                        raise RuntimeError(
                            f"taproot-apply: hub_ref_id={hub_ref_id} would "
                            "land at zero live evidence edges post-repoint "
                            "— aborting this hub's transaction rather than "
                            "stranding it (add-first invariant backstop)"
                        )

                store.update_ref(
                    hub_ref_id,
                    meta_patch={_DECOMPOSED_AT_META_KEY: now_fn().isoformat()},
                    conn=c,
                )
        except Exception:
            log.warning(
                "taproot-apply: split apply aborted for hub_ref_id=%s — no "
                "writes for this hub were kept (whole-hub transaction "
                "rolled back)",
                hub_ref_id,
                exc_info=True,
            )
            partial_failures += 1
            hub_rows.append(HubApplyOutcome(hub_ref_id, "error", "split apply aborted"))
            aborted = True

        if aborted:
            continue

        # Now that the hub's transaction has actually committed, the
        # needs_review filings collected during it are safe to fire — see
        # `pending_reviews`' docstring above.
        for reason, detail in pending_reviews:
            _needs_review(hub_ref_id, reason, detail)

        # Trigger-1 retraction checks (attach_evidence's deferred network
        # check) — drained only now that the hub's transaction committed.
        run_retraction_checks(store, pending_checks, hub_ref_id=hub_ref_id)

        split_applied += 1
        atoms_placed += hub_atoms_placed
        atoms_needs_review += hub_atoms_review
        edges_repointed += hub_edges_repointed
        edges_kept_needs_review += hub_edges_review
        lineage_copied += hub_lineage_copied
        atoms_unreferenced += hub_atoms_unreferenced
        atoms_withheld_ungrounded += hub_atoms_withheld
        for reason, n in hub_withheld_reasons.items():
            atoms_withheld_reasons[reason] = atoms_withheld_reasons.get(reason, 0) + n
        partial_failures += hub_partial_failures
        hub_rows.append(
            HubApplyOutcome(
                hub_ref_id,
                "split_applied",
                detail=(
                    f"atoms_placed={hub_atoms_placed} "
                    f"atoms_needs_review={hub_atoms_review} "
                    f"atoms_withheld_ungrounded={hub_atoms_withheld} "
                    f"withheld_reasons={hub_withheld_reasons} "
                    f"edges_repointed={hub_edges_repointed} "
                    f"edges_kept_needs_review={hub_edges_review} "
                    f"lineage_copied={hub_lineage_copied} "
                    f"atoms_unreferenced={hub_atoms_unreferenced}"
                ),
            )
        )

    return ApplyReport(
        stamped_passthrough=stamped_passthrough,
        split_applied=split_applied,
        atoms_placed=atoms_placed,
        atoms_needs_review=atoms_needs_review,
        edges_repointed=edges_repointed,
        edges_kept_needs_review=edges_kept_needs_review,
        lineage_copied=lineage_copied,
        atoms_unreferenced=atoms_unreferenced,
        atoms_withheld_ungrounded=atoms_withheld_ungrounded,
        atoms_withheld_reasons=atoms_withheld_reasons,
        skipped_already_stamped=skipped_already_stamped,
        skipped_verdict=skipped_verdict,
        no_claim_needs_review=no_claim_needs_review,
        no_claim_unevidenced=no_claim_unevidenced,
        partial_failures=partial_failures,
        hubs=hub_rows,
    )
