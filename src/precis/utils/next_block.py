"""Render a TOON "Next:" hint block (D2).

Every handler view that wants to teach the agent the next useful call
emits a "Next:" trailer. V1 had this everywhere and it was the
brand-defining UX touch — agents learned drill-down, citations,
alternate views just by reading these blocks.

D2 (2026-06-04) flipped the format from column-aligned padded prose
to a TOON table with natural-language headers:

    Next:
    {if you want to	execute this call}
    see the TOC	get(kind='paper', id='X', view='toc')
    read first 5 chunks	get(kind='paper', id='X~0..5')
    get the BibTeX entry	get(kind='paper', id='X', view='bibtex')

The header is a sentence fragment, not a schema name — column 1 is
the agent's intent ("if you want to ..."), column 2 is the literal
call to execute. Pairs with the active-voice wording rules in the
storage-v2 design discussion.

Backwards compat: callsites pass the same ``[(call, description)]``
list they always did; the renderer transposes (description first,
call second) so the audit at callsites only changes the *strings*,
not the data shape.

Pure logic — no DB, no IO. Just string formatting.
"""

from __future__ import annotations

from precis.format import render_agent_table


def format_next_block(
    calls: list[tuple[str, str]],
    *,
    indent: str = "  ",
) -> list[str]:
    """Render ``(call, description)`` pairs as the TOON Next-block lines.

    Returns the rendered table as a list of lines (no leading
    ``Next:`` header — :func:`render_next_section` adds that).
    Empty list returns ``[]``.

    ``indent`` is kept in the signature for legacy callsites; D2's
    TOON renderer doesn't indent rows (the table format takes care
    of alignment via tabs), so the argument is now ignored.
    """
    del indent  # legacy; TOON rows don't need indentation
    if not calls:
        return []
    rows: list[dict[str, str]] = []
    for call, desc in calls:
        rows.append({"if you want to": desc, "execute this call": call})
    table = render_agent_table(rows, schema=["if you want to", "execute this call"])
    return table.splitlines()


def render_next_section(calls: list[tuple[str, str]]) -> str:
    """Render the full "Next:" section including the header.

    Returns an empty string when ``calls`` is empty (no header, no
    blank line) so callers can unconditionally append the result.
    """
    lines = format_next_block(calls)
    if not lines:
        return ""
    return "\nNext:\n" + "\n".join(lines)
