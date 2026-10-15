"""Versioned positional anchors for the survey extraction pass.

Port of ``paper-extraction-pilot/bindings.py``'s anchor machinery — the
same constants, splitter and id forms, minus the schema-4 binding
document the pilot wrapped them in. Anchor ids are POSITIONAL:

* ``"<chunk_id>.<n>"``   — the n-th sentence span of a chunk (0-based)
* ``"<chunk_id>.<n>-<m>"`` — a run of sentences n..m inclusive
* ``"<chunk_id>#<k>"``   — the k-th numeric region (0-based); resolves to
                           the containing sentence for evidence
* ``[chunk_id, start, end]`` — raw offsets

so they are only meaningful under the splitter that produced them: a
different ``SENTENCE_BREAK`` re-points every id at different text while
the chunk bytes are untouched. ``ANCHOR_SCHEME`` is therefore
append-only — bump it whenever the splitter, ``numeric_regions`` or the
id format changes; a batch stamped with an older scheme is refused, not
re-pointed. Scheme history:

* 1 — the pilot splitter: broke after ``ca.``/``Fig.``/``satd.`` and
  friends whenever a digit or capital followed, splitting results from
  their conditions (never shipped in precis, pilot-only).
* 2 — :data:`_ABBREVIATIONS` skips a candidate break whose last token is
  a listed abbreviation's period.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

ANCHOR_SCHEME = 2

#  SENTENCE_BREAK is the CANDIDATE break set; _ends_in_abbreviation vetoes
#  a candidate whose last token is a listed abbreviation.
SENTENCE_BREAK = re.compile(r"(?<=[.!?])[ \t]+(?=[^a-z])|\n{2,}|(?<=\|)\n(?=\|)")

#  Tokens whose trailing period is an abbreviation, not a sentence end.
#  The trade-off is a rare under-split (a real sentence ending in "et
#  al.") against the measured over-split that motivated the guard.
_ABBREVIATIONS = frozenset(
    {
        "ca",
        "cf",
        "fig",
        "figs",
        "eq",
        "eqs",
        "ref",
        "refs",
        "sec",
        "no",
        "nos",
        "vs",
        "al",
        "e.g",
        "i.e",
        "dr",
        "prof",
        "st",
        "approx",
        "resp",
        "satd",
        "wt",
        "at",
        "vol",
        "ch",
        "sc",
    }
)


def _ends_in_abbreviation(tail: str) -> bool:
    """The final token before a break is a listed abbreviation ('ca.', 'Fig.')."""
    words = tail.split()
    if not words:
        return False
    token = words[-1].lower().strip("([{<«").rstrip(".!?)]}>»,;:")
    return token in _ABBREVIATIONS


def sentence_spans(text: str) -> list[tuple[int, int]]:
    """Split a chunk into stable, exactly reconstructable sentence spans."""
    spans = []
    start = 0
    for match in SENTENCE_BREAK.finditer(text):
        end = match.start()
        if (
            match.group()[0] in " \t"
            and end > 0
            and text[end - 1] == "."
            and _ends_in_abbreviation(text[:end])
        ):
            continue
        if text[start:end].strip():
            spans.append((start, end))
        start = match.end()
    if text[start:].strip():
        spans.append((start, len(text)))
    trimmed = []
    for start, end in spans:
        piece = text[start:end]
        lead = len(piece) - len(piece.lstrip())
        tail = len(piece.rstrip())
        if piece[lead:tail].strip():
            trimmed.append((start + lead, start + tail))
    return trimmed


def anchored_text(text: str) -> str:
    """The chunk text a reader sees: the raw text with ``[n]`` sentence
    markers inserted, nothing else — stripping ``[n]`` gives back the
    source byte-for-byte, and the markers delimit the same spans
    ``sentence_spans`` reports so an anchor id resolves to what the
    reader numbered."""
    spans = sentence_spans(text)
    if not spans:
        return text
    rendered = []
    cursor = 0
    for index, (start, end) in enumerate(spans):
        rendered.append(text[cursor:start])
        rendered.append(f"[{index}]")
        rendered.append(text[start:end])
        cursor = end
    rendered.append(text[cursor:])
    return "".join(rendered)


def anchor_index(chunks: Iterable[tuple[int, str]]) -> dict[str, tuple[int, int, int]]:
    """Map every anchor id of a chunk set to ``(chunk_id, start, end)``.

    ``chunks`` is an iterable of ``(chunk_id, text)`` pairs. Sentence ids
    are ``"<chunk_id>.<n>"``; a numeric atom id ``"<chunk_id>#<k>"`` points
    at the k-th numeric region's containing sentence (an atom anchor
    proves a value; its evidence is the sentence it sits in).
    """
    from precis.extract.verify import numeric_regions

    index: dict[str, tuple[int, int, int]] = {}
    for chunk_id, text in chunks:
        sentences = sentence_spans(text)
        for position, (start, end) in enumerate(sentences):
            index[f"{chunk_id}.{position}"] = (chunk_id, start, end)
        for region_index, (start, end, _part) in enumerate(numeric_regions(text)):
            sentence = next(
                (i for i, (a, b) in enumerate(sentences) if a <= start and end <= b),
                None,
            )
            if sentence is not None:
                s, e = sentences[sentence]
                index[f"{chunk_id}#{region_index}"] = (chunk_id, s, e)
    return index


def resolve_anchor(
    index: dict[str, tuple[int, int, int]], anchor: object
) -> tuple[int, int, int]:
    """One anchor id or raw ``[chunk, start, end]`` list to a chunk span."""
    if isinstance(anchor, str):
        if "-" in anchor and not anchor.startswith("-"):
            head, tail = anchor.split("-", 1)
            if head in index and tail.isdigit():
                chunk_id, start, _end = index[head]
                _chunk2, _s2, end2 = index.get(f"{chunk_id}.{tail}", (None, None, None))
                if _chunk2 == chunk_id and end2 is not None:
                    return chunk_id, start, end2
        if anchor in index:
            return index[anchor]
        raise ValueError(f"anchor id {anchor} is not in the anchor index")
    if (
        isinstance(anchor, (list, tuple))
        and len(anchor) == 3
        and all(isinstance(x, int) and not isinstance(x, bool) for x in anchor)
    ):
        return int(anchor[0]), int(anchor[1]), int(anchor[2])
    raise ValueError(f"unrecognized anchor {anchor}")


def resolve_spans(
    index: dict[str, tuple[int, int, int]], anchors: Iterable[object]
) -> list[tuple[int, int, int]]:
    """Resolve every anchor, merging equal spans."""
    out: list[tuple[int, int, int]] = []
    seen: set[tuple[int, int, int]] = set()
    for anchor in anchors:
        span = resolve_anchor(index, anchor)
        if span not in seen:
            seen.add(span)
            out.append(span)
    return out
