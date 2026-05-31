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
from enum import Enum


class ChunkClass(str, Enum):
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


# References: count citation-shaped lines per chunk. A chunk with high
# density (>30 % of non-empty lines) is treated as a reference list.
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
    tail_cap: int = 8,
) -> ClassifiedChunks:
    """Label each chunk by structural role.

    Args:
        chunks_text: ordered chunk bodies.
        head_cap: maximum number of leading chunks eligible for HEAD
            classification (most papers' front-matter is 1–5 chunks;
            preventing it from running away protects long abstracts).
        tail_cap: maximum number of trailing chunks eligible for
            REFERENCES / ACK / CONTACT classification.

    Returns:
        :class:`ClassifiedChunks` with per-chunk labels + the BODY
        indices for the segmenter.

    Empty input → empty output. A single-chunk paper is classified
    as BODY regardless of content (too short for the front-matter
    heuristic to fire).
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
    # Citation density: count lines matching any citation pattern;
    # require ≥3 matches AND ≥30 % of non-empty lines.
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return False
    matches = sum(
        1 for ln in lines if any(p.search(ln) for p in _CITATION_PATTERNS)
    )
    if matches >= 3 and matches / len(lines) >= 0.3:
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


__all__ = ["ChunkClass", "ClassifiedChunks", "classify_chunks"]
