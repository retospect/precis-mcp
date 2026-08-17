"""Assemble a Taproot claim hub's evidence for the web surfaces.

One place so the three reader surfaces agree on shape and — load-bearing —
on the ★ = *print-visible* rule: which supporters actually reach the
bibliography when the document is finalised. That set is the derived
``establishes`` originators, falling back to corroborators when no
originator has been derived yet (`taproot/cite.py::finding_cite_keys` →
`hub_cite_keys`, the same living-citation policy `precis resolve` prints
from). The fuller evidence (un-starred corroborators / contradictors) is
shown for context but does not print.

A hub is cited by a **cite head** — either its 6-char ``[<pub_id>]`` or its
``[fi<id>]`` finding handle (the two grammars the reference ring also
mines, `utils/refeye.py`). :func:`render_claim_evidence` resolves a head to
the hub's ref_id, then `derive_evidence` for the evidence (the same
derivation `finding_cite_keys` calls internally — this module reuses the
lower-level function directly so it can also thread the result into
`claim_trust`, once, rather than re-deriving it). :func:`render_claims_evidence`
is the plural twin — many cite heads' evidence in a handful of bulk queries
regardless of hub count (OPEN-ITEMS.md "/smartdraft reader" perf fix).

Consumers: the ``/claim/<head>`` page, the ``/preview/claim/<head>`` hover
fragment, and the reader sidebars (singular for one hub; plural for a
render-window's worth at once).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import TYPE_CHECKING, Any

from markupsafe import Markup

from precis.taproot.cite import hub_cite_keys
from precis.taproot.seniority import (
    CiterEdge,
    EvidenceEdge,
    HubEvidence,
    conjunct_atoms_bulk,
    derive_evidence,
    derive_evidence_bulk,
    hub_citers,
    is_claim_hub,
    is_claim_hub_bulk,
)
from precis.taproot.trust import claim_trust
from precis.utils import handle_registry
from precis.utils.mentions import strip_page_anchor_links
from precis.utils.pub_id_lookup import lookup_pub_id_finding
from precis.utils.table_data import parse_markdown_table
from precis_web.linkify import render_markdown
from precis_web.paper_ident import paper_head, paper_head_from_facts

if TYPE_CHECKING:
    from precis.store.store import Store

#: A hub-cite head in prose: a ``[fi<id>]`` finding handle or a 6-char
#: ``[<pub_id>]``, optionally pinned (`>`/`+` + handle list — ignored for
#: head extraction). Group 1 is the head. Kept in step with
#: `precis_web/linkify.py::_CLAIM_CITE_PATTERN` so the claim anchor and the
#: ``claims`` side-channel agree on what a hub cite looks like.
_CITE_HEAD_RE = re.compile(
    r"\[((?:fi[0-9]+)|(?:[a-z2-7]{6}))"
    r"(?:[>+][a-z]{2}[0-9]+(?:,[a-z]{2}[0-9]+)*)?\]"
)
_FI_HEAD_RE = re.compile(r"^fi[0-9]+$")


def _resolve_head_ref_id(store: Store, head: str) -> int | None:
    """A cite head (``fi<id>`` handle or 6-char pub_id) → its finding ref_id,
    or ``None``. An ``fi`` head resolves via the handle registry (following
    a merged-record redirect); anything else is a pub_id lookup."""
    if _FI_HEAD_RE.match(head):
        parsed = handle_registry.parse(head)
        if parsed is None:
            return None
        kind, is_chunk, ref_id = parsed
        if kind != "finding" or is_chunk:
            return None
        return int(ref_id)
    lookup = lookup_pub_id_finding(store, head)
    return lookup["ref_id"] if lookup is not None else None


def resolve_head_ref_id(store: Store, head: str) -> int | None:
    """Public alias for :func:`_resolve_head_ref_id` — the canonical
    cite-head → finding ref_id resolver. Exposed (rather than making the
    caller reach for the underscore name) so the smartdraft claim-trust
    badge (``smartdraft.py::claim_trust_for_block``, finding-trust-
    surfaces §3) reuses the exact same resolution the ``/claim`` page and
    the ``claims`` side-channel already use — head resolution can never
    fork across surfaces."""
    return _resolve_head_ref_id(store, head)


def cite_heads_in(text: str) -> list[str]:
    """The distinct cite heads syntactically present in ``text``, first-seen
    order — a pure regex scan, NO resolution. Lets a caller that already
    resolved a window's hubs map one row's heads to cached evidence without
    re-hitting the DB per row."""
    out: list[str] = []
    seen: set[str] = set()
    for match in _CITE_HEAD_RE.finditer(text or ""):
        head = match.group(1)
        if head not in seen:
            seen.add(head)
            out.append(head)
    return out


def hub_cite_heads(store: Store, texts: Iterable[str]) -> frozenset[str]:
    """The cite heads in ``texts`` that resolve to a live ``TAPROOT:claim``
    hub — the ``claims`` side-channel a reader threads into
    :func:`precis_web.linkify.linkify_refs` so a ``[fi123]`` / ``[<pub_id>]``
    cite renders as a claim anchor. Each distinct head is resolved once
    across the window (first to a ref_id, then the hub check runs as ONE
    bulk query over every distinct ref_id — :func:`~precis.taproot.
    seniority.is_claim_hub_bulk` — rather than one ``is_claim_hub`` round
    trip per head, OPEN-ITEMS.md's "/smartdraft reader" batch B)."""
    head_ref: dict[str, int] = {}
    for text in texts:
        for head in cite_heads_in(text):
            if head in head_ref:
                continue
            ref_id = _resolve_head_ref_id(store, head)
            if ref_id is not None:
                head_ref[head] = ref_id
    if not head_ref:
        return frozenset()
    hub_flags = is_claim_hub_bulk(store, head_ref.values())
    return frozenset(h for h, rid in head_ref.items() if hub_flags.get(rid))


def _unacq_map(paper_refs: dict[int, Any] | None) -> dict[int, dict[str, Any]]:
    """``{paper_ref_id: unacquirable_override}`` for the supporter papers that
    carry a Meta-tab (paper-level) unacquirable declaration — the per-row
    twin of the hub-harden check in
    :func:`~precis.taproot.trust._hub_grounding_unacquirable`. Read
    from the already-batched ``paper_refs`` so no extra fetch. Empty when
    ``paper_refs`` is ``None`` (a caller that didn't batch them)."""
    out: dict[int, dict[str, Any]] = {}
    for pid, r in (paper_refs or {}).items():
        override = (getattr(r, "meta", None) or {}).get("unacquirable_override")
        if isinstance(override, dict):
            out[pid] = override
    return out


def _edge_row(
    edge: EvidenceEdge,
    *,
    starred: bool,
    paper_ref: Any = None,
    unacq: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One supporter row for the templates. ``starred`` = this paper reaches
    the ``.bib`` on export (the ★ the user asked for); ``source_handle`` is
    the grounding ``pc<id>`` passage, ``None`` until the chase populates it.
    ``source_is_chunk`` = the handle parses as a universal chunk handle, so
    the template may render it as a ``/c/<handle>`` anchor (the legacy
    ``slug~ord`` form stays plain text).

    ``head`` is the shared identity header (year · title / venue · first …
    last) rendered via ``_paper_head`` — the same block the hovers use.
    When ``paper_ref`` (the full row) is supplied it carries venue/authors;
    otherwise it degrades to the edge's title+year. An evidence supporter is
    a paper we hold, so ``held=True`` (sky). Callers pass ``paper_ref`` ONLY
    for originators/corroborators (this hub's supporter set) so the singular
    and bulk paths enrich the identical id set — a contradictor is always
    facts-only, keeping ``render_claims_evidence`` == per-head equality.

    ``unacq`` (this paper's paper-level ``unacquirable_override``, else
    ``None``) names THIS row as a source declared unobtainable — the
    acquirability FACT, mode-less, that (via :func:`~precis.taproot.trust.
    claim_trust`'s harden rule) can push an otherwise-clean hub back to
    unverified when EVERY grounding paper carries one. Rendered as a plain
    "⊘ unacquirable" chip, never Ⓐ/✍ (those are claim-level, not a
    property of this paper row)."""
    parsed = handle_registry.parse(edge.source_handle) if edge.source_handle else None
    handle = handle_registry.format_handle("paper", edge.paper_ref_id)
    if paper_ref is not None:
        head = paper_head(paper_ref, held=True, handle=handle)
    else:
        head = paper_head_from_facts(
            ref_id=edge.paper_ref_id, title=edge.title, year=edge.year, handle=handle
        )
    return {
        "handle": handle,
        "paper_ref_id": edge.paper_ref_id,
        "title": edge.title,
        "year": edge.year,
        "head": head,
        "source_handle": edge.source_handle,
        "source_is_chunk": parsed is not None and parsed[1],
        "integrity": edge.integrity,
        "role": edge.derived_role,
        "starred": starred,
        "unacquirable": isinstance(unacq, dict),
        "unacq_note": unacq.get("note") if isinstance(unacq, dict) else None,
    }


def _citer_row(edge: CiterEdge) -> dict[str, Any]:
    """One "Used by" row: a thing that cites this claim, shaped for the
    template. ``handle`` is the citing chunk's universal handle (``pc<id>`` /
    ``dc<id>``) when the edge pinned a ``src_chunk_id`` — so the template
    renders it via ``linkify_refs`` as a hover-preview anchor that navigates
    to the passage (a ``pc`` handle reusing the shared ``precis-paper``
    window, a ``dc`` opening the draft reader — the target is chosen per-kind
    in :func:`~precis_web.linkify._render_universal_handle`). A chunk-less
    edge falls back to the source *record* handle, shown as plain mono text
    (``is_chunk`` False → no anchor, mirroring the grounding-passage
    convention); a code-less kind degrades to no handle, title only, rather
    than vanishing. ``is_chunk`` gates the ``[handle] | linkify_refs`` vs
    plain-text render."""
    handle: str | None = None
    is_chunk = False
    if edge.src_chunk_id is not None:
        handle = handle_registry.try_format(edge.kind, edge.src_chunk_id, chunk=True)
        is_chunk = handle is not None
    if handle is None:
        handle = handle_registry.try_format(edge.kind, edge.src_ref_id)
    return {
        "handle": handle,
        "is_chunk": is_chunk,
        "kind": edge.kind,
        "title": " ".join((edge.title or "").split()),
        "year": edge.year,
    }


#: Quote budget per grounding passage in the ``/preview/claim`` popover
#: (`entry["text"]`, whitespace-collapsed — the popover clamps further via
#: line-clamp CSS on top of this). The FULL claim page renders a separate,
#: structure-preserving ``entry["quote_html"]`` below (`_render_quote`).
_CHUNK_QUOTE_CHARS = 700

#: Prose-quote clamp for the full claim page (chars, on the REAL —
#: uncollapsed — text; see `_render_quote`). A quote that IS a table clamps
#: by row count instead (`_TABLE_ROW_CLAMP`), never mid-row.
_QUOTE_HTML_CHARS = 900
_TABLE_ROW_CLAMP = 20

#: Cell/header classes match the smartdraft table editor's read view
#: (`templates/smartdraft/view.html.j2`) so a rendered grounding-quote table
#: looks like any other table in the app.
_TABLE_HEAD_CLASS = (
    "border-b-2 border-slate-300 px-2 py-1 text-left align-top "
    "font-semibold text-slate-800"
)
_TABLE_CELL_CLASS = (
    "border-b border-slate-100 px-2 py-1 align-top text-slate-700 "
    "whitespace-pre-wrap break-words"
)


def _table_html(table: dict[str, Any], *, row_limit: int | None = None) -> Markup:
    """A recovered ``{header, rows, caption?}``
    (:func:`precis.utils.table_data.parse_markdown_table` — the SAME
    recovery the smartdraft table editor uses, so this is not a second
    markdown-table parser in the tree) → a real ``<table>``. Cell content
    still runs through :func:`render_markdown` (bold/code/sub/sup, ``$…$``
    left verbatim for client-side KaTeX) — a paper table cell can
    legitimately hold inline math. ``row_limit`` clamps the row COUNT, never
    mid-row, so a wide/long table stays structurally intact."""
    rows = table.get("rows") or []
    limit = row_limit if row_limit is not None else len(rows)
    truncated = len(rows) > limit
    shown = rows[:limit] if truncated else rows
    caption = (table.get("caption") or "").strip()
    parts: list[str] = ['<table class="w-full border-collapse text-sm">']
    if caption:
        parts.append(
            '<caption class="mb-1 text-left text-xs text-slate-500 italic">'
            f"{render_markdown(caption)}</caption>"
        )
    parts.append("<thead><tr>")
    for h in table.get("header") or []:
        parts.append(f'<th class="{_TABLE_HEAD_CLASS}">{render_markdown(h)}</th>')
    parts.append("</tr></thead><tbody>")
    for row in shown:
        parts.append("<tr>")
        for cell in row:
            parts.append(
                f'<td class="{_TABLE_CELL_CLASS}">{render_markdown(cell)}</td>'
            )
        parts.append("</tr>")
    parts.append("</tbody></table>")
    if truncated:
        parts.append(
            f'<p class="mt-1 text-xs text-slate-400 italic">… '
            f"{len(rows) - limit} more row(s) truncated</p>"
        )
    return Markup("".join(parts))


def _prose_block_html(block: str) -> Markup:
    """One blank-line-delimited prose block → a ``<p>``, keeping its own
    internal line breaks (``<br>`` — verbatim paper text wraps mid-sentence
    at the source's column width, worth keeping) and running each line
    through the same bold/code/sub/sup/math subset `render_markdown` gives
    every other quoted span in the app."""
    lines = [render_markdown(line) for line in block.splitlines()]
    return Markup("<p>") + Markup("<br>").join(lines) + Markup("</p>")


def _render_quote(text: str) -> tuple[Markup, bool]:
    """A grounding chunk's verbatim text → safe HTML for the full claim
    page, preserving the structure the old whitespace-collapse destroyed
    (fi191167 — a markdown table arrived as one unreadable pipe run).

    A quote that IS (up to whitespace) one GFM pipe table — the shape a
    ``chunk_kind='table'`` chunk's derived text always has, and what a
    Marker-ingested table chunk looks like too — renders as a single
    ``<table>``, clamped by row count. Anything else splits on blank lines
    into blocks; a block that is itself a whole table (a mixed prose+table
    chunk) still renders as a ``<table>``, everything else as a paragraph.
    The overall quote is clamped by character count on the REAL
    (uncollapsed) text before block-splitting — never mid-table, since the
    whole-quote-is-a-table case above is handled first and clamps by row
    instead. Returns ``(html, truncated)``."""
    # Collapse Marker's inert page-anchor citation links (``[11](#page-5-0)``)
    # BEFORE splitting: it drops the raw markdown (render_markdown does no
    # ref-linking, so it would otherwise show verbatim here) and, by
    # whitespace-normalising the bracket's inner text, removes any blank line
    # Marker's block-merge fused inside the span — the seam the paragraph
    # splitter below would otherwise shred into a stray ``<p>11</p>``.
    text = strip_page_anchor_links((text or "").strip("\n"))
    if not text.strip():
        return Markup(""), False

    whole_table = parse_markdown_table(text)
    if whole_table is not None and whole_table.get("header"):
        rows = whole_table.get("rows") or []
        truncated = len(rows) > _TABLE_ROW_CLAMP
        return _table_html(whole_table, row_limit=_TABLE_ROW_CLAMP), truncated

    truncated = len(text) > _QUOTE_HTML_CHARS
    if truncated:
        text = text[:_QUOTE_HTML_CHARS].rstrip() + "…"
    blocks = [b for b in re.split(r"\n\s*\n", text) if b.strip()]
    if not blocks:
        return Markup(""), truncated
    parts: list[Markup] = []
    for block in blocks:
        block_table = parse_markdown_table(block) if len(blocks) > 1 else None
        if block_table is not None and block_table.get("header"):
            parts.append(_table_html(block_table, row_limit=_TABLE_ROW_CLAMP))
        else:
            parts.append(_prose_block_html(block))
    return Markup("").join(parts), truncated


def _grounding_chunks(
    store: Store,
    rows: Iterable[tuple[dict[str, Any], str]],
    *,
    chunk_cache: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """The distinct grounding passages (``source_handle`` chunks) across the
    evidence rows, print-set-first order — the claim page's passage list and
    the hover popover's cited-chunk lines. Each entry quotes the chunk's
    verbatim text when the handle resolves (`_draft_ops.py::universal_chunk`);
    a dangling or non-chunk handle keeps its row with an empty quote so the
    pointer stays visible rather than silently vanishing.

    ``rows`` pairs each evidence row with its section label (``originator``/
    ``corroborator``/``contradictor``) so a passage cited from more than one
    role lists each ROLE, not just the paper — the "which role grounds this
    passage" a buried-passage report (fi191167) needs, beyond just listing
    the paper handles. ``entry["text"]`` stays the OLD whitespace-collapsed
    quote (popover compatibility, char-sliced there); ``entry["quote_html"]``
    is the new structure-preserving render (:func:`_render_quote`) the full
    claim page's ``<blockquote>`` uses.

    ``chunk_cache`` — a pre-fetched ``{handle: chunk}`` map
    (:func:`~precis.store.Store.universal_chunks`) — skips the per-handle
    ``store.drafts.universal_chunk`` round trip when given (a bulk caller resolving
    many hubs' grounding passages in one query, OPEN-ITEMS.md batch B)."""
    out: list[dict[str, Any]] = []
    by_handle: dict[str, dict[str, Any]] = {}
    for row, role_label in rows:
        handle = row["source_handle"]
        if not handle:
            continue
        entry = by_handle.get(handle)
        if entry is None:
            if row["source_is_chunk"]:
                chunk = (
                    chunk_cache.get(handle)
                    if chunk_cache is not None
                    else store.drafts.universal_chunk(handle)
                )
            else:
                chunk = None
            raw_text = (chunk or {}).get("text") or ""
            collapsed = " ".join(raw_text.split())
            if len(collapsed) > _CHUNK_QUOTE_CHARS:
                collapsed = collapsed[:_CHUNK_QUOTE_CHARS].rstrip() + "…"
            quote_html, quote_truncated = _render_quote(raw_text)
            entry = {
                "handle": handle,
                "is_chunk": row["source_is_chunk"],
                "text": collapsed,
                "quote_html": quote_html,
                "quote_truncated": quote_truncated,
                "papers": [],
                "starred": False,
            }
            by_handle[handle] = entry
            out.append(entry)
        if not any(p["handle"] == row["handle"] for p in entry["papers"]):
            entry["papers"].append(
                {"handle": row["handle"], "role": role_label, "starred": row["starred"]}
            )
        entry["starred"] = entry["starred"] or row["starred"]
    return out


def _supporter_ref_ids(evidence: HubEvidence) -> set[int]:
    """The paper ref_ids whose ``cite_key`` a hub's print-set resolution
    might need — originators AND corroborators (the fallback group), same
    set :func:`~precis.taproot.cite.hub_cite_keys` walks."""
    return {e.paper_ref_id for e in (*evidence.originators, *evidence.corroborators)}


def _source_handles(evidence: HubEvidence) -> set[str]:
    """Every distinct grounding ``source_handle`` across all evidence edges —
    read from :attr:`HubEvidence.grounding` (one per raw edge, so a paper that
    grounds the claim at two passages contributes both), not the per-paper
    seniority edges (which collapse a paper's multiple grounding chunks into
    one row)."""
    return {g.source_handle for g in evidence.grounding if g.source_handle}


def _render_one(
    store: Store,
    head: str,
    ref_id: int,
    evidence: HubEvidence,
    hub_ref: Any,
    *,
    cite_key_map: dict[int, list[str]] | None = None,
    chunk_cache: dict[str, dict[str, Any]] | None = None,
    paper_refs: dict[int, Any] | None = None,
    conjunct_atom_ids: list[int] | None = None,
) -> dict[str, Any]:
    """Shape one already-resolved hub's evidence for the web — the shared
    tail both :func:`render_claim_evidence` (singular) and
    :func:`render_claims_evidence` (bulk) call once they've each derived
    ``evidence`` their own way. ``cite_key_map``/``chunk_cache`` are the
    batch-B perf knobs threaded down to :func:`~precis.taproot.cite.
    hub_cite_keys` / :func:`_grounding_chunks`; ``None`` (the singular
    caller's default) falls back to their old per-call query behaviour."""
    claim = " ".join((getattr(hub_ref, "title", None) or "").split()) if hub_ref else ""

    # Print set (★): originators when derived, else corroborators as the
    # fallback — the same policy `finding_cite_keys` prints from.
    corroborators_print = not evidence.originators
    # The hub-derived trust label (trust-surfaces editor badges): empty print
    # set → "unverified", any print-visible supporter → "clean" (hub
    # "unsupported" is deferred — see `claim_trust`'s hub arm). Was a
    # dormant `None` placeholder; `claim_trust` is the ONE mapping every
    # trust surface reads, so this can't drift from the export/badge label.
    # Threading `evidence` (+ `cite_key_map`/`ref`) here is what stops
    # `claim_trust` from re-deriving this SAME hub's evidence a second time
    # (the "derives each hub twice" defect, OPEN-ITEMS.md batch C).
    trust = claim_trust(
        store,
        ref_id,
        evidence=evidence,
        cite_key_map=cite_key_map,
        ref=hub_ref,
        paper_refs=paper_refs,
        conjunct_atom_ids=conjunct_atom_ids,
    )
    status = trust.label
    # ``paper_refs`` (all supporter rows, batched once by both the singular and
    # bulk callers over the same id set) does double duty: it enriches the
    # identity header with venue + authors, and — via ``_unacq_map`` — supplies
    # the per-supporter unacquirable marks the claim-level "grounded on an
    # unacquirable source" banner refers to. ``paper_ref`` is passed ONLY for
    # originators/corroborators (see ``_edge_row`` — a contradictor stays
    # facts-only so the singular/bulk paths stay byte-identical); ``unacq`` is
    # looked up for every role from that same batch.
    refs_map = paper_refs or {}
    unacq_by_paper = _unacq_map(paper_refs)
    originators = [
        _edge_row(
            e,
            starred=True,
            paper_ref=refs_map.get(e.paper_ref_id),
            unacq=unacq_by_paper.get(e.paper_ref_id),
        )
        for e in evidence.originators
    ]
    corroborators = [
        _edge_row(
            e,
            starred=corroborators_print,
            paper_ref=refs_map.get(e.paper_ref_id),
            unacq=unacq_by_paper.get(e.paper_ref_id),
        )
        for e in evidence.corroborators
    ]
    contradictors = [
        _edge_row(e, starred=False, unacq=unacq_by_paper.get(e.paper_ref_id))
        for e in evidence.contradictors
    ]
    # Every distinct grounding passage, each labeled with the role it grounds
    # — the "reproduce fi191167, fix the grouping" ask (slice 1 item 4). Built
    # from `evidence.grounding` (one entry per RAW edge) rather than the
    # per-paper seniority rows, so a paper that grounds the claim at two chunks
    # surfaces BOTH passages (the corroborates-pc regression: derive dedupes
    # seniority by paper, but grounding is per-chunk). Attribution is keyed by
    # the edge's RAW relation, NOT the paper: a `contradicts` grounding is
    # always a contradictor (★-off), a support grounding takes its paper's
    # derived originator/corroborator role — so a paper that both corroborates
    # and contradicts the same claim doesn't get its contradicting passage
    # relabeled as support. Order is normalised here (role rank → year → paper
    # handle → grounding handle) so the singular and bulk paths emit identical
    # `chunks` (the render_claims==render_claim invariant).
    support_row: dict[int, dict[str, Any]] = {}
    support_role: dict[int, tuple[str, int]] = {}
    for rank, (label, group) in (
        (0, ("originator", originators)),
        (1, ("corroborator", corroborators)),
    ):
        for r in group:
            support_row.setdefault(r["paper_ref_id"], r)
            support_role.setdefault(r["paper_ref_id"], (label, rank))
    contradict_row = {r["paper_ref_id"]: r for r in contradictors}
    _grounding: list[tuple[tuple[Any, ...], dict[str, Any], str]] = []
    for g in evidence.grounding:
        if g.relation == "contradicts":
            prow = contradict_row.get(g.paper_ref_id)
            label, rank = "contradictor", 2
        else:
            prow = support_row.get(g.paper_ref_id)
            label, rank = support_role.get(g.paper_ref_id, ("corroborator", 1))
        if prow is None:
            continue
        parsed = handle_registry.parse(g.source_handle) if g.source_handle else None
        grow = {
            "handle": prow["handle"],
            "source_handle": g.source_handle,
            "source_is_chunk": parsed is not None and parsed[1],
            "starred": prow["starred"],
        }
        year = prow.get("year")
        # role rank → year asc (NULL last, matching _sort_group) → paper handle
        # → grounding handle. Sorting here (not in derive) is what keeps the
        # singular and bulk paths' `chunks` identical regardless of query order.
        sort_key = (
            rank,
            year is None,
            year or 0,
            prow["handle"],
            g.source_handle or "",
        )
        _grounding.append((sort_key, grow, label))
    _grounding.sort(key=lambda t: t[0])
    grounding_rows = [(grow, label) for _, grow, label in _grounding]
    cite_keys, _notes = hub_cite_keys(store, evidence, cite_key_map=cite_key_map)
    return {
        "head": head,
        "hub_ref_id": ref_id,
        "claim": claim,
        "status": status,
        # `trust_overridden` — True iff an author declared a claim-level
        # (Ⓐ/✍) softener HERE, on this hub. `trust_note` is always threaded
        # through (not gated on `trust_overridden`): it also carries the
        # harden rule's "grounded only on sources declared unacquirable"
        # explanation when `status` is 'unverified' for that reason (clean
        # was downgraded, but no author assertion was made — see
        # `precis.taproot.trust.claim_trust`).
        "trust_overridden": trust.overridden,
        "trust_note": trust.note,
        "originators": originators,
        "corroborators": corroborators,
        "contradictors": contradictors,
        "chunks": _grounding_chunks(store, grounding_rows, chunk_cache=chunk_cache),
        "coverage_note": evidence.coverage_note,
        "citation_misses": _citation_miss_rows(hub_ref),
        "inflight": not cite_keys,
    }


def _citation_miss_rows(hub_ref: Any) -> list[dict[str, Any]]:
    """The hub's ``meta.citation_misses`` (hub_refine's citation-following
    red flag; citation-taproot-resolve, shipped — git history) shaped for the
    claim page: "we read the paper this claim cites and the content isn't
    there". Each record is ``{marker, cited_ref, from_chunk}``; the template
    renders a red line per miss, linking the cited paper by ref_id."""
    meta = getattr(hub_ref, "meta", None) or {}
    rows: list[dict[str, Any]] = []
    for miss in meta.get("citation_misses") or []:
        if not isinstance(miss, dict):
            continue
        rows.append(
            {"marker": miss.get("marker"), "cited_ref_id": miss.get("cited_ref")}
        )
    return rows


def render_claim_evidence(store: Store, head: str) -> dict[str, Any] | None:
    """Resolve a cite head to its claim hub + derived evidence, shaped for the
    web. Returns ``None`` when ``head`` doesn't resolve to a live
    ``TAPROOT:claim`` hub (an ordinary finding, a bare paper cite, or prose
    that merely looks like a head) — the caller then renders nothing special.

    Single-hub entry point (also the export call-sites' resolution path via
    :func:`~precis.taproot.cite.finding_cite_keys` — this function itself
    isn't reused there, just the same locked policy). Still batches its
    OWN cite_key/grounding-chunk lookups into one query each rather than
    one per supporter/passage (OPEN-ITEMS.md batch B/C applied inward) —
    a page resolving MANY hubs at once should call :func:`render_claims_evidence`
    instead, which additionally batches ACROSS hubs.
    """
    ref_id = _resolve_head_ref_id(store, head)
    if ref_id is None or not is_claim_hub(store, ref_id):
        return None
    evidence = derive_evidence(store, ref_id, assume_hub=True)
    supporter_ids = _supporter_ref_ids(evidence)
    cite_key_map = store.ref_cite_keys_bulk(supporter_ids)
    chunk_cache = store.drafts.universal_chunks(_source_handles(evidence))
    hub_ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    # Supporter paper rows do double duty: enrich the identity header (venue +
    # authors) and carry the per-row unacquirable marks — and spare
    # ``claim_trust`` a re-fetch. The same batch the bulk path makes, over the
    # same id set, so both produce byte-identical rows (the
    # ``render_claims_evidence`` == per-head invariant that
    # test_render_claims_evidence_matches_singular_calls pins).
    paper_refs = store.fetch_refs_by_ids(list(supporter_ids)) if supporter_ids else {}
    return _render_one(
        store,
        head,
        ref_id,
        evidence,
        hub_ref,
        cite_key_map=cite_key_map,
        chunk_cache=chunk_cache,
        paper_refs=paper_refs,
        conjunct_atom_ids=conjunct_atoms_bulk(store, [ref_id])[ref_id],
    )


def claim_full_sentence(store: Store, hub_ref_id: int) -> str | None:
    """The hub's *full* claim sentence — its ``finding_body`` chunk at
    ``ord=0`` (:func:`~precis.taproot.hub.mint_hub` writes the whole sentence
    there; ``refs.title`` is full-length too since the ``[:200]`` cap was
    dropped, but legacy hubs may still carry a truncated title). ``None``
    when no body chunk exists (a legacy hub predating the finding_body
    write) — the caller then falls back to the title.

    Full-page-only (the ``/claim/<head>`` h1 wants the complete sentence),
    kept OUT of the shared :func:`_render_one` shape for the same reason as
    :func:`claim_citers`: the compact popover and the smartdraft Claims rail
    keep the short title, and threading a per-hub body-chunk fetch through the
    bulk path would re-add a round trip batch B removed."""
    text = store.drafts.chunk_text_at(hub_ref_id, 0)
    if not text or not text.strip():
        return None
    return " ".join(text.split())


def claim_citers(store: Store, hub_ref_id: int) -> list[dict[str, Any]]:
    """The "Used by" rows for a claim hub — its inbound ``cites`` edges (who
    invokes this claim), shaped for the template.

    Kept OUT of :func:`render_claim_evidence`/:func:`_render_one` on purpose:
    those share one locked output shape between the singular path and the bulk
    :func:`render_claims_evidence` (the invariant
    ``test_render_claims_evidence_matches_singular_calls`` pins), and citers
    belong only to the full ``/claim/<head>`` page — not the hover popover, not
    the smartdraft Claims rail (where a per-hub citers query would re-introduce
    the round trip batch B removed). The full-page route calls this and merges
    it into the context itself."""
    return [_citer_row(e) for e in hub_citers(store, hub_ref_id)]


def render_claims_evidence(store: Store, heads: Iterable[str]) -> list[dict[str, Any]]:
    """Plural twin of :func:`render_claim_evidence` — resolve MANY cite
    heads' claim-hub evidence in a handful of bulk queries, regardless of
    how many distinct hubs are involved, instead of the ~16 round trips
    per hub the singular path costs (OPEN-ITEMS.md "/smartdraft reader"
    O(all-hubs) TTFB fix, batch B). Output is the SAME shape, order, and
    content as ``[e for h in heads if (e := render_claim_evidence(store, h))
    is not None]`` — heads that don't resolve to a live hub are silently
    dropped, same as the singular function returning ``None``.

    Used by the smartdraft reader for its Claims rail (a render-window's
    worth of distinct hub cites); the docx/latex exporters keep calling
    the singular :func:`~precis.taproot.cite.finding_cite_keys` path
    directly (unaffected by this function existing)."""
    head_ref: dict[str, int] = {}
    ref_ids: list[int] = []
    for head in heads:
        if head in head_ref:
            continue
        ref_id = _resolve_head_ref_id(store, head)
        if ref_id is None:
            continue
        head_ref[head] = ref_id
        if ref_id not in ref_ids:
            ref_ids.append(ref_id)
    if not ref_ids:
        return []

    hub_flags = is_claim_hub_bulk(store, ref_ids)
    hub_ref_ids = [r for r in ref_ids if hub_flags.get(r)]
    if not hub_ref_ids:
        return []

    evidence_by_hub = derive_evidence_bulk(store, hub_ref_ids)
    supporter_ids: set[int] = set()
    source_handles: set[str] = set()
    for ev in evidence_by_hub.values():
        supporter_ids |= _supporter_ref_ids(ev)
        source_handles |= _source_handles(ev)
    cite_key_map = store.ref_cite_keys_bulk(supporter_ids)
    chunk_cache = store.drafts.universal_chunks(source_handles)
    hub_refs = store.fetch_refs_by_ids(hub_ref_ids)
    # Supporter-paper refs for the hub-clean unacquirable-override check, batched
    # once (mirrors cite_key_map) so claim_trust never re-fetches per hub.
    paper_refs = store.fetch_refs_by_ids(list(supporter_ids)) if supporter_ids else {}
    # Conjunct atoms batched once (mirrors cite_key_map) so claim_trust's
    # compound check issues no per-hub derive_conjuncts queries.
    atoms_by_hub = conjunct_atoms_bulk(store, hub_ref_ids)

    out: list[dict[str, Any]] = []
    for head, ref_id in head_ref.items():
        evidence = evidence_by_hub.get(ref_id)
        if evidence is None:
            continue
        out.append(
            _render_one(
                store,
                head,
                ref_id,
                evidence,
                hub_refs.get(ref_id),
                cite_key_map=cite_key_map,
                chunk_cache=chunk_cache,
                paper_refs=paper_refs,
                conjunct_atom_ids=atoms_by_hub.get(ref_id, []),
            )
        )
    return out
