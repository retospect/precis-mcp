"""Taproot Phase 2c — seniority derivation over a claim hub's evidence.

Build ticket: ``docs/backlog/taproot-phase2-hub-node.md`` (slice 2c);
design: ``docs/backlog/taproot.md`` §"Seniority is derived, not stored".

**Pure read/derive — no writes.** The evidence edges themselves are
written through the single door (:mod:`precis.taproot.hub`); this module
only *reads* them back and computes the originator/corroborator split.
There is no stored "seniority" column and none should ever be added —
the ordering is recomputed from the ``links`` citation graph on every
call.

Derivation (taproot.md, locked decision):

1. **Supporters** ``S`` = papers with an inbound ``establishes`` or
   ``corroborates`` edge onto the hub (deduped by paper ref_id).
   **Contradictors** = papers with an inbound ``contradicts`` edge — a
   *separate* group, never folded into the seniority split.
2. **Originators** among ``S``: walk ``cites`` edges *among S only* — a
   paper ``p`` is an originator iff some other ``q`` in ``S`` cites it.
   Corroborators = ``S`` minus the originators.
3. **Fallback**: if no intra-``S`` ``cites`` edges are held, every
   supporter stays a corroborator — we never guess an originator — and
   :attr:`HubEvidence.coverage_note` records that the edge coverage was
   insufficient. A global citation-count fallback is deferred to Phase 3
   (taproot.md's "S2 citation count" path).
4. Each group orders by ``refs.year`` ascending (earliest first; NULL
   sorts last), tie-broken by ``ref_id`` ascending.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from precis.errors import BadInput
from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE
from precis.utils import handle_registry

#: The two roles that feed the seniority split (`establishes` is the
#: already-derived role from a previous call; `corroborates` is the
#: write-time default — see `hub._DEFAULT_ROLE`). Both are read back
#: here and re-derived regardless of which one was stored at write time.
_SUPPORT_ROLES = ("establishes", "corroborates")
_CONTRADICTS_ROLE = "contradicts"
_ALL_ROLES = (*_SUPPORT_ROLES, _CONTRADICTS_ROLE)
_CITES_RELATION = "cites"

_UNDETERMINED_NOTE = "seniority undetermined: no intra-set citation edges held"


@dataclass(frozen=True)
class EvidenceEdge:
    """One paper's evidence contribution to a claim hub."""

    paper_ref_id: int
    title: str
    year: int | None
    #: 'establishes' | 'corroborates' | 'contradicts' — derived at read
    #: time, independent of the role slug the edge was stored under.
    derived_role: str
    is_originator: bool
    #: `links.meta['support']` — the chase verdict. `None` until Phase 3.
    support: str | None
    caveats: list[str]
    #: 'clean' | 'retracted' | 'corrected' | 'expression_of_concern'.
    integrity: str
    #: `links.meta['source_handle']` — the grounding chunk pointer
    #: (`pc<chunk_id>` / `slug~ord`) the evidence edge points at, when the
    #: chase has populated one (Phase 3). `None` until then — read by the
    #: reference ring's Claims explosion (refeye, Taproot slice R1) to show
    #: "cite -> claim -> grounding chunk" at pointer granularity.
    source_handle: str | None = None


@dataclass(frozen=True)
class GroundingRef:
    """One raw evidence edge's grounding-passage pointer — the chunk in a
    supporter/contradictor paper that grounds the claim.

    Distinct from :class:`EvidenceEdge` (one per paper, for the seniority
    split): grounding is one per *edge*, so a paper that grounds the claim at
    two different passages contributes two ``GroundingRef``s — the seniority
    edge for that paper is still one row. :attr:`source_handle` prefers the
    edge's ``meta['source_handle']`` (the Phase-3 chase pointer) and falls
    back to the ``links.src_chunk_id`` the paper→hub edge itself pins (what
    the ``draft-backfill`` arm records — ``meta.source_handle`` is unset
    there), formatted as a universal ``pc<id>`` chunk handle.

    :attr:`relation` is the RAW stored edge relation
    (``establishes``/``corroborates``/``contradicts``) so the web layer
    attributes the passage to the right role — a paper that both corroborates
    (at one chunk) and contradicts (at another) the same claim must NOT have
    its contradicting passage relabeled as support (keying by paper alone
    would)."""

    paper_ref_id: int
    source_handle: str
    relation: str


