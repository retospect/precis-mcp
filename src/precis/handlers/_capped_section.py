"""Shared selection and overflow for bounded child collections on bare reads.

Inline links use this renderer: selection and an explicit full-view
pointer bound child counts on routine reads, and individual text is not
clipped. Comment timelines share the *convention* — bound the children,
point at the complete view — but not this function: their two-tier
policy (newest-in-full, older as one-liners, hidden comments) is richer
than ``render_capped_section``'s single ``limit`` and lives in
:mod:`precis.handlers._comment_timeline`. ``log`` remains the audit
trail; ``comments`` is the opt-in complete conversation."""

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
