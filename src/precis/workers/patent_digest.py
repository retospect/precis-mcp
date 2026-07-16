"""Freedom-to-operate claims digest (slice 3).

The claim-writing tick must see **what the prior art already claims** — if
a scope is already claimed, we cannot claim it. This builds a working set
of eyes over the related patents' claim chunks and stamps it into the
writing todo's ``meta.working_set``, which the planner injects (the live
ADR-0051 §6 reader path, ``PRECIS_PLANNER_FISHEYE``). See
``docs/design/patent-authoring-loop.md``.

The view is **comprehensive, not a decaying fisheye**: every prior-art
**independent** claim renders verbatim (it defines existing legal scope and
must never be silently dropped); **dependent** claims render at a lower
extent under budget. Our own claims ride along, verbatim (the set is
small). Claim chunks are identified by the slice-1 ``patent_block`` marker.
"""

from __future__ import annotations

from typing import Any

from precis.utils import handle_registry

#: Extents (working-set reader-shape labels) for the two claim tiers. An
#: independent claim is verbatim (never compressed); a dependent claim is a
#: gloss under budget. Both are the ADR-0051 ``Extent`` vocabulary.
INDEPENDENT_EXTENT = "verbatim"
DEPENDENT_EXTENT = "summary"


def _claim_eyes(
    store: Any,
    patent_ref_ids: list[int],
    *,
    independent_extent: str,
    dependent_extent: str,
) -> list[dict[str, str]]:
    """Claim eyes over ``patent_ref_ids``, in **document order per patent**
    (slice-1 ``patent_block == 'claim'``) — an independent claim is
    immediately followed by its dependents, so each claim *family* reads
    together (the dependency-tree grouping) rather than all independents
    then all dependents. Extent is tiered by independence: an independent
    claim verbatim (never dropped), a dependent claim compressed."""
    eyes: list[dict[str, str]] = []
    for rid in patent_ref_ids:
        for b in store.list_blocks_for_ref(rid):
            meta = getattr(b, "meta", None) or {}
            if meta.get("patent_block") != "claim":
                continue
            handle = handle_registry.format_handle("patent", int(b.id), chunk=True)
            extent = (
                independent_extent
                if meta.get("claim_independent")
                else dependent_extent
            )
            eyes.append({"handle": handle, "extent": extent})
    return eyes


def build_claims_digest(
    store: Any,
    patent_ref_ids: list[int],
    *,
    our_claim_handles: list[str] | None = None,
    independent_extent: str = INDEPENDENT_EXTENT,
    dependent_extent: str = DEPENDENT_EXTENT,
) -> dict[str, Any]:
    """The reader-shape ``working_set`` dict for a freedom-to-operate view.

    Order: our own claims first (the pen list, verbatim), then each prior-art
    patent's claims **in document order** — every independent claim verbatim
    (never dropped), its dependents compressed and grouped under it. ``eyes``
    is deduplicated on handle, first extent wins (so an independent claim is
    never demoted by a later dependent reference).
    """
    ours = [
        {"handle": h, "extent": independent_extent} for h in (our_claim_handles or [])
    ]
    prior_art = _claim_eyes(
        store,
        patent_ref_ids,
        independent_extent=independent_extent,
        dependent_extent=dependent_extent,
    )
    eyes: list[dict[str, str]] = []
    seen: set[str] = set()
    for eye in (*ours, *prior_art):
        if eye["handle"] in seen:
            continue
        seen.add(eye["handle"])
        eyes.append(eye)
    return {"eyes": eyes, "edit_hint": list(our_claim_handles or [])}


def stamp_claims_digest(
    store: Any,
    target_ref_id: int,
    patent_ref_ids: list[int],
    *,
    our_claim_handles: list[str] | None = None,
    independent_extent: str = INDEPENDENT_EXTENT,
    dependent_extent: str = DEPENDENT_EXTENT,
) -> dict[str, Any]:
    """Build the claims digest and write it to ``target_ref_id``'s
    ``meta.working_set`` (the writing todo). Returns the working-set dict."""
    ws = build_claims_digest(
        store,
        patent_ref_ids,
        our_claim_handles=our_claim_handles,
        independent_extent=independent_extent,
        dependent_extent=dependent_extent,
    )
    store.stamp_ref_meta(target_ref_id, {"working_set": ws})
    return ws


def related_patent_ref_ids(store: Any, draft_ref_id: int) -> list[int]:
    """The patent ref_ids the draft cites / relates to (its prior art), via
    the draft's outbound links (the write-time autolinker materialises a
    ``cites`` edge for each bracketed patent). Deduped, order-stable."""
    try:
        links = store.links_for(draft_ref_id, direction="out")
    except Exception:  # pragma: no cover — no links / store hiccup
        return []
    dst_ids: list[int] = []
    seen: set[int] = set()
    for link in links:
        dst = getattr(link, "dst_ref_id", None)
        if dst is not None and dst != draft_ref_id and dst not in seen:
            seen.add(dst)
            dst_ids.append(int(dst))
    if not dst_ids:
        return []
    refs = store.fetch_refs_by_ids(dst_ids)
    return [did for did in dst_ids if getattr(refs.get(did), "kind", None) == "patent"]


def discover_our_claim_handles(store: Any, draft_ref_id: int) -> list[str]:
    """The draft's own claim chunks — the leaf chunks under a
    ``patent-claim``-styled section (ADR 0037) — as ``dc…`` handles, in
    reading order. These ride the digest verbatim so the claim-writing tick
    sees *all our claims so far* alongside the prior art."""
    try:
        chunks = store.reading_order(draft_ref_id)
    except Exception:  # pragma: no cover — no draft / store hiccup
        return []
    out: list[str] = []
    for c in chunks:
        if getattr(c, "chunk_kind", None) == "heading":
            continue  # the section heading itself is not a claim
        try:
            style = store.section_style_for(c.dc)
        except Exception:  # pragma: no cover — style resolve hiccup
            style = None
        if style == "patent-claim":
            out.append(c.dc)
    return out


def refresh_claims_digest(
    store: Any,
    todo_ref_id: int,
    draft_ref_id: int,
    *,
    our_claim_handles: list[str] | None = None,
) -> dict[str, Any]:
    """One-call entry for the loop: discover the draft's prior-art patents
    **and its own claims so far**, build the freedom-to-operate claims
    digest, and stamp it onto the writing todo's ``meta.working_set``. A
    no-op-safe empty digest when the draft cites no ingested patents yet.
    ``our_claim_handles`` defaults to auto-discovery; pass ``[]`` to skip."""
    if our_claim_handles is None:
        our_claim_handles = discover_our_claim_handles(store, draft_ref_id)
    return stamp_claims_digest(
        store,
        todo_ref_id,
        related_patent_ref_ids(store, draft_ref_id),
        our_claim_handles=our_claim_handles,
    )