@dataclass(frozen=True)
class HubEvidence:
    """A claim hub's evidence, split by derived seniority."""

    hub_ref_id: int
    originators: list[EvidenceEdge]
    corroborators: list[EvidenceEdge]
    contradictors: list[EvidenceEdge]
    coverage_note: str | None
    #: Every grounding-passage pointer across all evidence edges, one per raw
    #: edge (NOT deduped by paper) — so two corroborates edges from one paper
    #: pinning different chunks both surface as grounding passages. Read by
    #: the web layer's "Grounding passages" section; ordering is normalised
    #: for display in ``claim_render._render_one``, so the derive order here
    #: only needs to be a stable set, not display order.
    grounding: list[GroundingRef] = field(default_factory=list)


def _is_claim_hub(store: Any, ref_id: int) -> bool:
    """True iff ``ref_id`` is a live ``finding`` carrying ``TAPROOT:claim``.

    Mirrors :func:`precis.taproot.hub._is_claim_hub`'s guard (kept as a
    separate read-only copy here rather than importing the private
    write-door helper, since this module opens its own connection).
    """
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT 1
            FROM refs r
            JOIN ref_tags rt ON rt.ref_id = r.ref_id
            JOIN tags t ON t.tag_id = rt.tag_id
                       AND t.namespace = %(ns)s AND t.value = %(val)s
            WHERE r.ref_id = %(rid)s AND r.kind = 'finding' AND r.deleted_at IS NULL
            LIMIT 1
            """,
            {"ns": TAPROOT_NAMESPACE, "val": TAPROOT_CLAIM, "rid": ref_id},
        ).fetchone()
    return row is not None


def is_claim_hub(store: Any, ref_id: int) -> bool:
    """Public wrapper over :func:`_is_claim_hub` — the one hub-detection
    check other modules (e.g. :mod:`precis.taproot.cite`) should call
    rather than reaching into the private name."""
    return _is_claim_hub(store, ref_id)


def is_claim_hub_bulk(store: Any, ref_ids: Iterable[int]) -> dict[int, bool]:
    """Bulk twin of :func:`is_claim_hub` — one query for many ``ref_ids``
    instead of one per id (the smartdraft reader's Claims rail resolves a
    whole render-window's worth of distinct cite heads in one pass; the
    per-head O(N) query count was the O(all-hubs) TTFB defect's first
    tax — OPEN-ITEMS.md's "``/smartdraft`` reader" item)."""
    ids = sorted({int(r) for r in ref_ids})
    if not ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id
            FROM refs r
            JOIN ref_tags rt ON rt.ref_id = r.ref_id
            JOIN tags t ON t.tag_id = rt.tag_id
                       AND t.namespace = %(ns)s AND t.value = %(val)s
            WHERE r.ref_id = ANY(%(ids)s) AND r.kind = 'finding'
              AND r.deleted_at IS NULL
            """,
            {"ns": TAPROOT_NAMESPACE, "val": TAPROOT_CLAIM, "ids": ids},
        ).fetchall()
    hub_ids = {int(row[0]) for row in rows}
    return {rid: rid in hub_ids for rid in ids}


def _find_originators(store: Any, supporter_ids: list[int]) -> set[int]:
    """Supporters cited by at least one *other* supporter.

    One bounded query over the ``links`` table rather than N per-paper
    ``links_for`` calls (acceptance #4 asks for a bounded query since
    |S| is small but this keeps it O(1) round trips regardless).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE relation = %s AND src_ref_id = ANY(%s) AND dst_ref_id = ANY(%s)",
            (_CITES_RELATION, supporter_ids, supporter_ids),
        ).fetchall()
    return {dst for src, dst in rows if src != dst}


