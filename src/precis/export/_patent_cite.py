"""In-text citation strings for ``doc_type=patent`` export (slice 6).

A patent **specification** cites prior art *in the running text* by number
("U.S. Patent No. 2,943,737") with **no bibliography** — unlike a paper,
which gets ``\\cite`` + a reference list. These formatters render the
in-text string from a patent (or paper) ref's ``meta``. Pure — no store,
no I/O. See ``docs/design/patent-authoring-loop.md`` (export reconciliation).
"""

from __future__ import annotations

import re
from typing import Any


def _group_thousands(digits: str) -> str:
    """``"2943737"`` → ``"2,943,737"`` (US grant-number convention)."""
    return f"{int(digits):,}" if digits.isdigit() else digits


def format_patent_citation(meta: dict[str, Any] | None, slug: str = "") -> str:
    """A patent ref's ``meta`` → its in-text citation string.

    US granted patents → ``U.S. Patent No. 2,943,737``; US published
    applications → ``U.S. Patent Application Publication No. 2015/0101966
    A1``. Other authorities fall back to the uppercased DOCDB number.
    """
    meta = meta or {}
    country = str(meta.get("country") or "").lower()
    kind_code = str(meta.get("kind_code") or "").upper()
    number = str(meta.get("doc_number") or "")
    digits = re.sub(r"\D", "", number)
    if country == "us":
        # A US publication number is YYYY + 7 digits (11 total); a grant
        # number is 7–8 digits. Length is a more reliable discriminator
        # than the kind code (old grants are kind "A", like an application).
        if len(digits) == 11:
            body = f"{digits[:4]}/{digits[4:]}"
            tail = f" {kind_code}" if kind_code else ""
            return f"U.S. Patent Application Publication No. {body}{tail}"
        return f"U.S. Patent No. {_group_thousands(digits)}"
    label = (slug or f"{country}{number}{kind_code}").upper()
    return f"Patent No. {label}"


def _first_author_surname(authors: Any) -> str:
    """Best-effort surname of the first author (a list of names or
    ``{name}`` dicts). ``""`` when nothing usable."""
    if not isinstance(authors, list) or not authors:
        return ""
    a0 = authors[0]
    name = str(a0.get("name") if isinstance(a0, dict) else a0).strip()
    if not name:
        return ""
    # "Surname, Given" → Surname; "Given Surname" → last token.
    return name.split(",")[0].strip() if "," in name else name.split()[-1]


def paper_inline_citation(ref: Any) -> str:
    """A paper ref → a light in-text ``(Surname et al., YYYY)`` for
    patent-mode export (no bibliography). Best-effort; falls back to the
    quoted title, then the slug."""
    meta = getattr(ref, "meta", None) or {}
    year = ""
    pd = meta.get("publication_date") or meta.get("year")
    if pd:
        m = re.search(r"\d{4}", str(pd))
        if m:
            year = m.group(0)
    surname = _first_author_surname(meta.get("authors") or meta.get("applicants"))
    if surname and year:
        return f"({surname} et al., {year})"
    if surname:
        return f"({surname} et al.)"
    title = getattr(ref, "title", None) or meta.get("title")
    if title:
        return f"“{title!s}”"
    return str(getattr(ref, "slug", None) or "")
