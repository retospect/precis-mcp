"""Per-node peek cards for the graph focus page
(``docs/backlog/web-graph-navigation.md`` items 8 and 8b).

Hover or keyboard focus on a ring node shows a short summary of that
node: its hook line and kind, its per-relation link counts (stored
direction, by ruling), and a kind-specific line — a paper's first
author, year, venue and clipped abstract; a finding's or rxn's
provenance count; a memory's section tag. Every card is computed in the
render pass and embedded in the page (Reto 2026-10-15, gr489659:
"embedded, not fetched — it makes it responsive"), so a hover never
makes a request.

The read shape is fixed: one :meth:`Store.fetch_refs_by_ids`, one
:meth:`Store.link_rel_summary_for_refs` and, only when memories are on
the ring, one :meth:`Store.ref_tags_bulk` — three batched queries per
render whatever the ring size. The budget is about
:data:`PEEK_BYTES_PER_NODE` of text per node; past the page total the
abstracts go first, longest first.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from precis.utils import handle_registry
from precis.utils.authors import author_names

if TYPE_CHECKING:
    from precis.store.store import Store

#: Text budget per card, summed over the page (a 100-node ring is ~40 KB).
PEEK_BYTES_PER_NODE = 400
_HOOK_CHARS = 120
_ABSTRACT_CHARS = 300
#: Per-rel count rows shown on a card; the rest fold into "+N more".
_COUNT_ROWS = 6
#: Relations that count as a finding's or rxn's provenance — the evidence
#: links (prod 2026-10-15: inbound ``corroborates``/``cites``/``supports``/
#: ``establishes`` dominate findings), counted in either direction.
PROVENANCE_RELS: frozenset[str] = frozenset(
    {"establishes", "corroborates", "supports", "derived-from", "cites", "cited-by"}
)
_PROVENANCE_KINDS: frozenset[str] = frozenset({"finding", "rxn"})

_WS_RE = re.compile(r"\s+")
_SENTENCE_RE = re.compile(r"(?<=[.!?])\s")


def _clip(text: str, limit: int) -> str:
    one = _WS_RE.sub(" ", text or "").strip()
    return one if len(one) <= limit else one[: limit - 1].rstrip() + "…"


def _hook(title: str) -> str:
    """Title, or its first sentence when the title is a whole body."""
    one = _WS_RE.sub(" ", title or "").strip()
    if len(one) > _HOOK_CHARS:
        one = _SENTENCE_RE.split(one, maxsplit=1)[0]
    return _clip(one, _HOOK_CHARS)


def _section(tags: list[tuple[str, str]]) -> str:
    for ns, val in tags:
        if ns == "section":
            return val
        if val.startswith("section:"):
            return val.split(":", 1)[1]
    return ""


def _size(card: dict[str, Any]) -> int:
    return sum(len(str(v)) for v in card.values() if isinstance(v, str)) + sum(
        len(r["rel"]) + 6 for r in card["counts"]
    )


def peek_cards(store: Store, nodes: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """``{handle: card}`` for ``nodes`` (``{kind, id, label}`` dicts).

    A card is ``{handle, kind, hook, counts: [{dir, rel, n}], more,
    byline, abstract, provenance, section}``; the string fields are
    plain text (the template escapes them) and empty when they don't
    apply to the kind.
    """
    ids = list(dict.fromkeys(int(n["id"]) for n in nodes))
    if not ids:
        return {}
    refs = store.fetch_refs_by_ids(ids)
    summary = store.link_rel_summary_for_refs(ids)
    mem_ids = [i for i in ids if i in refs and refs[i].kind == "memory"]
    tags = store.ref_tags_bulk(mem_ids) if mem_ids else {}

    cards: dict[str, dict[str, Any]] = {}
    for n in nodes:
        rid, kind = int(n["id"]), str(n["kind"])
        h = handle_registry.try_format(kind, rid) or f"{kind}:{rid}"
        if h in cards:
            continue
        ref = refs.get(rid)
        rels = sorted(
            summary.get(rid, {}).items(), key=lambda kv: (-kv[1], kv[0][1], kv[0][0])
        )
        card: dict[str, Any] = {
            "handle": h,
            "kind": kind,
            "hook": _hook((ref.title if ref else "") or str(n.get("label") or h)),
            "counts": [
                {"dir": d, "rel": rel, "n": c} for (d, rel), c in rels[:_COUNT_ROWS]
            ],
            "more": sum(c for _, c in rels[_COUNT_ROWS:]),
            "byline": "",
            "abstract": "",
            "provenance": "",
            "section": "",
        }
        if ref is not None and kind == "paper":
            names = author_names(ref.authors)
            venue = ref.meta.get("journal") or ref.meta.get("venue") or ""
            parts = [
                names[0] + (" et al." if len(names) > 1 else "") if names else "",
                str(ref.year) if ref.year else "",
                _clip(str(venue), 60),
            ]
            card["byline"] = " · ".join(p for p in parts if p)
            card["abstract"] = _clip(
                str(ref.meta.get("abstract") or ""), _ABSTRACT_CHARS
            )
        if kind in _PROVENANCE_KINDS:
            prov = sum(c for (_, rel), c in rels if rel in PROVENANCE_RELS)
            card["provenance"] = f"{prov} provenance link{'' if prov == 1 else 's'}"
        if kind == "memory":
            card["section"] = _section(tags.get(rid, []))
        cards[h] = card

    budget = PEEK_BYTES_PER_NODE * len(cards)
    total = sum(_size(c) for c in cards.values())
    for c in sorted(cards.values(), key=lambda c: -len(c["abstract"])):
        if total <= budget or not c["abstract"]:
            break
        total -= len(c["abstract"])
        c["abstract"] = ""
    return cards
