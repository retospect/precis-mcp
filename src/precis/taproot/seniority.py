"""Taproot Phase 2c — seniority derivation over a claim hub's evidence.

Build ticket: ``docs/proposals/taproot-phase2-hub-node.md`` (slice 2c);
design: ``docs/proposals/taproot.md`` §"Seniority is derived, not stored".

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

from dataclasses import dataclass
from typing import Any

from precis.errors import BadInput
from precis.taproot.canon import TAPROOT_CLAIM, TAPROOT_NAMESPACE

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
class HubEvidence:
    """A claim hub's evidence, split by derived seniority."""

    hub_ref_id: int
    originators: list[EvidenceEdge]
    corroborators: list[EvidenceEdge]
    contradictors: list[EvidenceEdge]
    coverage_note: str | None


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


def _fetch_evidence_rows(
    store: Any, hub_ref_id: int
) -> list[tuple[int, str, dict[str, Any]]]:
    """Direct ``paper -> hub`` read: ``(src_ref_id, relation, meta)`` rows.

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
            SELECT l.src_ref_id, l.relation, l.meta
            FROM links l
            JOIN refs p ON p.ref_id = l.src_ref_id
            WHERE l.dst_ref_id = %(hub)s
              AND l.relation = ANY(%(roles)s)
              AND p.kind = 'paper'
              AND p.deleted_at IS NULL
            """,
            {"hub": hub_ref_id, "roles": list(_ALL_ROLES)},
        ).fetchall()
    return [(row[0], row[1], row[2] or {}) for row in rows]


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


def derive_evidence(store: Any, hub_ref_id: int) -> HubEvidence:
    """Derive a claim hub's evidence, split into originators/corroborators/
    contradictors. Pure read — writes nothing.

    Raises :class:`BadInput` when ``hub_ref_id`` is not a live
    ``TAPROOT:claim`` finding.
    """
    if not _is_claim_hub(store, hub_ref_id):
        raise BadInput(
            f"ref_id={hub_ref_id} is not a TAPROOT:claim finding",
            next=(
                "derive_evidence only reads TAPROOT:claim hubs — tag the "
                "finding TAPROOT:claim or pick an existing claim hub"
            ),
        )

    support_edges: dict[int, dict[str, Any]] = {}
    contradict_edges: dict[int, dict[str, Any]] = {}
    for src_ref_id, relation, meta in _fetch_evidence_rows(store, hub_ref_id):
        target = contradict_edges if relation == _CONTRADICTS_ROLE else support_edges
        target.setdefault(src_ref_id, meta)

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
    )


__all__ = [
    "ClaimLinks",
    "ClaimRef",
    "EvidenceEdge",
    "HubEvidence",
    "derive_evidence",
    "derive_refines",
    "is_claim_hub",
]
