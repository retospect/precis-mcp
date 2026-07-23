"""Citer-verdict sidecar — narrow, capped chunk-level citation display.

``docs/design/citation-chunk-grounding.md`` Part 3 ("sidecar render"):
a one-off, capped attachment for a chunk that carries resolved
chunk-scoped ``cites`` edges from ``workers/inbound_chase.py`` —
deliberately **not** a slice of the deferred fidelity-ladder /
turn-routing-context-DSL proposal
(``docs/proposals/turn-routing-and-context-dsl.md``, still "deferred —
design captured, not sliced"); see the design doc's "Status" section,
"pragmatic call, not left open".

Both directions the design doc names are now buildable, because
``inbound_chase._resolve_citer_chunk`` runs a *second* locate pass
into the cited paper Y's own chunks (docs/design/citation-chunk-
grounding.md, "Part 2/3 deepening"): a chunk-scoped ``cites`` link's
``src_pos`` (the citer's located chunk) is always set when the citer
has any chunks; ``dst_pos`` (Y's located chunk) is set whenever that
second locate finds a confident match in Y, and left unset when it
doesn't (a citer engaging with Y's paper-level contribution rather
than one specific passage — expected, not a failure). Two render
entry points share the same verdict-filtering/capping/best-first
logic (:func:`_render_sidecar`):

- :func:`render_citer_sidecar` — outbound, ``direction='out'`` filtered
  on ``src_pos`` — "chunk C of paper X cites Y" (the original build).
- :func:`render_cited_by_sidecar` — inbound, ``direction='in'``
  filtered on ``dst_pos`` — "chunk D of paper Y is cited by X",
  answering the design doc's other named case now that ``dst_pos``
  exists.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

from precis.handlers._links_render import _get_call_for

if TYPE_CHECKING:
    from precis.store import Ref, Store

#: Only these verdicts are worth surfacing in a capped sidecar — a
#: ``no`` verdict is an explicit "this citer doesn't substantively
#: engage", not worth the row (design doc, "Inbound sweep policy").
_SURFACE_VERDICTS = {"yes", "partial"}

#: Cap on sidecar entries (design doc: "cap at ~5").
_MAX_ENTRIES = 5

#: Best-first ordering key for the two surfaced verdicts.
_QUALITY = {"yes": 0, "partial": 1}


def render_citer_sidecar(store: Store, ref: Ref, chunk_pos: int) -> str:
    """Capped sidecar for the outbound ``cites`` verdicts on one chunk.

    Rendering chunk C of paper X: which papers does *this chunk*
    cite, and with what verdict. Returns ``""`` when there's nothing
    to show (no chunk-scoped ``cites`` links at this ``src_pos``, or
    none carry a surfaceable verdict) — callers append
    unconditionally, so the empty case must produce no output.
    """
    return _render_sidecar(
        store,
        ref,
        chunk_pos,
        direction="out",
        heading="Cites (verified)",
        row_key="cites",
    )


def render_cited_by_sidecar(store: Store, ref: Ref, chunk_pos: int) -> str:
    """Capped sidecar for the inbound ``cites`` verdicts on one chunk.

    Rendering chunk D of the *cited* paper Y: who cites *this specific
    passage* (``dst_pos == chunk_pos``), and with what verdict — the
    design doc's other named sidecar case, now buildable since
    ``inbound_chase``'s second locate pass populates ``dst_pos``.
    Returns ``""`` when there's nothing to show, same contract as
    :func:`render_citer_sidecar`.
    """
    return _render_sidecar(
        store,
        ref,
        chunk_pos,
        direction="in",
        heading="Cited by (verified)",
        row_key="cited by",
    )


def _render_sidecar(
    store: Store,
    ref: Ref,
    chunk_pos: int,
    *,
    direction: Literal["out", "in"],
    heading: str,
    row_key: str,
) -> str:
    """Shared verdict-filtering/capping/best-first render for both directions.

    Each row is relation + terse verdict + the other paper's identity
    at title/author/year density (**not** the raw ``card_combined``
    chunk text — that also carries the full abstract, too verbose for
    a capped row; see module docstring) — never eagerly expanded, only
    a ``get(...)`` call to fetch it. Truncated entries beyond the cap
    are noted as a bare count, not expanded.
    """
    links = store.links_for(ref.id, direction=direction, relation="cites")
    pos_attr = "src_pos" if direction == "out" else "dst_pos"
    other_attr = "dst_ref_id" if direction == "out" else "src_ref_id"
    candidates = [
        lk
        for lk in links
        if getattr(lk, pos_attr) == chunk_pos
        and lk.meta.get("supports") in _SURFACE_VERDICTS
    ]
    if not candidates:
        return ""

    # Best-first: yes before partial. The design doc's tiebreak ("the
    # citing paper's own citation count, if readily available") isn't
    # — this repo doesn't persist S2 citationCount on a paper ref
    # (checked) — so ties keep insertion (link id) order, the
    # documented fallback.
    candidates.sort(key=lambda lk: (_QUALITY[lk.meta["supports"]], lk.id))

    shown, extra = candidates[:_MAX_ENTRIES], candidates[_MAX_ENTRIES:]
    other_ids = {getattr(lk, other_attr) for lk in shown}
    targets = store.fetch_refs_by_ids(other_ids)

    rows: list[dict[str, str]] = []
    for lk in shown:
        other_id = getattr(lk, other_attr)
        target = targets.get(other_id)
        rows.append(
            {
                row_key: _identity(target, other_id),
                "verdict": _verdict_text(lk.meta),
                "how to get": _get_call_for(target, other_id),
            }
        )

    from precis.format import render_agent_table

    out = f"\n\n{heading}:\n" + render_agent_table(
        rows, schema=[row_key, "verdict", "how to get"]
    )
    if extra:
        out += f"\n({len(extra)} more)"
    return out


def _identity(ref: Ref | None, fallback_id: int) -> str:
    """Terse title/author/year identity — ``card_combined``-fidelity
    *density*, not its literal (much longer, abstract-carrying) text."""
    if ref is None:
        return f"<ref {fallback_id}>"
    bits = [ref.title or "(untitled)"]
    authors = ref.authors or []
    if authors:
        first = authors[0].get("family") or authors[0].get("name") or ""
        suffix = " et al." if len(authors) > 1 else ""
        if first:
            bits.append(f"{first}{suffix}")
    if ref.year:
        bits.append(f"({ref.year})")
    return " ".join(bits)


def _verdict_text(meta: dict[str, Any]) -> str:
    supports = meta.get("supports", "?")
    caveats = meta.get("caveats") or []
    if caveats:
        return f"{supports}: {caveats[0]}"
    return str(supports)


__all__ = ["render_cited_by_sidecar", "render_citer_sidecar"]
