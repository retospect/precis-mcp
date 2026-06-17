"""Canonical handling of ``refs.authors`` entries.

The ``refs.authors`` JSONB column holds author dicts in more than one
shape, because different writers built it differently over time:

* ingest (Crossref / Semantic Scholar / provenance) writes
  ``{"name": "Family, Given"}`` or ``{"name": "Given Family"}`` — this
  is the shape actually present in storage today (every ingest path
  funnels through ``{"name": ...}``).
* the web metadata editor parses operator input into
  ``{"family": ..., "given": ...}``.
* a few legacy call sites pass bare strings.

Readers must tolerate all three; indexing ``a["family"]`` directly is
the bug this module exists to prevent — it silently blanks the
``{"name"}`` shape (and a ``{"name"}``-only reader blanks the
``{"family", "given"}`` shape). Funnel every *read* through
:func:`author_names` / :func:`author_display`, and every *write*
through :func:`to_name_dicts` so new rows converge on the single
``{"name"}`` shape.
"""

from __future__ import annotations

from typing import Any

__all__ = ["author_display", "author_names", "to_name_dicts"]


def author_display(entry: Any, *, order: str = "natural") -> str:
    """One author's display name, tolerant of every stored shape.

    ``order='natural'`` → ``"Given Family"`` (inline reading order);
    ``order='sortable'`` → ``"Family, Given"`` (citation / bib order).
    The order only affects ``{"family", "given"}`` entries — a bare
    ``{"name"}`` or string is returned as-is (we can't reliably split
    it). Returns ``""`` for empty / garbage so callers can filter.
    Pure — never raises.
    """
    if isinstance(entry, dict):
        family = (entry.get("family") or "").strip()
        given = (entry.get("given") or "").strip()
        if family and given:
            return f"{family}, {given}" if order == "sortable" else f"{given} {family}"
        if family:
            return family
        if given:
            return given
        return (entry.get("name") or "").strip()
    return str(entry or "").strip()


def author_names(raw: Any, *, order: str = "natural") -> list[str]:
    """Display names from a ``refs.authors`` value (or a packed byline).

    Accepts a list of dicts / strings (mixed shapes fine), a
    semicolon-packed string, or ``None`` / garbage. Empty entries are
    dropped. Pure — never raises.
    """
    if isinstance(raw, list):
        return [n for n in (author_display(a, order=order) for a in raw) if n]
    if isinstance(raw, str) and raw.strip():
        return [a.strip() for a in raw.split(";") if a.strip()]
    return []


def to_name_dicts(raw: Any) -> list[dict[str, str]]:
    """Canonical storage shape — ``[{"name": "Family, Given"}, ...]``.

    Use on every write path so the column converges on one shape. Names
    are rendered sortable (``Family, Given``) to match the dominant
    Crossref ingest convention.
    """
    return [{"name": n} for n in author_names(raw, order="sortable")]
