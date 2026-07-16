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


#: WIPO ST.3 authority code → the adjective a citation uses ("European
#: Patent No. …", "Chinese Patent Application Publication No. …"). Covers the
#: common authorities; an unlisted code falls back to the bare DOCDB form.
_AUTHORITY_NAMES: dict[str, str] = {
    "ep": "European",
    "gb": "United Kingdom",
    "de": "German",
    "fr": "French",
    "jp": "Japanese",
    "cn": "Chinese",
    "kr": "Korean",
    "ca": "Canadian",
    "au": "Australian",
    "in": "Indian",
    "ru": "Russian",
    "ch": "Swiss",
    "nl": "Netherlands",
    "se": "Swedish",
    "es": "Spanish",
    "it": "Italian",
    "br": "Brazilian",
    "mx": "Mexican",
    "tw": "Taiwanese",
    "at": "Austrian",
    "be": "Belgian",
    "dk": "Danish",
    "fi": "Finnish",
    "no": "Norwegian",
    "il": "Israeli",
    "sg": "Singaporean",
    "za": "South African",
    "nz": "New Zealand",
}


def _us_citation(digits: str, kind_code: str) -> str:
    # A US publication number is YYYY + 7 digits (11 total); a grant number
    # is 7–8 digits. Length is a more reliable discriminator than the kind
    # code (old grants are kind "A", like an application publication).
    if len(digits) == 11:
        body = f"{digits[:4]}/{digits[4:]}"
        tail = f" {kind_code}" if kind_code else ""
        return f"U.S. Patent Application Publication No. {body}{tail}"
    return f"U.S. Patent No. {_group_thousands(digits)}"


def _wo_citation(digits: str, kind_code: str) -> str:
    # PCT: WO + YYYY + 6-digit serial (post-2002) → "WO 2023/123456".
    body = f"{digits[:4]}/{digits[4:]}" if len(digits) >= 10 else digits
    tail = f" {kind_code}" if kind_code else ""
    return f"PCT International Publication No. WO {body}{tail}"


def format_patent_citation(meta: dict[str, Any] | None, slug: str = "") -> str:
    """A patent ref's ``meta`` → its in-text citation string, per authority.

    * **US** — ``U.S. Patent No. 2,943,737`` (grant) /
      ``U.S. Patent Application Publication No. 2015/0101966 A1`` (published
      application).
    * **PCT/WO** — ``PCT International Publication No. WO 2023/123456 A1``.
    * **Other named authorities** (EP, GB, DE, JP, CN, …) —
      ``<Authority> Patent[ Application Publication] No. <CC> <number> <kind>``,
      e.g. ``European Patent No. EP 1234567 B1`` /
      ``Chinese Patent Application Publication No. CN 101787123 A``.
    * **Unknown authority / missing data** — the bare uppercased DOCDB id.

    The application-vs-grant split for a named authority keys on the kind
    code (``A*`` = published application, else a granted patent).
    """
    meta = meta or {}
    country = str(meta.get("country") or "").lower()
    kind_code = str(meta.get("kind_code") or "").upper()
    number = str(meta.get("doc_number") or "")
    digits = re.sub(r"\D", "", number)
    if country == "us":
        return _us_citation(digits, kind_code)
    if country == "wo":
        return _wo_citation(digits, kind_code)
    name = _AUTHORITY_NAMES.get(country)
    if name and digits:
        doctype = (
            "Patent Application Publication" if kind_code.startswith("A") else "Patent"
        )
        tail = f" {kind_code}" if kind_code else ""
        return f"{name} {doctype} No. {country.upper()} {digits}{tail}"
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
