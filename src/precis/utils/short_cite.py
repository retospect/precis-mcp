"""Author-year short-cite for a ref — ``Wang'20`` (source-backfill slice 2).

The grounding line names papers at the point of use as a *label*, not by fat
handle: ``✓ Wang'20`` reads at a glance where ``✓ pa234`` does not. This is
deterministic (the *fact* layer). Author dicts store a full-name string under
``"name"`` (``ingest/*``); we take the first author's surname + the 2-digit
year, falling back to slug then title when the metadata is thin.
"""

from __future__ import annotations

from typing import Any

_TITLE_CAP = 24


def _surname(name: str) -> str:
    """The surname from a full-name string — the part before a comma
    (``"Wang, Jun"``) else the last whitespace token (``"Jun Wang"``)."""
    name = name.strip()
    if not name:
        return ""
    if "," in name:
        return name.split(",", 1)[0].strip()
    parts = name.split()
    return parts[-1] if parts else ""


def short_cite(ref: Any) -> str:
    """``Surname'YY`` for a ref, e.g. ``Wang'20``. Degrades gracefully: surname
    alone when the year is missing; the slug, then a capped title, then ``?``
    when there are no authors at all."""
    authors = getattr(ref, "authors", None) or []
    year = getattr(ref, "year", None)
    surname = ""
    if authors:
        first = authors[0] or {}
        surname = _surname(
            str(first.get("name", "") if isinstance(first, dict) else "")
        )
    if surname:
        return f"{surname}'{year % 100:02d}" if year else surname
    slug = getattr(ref, "slug", None)
    if slug:
        return str(slug)
    title = " ".join((getattr(ref, "title", None) or "").split())
    if not title:
        return "?"
    return f"{title[:_TITLE_CAP]}…" if len(title) > _TITLE_CAP + 1 else title
