"""Promote per-paper glossaries into concept nodes (reading-prep loop, slice 2c).

Reads a paper's ``card_glossary`` (slice 1) and turns each term into a
``concept`` node — minted fresh, or, if a concept of that name already exists
**corpus-wide**, the existing node gains this paper as provenance and joins the
cohort. That is the native dedup the concept graph buys: one ``backpropagation``
node, not one per paper.

Dedup is **name-anchored** (normalized exact-name match, the greenlit
conservative default); embedding-similarity dedup is a later refinement (it would
couple promotion to the async embedder). Cohort membership is ``meta.cohorts`` (a
**list** — a deduped concept can serve several cohorts). No graph edges here
(``has-prerequisite`` etc. land in slice 2d/3) and no cards yet. Provenance is a
``concept --derived-from--> paper`` link. See docs/design/reading-prep-loop.md.
"""

from __future__ import annotations

import logging
from typing import Any

from precis.reading.concepts import (
    concept_card_text,
    initial_concept_meta,
    normalize_name,
)

log = logging.getLogger(__name__)

_GLOSSARY_KIND = "card_glossary"


def _find_existing(store: Any, name: str) -> int | None:
    """Corpus-wide concept with the same normalized name, or None."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'concept' AND deleted_at IS NULL "
            "AND meta->>'norm_name' = %s ORDER BY ref_id LIMIT 1",
            (normalize_name(name),),
        ).fetchone()
    return int(row[0]) if row else None


def create_concept(
    store: Any,
    *,
    name: str,
    definition: str = "",
    aliases: list[str] | None = None,
    cohort: str | None = None,
    source_paper_id: int | None = None,
    extra: dict[str, Any] | None = None,
) -> int:
    """Mint a new concept node: ref + embeddable card + optional provenance link.
    Returns the concept ref id. Reuses the shared node helpers so a promoted node
    is identical to a hand-authored one."""
    meta_extra: dict[str, Any] = dict(extra or {})
    if cohort:
        meta_extra.setdefault("cohorts", [cohort])
    meta = initial_concept_meta(name, definition, aliases=aliases, extra=meta_extra)
    card = concept_card_text(name, definition, aliases)
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="concept", slug=None, title=name.strip(), meta=meta, conn=conn
        )
        store.upsert_card_combined(ref.id, card, conn=conn)
        if source_paper_id is not None:
            store.add_link(
                src_ref_id=ref.id,
                dst_ref_id=source_paper_id,
                relation="derived-from",
                conn=conn,
            )
    return int(ref.id)


def _attach(store: Any, *, concept_id: int, paper_id: int, cohort: str | None) -> None:
    """Dedup path: give an existing concept the new paper as provenance and add
    it to the cohort (idempotent). Mastery + definition are left untouched."""
    store.add_link(src_ref_id=concept_id, dst_ref_id=paper_id, relation="derived-from")
    if not cohort:
        return
    ref = store.get_ref(kind="concept", id=concept_id)
    if ref is None:
        return
    cohorts = list((ref.meta or {}).get("cohorts") or [])
    if cohort not in cohorts:
        cohorts.append(cohort)
        store.update_ref(concept_id, meta_patch={"cohorts": cohorts})


def _read_glossary_meta(store: Any, paper_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM chunks WHERE ref_id = %s AND chunk_kind = %s "
            "AND retired_at IS NULL LIMIT 1",
            (paper_id, _GLOSSARY_KIND),
        ).fetchone()
    return (row[0] or {}) if row and row[0] else {}


def promote_paper(
    store: Any, *, paper_id: int, cohort: str | None = None
) -> dict[str, int]:
    """Promote one paper's glossary terms to concepts. Returns
    ``{minted, linked, terms}``. No-op (all zero) if the paper has no glossary."""
    gmeta = _read_glossary_meta(store, paper_id)
    clusters = gmeta.get("clusters") or []
    version = gmeta.get("glossary_version")
    minted = linked = terms = 0
    for cluster in clusters:
        if not isinstance(cluster, dict):
            continue
        for term in cluster.get("terms") or []:
            if not isinstance(term, dict):
                continue
            name = str(term.get("term") or "").strip()
            if not name:
                continue
            terms += 1
            existing = _find_existing(store, name)
            if existing is not None:
                _attach(store, concept_id=existing, paper_id=paper_id, cohort=cohort)
                linked += 1
                continue
            create_concept(
                store,
                name=name,
                definition=str(term.get("definition") or "").strip(),
                cohort=cohort,
                source_paper_id=paper_id,
                extra={"source_glossary_version": version} if version else None,
            )
            minted += 1
    return {"minted": minted, "linked": linked, "terms": terms}


def promote_cohort(store: Any, *, paper_ids: list[int], cohort: str) -> dict[str, int]:
    """Promote a whole reading cohort. Returns aggregate ``{minted, linked,
    terms, papers}``."""
    totals = {"minted": 0, "linked": 0, "terms": 0, "papers": 0}
    for pid in paper_ids:
        r = promote_paper(store, paper_id=pid, cohort=cohort)
        totals["minted"] += r["minted"]
        totals["linked"] += r["linked"]
        totals["terms"] += r["terms"]
        totals["papers"] += 1
    return totals


__all__ = ["create_concept", "promote_cohort", "promote_paper"]
