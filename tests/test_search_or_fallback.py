"""Lexical count metadata reports the executed leg without changing counts."""

import pytest

from precis.embedder import MockEmbedder
from precis.store import ChunkInsert, Store


@pytest.mark.parametrize(
    "q, fallback, expected",
    [
        ("copper nitrate missing", True, 1),
        ("copper nitrate reduction", False, 1),
        ('"copper nitrate missing"', False, 0),
    ],
)
@pytest.mark.parametrize("yearless", [False, True])
def test_count_reports_or_fallback(
    store: Store, q: str, fallback: bool, expected: int, yearless: bool
) -> None:
    ref = store.insert_ref(kind="paper", slug="fallback-test", title="test")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="copper nitrate reduction")]
    )
    info = {"or_fallback": True}
    count = (
        store.chunks.count_paper_yearless_matches
        if yearless
        else store.chunks.count_chunks_lexical
    )
    assert count(q=q, include_ref_ids=[ref.id], lexical_info_out=info) == expected
    assert info["or_fallback"] is fallback


@pytest.mark.parametrize("mode", ["lexical", "hybrid"])
@pytest.mark.parametrize("scoped", [False, True])
@pytest.mark.parametrize(
    "q, fallback",
    [
        ("copper nitrate missing", True),
        ("copper nitrate reduction", False),
        ('"copper nitrate missing"', False),
    ],
)
def test_search_reports_or_fallback(
    store: Store, mode: str, scoped: bool, q: str, fallback: bool
) -> None:
    ref = store.insert_ref(kind="paper", slug="search-fallback-test", title="test")
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="copper nitrate reduction")]
    )
    info = {"or_fallback": True}
    store.chunks.search_chunks(
        q=q,
        mode=mode,
        kind="paper",
        scope_ref_id=ref.id if scoped else None,
        query_vec=MockEmbedder(dim=1024).embed_one(q),
        lexical_info_out=info,
    )
    assert info["or_fallback"] is fallback
