"""Render the headline for search-result responses.

Every search handler emits a first line like ``# 10 paper match(es)
for 'photocatalysis'``. The MCP critic asked for a "you're seeing
N of K" pagination cue so an agent that asks for ``page_size=10`` and
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
    n_strong: int | None = None,
) -> str:
    """Format the leading ``# N [of K] {noun}(s) for 'query'`` line.

    ``total`` semantics:

    * ``None`` — kind has no meaningful total (semantic-only search).
      Render ``# N {noun}(s) for 'q'`` (no "of K").
    * ``total == n_returned`` — caller saw everything. Render the
      same shorter form so we don't say "10 of 10" when the agent
      already knows they got all of it.
    * ``total > n_returned`` — capped by ``page_size``. Render
      ``# N of K {noun}(s) for 'q'`` so the agent knows pagination
      is in play.

    ``n_strong`` semantics:

    * ``None`` — no confidence information to surface.
    * ``n_strong < n_returned`` — the caller detected a score cliff
      (e.g. a unique literal query where the top hit dominates).
      Append ``(N strong)`` so the agent knows most of the tail
      matches are low-confidence and pagination isn't worth it.
      MCP critic MINOR-$ 2026-05-02: agents checking their own
      write with a unique marker used to pay tokens paging
      through semantically-related hits.
    * ``n_strong >= n_returned`` — every returned hit is
      high-confidence; no annotation.

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
        base = f"# {n_returned} {noun}{plural} for {query!r}"
    else:
        base = f"# {n_returned} of {total} {noun}{plural} for {query!r}"
    if n_strong is not None and 0 < n_strong < n_returned:
        base += f"  ({n_strong} strong)"
    return base


def detect_score_cliff(scores: list[float], *, ratio: float = 0.5) -> int | None:
    """Return the count of ``scores`` ≥ ``ratio × scores[0]``, else ``None``.

    A "cliff" is a score gap where the top hit clearly dominates
    — typical of exact-literal queries where one block contains
    the token and every other match is a distant semantic
    neighbour. The function returns ``None`` when there's no cliff
    to report (no scores, single hit, or every score is above the
    threshold) so callers can pass the result straight to
    ``format_search_headline(n_strong=...)`` without a branch.

    ``ratio=0.5`` is deliberately generous — we want to surface a
    confidence cue even when the top hit is only 2× the tail,
    which is common for good literal matches inside fused lexical +
    semantic rankings.
    """
    if not scores or len(scores) <= 1:
        return None
    top = scores[0]
    if top <= 0:
        return None
    threshold = top * ratio
    n_strong = sum(1 for s in scores if s >= threshold)
    # Only report when there's a meaningful cliff — every score
    # above threshold means "no cliff, agent should treat all hits
    # as equally strong."
    if n_strong >= len(scores):
        return None
    return n_strong


__all__ = ["detect_score_cliff", "format_search_headline"]
