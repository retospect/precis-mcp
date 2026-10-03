"""A conservative "is this paper probably a review?" heuristic.

There is NO stored review flag: ``refs.meta.entry_type`` is the Crossref
type (``journal-article``, ``posted-content`` …) and does not separate a
review article from a research article. :func:`is_review_like` guesses from
the two strings every paper ref carries — its title and ``meta.journal`` —
so :func:`precis.taproot.cite.hub_cite_keys` can prefer a primary source over
a review when it picks which paper a claim hub cites.

Conservative on purpose: a false *positive* demotes a primary paper to the
reviews-only tier (it is still printed when nothing better exists), a false
negative lets a review stand in as a primary. Both are recoverable (a pin
overrides), so the patterns below favour precision over recall — ambiguous
words ("perspective", "state-of-the-art", singular "Review" in a journal
name) are only matched in the shapes that are reliably review-ish.
"""

from __future__ import annotations

import re

#: Bump on ANY change to the patterns below. It rides in every citation
#: fallback decision record (:class:`precis.taproot.cite.FallbackDecision`),
#: so a reading taken under an older pattern set stays identifiable after
#: the patterns change.
PATTERNS_VERSION = "2026-10-02.1"

# Journal names that contain "Review" but publish research, not reviews —
# checked first and win. "Physical Review Letters/B/Applied/X", the APS
# "Phys. Rev. …" abbreviations, "Review of Scientific Instruments", and
# "Progress in Photovoltaics" (primary research despite the "Progress in"
# venue rule).
_JOURNAL_PRIMARY_RE = re.compile(
    r"\bPhysical Reviews?\b|\bPhys\.? ?Rev\b|\bReview of Scientific Instruments\b"
    r"|\bRev\.? Sci\.? Instrum|\bProg(?:ress|\.)? (?:in )?Photovolt",
    re.IGNORECASE,
)

# Review venues. Plural "Reviews" is nearly always a review venue (Chemical
# Reviews, Nature Reviews X, Chem. Soc. Rev., Reviews in X, Applied Physics
# Reviews); singular "Review" only in the "Annual Review of X" form.
_JOURNAL_REVIEW_RE = re.compile(
    r"\bReviews\b"
    r"|\bAnnual Review\b|\bAnnu\.? Rev\b"
    r"|\bChem(?:ical)?\.? (?:Soc(?:iety)?\.? )?Rev\b"
    r"|\bNat(?:ure)?\.? Rev\b"
    r"|\bRev\.? Mod\.? Phys\b"
    r"|\bProgress in\b|\bReports? on Progress\b|\bTrends in\b"
    r"|\bMaterials Science and Engineering: R\b"
    r"|\bPhysics Reports\b|\bPhys\.? Rep\b",
    re.IGNORECASE,
)

# Title phrasing, matched at word boundaries. "peer-review(ed)", "reviewer"
# and "reviewing" are NOT review phrasing — "reviewer"/"reviewed" fail the
# trailing \b, "peer-review" the lookbehind. "perspective" only as a
# title-leading word, a "…: a perspective" tail, or "perspective(s) on";
# "from the perspective of the cell" is not a review. "state-of-the-art"
# (hyphenated) is a ubiquitous adjective in research abstracts and is not
# matched; the spaced noun phrase "the state of the art" is.
_TITLE_REVIEW_RE = re.compile(
    r"(?<!peer-)\breviews?\b"
    r"|\boverview\b"
    r"|\bprogress in\b"
    r"|\badvance(?:ment)?s in\b"
    r"|\brecent (?:advances|developments)\b"
    r"|\bfuture (?:perspectives?|directions|prospects|outlook)\b"
    r"|\bchallenges,? (?:and )?(?:opportunities|prospects|perspectives)\b"
    r"|\bopportunities,? (?:and )?challenges\b"
    r"|\ba survey of\b"
    r"|\bstate of the art\b"
    r"|\broadmap\b"
    r"|\btutorial\b"
    r"|^perspectives?\b"
    r"|[:\-–—(]\s*(?:an? )?(?:[\w'’-]+ ){0,2}perspectives?\s*\)?\s*$"
    r"|\bperspectives? on\b",
    re.IGNORECASE,
)


def review_reason(title: str | None, journal: str | None) -> str | None:
    """Why :func:`is_review_like` says yes — ``"journal ~ 'Chemical Reviews'"``
    / ``"title ~ 'a review'"`` (the matched text) — or ``None`` for a
    research-like paper. The venue rule is checked before the title rule."""
    if journal and not _JOURNAL_PRIMARY_RE.search(journal):
        m = _JOURNAL_REVIEW_RE.search(journal)
        if m:
            return f"journal ~ {m.group(0)!r}"
    if title:
        m = _TITLE_REVIEW_RE.search(title)
        if m:
            return f"title ~ {m.group(0).strip()!r}"
    return None


def is_review_like(title: str | None, journal: str | None) -> bool:
    """True iff the paper looks like a review/perspective rather than a
    research article — by venue (``journal``) or by title phrasing.

    A heuristic, not a classification: see the module docstring for the
    deliberate conservatism. Pure; ``None``/empty inputs count as no signal.
    :func:`review_reason` names the pattern that fired.
    """
    return review_reason(title, journal) is not None
