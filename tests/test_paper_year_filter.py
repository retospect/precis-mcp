"""Publish-date filter for paper search — ``after=`` / ``before=`` on
``search(kind='paper')`` and the ``year_from`` / ``year_to`` store
plumbing behind it.

Covers: inclusive range semantics across all three search modes, NULL-year
exclusion + the "omitted" count, and handler-boundary validation.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.errors import BadInput
from precis.handlers.paper import PaperHandler
from precis.store import BlockInsert, Store
from tests.conftest import chunk_handle

_TEXT = "quantum batteries store energy efficiently"


def _seed(store: Store, *, slug: str, year: int | None, embed: bool = True) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug, year=year)
    e = MockEmbedder(dim=1024)
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(
                pos=0, text=_TEXT, embedding=(e.embed_one(_TEXT) if embed else None)
            )
        ],
    )
    return ref.id


# ── store-level: year_from / year_to across modes ─────────────────


def _slugs(hits: list) -> set[str]:
    return {ref.slug for _b, ref, _s in hits}


def test_year_from_excludes_older(store: Store) -> None:
    _seed(store, slug="p2018", year=2018, embed=False)
    _seed(store, slug="p2022", year=2022, embed=False)
    hits = store.search_blocks(q=_TEXT, mode="lexical", kind="paper", year_from=2020)
    assert _slugs(hits) == {"p2022"}


def test_year_to_excludes_newer(store: Store) -> None:
    _seed(store, slug="p2018", year=2018, embed=False)
    _seed(store, slug="p2022", year=2022, embed=False)
    hits = store.search_blocks(q=_TEXT, mode="lexical", kind="paper", year_to=2020)
    assert _slugs(hits) == {"p2018"}


def test_range_is_inclusive(store: Store) -> None:
    _seed(store, slug="p2019", year=2019, embed=False)
    _seed(store, slug="p2020", year=2020, embed=False)
    _seed(store, slug="p2021", year=2021, embed=False)
    hits = store.search_blocks(
        q=_TEXT, mode="lexical", kind="paper", year_from=2019, year_to=2021
    )
    assert _slugs(hits) == {"p2019", "p2020", "p2021"}


def test_semantic_mode_applies_year_filter(store: Store) -> None:
    _seed(store, slug="p2018", year=2018, embed=True)
    _seed(store, slug="p2022", year=2022, embed=True)
    qv = MockEmbedder(dim=1024).embed_one(_TEXT)
    hits = store.search_blocks(
        q=_TEXT,
        query_vec=qv,
        mode="semantic",
        kind="paper",
        max_distance=None,
        year_from=2020,
    )
    assert _slugs(hits) == {"p2022"}


def test_hybrid_mode_applies_year_filter(store: Store) -> None:
    _seed(store, slug="p2018", year=2018, embed=True)
    _seed(store, slug="p2022", year=2022, embed=True)
    qv = MockEmbedder(dim=1024).embed_one(_TEXT)
    hits = store.search_blocks(q=_TEXT, query_vec=qv, kind="paper", year_from=2020)
    assert _slugs(hits) == {"p2022"}


def test_null_year_excluded_from_range(store: Store) -> None:
    _seed(store, slug="pNone", year=None, embed=False)
    _seed(store, slug="p2022", year=2022, embed=False)
    hits = store.search_blocks(q=_TEXT, mode="lexical", kind="paper", year_from=2000)
    assert _slugs(hits) == {"p2022"}  # pNone dropped (NULL year)


def test_count_yearless_matches(store: Store) -> None:
    _seed(store, slug="pNone", year=None, embed=False)
    _seed(store, slug="p2022", year=2022, embed=False)
    n = store.count_paper_yearless_matches(q=_TEXT)
    assert n == 1  # only pNone lacks a year


# ── handler-level: after= / before= validation + trailer ──────────


def _handler(store: Store) -> PaperHandler:
    # No embedder → lexical degrade; the year filter applies to all legs.
    return PaperHandler(hub=Hub(store=store))


def test_handler_after_filters(store: Store) -> None:
    _seed(store, slug="p2018", year=2018, embed=False)
    _seed(store, slug="p2022", year=2022, embed=False)
    out = _handler(store).search(q=_TEXT, after=2020)
    assert chunk_handle(store, "p2022", ord=0) in out.body
    assert chunk_handle(store, "p2018") not in out.body


def test_handler_after_gt_before_rejected(store: Store) -> None:
    with pytest.raises(BadInput, match="later than"):
        _handler(store).search(q=_TEXT, after=2023, before=2019)


def test_handler_non_numeric_year_rejected(store: Store) -> None:
    with pytest.raises(BadInput, match="4-digit year"):
        _handler(store).search(q=_TEXT, after="soon")


def test_handler_out_of_range_year_rejected(store: Store) -> None:
    with pytest.raises(BadInput, match="plausible range"):
        _handler(store).search(q=_TEXT, before=3000)


def test_handler_surfaces_yearless_omission(store: Store) -> None:
    _seed(store, slug="pNone", year=None, embed=False)
    _seed(store, slug="p2022", year=2022, embed=False)
    out = _handler(store).search(q=_TEXT, after=2000)
    assert chunk_handle(store, "p2022", ord=0) in out.body
    assert "omitted" in out.body  # the NULL-year heads-up
    assert "/papers/triage" in out.body
