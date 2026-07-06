"""Form → canonical section / item classifier for the ``edgar`` kind.

Standard SEC filing sections carry stable, well-known structure. The
parser (``_edgar_parse.py``) labels each block with the section it
belongs to so search can scope to a section (e.g. "risk factors across
all 10-Ks") and the quarter-to-quarter diff layer can align the same
section across consecutive filings.

Section identity lives at the **block level** (spec § "Section tagging"):
each block is stamped with ``chunk_kind='edgar_section'`` +
``meta.section_path`` + ``meta.item_code``. This module owns the
classifier — a table of heading patterns per form family mapping to a
canonical :class:`Section`. Unrecognised headings fall back to
:data:`BODY` (``section:body``).

The classifier is pure and heading-driven: give it a heading string and
the filing's form, get back a :class:`Section` (or ``None`` when the
heading isn't a recognised section boundary — the parser then keeps
accumulating text under the current section).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class Section:
    """A canonical filing section.

    ``item_code`` is the lowercased SEC item code (``'1a'``, ``'2.02'``)
    or ``''`` for named / body sections. ``canonical_id`` is the stable
    slug used for tagging + diff alignment (``'item-1a'``,
    ``'item-2.02'``, ``'prospectus-summary'``, ``'body'``).
    ``section_path`` is the human-facing breadcrumb stored on
    ``blocks.meta``.
    """

    item_code: str
    canonical_id: str
    section_path: list[str] = field(default_factory=list)


#: Fallback for text under no recognised heading.
BODY = Section(item_code="", canonical_id="body", section_path=["Body"])


# ---------------------------------------------------------------------------
# Canonical section titles per family
# ---------------------------------------------------------------------------

# 10-K / 10-Q item titles keyed by lowercased item code. 10-Q reuses
# several codes across Part I / Part II; the title chosen is the
# high-value / most-searched meaning (e.g. 1a → Risk Factors).
_PERIODIC_TITLES: dict[str, str] = {
    "1": "Business",
    "1a": "Risk Factors",
    "1b": "Unresolved Staff Comments",
    "1c": "Cybersecurity",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity",
    "6": "Selected Financial Data",
    "7": "Management's Discussion and Analysis",
    "7a": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants",
    "9a": "Controls and Procedures",
    "9b": "Other Information",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits, Financial Statement Schedules",
}

# 8-K item-code titles keyed by ``N.NN`` code.
_EIGHTK_TITLES: dict[str, str] = {
    "1.01": "Entry into a Material Definitive Agreement",
    "1.02": "Termination of a Material Definitive Agreement",
    "1.03": "Bankruptcy or Receivership",
    "1.04": "Mine Safety — Reporting of Shutdowns",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition",
    "2.03": "Creation of a Direct Financial Obligation",
    "2.04": "Triggering Events That Accelerate a Financial Obligation",
    "2.05": "Costs Associated with Exit or Disposal Activities",
    "2.06": "Material Impairments",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities",
    "3.03": "Material Modification to Rights of Security Holders",
    "4.01": "Changes in Registrant's Certifying Accountant",
    "4.02": "Non-Reliance on Previously Issued Financial Statements",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure/Election of Directors or Officers",
    "5.03": "Amendments to Articles of Incorporation or Bylaws",
    "5.07": "Submission of Matters to a Vote of Security Holders",
    "7.01": "Regulation FD Disclosure",
    "8.01": "Other Events",
    "9.01": "Financial Statements and Exhibits",
}

# S-1 (and other prospectus-style registrations) named sections. Keyed
# by canonical slug; the value is (display title, matching regex).
_S1_SECTIONS: tuple[tuple[str, str, re.Pattern[str]], ...] = (
    ("prospectus-summary", "Prospectus Summary", re.compile(r"prospectus\s+summary")),
    ("risk-factors", "Risk Factors", re.compile(r"^risk\s+factors")),
    ("use-of-proceeds", "Use of Proceeds", re.compile(r"use\s+of\s+proceeds")),
    (
        "mdna",
        "Management's Discussion and Analysis",
        re.compile(r"management.?s\s+discussion\s+and\s+analysis"),
    ),
    ("business", "Business", re.compile(r"^business\b")),
    ("dilution", "Dilution", re.compile(r"^dilution\b")),
    ("management", "Management", re.compile(r"^management\b")),
)


# ---------------------------------------------------------------------------
# Matchers
# ---------------------------------------------------------------------------

# "Item 1A.", "ITEM 1A —", "Item 7A." etc. Captures number + optional
# single letter suffix.
_PERIODIC_ITEM_RE = re.compile(r"^\s*item\s+(\d{1,2})\s*([a-c])?\b", re.IGNORECASE)
# "Item 2.02", "ITEM 5.02" — 8-K dotted codes.
_EIGHTK_ITEM_RE = re.compile(r"^\s*item\s+(\d)\.(\d{2})\b", re.IGNORECASE)


def _norm(text: str) -> str:
    """Collapse whitespace for heading matching."""
    return re.sub(r"\s+", " ", text or "").strip()


def _base_form(form: str) -> str:
    """Normalise a form code to its family base.

    ``10-K/A`` → ``10-K``; ``10-KSB`` → ``10-K`` prefix family; case-
    folded and whitespace-stripped.
    """
    f = (form or "").strip().upper()
    # Amendment suffix.
    f = f.split("/")[0].strip()
    return f


def classify_heading(text: str, *, form: str) -> Section | None:
    """Classify a heading string into a :class:`Section`, or ``None``.

    Returns ``None`` when ``text`` is not a recognised section boundary
    for ``form`` — the parser keeps the current section in that case.
    """
    heading = _norm(text)
    if not heading:
        return None
    base = _base_form(form)

    if base.startswith("8-K") or base == "6-K":
        return _match_8k(heading)
    if base.startswith("10-K") or base.startswith("10-Q"):
        return _match_periodic(heading)
    if base.startswith("S-1") or base.startswith("S-3") or base.startswith("424"):
        return _match_s1(heading) or _match_periodic(heading)
    # Unknown form: still try the periodic matcher — many forms borrow
    # the "Item N" convention.
    return _match_periodic(heading)


def _match_8k(heading: str) -> Section | None:
    m = _EIGHTK_ITEM_RE.match(heading)
    if m is None:
        return None
    code = f"{m.group(1)}.{m.group(2)}"
    title = _EIGHTK_TITLES.get(code, "Other")
    return Section(
        item_code=code,
        canonical_id=f"item-{code}",
        section_path=[f"Item {code}", title],
    )


def _match_periodic(heading: str) -> Section | None:
    m = _PERIODIC_ITEM_RE.match(heading)
    if m is None:
        return None
    num = m.group(1)
    letter = (m.group(2) or "").lower()
    code = f"{num}{letter}"
    title = _PERIODIC_TITLES.get(code)
    display_code = f"{num}{letter.upper()}"
    path = [f"Item {display_code}"]
    if title:
        path.append(title)
    return Section(
        item_code=code,
        canonical_id=f"item-{code}",
        section_path=path,
    )


def _match_s1(heading: str) -> Section | None:
    low = heading.lower()
    for slug, title, pat in _S1_SECTIONS:
        if pat.search(low):
            return Section(
                item_code="",
                canonical_id=slug,
                section_path=[title],
            )
    return None


__all__ = ["BODY", "Section", "classify_heading"]
