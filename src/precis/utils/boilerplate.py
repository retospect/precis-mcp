"""Heuristic chunk classifier — head / body / references / contact / ack.

Real scientific papers are not uniform sequences of body text. They
open with a title block (journal header, authors, ORCIDs, affiliations,
abstract) and close with a back-matter trail (acknowledgements,
references, contact info). When the TOC segmenter treats those
chunks as "body content", two failures cascade:

1. The outlier chunks dominate the embedding-distance signal — most
   "topic shifts" the algorithm finds are at boilerplate boundaries,
   not at within-body content shifts.
2. RAKE / KeyBERT produces nonsense labels for them — references
   surface as "diffraction data newtown square" or "jcpds pdf-2
   database" instead of "References".

This module classifies each chunk before the segmenter runs, labels
the boilerplate explicitly, and hands only the body chunks to the
segmenter.

Pure heuristics; no model, no network. Cheap to call (one pass over
chunks_text). Deterministic.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum


class ChunkClass(StrEnum):
    """What kind of chunk this is."""

    HEAD = "head"  # title + abstract + authors at the top
    BODY = "body"  # actual content
    REFERENCES = "references"  # citation list
    ACKNOWLEDGEMENTS = "acknowledgements"
    CONTACT = "contact"  # correspondence / corresponding author info


@dataclass(frozen=True)
class ClassifiedChunks:
    """Per-chunk classification result.

    ``classes[i]`` is the label assigned to chunks_text[i] / positions[i].
    ``body_indices`` is the list of indices (into the original list)
    whose label is BODY — what the segmenter should operate on.
    """

    classes: tuple[ChunkClass, ...]
    body_indices: tuple[int, ...]


# ── detection patterns ──────────────────────────────────────────────


# References: count citation-shaped lines per chunk. See
# :func:`_is_references_chunk` for the two-tier density check (loose
# patterns need >=30% AND an absolute >=3-match floor; the strict
# marker-citation pattern below needs only >=50% with no floor, since
# it's precise enough to trust on a short chunk).
#
# Marker line shape — an optional leading markdown bullet, a bracketed
# numeric marker, then the entry text (``"- [15] Smith, J. A.; Doe, K.
# B. ... 2015, 25, 115."``). This is the shape Marker's PDF-to-markdown
# block splitter actually emits, one entry per chunk. It's *shared*
# (the marker-line structure itself) with :mod:`precis.workers.bib_parse`
# (its ``_MARKER_LINE_RE``) so the two bibliography detectors can't
# drift apart on what a "bibliography line" looks like structurally.
MARKER_LINE_RE = re.compile(r"^\s*-?\s*\[(?P<marker>\d{1,4})\]\s+(?P<rest>\S.*)$")

# A marker-line whose content *also* looks author/citation-shaped
# (surname, comma, initial — the same author check as the other
# patterns below). Deliberately stricter than the bare ``MARKER_LINE_RE``
# structural shape: this classifier runs on chunks as small as one
# line, so unlike bib_parse's chunk-level majority-vote ratio, there's
# no "outvoted by other lines" protection here — an ordinary bracketed
# body bullet with no citation content (``"- [1] See supporting
# information for experimental details."``) would otherwise flip a
# single-line chunk on marker shape alone. Requiring the author-comma-
# initial content too keeps that from over-matching.
_MARKER_CITATION_LINE_RE = re.compile(
    r"^\s*-?\s*\[\d{1,4}\]\s+[A-Z][a-zA-Z'\-]+,\s*[A-Z]"
)

# Loose citation-shaped patterns. These are individually weak — e.g.
# ``^\s*\[\d+\]\s+[A-Z][a-zA-Z'\-]+`` matches "[1] Introduction" and
# "[12] Figure showing the XRD pattern..." just as readily as a real
# citation — so they are ONLY ever counted under the higher ``matches
# >= 3`` density floor in :func:`_is_references_chunk`, which makes a
# 1-2 line chunk unreachable by design (see the gr196447 review note
# there). ``_MARKER_CITATION_LINE_RE`` is intentionally NOT one of
# these — it's counted separately, under its own lower/looser
# threshold, precisely because it's strict enough (marker shape AND
# author-comma-initial content) to be trusted on a short chunk.
_CITATION_PATTERNS = (
    re.compile(r"^\s*\(?\d+\)?\s+[A-Z][a-zA-Z'\-]+,\s*[A-Z]"),  # "(1) Smith, J."
    re.compile(r"^\s*\[\d+\]\s+[A-Z][a-zA-Z'\-]+"),  # "[1] Smith"
    re.compile(r"^\s*\d+\.\s+[A-Z][a-zA-Z'\-]+,\s*[A-Z]"),  # "1. Smith, J."
    # Author-initial-year-journal patterns (DOI-style citation lines).
    re.compile(r"[A-Z][a-z]+,\s+[A-Z]\.\s*[A-Z]?\.?\s*[A-Z]?\.?[,;]"),
)

_ORCID_RE = re.compile(r"\b\d{4}-\d{4}-\d{4}-\d{3}[\dX]\b")
_EMAIL_RE = re.compile(r"\b[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
_DOI_RE = re.compile(r"\b10\.\d{4,9}/\S+\b")

# Headings that name what a chunk is for (case-insensitive match in the
# first ~120 chars of the chunk, so an inline H2 / bolded section is
# enough).
_HEAD_HEADING_RE = re.compile(
    r"\b(abstract|graphical\s+abstract|keywords|highlights)\b", re.IGNORECASE
)
_REFERENCES_HEADING_RE = re.compile(
    r"^\s*(?:#+\s*)?(references|bibliography|works\s+cited|literature\s+cited)\b",
    re.IGNORECASE | re.MULTILINE,
)
_ACK_HEADING_RE = re.compile(
    r"^\s*(?:#+\s*)?(acknowledg[e]?ments?|funding|author\s+contributions?|"
    r"competing\s+interests?|conflict\s+of\s+interest|disclosure)\b",
    re.IGNORECASE | re.MULTILINE,
)
_CONTACT_HEADING_RE = re.compile(
    r"\b(corresponding\s+author|correspondence\s+(?:to|address)|notes)\b",
    re.IGNORECASE,
)


def classify_chunks(
    chunks_text: Sequence[str],
    *,
    head_cap: int = 5,
    tail_cap: int = 500,
) -> ClassifiedChunks:
    """Label each chunk by structural role.

    Args:
        chunks_text: ordered chunk bodies.
        head_cap: maximum number of leading chunks eligible for HEAD
            classification (most papers' front-matter is 1–5 chunks;
            preventing it from running away protects long abstracts).
        tail_cap: maximum number of trailing chunks eligible for
            REFERENCES / ACK / CONTACT classification. Generously
            large by design — real bibliographies routinely run
            50-150+ entries, and on Marker's one-entry-per-chunk
            splitter that's 50-150+ trailing chunks. Correctness
            doesn't come from this cap; it comes from the tail walk
            stopping at the first chunk (from the end) that doesn't
            look like references/ack/contact — this is just a
            defensive upper bound on how far that walk is allowed to
            search before giving up.

    Returns:
        :class:`ClassifiedChunks` with per-chunk labels + the BODY
        indices for the segmenter.

    Empty input → empty output. A single-chunk paper is classified
    as BODY regardless of content (too short for the front-matter
    heuristic to fire).

    Known limitation (not fixed here — the tail walk starts at the
    last chunk and stops at the first non-matching one): a document
    laid out as body → references → appendix/SI puts non-citation-
    shaped appendix content at the tail, which blocks the walk before
    it ever reaches the real references section above it. This
    heuristic assumes references are the document's tail.
    """
    n = len(chunks_text)
    if n == 0:
        return ClassifiedChunks(classes=(), body_indices=())
    if n <= 2:
        # Tiny papers don't have meaningful boilerplate to strip.
        return ClassifiedChunks(
            classes=tuple([ChunkClass.BODY] * n),
            body_indices=tuple(range(n)),
        )

    classes: list[ChunkClass] = [ChunkClass.BODY] * n

    # Head pass: walk from the start. A chunk is HEAD if it contains
    # an abstract / keywords heading, has unusually high density of
    # ORCIDs / DOIs, or is very short and lives in the first ``head_cap``
    # positions. Stop as soon as we hit a chunk that looks substantive.
    for i in range(min(head_cap, n)):
        if _is_head_chunk(chunks_text[i], at_index=i):
            classes[i] = ChunkClass.HEAD
        else:
            # Stop the head walk at the first non-head chunk — body
            # starts here. Otherwise a stray "Abstract" mention deep
            # into a paper would mis-label.
            break

    # Tail pass: walk from the end. Tail chunks are classified more
    # carefully because the patterns (REFERENCES, ACK, CONTACT) can
    # appear in any order at the end of a paper.
    for offset in range(min(tail_cap, n)):
        i = n - 1 - offset
        if i <= 0 or classes[i] == ChunkClass.HEAD:
            break  # don't cross into head territory
        text = chunks_text[i]
        if _is_contact_chunk(text):
            classes[i] = ChunkClass.CONTACT
        elif _is_references_chunk(text):
            classes[i] = ChunkClass.REFERENCES
        elif _is_acknowledgements_chunk(text):
            classes[i] = ChunkClass.ACKNOWLEDGEMENTS
        else:
            # Stop the tail walk at the first body chunk.
            break

    body_indices = tuple(i for i, c in enumerate(classes) if c == ChunkClass.BODY)
    return ClassifiedChunks(classes=tuple(classes), body_indices=body_indices)


# ── per-class detectors ─────────────────────────────────────────────


def _is_head_chunk(text: str, *, at_index: int) -> bool:
    """True if ``text`` looks like front-matter (title, abstract, authors)."""
    if not text or not text.strip():
        return True  # empty chunk at the start is structural noise

    head = text[:400]

    # Strong positive: abstract / keywords / highlights heading.
    if _HEAD_HEADING_RE.search(head):
        return True

    # Position 0 is almost always title / journal-template content;
    # accept liberally.
    if at_index == 0 and len(text) < 1500:
        return True

    # Dense ORCIDs / emails are author affiliations.
    if len(_ORCID_RE.findall(text)) >= 2:
        return True
    if len(_EMAIL_RE.findall(text)) >= 2 and len(text) < 1000:
        return True

    return False


def _is_references_chunk(text: str) -> bool:
    """True if ``text`` looks like a citation list."""
    if not text or not text.strip():
        return False
    if _REFERENCES_HEADING_RE.search(text):
        return True
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    # Two independent density checks, gated at different thresholds
    # because the two pattern groups have very different false-positive
    # risk on a short chunk:
    #
    # (a) Loose patterns (_CITATION_PATTERNS) — individually weak (e.g.
    #     "^\s*\[\d+\]\s+[A-Z]..." matches "[1] Introduction" and "[12]
    #     Figure showing..." just as readily as a real citation), so
    #     they only count under the higher matches >= 3 absolute floor.
    #     That floor makes a 1-2 line chunk unreachable by design —
    #     it's what keeps a short numbered body list (a Methods/Notes
    #     section, a figure caption) from flipping on the loose
    #     patterns alone.
    # (b) The strict marker-citation pattern — marker shape AND
    #     author-comma-initial content together — is precise enough to
    #     trust on a short chunk, so it gets its own lower floor
    #     (>= 1 match) at a higher ratio (>= 50 %, vs. 30 % for the
    #     loose group) so a 2-line entry-plus-continuation still
    #     qualifies. This is what recognizes Marker's real
    #     one-entry-per-chunk bibliography shape
    #     ("- [15] Smith, J. A.; Doe, K. B. ... 2015, 25, 115.").
    loose_matches = sum(
        1 for ln in lines if any(p.search(ln) for p in _CITATION_PATTERNS)
    )
    if loose_matches >= 3 and loose_matches / len(lines) >= 0.3:
        return True
    strict_matches = sum(1 for ln in lines if _MARKER_CITATION_LINE_RE.search(ln))
    if strict_matches >= 1 and strict_matches / len(lines) >= 0.5:
        return True
    # DOI-heavy chunks deep in the paper are references.
    if len(_DOI_RE.findall(text)) >= 3:
        return True
    return False


def _is_acknowledgements_chunk(text: str) -> bool:
    """Heading-driven; ack/funding/competing-interest blocks."""
    if not text or not text.strip():
        return False
    return bool(_ACK_HEADING_RE.search(text))


def _is_contact_chunk(text: str) -> bool:
    """Corresponding-author / contact info chunks at the very end."""
    if not text or not text.strip():
        return False
    if _CONTACT_HEADING_RE.search(text):
        return True
    # Short tail chunk with email + name pattern is usually contact.
    if len(text) < 600 and _EMAIL_RE.search(text):
        return True
    return False


__all__ = ["MARKER_LINE_RE", "ChunkClass", "ClassifiedChunks", "classify_chunks"]
