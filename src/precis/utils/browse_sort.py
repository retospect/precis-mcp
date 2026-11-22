"""Classify queryless numeric browse without hijacking ranked source searches."""

from typing import Any

from precis.utils.match_all import fold_match_all_q


def gripe_export(kind: str | None, args: dict[str, Any]) -> bool:
    """Recognize a queryless, unlinked gripe export before source routing."""
    return (
        str(kind or "").strip().lower() in {"gripe", "gr"}
        and args.get("view") in ("full", "body")
        and not str(fold_match_all_q(args.get("q")) or "").strip()
        and args.get("link") is None
    )


def numeric_browse(kind: str | None, args: dict[str, Any]) -> bool:
    """Priority browse is status/tag enumeration, not linked/special/dated search."""
    return gripe_export(kind, args) or (
        str(kind or "").strip().lower() in {"gripe", "todo", "quest", "gr", "td", "qu"}
        and not str(args.get("q") or "").strip()
        and all(
            args.get(key) is None
            for key in ("view", "since", "until", "link", "folder")
        )
    )
