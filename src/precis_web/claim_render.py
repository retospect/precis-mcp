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
mines, `utils/refeye.py`). :func:`render_claim_evidence` resolves either
head to the hub's ref_id and defers to `finding_cite_keys` for the
evidence.

Consumers: the ``/claim/<head>`` page, the ``/preview/claim/<head>`` hover
fragment, and the reader sidebars.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from markupsafe import Markup

from precis.taproot.cite import finding_cite_keys
from precis.taproot.seniority import EvidenceEdge, is_claim_hub
from precis.taproot.trust import claim_trust
from precis.utils import handle_registry
from precis.utils.pub_id_lookup import lookup_pub_id_finding
from precis.utils.table_data import parse_markdown_table
from precis_web.linkify import render_markdown

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


def _resolve_head_ref_id(store: Any, head: str) -> int | None:
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


def resolve_head_ref_id(store: Any, head: str) -> int | None:
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


def hub_cite_heads(store: Any, texts: Iterable[str]) -> frozenset[str]:
    """The cite heads in ``texts`` that resolve to a live ``TAPROOT:claim``
    hub — the ``claims`` side-channel a reader threads into
    :func:`precis_web.linkify.linkify_refs` so a ``[fi123]`` / ``[<pub_id>]``
    cite renders as a claim anchor. Each distinct head is resolved once
    across the window, so a hub cited many times costs one lookup."""
    heads: set[str] = set()
    resolved: dict[str, bool] = {}
    for text in texts:
        for head in cite_heads_in(text):
            if head not in resolved:
                ref_id = _resolve_head_ref_id(store, head)
                resolved[head] = ref_id is not None and is_claim_hub(store, ref_id)
            if resolved[head]:
                heads.add(head)
    return frozenset(heads)


def _edge_row(edge: EvidenceEdge, *, starred: bool) -> dict[str, Any]:
    """One supporter row for the templates. ``starred`` = this paper reaches
    the ``.bib`` on export (the ★ the user asked for); ``source_handle`` is
    the grounding ``pc<id>`` passage, ``None`` until the chase populates it.
    ``source_is_chunk`` = the handle parses as a universal chunk handle, so
    the template may render it as a ``/c/<handle>`` anchor (the legacy
    ``slug~ord`` form stays plain text)."""
    parsed = handle_registry.parse(edge.source_handle) if edge.source_handle else None
    return {
        "handle": handle_registry.format_handle("paper", edge.paper_ref_id),
        "paper_ref_id": edge.paper_ref_id,
        "title": edge.title,
        "year": edge.year,
        "source_handle": edge.source_handle,
        "source_is_chunk": parsed is not None and parsed[1],
        "integrity": edge.integrity,
        "role": edge.derived_role,
        "starred": starred,
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
    text = (text or "").strip("\n")
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
    store: Any, rows: Iterable[tuple[dict[str, Any], str]]
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
    claim page's ``<blockquote>`` uses."""
    out: list[dict[str, Any]] = []
    by_handle: dict[str, dict[str, Any]] = {}
    for row, role_label in rows:
        handle = row["source_handle"]
        if not handle:
            continue
        entry = by_handle.get(handle)
        if entry is None:
            chunk = store.universal_chunk(handle) if row["source_is_chunk"] else None
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


def render_claim_evidence(store: Any, head: str) -> dict[str, Any] | None:
    """Resolve a cite head to its claim hub + derived evidence, shaped for the
    web. Returns ``None`` when ``head`` doesn't resolve to a live
    ``TAPROOT:claim`` hub (an ordinary finding, a bare paper cite, or prose
    that merely looks like a head) — the caller then renders nothing special.
    """
    ref_id = _resolve_head_ref_id(store, head)
    if ref_id is None:
        return None
    cite = finding_cite_keys(store, ref_id)
    if not cite.is_hub or cite.evidence is None:
        return None
    evidence = cite.evidence

    hub_ref = store.fetch_refs_by_ids([ref_id]).get(ref_id)
    claim = " ".join((getattr(hub_ref, "title", None) or "").split()) if hub_ref else ""

    # Print set (★): originators when derived, else corroborators as the
    # fallback — the same policy `finding_cite_keys` prints from.
    corroborators_print = not evidence.originators
    # The hub-derived trust label (finding-trust-surfaces §3): empty print
    # set → "unverified", any print-visible supporter → "clean" (hub
    # "unsupported" is deferred — see `claim_trust`'s hub arm). Was a
    # dormant `None` placeholder; `claim_trust` is the ONE mapping every
    # trust surface reads, so this can't drift from the export/badge label.
    status = claim_trust(store, ref_id).label
    originators = [_edge_row(e, starred=True) for e in evidence.originators]
    corroborators = [
        _edge_row(e, starred=corroborators_print) for e in evidence.corroborators
    ]
    contradictors = [_edge_row(e, starred=False) for e in evidence.contradictors]
    # Every distinct grounding passage across all three roles, each labeled
    # with the role(s) it grounds — the "reproduce fi191167, fix the
    # grouping" ask (slice 1 item 4): a passage cited from more than one
    # role must show each, not just the paper handle.
    grounding_rows = (
        [(r, "originator") for r in originators]
        + [(r, "corroborator") for r in corroborators]
        + [(r, "contradictor") for r in contradictors]
    )
    return {
        "head": head,
        "hub_ref_id": ref_id,
        "claim": claim,
        "status": status,
        "originators": originators,
        "corroborators": corroborators,
        "contradictors": contradictors,
        "chunks": _grounding_chunks(store, grounding_rows),
        "coverage_note": evidence.coverage_note,
        "inflight": cite.inflight,
    }
