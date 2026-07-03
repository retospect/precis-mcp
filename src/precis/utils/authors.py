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

Authored artifacts (``kind='draft'``) additionally carry a per-author
**affiliation** — an institution string plus an optional ROR id
(https://ror.org, the canonical de-duplicated organisation identifier).
That richer shape is ``{"name", "affiliation", "ror"}``;
:func:`to_author_dicts` is the write-path normaliser that *preserves*
those two keys (``to_name_dicts`` intentionally drops them), and
:func:`build_byline` turns the list into a rendered byline —
authors with superscript marks + the deduped affiliation list — shared
verbatim by the LaTeX / docx exporters and the web reader.
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "author_display",
    "author_names",
    "build_byline",
    "to_author_dicts",
    "to_name_dicts",
]


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
    Crossref ingest convention. Affiliation / ROR (if present) are
    *dropped* — use :func:`to_author_dicts` on the draft-authoring path
    where those must survive.
    """
    return [{"name": n} for n in author_names(raw, order="sortable")]


def to_author_dicts(raw: Any) -> list[dict[str, str]]:
    """Canonical draft-author storage shape, preserving affiliation + ROR.

    Like :func:`to_name_dicts` (sortable ``{"name"}``) but carries the
    optional ``affiliation`` (institution string) and ``ror`` (an
    https://ror.org id) through to storage. Accepts the same tolerant
    inputs as the readers — a list of dicts (``{"name"}`` /
    ``{"family", "given"}``, either with ``affiliation`` / ``ror``) or
    bare strings, or a semicolon-packed string (names only). Entries
    with no resolvable name are dropped. Pure — never raises.
    """
    if isinstance(raw, str):
        return [{"name": n} for n in author_names(raw, order="sortable")]
    if not isinstance(raw, list):
        return []
    out: list[dict[str, str]] = []
    for a in raw:
        name = author_display(a, order="sortable")
        if not name:
            continue
        entry: dict[str, str] = {"name": name}
        if isinstance(a, dict):
            aff = (a.get("affiliation") or "").strip()
            ror = (a.get("ror") or "").strip()
            if aff:
                entry["affiliation"] = aff
            if ror:
                entry["ror"] = ror
        out.append(entry)
    return out


def build_byline(raw: Any) -> dict[str, Any]:
    """Structured byline for rendering — the shared "notation of
    associations" consumed by both exporters and the web reader.

    Returns ``{"authors": [...], "affiliations": [...], "multi": bool}``:

    * ``authors`` — ordered ``{"name": "Given Family", "marks": [int...],
      "sup": "1,2"}``. ``marks`` indexes into ``affiliations``; ``sup`` is
      the pre-rendered comma-joined superscript, blank when there is only
      one distinct affiliation (a single shared institution reads better
      listed once, unnumbered).
    * ``affiliations`` — ordered ``{"index": int, "org": str, "ror": str}``,
      **deduped by ROR id** (falling back to the lower-cased org string),
      numbered 1.. in order of first appearance.
    * ``multi`` — whether more than one distinct affiliation exists (i.e.
      whether the superscript marks are meaningful).

    When no author carries an affiliation, ``affiliations`` is empty and
    every ``sup`` is blank — the byline degrades to a plain name list.
    Pure — never raises.
    """
    if isinstance(raw, list):
        items: list[Any] = raw
    elif isinstance(raw, str) and raw.strip():
        items = [a.strip() for a in raw.split(";") if a.strip()]
    else:
        items = []

    affiliations: list[dict[str, Any]] = []
    by_key: dict[str, int] = {}
    authors: list[dict[str, Any]] = []
    for a in items:
        name = author_display(a, order="natural")
        if not name:
            continue
        aff = ror = ""
        if isinstance(a, dict):
            aff = (a.get("affiliation") or "").strip()
            ror = (a.get("ror") or "").strip()
        marks: list[int] = []
        if aff or ror:
            key = ror.lower() if ror else aff.lower()
            idx = by_key.get(key)
            if idx is None:
                idx = len(affiliations) + 1
                by_key[key] = idx
                affiliations.append({"index": idx, "org": aff, "ror": ror})
            marks = [idx]
        authors.append({"name": name, "marks": marks, "sup": ""})

    multi = len(affiliations) > 1
    if multi:
        for author in authors:
            author["sup"] = ",".join(str(m) for m in author["marks"])
    return {"authors": authors, "affiliations": affiliations, "multi": multi}