def _grounding_handle(src_chunk_id: int | None, meta: dict[str, Any]) -> str | None:
    """The grounding-passage handle for one evidence edge: the Phase-3 chase
    pointer ``meta['source_handle']`` when present, else the ``pc<id>`` handle
    of the ``links.src_chunk_id`` the paper→hub edge pins directly (the
    ``draft-backfill`` arm's storage — it writes the grounding chunk into the
    edge's ``src_chunk_id`` column and leaves ``meta.source_handle`` unset).
    ``None`` when the edge grounds no chunk at all."""
    stored = meta.get("source_handle")
    if stored:
        return str(stored)
    if src_chunk_id is not None:
        return handle_registry.try_format("paper", src_chunk_id, chunk=True)
    return None


def _fetch_evidence_rows(
    store: Any, hub_ref_id: int
) -> list[tuple[int, int | None, str, dict[str, Any]]]:
    """Direct ``paper -> hub`` read: ``(src_ref_id, src_chunk_id, relation,
    meta)`` rows.

    Bypasses ``store.links_for``'s inverse-relation rewrite on purpose.
    ``contradicts`` has a registered inverse ``contradicted-by``
    (migration 0001), so ``links_for(hub, direction='in',
    relation='contradicts')`` would ALSO match ``(src=hub,
    relation='contradicted-by')`` rows — surfacing the hub as a
    contradictor of itself. A hub<->hub ``contradicts`` edge (the
    opposite-claim link ``hub.apply_placement``'s ``new_contradicts``
    branch writes) shares the same slug too and would otherwise surface
    the *other hub* (a finding, not a paper) as a contradictor. Both are
    excluded by joining ``refs`` and requiring the stored source endpoint
    be ``kind='paper'`` — taproot.md decision #2: "endpoint kinds
    disambiguate." ``establishes``/``corroborates`` have no inverse slug
    (migrations 0094/0085) so they aren't exposed to the same bug, but
    they're routed through this one query for uniformity.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.src_ref_id, l.src_chunk_id, l.relation, l.meta
            FROM links l
            JOIN refs p ON p.ref_id = l.src_ref_id
            WHERE l.dst_ref_id = %(hub)s
              AND l.relation = ANY(%(roles)s)
              AND p.kind = 'paper'
              AND p.deleted_at IS NULL
            """,
            {"hub": hub_ref_id, "roles": list(_ALL_ROLES)},
        ).fetchall()
    return [(row[0], row[1], row[2], row[3] or {}) for row in rows]


