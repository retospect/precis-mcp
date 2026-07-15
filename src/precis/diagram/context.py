"""The prepared-context assembler for diagram editing (ADR 0057), generic over
:class:`~precis.diagram.lang.DiagramLang`.

Given a diagram's source chunk, produce the block the model edits *inside*:
every depicting element (id + geometry/topology) paired with the chunk it is
bound to, followed by the linked chunk bodies inlined. This is the diagram
analogue of the planner's ``_render_anchor_context`` — generalised from one
anchor chunk to a whole diagram's element set. The only language-specific hook
is ``lang.elements(source)`` (the anchor list); everything else is data.
"""

from __future__ import annotations

from typing import Any, Protocol

from precis.diagram.lang import DiagramLang

#: Cap on an inlined linked-chunk body — keep the prompt bounded.
_BODY_CHARS = 1200


class _StoreLike(Protocol):
    def element_bindings(self, node_chunk_id: int) -> list[dict[str, Any]]: ...
    def universal_chunk(self, handle: str) -> dict[str, Any] | None: ...


def render_diagram_context(
    lang: DiagramLang, store: _StoreLike, node_chunk_id: int, source: str
) -> str:
    """The ``## Diagram elements ↔ linked context`` block (+ inlined bodies),
    or ``""`` when nothing is bound (the block is simply omitted). Safe on an
    empty/unparseable ``source`` — coordinates degrade to ``(no matching
    element)``, which is itself the dangling-binding signal."""
    binds = store.element_bindings(node_chunk_id)
    if not binds:
        return ""
    coords = {e.id: (e.tag, e.coords) for e in lang.elements(source)}

    lines = [
        "## Diagram elements ↔ linked context",
        "Each depicting element and the chunk it is bound to. Adjust bindings "
        "with the `links` field of your reply (add/keep/drop element→chunk "
        "edges); keep every element faithful to the linked source.",
    ]
    for b in binds:
        tag, crd = coords.get(b["element"], ("?", ""))
        where = f"  {crd}" if crd else "  (no matching element — dangling)"
        title = f"  {b['title']}" if b.get("title") else ""
        lines.append(
            f"- {b['element']}  [{tag}]{where}  → {b['handle']} "
            f"({b['relation']}){title}"
        )

    bodies: list[str] = []
    seen: set[str] = set()
    for b in binds:
        handle = b["handle"]
        if handle in seen:
            continue
        seen.add(handle)
        text = _linked_text(store, b)
        if text:
            bodies.append(
                f"### {handle} — {b['kind']}:{b['ident']} — {b.get('title') or ''}\n"
                + _quote(text)
            )
    if bodies:
        lines.append("\n## Linked chunk bodies")
        lines.extend(bodies)
    return "\n".join(lines)


def _linked_text(store: _StoreLike, binding: dict[str, Any]) -> str:
    """The bound target's text — a chunk body for a chunk-level target, else
    ``""`` (a ref-level target's title already appears in the element line)."""
    if binding.get("chunk_id") is None:
        return ""  # ref-level target (e.g. a memory record) — title suffices
    got = store.universal_chunk(binding["handle"])
    if not got:
        return ""
    return str(got.get("text") or "").strip()[:_BODY_CHARS]


def _quote(text: str) -> str:
    return "\n".join("> " + ln for ln in text.splitlines()) or "> (empty)"
