"""Render the headline for search-result responses.

Every search handler emits a first line like ``# 10 paper match(es)
for 'photocatalysis'``. The MCP critic asked for a "you're seeing
N of K" pagination cue so an agent that asks for ``top_k=10`` and
gets exactly 10 hits knows whether there are more.

This helper centralises the format so the wording is consistent
across kinds. Two pieces vary per handler: the noun ("paper",
"todo", "memory match(es)") and whether the kind has a meaningful
total. Lexical and fused search expose totals; pure semantic does
not (every embedded block is a hit at some distance).

Usage::

    from precis.utils.search_header import format_search_headline

    line = format_search_headline(
        n_returned=len(hits),
        total=store.count_blocks_lexical(q=q, kind='paper', ...),
        noun='paper hit',
        query=q,
    )
    # → "# 10 of 1234 paper hits for 'photocatalysis'"

Pure logic — no DB, no IO. Just string formatting.
"""

from __future__ import annotations


def format_search_headline(
    *,
    n_returned: int,
    total: int | None,
    noun: str,
    query: str,
) -> str:
    """Format the leading ``# N [of K] {noun}(s) for 'query'`` line.

    ``total`` semantics:

    * ``None`` — kind has no meaningful total (semantic-only search).
      Render ``# N {noun}(s) for 'q'`` (no "of K").
    * ``total == n_returned`` — caller saw everything. Render the
      same shorter form so we don't say "10 of 10" when the agent
      already knows they got all of it.
    * ``total > n_returned`` — capped by ``top_k``. Render
      ``# N of K {noun}(s) for 'q'`` so the agent knows pagination
      is in play.

    ``noun`` is singular; the function pluralises by appending
    ``"s"`` when ``n_returned != 1``.  Pass a noun phrase like
    ``"paper match"`` and you'll get ``"paper match(es)"`` shape
    via the standard pluralisation rule.
    """
    if n_returned == 1:
        plural = ""
    elif noun.endswith(("s", "x", "z", "ch", "sh")):
        # Sibilant-ending words take "-es" in English. Without this,
        # ``"paper match"`` would pluralise to ``"matchs"``.
        plural = "es"
    else:
        plural = "s"
    if total is None or total <= n_returned:
        return f"# {n_returned} {noun}{plural} for {query!r}"
    return f"# {n_returned} of {total} {noun}{plural} for {query!r}"


__all__ = ["format_search_headline"]
