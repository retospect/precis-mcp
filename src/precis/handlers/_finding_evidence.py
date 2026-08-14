"""``get(kind='finding', view='evidence')`` rendering (Taproot Phase 2c).

Split out of ``finding.py`` (docs/backlog/codereview-handler-size-cleanups.md):
this rendering pass only ever touched ``self.store``, never any other
handler state, so it moves cleanly as free functions taking the store
directly. ``FindingHandler.get`` calls :func:`render_evidence_view`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from precis.store.types import Ref
from precis.taproot import seniority

if TYPE_CHECKING:
    from precis.store import Store

from precis.response import Response


def render_evidence_view(store: Store, ref: Ref) -> Response:
    """Render ``view='evidence'``: the hub's edges by derived role.

    A patent evidence edge renders its full bibliography-style citation
    (applicant, title, publication number + kind code, year) instead of
    a bare title, and same-family patent edges within one role-list
    collapse to one row keyed to the family's deterministic
    representative (docs/backlog/patent-evidence-parity.md Phase 3;
    :func:`_collapse_patent_families`) — a paper's own family is
    untouched (papers carry no ``family_id``), so this is a no-op for
    every pre-existing paper-only hub.

    An evidence paper with zero body blocks — a stub never fetched —
    renders with an ``(unfetched)`` annotation (gr180155's per-hub
    analogue of the draft citations view's to-fetch worklist), so a
    reader of one hub's evidence list, not just a citing draft's
    worklist, can see which claims rest on un-verifiable evidence.
    """
    from precis.export._patent_cite import format_patent_bibliography_entry
    from precis.format import render_agent_table

    evidence = seniority.derive_evidence(store, ref.id)
    all_edges = evidence.originators + evidence.corroborators + evidence.contradictors

    header = [f"# evidence for finding {ref.id}", "", ref.title]
    if not all_edges:
        header.append("")
        header.append("no evidence edges yet for this claim hub")
        return Response(body="\n".join(header))

    refs_by_id = store.fetch_refs_by_ids({e.paper_ref_id for e in all_edges})
    fetched_paper_ids = store.blocks.ref_ids_with_chunks(
        [e.paper_ref_id for e in all_edges]
    )

    def _label(e: seniority.EvidenceEdge, note: str | None) -> str:
        source_ref = refs_by_id.get(e.paper_ref_id)
        if source_ref is not None and source_ref.kind == "patent":
            label = format_patent_bibliography_entry(source_ref)
        else:
            label = e.title[:80] + ("…" if len(e.title) > 80 else "")
        if note:
            label = f"{label} ({note})"
        if e.paper_ref_id not in fetched_paper_ids:
            label = f"{label} (unfetched)"
        if e.is_originator:
            label = f"★ {label}"
        return label

    def _table(edges: list[seniority.EvidenceEdge]) -> str:
        rows: list[dict[str, str]] = []
        for e, note in _collapse_patent_families(store, edges, refs_by_id):
            rows.append(
                {
                    "paper": _label(e, note),
                    "year": str(e.year) if e.year is not None else "—",
                    "support": e.support or "—",
                    "integrity": e.integrity,
                    "caveats": "; ".join(e.caveats) if e.caveats else "—",
                }
            )
        schema = ["paper", "year", "support", "integrity", "caveats"]
        return render_agent_table(rows, schema=schema)

    lines = list(header)
    lines += ["", "## originators (establishes)", ""]
    lines.append(_table(evidence.originators) if evidence.originators else "(none)")

    lines += ["", "## corroborators", ""]
    lines.append(_table(evidence.corroborators) if evidence.corroborators else "(none)")
    if evidence.coverage_note:
        lines += ["", evidence.coverage_note]

    lines += ["", "## contradicts", ""]
    lines.append(_table(evidence.contradictors) if evidence.contradictors else "(none)")

    if not any(e.support for e in all_edges):
        lines += ["", "support outcomes are populated by chase (Phase 3)"]

    return Response(body="\n".join(lines))


def _collapse_patent_families(
    store: Store,
    edges: list[seniority.EvidenceEdge],
    refs_by_id: dict[int, Ref],
) -> list[tuple[seniority.EvidenceEdge, str | None]]:
    """Collapse same-family patent evidence edges to one row per family
    (``view='evidence'``, docs/backlog/patent-evidence-parity.md Phase
    3) — family identity is EPO-authoritative data, so two edges citing
    sibling family members for the same claim are the same warrant, not
    two separate ones. A non-patent edge, or a patent edge with no
    ``family_id``, passes through unchanged (one row each); each role-list
    (originators/corroborators/contradictors) already carries at most one
    edge per paper (:class:`~precis.taproot.seniority.EvidenceEdge`), so
    grouping by ``paper_ref_id`` for the non-family case can't collide.

    The rendered row is the family's deterministic representative
    (:func:`precis.handlers._patent_family.family_representative`) when
    it's among this list's edges, else the first (already
    seniority-ordered, i.e. senior-first) member. The second tuple element
    is a ``"passage in <SLUG>[, <SLUG>...]"`` note naming EVERY *other*
    family member still in the group (deduped, group order — with 3+
    grounded siblings, an earlier version surfaced only the first and
    silently dropped the rest), so a grounded passage that actually lives
    in a non-representative sibling stays traceable rather than silently
    dropped.
    """
    from precis.handlers._patent_family import family_representative

    order: list[str] = []
    groups: dict[str, list[seniority.EvidenceEdge]] = {}
    for e in edges:
        source_ref = refs_by_id.get(e.paper_ref_id)
        family_id = (
            (source_ref.meta or {}).get("family_id")
            if source_ref is not None and source_ref.kind == "patent"
            else None
        )
        key = f"family:{family_id}" if family_id else f"solo:{e.paper_ref_id}"
        if key not in groups:
            order.append(key)
            groups[key] = []
        groups[key].append(e)

    out: list[tuple[seniority.EvidenceEdge, str | None]] = []
    for key in order:
        group = groups[key]
        if not key.startswith("family:") or len(group) == 1:
            out.append((group[0], None))
            continue
        family_id = key.split(":", 1)[1]
        rep = family_representative(store, family_id)
        canonical = group[0]
        if rep is not None:
            match = next((e for e in group if e.paper_ref_id == rep.id), None)
            if match is not None:
                canonical = match
        others = [e for e in group if e.paper_ref_id != canonical.paper_ref_id]
        note: str | None = None
        if others:
            other_slugs: list[str] = []
            seen_slugs: set[str] = set()
            for other in others:
                other_ref = refs_by_id.get(other.paper_ref_id)
                other_slug = (other_ref.slug if other_ref is not None else None) or str(
                    other.paper_ref_id
                )
                other_slug = other_slug.upper()
                if other_slug not in seen_slugs:
                    seen_slugs.add(other_slug)
                    other_slugs.append(other_slug)
            note = f"passage in {', '.join(other_slugs)}"
        out.append((canonical, note))
    return out