def _fetch_paper_facts(
    store: Any, ref_ids: set[int]
) -> dict[int, tuple[str, int | None, str | None]]:
    """Bulk-fetch ``(title, year, retraction_status)`` per paper ref_id."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, year, retraction_status FROM refs "
            "WHERE ref_id = ANY(%s)",
            (list(ref_ids),),
        ).fetchall()
    return {row[0]: (row[1], row[2], row[3]) for row in rows}


def _build_edge(
    ref_id: int,
    meta: dict[str, Any] | None,
    facts: dict[int, tuple[str, int | None, str | None]],
    *,
    derived_role: str,
    is_originator: bool,
) -> EvidenceEdge:
    title, year, retraction_status = facts.get(ref_id, (f"<ref {ref_id}>", None, None))
    meta = meta or {}
    return EvidenceEdge(
        paper_ref_id=ref_id,
        title=title,
        year=year,
        derived_role=derived_role,
        is_originator=is_originator,
        support=meta.get("support"),
        caveats=list(meta.get("caveats") or []),
        integrity=retraction_status or "clean",
        source_handle=meta.get("source_handle"),
    )


def _sort_group(edges: list[EvidenceEdge]) -> list[EvidenceEdge]:
    """Earliest year first; NULL year sorts last; tie-break ref_id asc."""
    return sorted(edges, key=lambda e: (e.year is None, e.year or 0, e.paper_ref_id))


_REFINES_RELATION = "refines"


@dataclass(frozen=True)
class ClaimRef:
    """A neighbouring claim hub reached over a ``refines`` link — its ref_id
    plus its claim sentence (the finding ``title``), for the ring's advisory
    "see also" line."""

    hub_ref_id: int
    sentence: str


@dataclass(frozen=True)
class ClaimLinks:
    """A claim hub's advisory ``refines`` neighbourhood (migration 0100).

    Both directions of the directed, no-inverse ``refines`` edge, read via
    explicit ``src``/``dst`` filtering (never :func:`store.links_for`, whose
    inverse rewrite doesn't apply to a no-inverse slug but which we avoid for
    the same clarity reason :func:`_fetch_evidence_rows` does):

    * :attr:`refines` — coarser hubs this hub sharpens (outbound: src=hub).
    * :attr:`refined_by` — sharper hubs that refine this one (inbound:
      dst=hub); "a sharper version of this claim exists."
    """

    refines: list[ClaimRef]
    refined_by: list[ClaimRef]


def derive_refines(store: Any, hub_ref_id: int) -> ClaimLinks:
    """Read a claim hub's ``refines`` neighbours in both directions.

    Pure read. Only live ``TAPROOT:claim`` finding neighbours are returned
    (a soft-deleted or non-hub endpoint is dropped) — the endpoint guard
    :func:`~precis.taproot.hub.link_claims` enforces at write time is
    re-checked here so a later delete/untag can't surface a stale neighbour.
    Each group is ordered by neighbour ref_id ascending (deterministic).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.src_ref_id, l.dst_ref_id
            FROM links l
            JOIN refs a ON a.ref_id = l.src_ref_id
            JOIN refs b ON b.ref_id = l.dst_ref_id
            JOIN ref_tags rt ON rt.ref_id = (
                CASE WHEN l.src_ref_id = %(hub)s THEN l.dst_ref_id
                     ELSE l.src_ref_id END)
            JOIN tags t ON t.tag_id = rt.tag_id
                       AND t.namespace = %(ns)s AND t.value = %(val)s
            WHERE l.relation = %(rel)s
              AND (l.src_ref_id = %(hub)s OR l.dst_ref_id = %(hub)s)
              AND a.kind = 'finding' AND a.deleted_at IS NULL
              AND b.kind = 'finding' AND b.deleted_at IS NULL
            """,
            {
                "hub": hub_ref_id,
                "rel": _REFINES_RELATION,
                "ns": TAPROOT_NAMESPACE,
                "val": TAPROOT_CLAIM,
            },
        ).fetchall()

    neighbour_ids = {
        (dst if src == hub_ref_id else src) for src, dst in rows if src != dst
    }
    titles = _fetch_paper_facts(store, neighbour_ids)  # (title, year, retraction)

    def _ref(ref_id: int) -> ClaimRef:
        title = titles.get(ref_id, (f"<claim {ref_id}>", None, None))[0]
        return ClaimRef(hub_ref_id=ref_id, sentence=title or f"<claim {ref_id}>")

    refines = sorted(
        (_ref(dst) for src, dst in rows if src == hub_ref_id and dst != hub_ref_id),
        key=lambda cr: cr.hub_ref_id,
    )
    refined_by = sorted(
        (_ref(src) for src, dst in rows if dst == hub_ref_id and src != hub_ref_id),
        key=lambda cr: cr.hub_ref_id,
    )
    return ClaimLinks(refines=refines, refined_by=refined_by)


@dataclass(frozen=True)
class CiterEdge:
    """One thing that *cites* this claim hub — the "who uses this claim" set,
    an inbound ``cites`` edge (``src -> hub``). Distinct from the three
    evidence roles (``establishes``/``corroborates``/``contradicts``, which
    are a paper's *support* for the claim): a ``cites`` edge onto the hub is a
    manuscript or paper INVOKING the claim, pinned to the citing chunk
    (:attr:`src_chunk_id`) when the writer recorded one."""

    src_ref_id: int
    src_chunk_id: int | None
    kind: str
    title: str | None
    year: int | None


_CITES_INBOUND_ROLE = "cites"


def hub_citers(store: Any, hub_ref_id: int) -> list[CiterEdge]:
    """Everything that cites a claim hub — its inbound ``cites`` edges, joined
    to the citing ref's kind/title/year. Pure read.

    Read via an explicit ``dst_ref_id`` filter (never :func:`store.links_for`,
    whose inverse rewrite would fold ``cited-by`` rows in — same clarity
    reason :func:`_fetch_evidence_rows` and :func:`derive_refines` bypass it).
    Soft-deleted citers are dropped. Any src kind is surfaced (a draft
    manuscript, another paper) — the web layer turns ``(kind, src_chunk_id)``
    into the universal chunk handle that links to the citing passage. Ordered
    newest-first (NULL year last), then ``src_ref_id`` ascending for a stable
    render.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.src_ref_id, l.src_chunk_id, r.kind, r.title, r.year
            FROM links l
            JOIN refs r ON r.ref_id = l.src_ref_id
            WHERE l.dst_ref_id = %(hub)s
              AND l.relation = %(rel)s
              AND r.deleted_at IS NULL
            ORDER BY r.year DESC NULLS LAST, l.src_ref_id
            """,
            {"hub": hub_ref_id, "rel": _CITES_INBOUND_ROLE},
        ).fetchall()
    return [CiterEdge(r[0], r[1], r[2], r[3], r[4]) for r in rows]


def derive_evidence(
    store: Any, hub_ref_id: int, *, assume_hub: bool = False
) -> HubEvidence:
    """Derive a claim hub's evidence, split into originators/corroborators/
    contradictors. Pure read — writes nothing.

    Raises :class:`BadInput` when ``hub_ref_id`` is not a live
    ``TAPROOT:claim`` finding — unless ``assume_hub=True``, which skips the
    check (one fewer query) for a caller that already confirmed it, e.g.
    via :func:`is_claim_hub`/:func:`is_claim_hub_bulk` a moment ago
    (:mod:`precis.taproot.trust`'s ``claim_trust``/``_hub_trust``, or
    ``precis_web.claim_render``'s bulk render path). Public default stays
    ``False`` — the safety check is the contract unless a caller opts out.
    """
    if not assume_hub and not _is_claim_hub(store, hub_ref_id):
        raise BadInput(
            f"ref_id={hub_ref_id} is not a TAPROOT:claim finding",
            next=(
                "derive_evidence only reads TAPROOT:claim hubs — tag the "
                "finding TAPROOT:claim or pick an existing claim hub"
            ),
        )

    support_edges: dict[int, dict[str, Any]] = {}
    contradict_edges: dict[int, dict[str, Any]] = {}
    grounding: list[GroundingRef] = []
    seen_grounding: set[tuple[int, str]] = set()
    for src_ref_id, src_chunk_id, relation, meta in _fetch_evidence_rows(
        store, hub_ref_id
    ):
        target = contradict_edges if relation == _CONTRADICTS_ROLE else support_edges
        target.setdefault(src_ref_id, meta)
        handle = _grounding_handle(src_chunk_id, meta)
        if handle and (src_ref_id, handle) not in seen_grounding:
            seen_grounding.add((src_ref_id, handle))
            grounding.append(GroundingRef(src_ref_id, handle, relation))

    supporter_ids = list(support_edges)
    originator_ids = _find_originators(store, supporter_ids) if supporter_ids else set()
    coverage_note = _UNDETERMINED_NOTE if supporter_ids and not originator_ids else None

    facts = _fetch_paper_facts(store, set(support_edges) | set(contradict_edges))

    originators: list[EvidenceEdge] = []
    corroborators: list[EvidenceEdge] = []
    for ref_id, meta in support_edges.items():
        is_originator = ref_id in originator_ids
        edge = _build_edge(
            ref_id,
            meta,
            facts,
            derived_role="establishes" if is_originator else "corroborates",
            is_originator=is_originator,
        )
        (originators if is_originator else corroborators).append(edge)

    contradictors = [
        _build_edge(
            ref_id, meta, facts, derived_role="contradicts", is_originator=False
        )
        for ref_id, meta in contradict_edges.items()
    ]

    return HubEvidence(
        hub_ref_id=hub_ref_id,
        originators=_sort_group(originators),
        corroborators=_sort_group(corroborators),
        contradictors=_sort_group(contradictors),
        coverage_note=coverage_note,
        grounding=grounding,
    )


# ── Bulk derivation (OPEN-ITEMS.md "/smartdraft reader" perf fix, batch B) ──
#
# A page (or export) resolving many hubs at once — e.g. the smartdraft
# reader's Claims rail, cited-hub-per-window — shouldn't pay N x (3+)
# queries for N hubs. These bulk twins fetch the SAME three query shapes
# :func:`derive_evidence` does, but once across every hub, then partition
# the results back per hub in Python. Assumes every id in ``hub_ref_ids``
# is already a confirmed live ``TAPROOT:claim`` hub (via
# :func:`is_claim_hub_bulk`) — no per-hub re-check, mirroring
# ``derive_evidence(assume_hub=True)``.


def _fetch_evidence_rows_bulk(
    store: Any, hub_ref_ids: list[int]
) -> dict[int, list[tuple[int, int | None, str, dict[str, Any]]]]:
    """Bulk twin of :func:`_fetch_evidence_rows` — one query for every hub
    in ``hub_ref_ids`` instead of one per hub. Same ``p.kind = 'paper'``
    endpoint-disambiguation guard (see :func:`_fetch_evidence_rows`'s
    docstring for why that matters)."""
    out: dict[int, list[tuple[int, int | None, str, dict[str, Any]]]] = {
        h: [] for h in hub_ref_ids
    }
    if not hub_ref_ids:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT l.dst_ref_id, l.src_ref_id, l.src_chunk_id, l.relation, l.meta
            FROM links l
            JOIN refs p ON p.ref_id = l.src_ref_id
            WHERE l.dst_ref_id = ANY(%(hubs)s)
              AND l.relation = ANY(%(roles)s)
              AND p.kind = 'paper'
              AND p.deleted_at IS NULL
            """,
            {"hubs": hub_ref_ids, "roles": list(_ALL_ROLES)},
        ).fetchall()
    for hub_id, src, src_chunk_id, relation, meta in rows:
        out.setdefault(int(hub_id), []).append(
            (int(src), src_chunk_id, str(relation), meta or {})
        )
    return out


def _find_originators_bulk(
    store: Any, supporters_by_hub: dict[int, list[int]]
) -> dict[int, set[int]]:
    """Bulk twin of :func:`_find_originators` — one ``cites`` query across
    the UNION of every hub's supporter set, then re-partitioned per hub.

    Safe even when a paper supports more than one hub: a ``cites`` edge
    only counts toward a given hub when BOTH endpoints are in THAT hub's
    own supporter set, so pooling candidate ids into one query first and
    filtering per hub afterward gives the same answer :func:`_find_originators`
    would for each hub called separately."""
    all_ids = sorted({rid for ids in supporters_by_hub.values() for rid in ids})
    if not all_ids:
        return {h: set() for h in supporters_by_hub}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE relation = %s AND src_ref_id = ANY(%s) AND dst_ref_id = ANY(%s)",
            (_CITES_RELATION, all_ids, all_ids),
        ).fetchall()
    edges = [(int(src), int(dst)) for src, dst in rows if src != dst]
    out: dict[int, set[int]] = {}
    for hub_id, supporter_ids in supporters_by_hub.items():
        s = set(supporter_ids)
        out[hub_id] = {dst for src, dst in edges if src in s and dst in s}
    return out


