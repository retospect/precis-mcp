"""Render a column-aligned "Next:" hint block.

Every handler view that wants to teach the agent the next useful call
emits a "Next:" trailer. V1 had this everywhere and it was the
brand-defining UX touch — agents learned drill-down, citations,
alternate views just by reading these blocks.

Usage:

    from precis.utils.next_block import format_next_block

    lines = format_next_block([
        ("get(kind='paper', id='X~46..105/toc')", "drill into theory"),
        ("get(kind='paper', id='X', view='bibtex')", "BibTeX citation"),
    ])
    response_body = body + "\\n\\nNext:\\n" + "\\n".join(lines)

Pure logic — no DB, no IO. Just string formatting.
"""

from __future__ import annotations


def format_next_block(
    calls: list[tuple[str, str]],
    *,
    indent: str = "  ",
) -> list[str]:
    """Render `(call, description)` pairs as column-aligned hint lines.

    Output looks like::

        get(kind='paper', id='X~46..105/toc')   — drill into theory
        get(kind='paper', id='X', view='bibtex') — BibTeX citation

    The widest call sets the column for everyone so the em-dashes line
    up. Empty list returns ``[]``.
    """
    if not calls:
        return []
    width = max(len(call) for call, _ in calls)
    return [f"{indent}{call:<{width}}  — {desc}" for call, desc in calls]


def render_next_section(calls: list[tuple[str, str]]) -> str:
    """Render the full "Next:" section including the header.

    Returns an empty string when ``calls`` is empty (no header, no
    blank line) so callers can unconditionally append the result.
    """
    lines = format_next_block(calls)
    if not lines:
        return ""
    return "\nNext:\n" + "\n".join(lines)
