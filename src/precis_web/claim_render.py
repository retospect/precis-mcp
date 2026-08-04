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

from precis.taproot.cite import finding_cite_keys
from precis.taproot.seniority import EvidenceEdge, is_claim_hub
from precis.utils import handle_registry
from precis.utils.pub_id_lookup import lookup_pub_id_finding

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


#: Quote budget per grounding passage on the claim page; the popover clamps
#: further via CSS.
_CHUNK_QUOTE_CHARS = 700


def _grounding_chunks(
    store: Any, rows: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The distinct grounding passages (``source_handle`` chunks) across the
    evidence rows, print-set-first order — the claim page's passage list and
    the hover popover's cited-chunk lines. Each entry quotes the chunk's
    verbatim text when the handle resolves (`_draft_ops.py::universal_chunk`);
    a dangling or non-chunk handle keeps its row with an empty quote so the
    pointer stays visible rather than silently vanishing."""
    out: list[dict[str, Any]] = []
    by_handle: dict[str, dict[str, Any]] = {}
    for row in rows:
        handle = row["source_handle"]
        if not handle:
            continue
        entry = by_handle.get(handle)
        if entry is None:
            chunk = store.universal_chunk(handle) if row["source_is_chunk"] else None
            text = " ".join(((chunk or {}).get("text") or "").split())
            if len(text) > _CHUNK_QUOTE_CHARS:
                text = text[:_CHUNK_QUOTE_CHARS].rstrip() + "…"
            entry = {
                "handle": handle,
                "is_chunk": row["source_is_chunk"],
                "text": text,
                "papers": [],
                "starred": False,
            }
            by_handle[handle] = entry
            out.append(entry)
        if row["handle"] not in entry["papers"]:
            entry["papers"].append(row["handle"])
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
    originators = [_edge_row(e, starred=True) for e in evidence.originators]
    corroborators = [
        _edge_row(e, starred=corroborators_print) for e in evidence.corroborators
    ]
    contradictors = [_edge_row(e, starred=False) for e in evidence.contradictors]
    return {
        "head": head,
        "hub_ref_id": ref_id,
        "claim": claim,
        "status": None,
        "originators": originators,
        "corroborators": corroborators,
        "contradictors": contradictors,
        "chunks": _grounding_chunks(store, originators + corroborators + contradictors),
        "coverage_note": evidence.coverage_note,
        "inflight": cite.inflight,
    }
