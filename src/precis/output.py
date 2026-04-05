"""Output formatting — provenance markers, hints, node rendering.

Markers::

    =  verbatim (author's text, safe to quote)
    ~  derived (keywords, summary — not quotable)
    %  annotation (user note or comment)
"""

from __future__ import annotations

from precis.protocol import Node
from precis.uri import SEP

# Marker constants
VERBATIM = "="
DERIVED = "~"
ANNOTATION = "%"


def format_verbatim(text: str) -> str:
    """Mark text as verbatim (quotable)."""
    return f"{VERBATIM}\n{text}"


def format_derived(text: str) -> str:
    """Mark text as derived (not quotable)."""
    return f"{DERIVED}  {text}"


def format_annotation(text: str) -> str:
    """Mark text as a user annotation."""
    return f"  {ANNOTATION} {text}"


def format_node_header(
    node: Node,
    *,
    show_slug: bool = True,
    show_index: bool = False,
    show_page: bool = False,
    show_source: bool = False,
) -> str:
    """Format the identifier line for a node.

    Adapts output based on document type conventions:
    - Writable docs: slug is primary (for put())
    - Read-only docs: index is primary (for sequential reading)
    """
    parts = []

    if show_index and node.index is not None:
        parts.append(f"{SEP}{node.index}")

    if show_slug:
        parts.append(node.slug)

    parts.append(str(node.path))

    if show_page and node.page:
        parts.append(f"(p{node.page})")

    if show_source and node.source_file:
        loc = node.source_file
        if node.source_line_end > node.source_line_start:
            loc += f":{node.source_line_start}..{node.source_line_end}"
        elif node.source_line_start:
            loc += f":{node.source_line_start}"
        parts.append(loc)

    if node.node_type == "h":
        hashes = "#" * node.heading_level()
        parts.append(f"{hashes}| {node.text}")
    # comments badge
    if node.comments:
        parts.append(f"💬{len(node.comments)}")

    return "  ".join(parts)


def format_node_full(
    node: Node,
    *,
    show_slug: bool = True,
    show_index: bool = False,
    show_page: bool = False,
    show_source: bool = False,
) -> str:
    """Format a node with full verbatim text."""
    header = format_node_header(
        node,
        show_slug=show_slug,
        show_index=show_index,
        show_page=show_page,
        show_source=show_source,
    )
    lines = [header]

    if node.node_type != "h":
        lines.append(format_verbatim(node.text))

    for c in node.comments:
        lines.append(format_annotation(f"[{c.get('author', '?')}] {c.get('text', '')}"))

    return "\n".join(lines)


def format_node_precis(
    node: Node,
    *,
    show_slug: bool = True,
    show_index: bool = False,
    show_page: bool = False,
    show_source: bool = False,
) -> str:
    """Format a node with derived precis (keywords or summary)."""
    header = format_node_header(
        node,
        show_slug=show_slug,
        show_index=show_index,
        show_page=show_page,
        show_source=show_source,
    )

    if node.node_type == "h":
        return header

    precis = node.precis or node.text
    line = f"{header}  {DERIVED}  {precis}"

    for c in node.comments:
        line += "\n" + format_annotation(
            f"[{c.get('author', '?')}] {c.get('text', '')}"
        )

    return line


def format_hints(hints: list[str]) -> str:
    """Format hints as a Next: block."""
    if not hints:
        return ""
    return "\n\nNext:\n  " + "\n  ".join(hints)


def truncate(text: str | None, max_chars: int = 120) -> str:
    """Truncate text, adding ellipsis if needed."""
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 1].rstrip() + "…"
