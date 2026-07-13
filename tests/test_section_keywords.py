"""Pure unit tests for the section-keyword roll-up (source-backfill slice 2)."""

from __future__ import annotations

from types import SimpleNamespace as NS
from typing import Any

from precis.utils.section_keywords import rollup_keywords, rollup_label


def _chunks(*handles: str) -> list[Any]:
    return [NS(handle=h) for h in handles]


def _views(**kw: str) -> dict[str, dict[str, str]]:
    return {h: {"keywords": v} for h, v in kw.items()}


def test_ranks_by_cross_chunk_frequency() -> None:
    views = _views(
        a="garnet, conductivity",
        b="garnet, sintering",
        c="garnet",
        d="",
    )
    # garnet in 3 chunks (rank 1); conductivity & sintering tie at 1, so the
    # first-seen (conductivity, in chunk a) wins the tiebreak.
    assert rollup_keywords(views, _chunks("a", "b", "c", "d"), top_k=2) == [
        "garnet",
        "conductivity",
    ]


def test_rollup_label_joins_or_empties() -> None:
    views = _views(a="garnet, conductivity")
    assert rollup_label(views, _chunks("a"), top_k=2) == "garnet · conductivity"
    # an all-empty run yields "" so the caller keeps a bare count
    assert rollup_label({}, _chunks("a", "b")) == ""
    assert rollup_label(_views(a=""), _chunks("a")) == ""


def test_missing_and_blank_chunks_contribute_nothing() -> None:
    views = _views(a="x", b="")  # c absent from views entirely
    assert rollup_keywords(views, _chunks("a", "b", "c")) == ["x"]


def test_top_k_truncates() -> None:
    views = _views(a="one, two, three, four, five")
    assert rollup_keywords(views, _chunks("a"), top_k=3) == ["one", "two", "three"]
