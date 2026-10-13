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

It also owns the *proceedings-venue* test, :func:`proceedings_venue`, which
two callers share: :func:`review_reason` (a proceedings venue is not a review
journal, so "Progress In Electromagnetics Research Symposium" must not fire
the "Progress in" venue rule) and the ``report`` source-genre gate
(``nanopub/gates.py::check_report_sources``, which accepts a ``journal-article``
filed in a conference digest). One definition, so the two cannot disagree
about what a conference venue is.
"""

from __future__ import annotations

import re

#: Bump on ANY change to the patterns below. It rides in every citation
#: fallback decision record (:class:`precis.taproot.cite.FallbackDecision`),
#: so a reading taken under an older pattern set stays identifiable after
#: the patterns change.
PATTERNS_VERSION = "2026-10-03.1"

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


# Venue words that mark a proceedings-style venue, canonical case by
# lowercase key. Word-bounded and case-insensitive: "EPJ Web of Conferences"
# (plural) does not fire.
_VENUE_WORDS: dict[str, str] = {
    "symposium": "Symposium",
    "digest": "Digest",
    "proceedings": "Proceedings",
    "conference": "Conference",
    "workshop": "Workshop",
    "meeting abstracts": "Meeting Abstracts",
}
_VENUE_RE = re.compile(
    r"\b(?:Symposium|Digest|Proceedings|Conference|Workshop|Meeting\s+Abstracts)\b",
    re.IGNORECASE,
)

# Journals whose names carry a venue word but which are journals (a prod
# corpus read, 2026-10-03, of journal-article titles matching the venue
# words). Checked FIRST and win; anchored on the start of the name. The
# Combustion Institute, Privacy Enhancing Technologies and Civil Engineering
# in the Ocean entries are borderline (proceedings-style series that Crossref
# files as journals); they are listed because refusing is the safe direction,
# a wrongly refused report is re-grounded or re-typed, a wrongly accepted
# one signs a finding as a report.
_NOT_VENUE_RE = re.compile(
    r"^Proceedings of the National Academy of Sciences"
    r"|^Proceedings of the Royal Society"
    r"|^Proceedings of the IEEE$"
    r"|^Proceedings of the (?:IRE|Institute of Radio Engineers)$"
    r"|^Proceedings of the ACM on "
    r"|^Proceedings of the Institution of "
    r"|^Proceedings of the (?:Indian National Science Academy|Japan Academy"
    r"|Bulgarian Academy of Sciences|Physical Society|Nutrition Society"
    r"|American Mathematical Society|Combustion Institute|Design Society"
    r"|Association for Information Science)"
    r"|^Proceedings of (?:Universities|Engineering and Technology Innovation"
    r"|the Southwest State University|Civil Engineering in the Ocean)"
    r"|^Proceedings on Privacy Enhancing Technologies"
    r"|^Mathematical Proceedings of"
    r"|^Digest Journal of"
    r"|^Current Digest of",
    re.IGNORECASE,
)


def venue_vetoed(journal: str | None) -> bool:
    """True iff ``journal`` is on the not-venue list (:data:`_NOT_VENUE_RE`):
    a journal whose name carries a venue word but is not a proceedings
    venue."""
    return bool(journal and _NOT_VENUE_RE.search(journal.strip()))


def proceedings_venue(journal: str | None) -> str | None:
    """The proceedings-venue word ``journal`` names — ``"Symposium"``,
    ``"Digest"``, ``"Proceedings"``, ``"Conference"``, ``"Workshop"`` or
    ``"Meeting Abstracts"``, canonical case, leftmost match — or ``None``.

    The not-venue list is checked first (``Proceedings of the National
    Academy of Sciences`` is a journal), and the match is word-bounded
    (``EPJ Web of Conferences`` does not fire). A venue name says what the
    publication IS whatever Crossref calls it: the SID Symposium Digest is
    filed as ``journal-article``."""
    if not journal or venue_vetoed(journal):
        return None
    m = _VENUE_RE.search(journal)
    if not m:
        return None
    return _VENUE_WORDS[re.sub(r"\s+", " ", m.group(0).lower())]


def review_reason(title: str | None, journal: str | None) -> str | None:
    """Why :func:`is_review_like` says yes — ``"journal ~ 'Chemical Reviews'"``
    / ``"title ~ 'a review'"`` (the matched text) — or ``None`` for a
    research-like paper. The venue rule is checked before the title rule,
    and is skipped for a proceedings venue (:func:`proceedings_venue`): a
    conference series is not a review journal, whatever "Progress in" its
    name carries. The title rule still applies."""
    if (
        journal
        and not _JOURNAL_PRIMARY_RE.search(journal)
        and proceedings_venue(journal) is None
    ):
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
