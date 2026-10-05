"""Classify queryless numeric browse without hijacking ranked source searches."""

from typing import Any


def numeric_browse(kind: str | None, args: dict[str, Any]) -> bool:
    """Priority browse is status/tag enumeration, not linked/special/dated search."""
    return (
        str(kind or "").strip().lower() in {"gripe", "todo", "quest", "gr", "td", "qu"}
        and not str(args.get("q") or "").strip()
        and all(
            args.get(key) is None
            for key in ("view", "since", "until", "link", "folder")
        )
    )
