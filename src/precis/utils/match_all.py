"""The ``q='*'`` match-all fold shared by every ``search`` entry point.

Agents spell "everything matching the filters" as ``q='*'`` (gr538071).
Left alone it is a *literal* full-text query — it matches nothing (or,
worse, reaches a remote leg: patent OPS, JLCPCB live lookup). Every
kind's ``search`` folds the spelling to no-query at the top, before
view dispatch, so the kind's own q-less listing / browse / refusal
path answers it — one door, uniform across kinds. ``search_hits``
stays a ranked-only surface and is deliberately out of scope.
"""

from __future__ import annotations

__all__ = ["fold_match_all_q"]


def fold_match_all_q(q: str | None) -> str | None:
    """Fold the ``q='*'`` match-all spelling to ``None``.

    Returns ``q`` unchanged for anything else — ``None`` and blank
    strings included; the q-less checks downstream already handle
    those.
    """
    return None if isinstance(q, str) and q.strip() == "*" else q
