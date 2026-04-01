"""Citation system — style engine, bookmark/link helpers, DOCX style creation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

# ---------------------------------------------------------------------------
# Markdown citation patterns
# ---------------------------------------------------------------------------

# Inline citation: [@key]
CITE_RE = re.compile(r"\[@([^\]\s]+)\]")

# Orphaned citation (lost bookmark, style survived): [@?:displayed text]
ORPHAN_CITE_RE = re.compile(r"\[@\?:([^\]]+)\]")

# Bibliography definition: [@key]: full reference text
BIB_DEF_RE = re.compile(r"^\[@([^\]\s]+)\]:\s*(.+)$")

ORPHAN_PREFIX = "?:"

# Malformed citations — missing @ or has ~chunk / #chunk suffix
# [slug~N] or [slug#N] — chunk reference used as citation
MALFORMED_CHUNK_RE = re.compile(r"\[([a-z][a-z0-9_]*(?:19|20)\d{2}[a-z]+)[~#](\d+)\]")
# [slug] without @ — looks like a paper slug (word+year+word) but missing @
MALFORMED_NO_AT_RE = re.compile(r"(?<!\[)\[([a-z][a-z0-9_]*(?:19|20)\d{2}[a-z]+)\](?![\]:(])")


def is_orphan_key(key: str) -> bool:
    """True if the key is an orphaned citation (lost bookmark, style survived)."""
    return key.startswith(ORPHAN_PREFIX)


def orphan_text(key: str) -> str:
    """Extract the displayed text from an orphan key."""
    return key[len(ORPHAN_PREFIX) :]


# ---------------------------------------------------------------------------
# Bookmark naming (for bibliography definitions only)
# ---------------------------------------------------------------------------

REF_BOOKMARK_RE = re.compile(r"^ref_(.+)$")


def ref_bookmark_name(key: str) -> str:
    """Generate bookmark name for a bibliography entry."""
    return f"ref_{key}"


def parse_ref_bookmark(name: str) -> str | None:
    """Parse ref_key → key or None."""
    m = REF_BOOKMARK_RE.match(name)
    if m:
        return m.group(1)
    return None


# ---------------------------------------------------------------------------
# Citation styles
# ---------------------------------------------------------------------------

STYLES = ("author-year", "numbered", "superscript")


@dataclass
class BibEntry:
    """A bibliography entry with parsed metadata for rendering."""

    key: str
    text: str  # full formatted reference
    author_short: str = ""  # "Smith" or "Smith & Jones" or "Smith et al."
    year: str = ""

    def render_author_year(self) -> str:
        if self.author_short and self.year:
            return f"({self.author_short} {self.year})"
        return f"[@{self.key}]"


@dataclass
class CitationIndex:
    """Tracks citation order and bibliography for a document."""

    style: str = "author-year"
    entries: dict[str, BibEntry] = field(default_factory=dict)
    cite_order: list[str] = field(default_factory=list)

    def register_bib(self, key: str, text: str) -> None:
        """Register a bibliography entry."""
        author_short, year = _parse_author_year(text)
        self.entries[key] = BibEntry(
            key=key, text=text, author_short=author_short, year=year
        )

    def cite(self, key: str) -> int:
        """Record a citation occurrence. Returns the 1-based citation number."""
        if key not in self.cite_order:
            self.cite_order.append(key)
        return self.cite_order.index(key) + 1

    def render_inline(self, key: str) -> tuple[str, bool]:
        """Render an inline citation. Returns (text, is_superscript)."""
        num = self.cite(key)
        if self.style == "numbered":
            return f"[{num}]", False
        elif self.style == "superscript":
            return str(num), True
        else:  # author-year
            entry = self.entries.get(key)
            if entry:
                return entry.render_author_year(), False
            return f"[@{key}]", False

    def to_dict(self) -> dict[str, Any]:
        return {
            "style": self.style,
            "entries": {
                k: {"text": e.text, "author_short": e.author_short, "year": e.year}
                for k, e in self.entries.items()
            },
            "cite_order": self.cite_order,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CitationIndex:
        idx = cls(style=data.get("style", "author-year"))
        idx.cite_order = data.get("cite_order", [])
        for k, v in data.get("entries", {}).items():
            idx.entries[k] = BibEntry(
                key=k,
                text=v.get("text", ""),
                author_short=v.get("author_short", ""),
                year=v.get("year", ""),
            )
        return idx


# ---------------------------------------------------------------------------
# Author/year extraction heuristic
# ---------------------------------------------------------------------------

# Matches "(2020)" or ", 2020" or "(2020)." at the end-ish of a reference
_YEAR_RE = re.compile(r"\b((?:19|20)\d{2})\b")
# First word(s) before a comma — typically the author surname
_AUTHOR_RE = re.compile(r"^([A-Z][a-z]+(?:\s*(?:&|and)\s*[A-Z][a-z]+)?)")


def _parse_author_year(text: str) -> tuple[str, str]:
    """Best-effort extraction of (author_short, year) from a reference string.

    Returns ("", "") if nothing found.
    """
    year = ""
    author = ""

    ym = _YEAR_RE.search(text)
    if ym:
        year = ym.group(1)

    am = _AUTHOR_RE.match(text.strip())
    if am:
        author = am.group(1)
        # Check for et al. — if there's more text after the matched authors
        # before the year, assume et al.
        rest = text.strip()[am.end() :].lstrip(",").strip()
        if rest and not rest[0].isdigit() and "&" not in am.group(1):
            # More authors follow — use et al.
            if "," in rest.split("(")[0] if "(" in rest else rest:
                author = f"{author} et al."

    return author, year


# ---------------------------------------------------------------------------
# DOCX style names
# ---------------------------------------------------------------------------

CITATION_REF_STYLE = "CitationRef"
BIB_ENTRY_STYLE = "BibEntry"
