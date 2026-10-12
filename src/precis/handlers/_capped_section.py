"""Shared selection and overflow for bounded child collections on bare reads.

Inline links and gripe comments use this one capped-section renderer:
selection and an explicit full-view pointer bound child counts on routine
reads, and individual text is not clipped. Gripe keeps newest comments
chronologically; ``log`` remains the audit trail, while ``comments`` is
the opt-in complete conversation (policy detail:
:mod:`precis.handlers._comment_timeline`)."""

from collections.abc import Callable, Sequence
from typing import Any

DEFAULT_CHILD_ROW_CAP = 20


def render_capped_section[T](
    items: Sequence[T],
    *,
    limit: int | None,
    key: Callable[[T], Any],
    render: Callable[[list[T], int], str],
    overflow: Callable[[int], str],
) -> str:
    """Select priority rows, render them and point explicitly at the full view."""
    ordered = sorted(items, key=key)
    selected = ordered if limit is None else ordered[:limit]
    body = render(selected, len(ordered))
    withheld = len(ordered) - len(selected)
    return body + (overflow(withheld) if withheld else "")
