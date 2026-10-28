"""Context and transport budgets for complete agent-facing results.

Oversized reads refuse with a targeted-read next step. They never return a
partial body or a continuation cursor: search, tree traversal, and targeted
views are the context-management surface.
"""

from __future__ import annotations

import os

from precis.errors import BadInput

DEFAULT_CONTEXT_BODY_BYTES = 4096
DEFAULT_MAX_BODY_BYTES = 24576


def _env_cap(name: str, default: int, *, zero_disables: bool = False) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    if value <= 0:
        return 0 if zero_disables else default
    return value


def check_result_budget(
    body: str,
    *,
    verb: str,
    kind: str | None,
    context_limited: bool,
    targeted_hint: str | None,
) -> None:
    size = len(body.encode("utf-8"))
    context_cap = (
        _env_cap(
            "PRECIS_TOOL_RESULT_CONTEXT_BYTES",
            DEFAULT_CONTEXT_BODY_BYTES,
            zero_disables=True,
        )
        if context_limited
        else 0
    )
    frame_cap = _env_cap("PRECIS_MAX_BODY_BYTES", DEFAULT_MAX_BODY_BYTES)
    if context_cap and size > context_cap:
        cap_name = f"PRECIS_TOOL_RESULT_CONTEXT_BYTES ({context_cap} bytes)"
    elif size > frame_cap:
        cap_name = f"PRECIS_MAX_BODY_BYTES ({frame_cap} bytes)"
    else:
        return
    next_call = targeted_hint or (
        f"search(kind={kind!r}, q='<more specific terms>', page_size=5)"
        if kind
        else "search(q='<more specific terms>', page_size=5)"
    )
    raise BadInput(
        f"{verb}: result is {size} bytes and exceeds {cap_name}; "
        "no partial result was returned",
        next=next_call,
    )


__all__ = [
    "DEFAULT_CONTEXT_BODY_BYTES",
    "DEFAULT_MAX_BODY_BYTES",
    "check_result_budget",
]
