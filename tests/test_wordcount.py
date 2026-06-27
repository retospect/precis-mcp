"""Unit tests for the draft word-count core (proposal writing).

Pure / DB-free — exercises :mod:`precis.utils.wordcount` directly with
lightweight stand-in chunks, mirroring the :class:`DraftChunk` shape the
handler feeds it (``chunk_id`` / ``parent_chunk_id`` / ``chunk_kind`` /
``text`` / ``meta``)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.utils.wordcount import aggregate_word_counts, count_words


@dataclass
class _C:
    chunk_id: int
    parent_chunk_id: int | None
    chunk_kind: str
    text: str
    meta: dict[str, Any] = field(default_factory=dict)


def test_count_words_plain() -> None:
    assert count_words("the quick brown fox jumps") == 5
    assert count_words("") == 0
    assert count_words(None) == 0


def test_count_words_hyphen_and_apostrophe_are_one_word() -> None:
    assert count_words("state-of-the-art don't break") == 3


def test_count_words_markdown_link_keeps_visible_text() -> None:
    # "[the source](pc12)" → "the source" (2 words), not the target.
    assert count_words("see [the source](pc12) here") == 4


def test_count_words_strips_bare_handle_refs() -> None:
    # "[dc4]" and "[§wang2020~3]" are references, not prose words.
    assert count_words("as shown [dc4] and [§wang2020~3] it works") == 5


def test_aggregate_subtree_inclusive() -> None:
    chunks = [
        _C(1, None, "heading", "Intro", {"word_target": {"min": 5, "max": 10}}),
        _C(2, 1, "paragraph", "one two three four five six seven"),  # 7
        _C(3, 1, "heading", "Sub"),
        _C(4, 3, "paragraph", "alpha beta gamma"),  # 3
        _C(5, None, "heading", "Methods", {"word_target": {"min": 50, "max": 100}}),
        _C(6, 5, "paragraph", "only three words"),  # 3
    ]
    report = aggregate_word_counts(chunks)
    assert report.total == 13
    by_id = {s.chunk_id: s for s in report.sections}
    # Intro includes its own paragraph (7) + the Sub subsection (3) = 10.
    assert by_id[1].words == 10
    assert by_id[1].verdict == "ok"
    # Sub: 3 words, no target.
    assert by_id[3].words == 3
    assert by_id[3].verdict == "none"
    assert by_id[3].target is None
    # Methods: 3 words, target 50–100 → under.
    assert by_id[5].words == 3
    assert by_id[5].verdict == "under"


def test_aggregate_over_target() -> None:
    chunks = [
        _C(1, None, "heading", "Abstract", {"word_target": {"min": 1, "max": 3}}),
        _C(2, 1, "paragraph", "one two three four five"),  # 5 > 3
    ]
    report = aggregate_word_counts(chunks)
    assert report.sections[0].verdict == "over"


def test_non_prose_kinds_excluded() -> None:
    chunks = [
        _C(1, None, "heading", "S", {"word_target": {"min": 1, "max": 100}}),
        _C(2, 1, "equation", "E = m c squared and friends"),
        _C(3, 1, "figure", "a caption with several words here"),
        _C(4, 1, "table", "cells of a table not counted"),
        _C(5, 1, "paragraph", "two words"),  # only these count
    ]
    report = aggregate_word_counts(chunks)
    assert report.total == 2
    assert report.sections[0].words == 2


def test_empty_section_is_zero_not_error() -> None:
    chunks = [_C(1, None, "heading", "Empty", {"word_target": {"min": 10, "max": 20}})]
    report = aggregate_word_counts(chunks)
    assert report.total == 0
    assert report.sections[0].words == 0
    assert report.sections[0].verdict == "under"


def test_partial_target_bounds() -> None:
    # max-only ⇒ floor 0; min-only ⇒ no upper bound.
    chunks_max = [
        _C(1, None, "heading", "S", {"word_target": {"max": 3}}),
        _C(2, 1, "paragraph", "a b c d"),  # 4 > 3
    ]
    assert aggregate_word_counts(chunks_max).sections[0].verdict == "over"
    chunks_min = [
        _C(1, None, "heading", "S", {"word_target": {"min": 2}}),
        _C(2, 1, "paragraph", "a b c"),  # 3 >= 2, no ceiling
    ]
    assert aggregate_word_counts(chunks_min).sections[0].verdict == "ok"