def derive_evidence_bulk(
    store: Any, hub_ref_ids: Iterable[int]
) -> dict[int, HubEvidence]:
    """Bulk twin of :func:`derive_evidence` — resolve many hubs' evidence in
    THREE queries total (evidence rows, intra-set ``cites``, paper facts),
    regardless of N, instead of N x (3+). ``hub_ref_ids`` is assumed already
    hub-confirmed (see module note above); an id that turns out to carry no
    evidence rows simply gets an empty :class:`HubEvidence` back, same as
    :func:`derive_evidence` would for an evidence-less hub."""
    ids = list(dict.fromkeys(int(h) for h in hub_ref_ids))  # dedupe, keep order
    if not ids:
        return {}

    rows_by_hub = _fetch_evidence_rows_bulk(store, ids)

    support_by_hub: dict[int, dict[int, dict[str, Any]]] = {}
    contradict_by_hub: dict[int, dict[int, dict[str, Any]]] = {}
    grounding_by_hub: dict[int, list[GroundingRef]] = {}
    all_paper_ids: set[int] = set()
    for hub_id in ids:
        support_edges: dict[int, dict[str, Any]] = {}
        contradict_edges: dict[int, dict[str, Any]] = {}
        grounding: list[GroundingRef] = []
        seen_grounding: set[tuple[int, str]] = set()
        for src_ref_id, src_chunk_id, relation, meta in rows_by_hub.get(hub_id, []):
            target = (
                contradict_edges if relation == _CONTRADICTS_ROLE else support_edges
            )
            target.setdefault(src_ref_id, meta)
            all_paper_ids.add(src_ref_id)
            handle = _grounding_handle(src_chunk_id, meta)
            if handle and (src_ref_id, handle) not in seen_grounding:
                seen_grounding.add((src_ref_id, handle))
                grounding.append(GroundingRef(src_ref_id, handle, relation))
        support_by_hub[hub_id] = support_edges
        contradict_by_hub[hub_id] = contradict_edges
        grounding_by_hub[hub_id] = grounding

    originators_by_hub = _find_originators_bulk(
        store, {h: list(s) for h, s in support_by_hub.items()}
    )
    facts = _fetch_paper_facts(store, all_paper_ids)

    out: dict[int, HubEvidence] = {}
    for hub_id in ids:
        support_edges = support_by_hub[hub_id]
        contradict_edges = contradict_by_hub[hub_id]
        originator_ids = originators_by_hub.get(hub_id, set())
        coverage_note = (
            _UNDETERMINED_NOTE if support_edges and not originator_ids else None
        )

        originators: list[EvidenceEdge] = []
        corroborators: list[EvidenceEdge] = []
        for ref_id, meta in support_edges.items():
            is_originator = ref_id in originator_ids
            edge = _build_edge(
                ref_id,
                meta,
                facts,
                derived_role="establishes" if is_originator else "corroborates",
                is_originator=is_originator,
            )
            (originators if is_originator else corroborators).append(edge)

        contradictors = [
            _build_edge(
                ref_id, meta, facts, derived_role="contradicts", is_originator=False
            )
            for ref_id, meta in contradict_edges.items()
        ]

        out[hub_id] = HubEvidence(
            hub_ref_id=hub_id,
            originators=_sort_group(originators),
            corroborators=_sort_group(corroborators),
            contradictors=_sort_group(contradictors),
            coverage_note=coverage_note,
            grounding=grounding_by_hub[hub_id],
        )
    return out


__all__ = [
    "ClaimLinks",
    "ClaimRef",
    "EvidenceEdge",
    "GroundingRef",
    "HubEvidence",
    "derive_evidence",
    "derive_evidence_bulk",
    "derive_refines",
    "is_claim_hub",
    "is_claim_hub_bulk",
]
