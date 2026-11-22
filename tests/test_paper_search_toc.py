"""Paper keyword navigation uses the matched pool and stateless AND filters."""

from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers._paper_search_toc import pool_keywords
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store


def _handler(store: Store) -> PaperHandler:
    for i in range(8):
        ref = store.insert_ref(
            kind="paper", slug=f"tocpaper{i}", title=f"Pool paper {i}"
        )
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text="catalysis matched passage",
                )
            ],
        )
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE ref_id = %s",
                (
                    [
                        "catalysis",
                        "solar" if i < 4 else "thermal",
                        "oxidation" if i % 2 else "reduction",
                    ],
                    ref.id,
                ),
            )
            conn.commit()
    return PaperHandler(hub=Hub(store=store))


def test_toc_over_page_renders_clusters(store: Store) -> None:
    body = (
        _handler(store)
        .search(q="catalysis", mode="lexical", view="toc", page_size=3)
        .body
    )
    assert "keyword clusters" in body and "retrieved pool" in body
    assert "overlap" in body
    assert "kw:solar" in body
    assert "'kw': ['solar']" in body


def test_toc_under_page_returns_plain_list(store: Store) -> None:
    body = (
        _handler(store)
        .search(q="catalysis", mode="lexical", view="toc", page_size=10)
        .body
    )
    assert "keyword clusters" not in body
    assert "tocpaper" in body


def test_kw_drill_shrinks_pool_and_is_stateless(store: Store) -> None:
    handler = _handler(store)
    args: dict[str, Any] = dict(
        q="catalysis", mode="lexical", kw=["solar", "oxidation"], page_size=3
    )
    body = handler.search(**args).body
    assert body == handler.search(**args).body
    assert "kw: solar + oxidation" in body
    assert "tocpaper1" in body and "tocpaper3" in body
    assert "tocpaper0" not in body and "tocpaper5" not in body
    nested = handler.search(q="catalysis", mode="lexical", kw="solar", page_size=3).body
    assert "keyword clusters" in nested
    assert "'solar', 'oxidation'" in nested


def test_over_page_next_block_names_toc(store: Store) -> None:
    body = _handler(store).search(q="catalysis", mode="lexical", page_size=3).body
    _, sep, hints = body.partition("\nNext:\n")
    assert sep
    assert (
        "search(kind='paper', q='catalysis', view='toc', mode='lexical', page_size=3)"
        in hints
    )
    assert "search(kind='paper', q='catalysis solar', mode='lexical')" in hints
    assert "tighten the query with the top hit's keyword" in hints
    assert "replace <salient term>" not in hints


def test_extraction_failure_falls_to_none_bucket() -> None:
    block = MagicMock(
        id=1,
        text="eligible paragraph " * 20,
        chunk_kind="paragraph",
        keywords=None,
        meta={},
    )
    ref = MagicMock(id=1, meta={})
    store = MagicMock()
    store.pool.connection.side_effect = RuntimeError("embedding lookup unavailable")
    keywords, notice = pool_keywords([(block, ref, 1.0)], store, MagicMock())
    assert keywords == {1: []}
    assert "unavailable" in notice


def test_keyword_switch_refuses_navigation(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.bench import switches

    monkeypatch.setattr(switches, "current", lambda: MagicMock(kwd=False))
    with pytest.raises(BadInput, match="no-kwd"):
        PaperHandler(hub=Hub(store=store)).search(q="catalysis", view="toc")


def test_only_matched_chunks_supply_facets(store: Store) -> None:
    handler = _handler(store)
    ref = store.get_ref(kind="paper", id="tocpaper0")
    assert ref is not None
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=1, text="unrelated biodiversity passage")]
    )
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = 1",
            (["biodiversity"], ref.id),
        )
        conn.commit()
    body = handler.search(q="catalysis", mode="lexical", kw="biodiversity").body
    assert "no paper blocks match" in body


def test_section_fallback_precedes_extraction() -> None:
    block = MagicMock(id=1, keywords=None, meta={"section_path": ["Photocatalysis"]})
    store = MagicMock()
    keywords, notice = pool_keywords(
        [(block, MagicMock(id=1), 1.0)], store, MagicMock()
    )
    assert keywords == {1: ["photocatalysis"]}
    assert not notice
    store.pool.connection.assert_not_called()


def test_missing_keywords_use_one_embedding_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from precis.workers import chunk_keywords

    monkeypatch.setattr(
        chunk_keywords,
        "_prepare_keyword_candidates",
        lambda text, abbrevs: [("solar energy", None)],
    )
    blocks = [
        MagicMock(
            id=i,
            keywords=None,
            meta={},
            text="solar energy " * 30,
            chunk_kind="paragraph",
        )
        for i in (1, 2)
    ]
    ref = MagicMock(id=1, meta={})
    store = MagicMock()
    store.pool.connection.return_value.__enter__.return_value.execute.return_value.fetchall.return_value = [
        (1, "[1,0]"),
        (2, "[1,0]"),
    ]
    embedder = MagicMock(model="bge-m3")
    embedder.embed.return_value = [[1.0, 0.0], [1.0, 0.0]]
    labels, notice = pool_keywords([(b, ref, 1.0) for b in blocks], store, embedder)
    embedder.embed.assert_called_once_with(["solar energy", "solar energy"])
    assert labels == {1: ["solar energy"], 2: ["solar energy"]}
    assert not notice
    embedder.embed.side_effect = RuntimeError("backend down")
    labels, notice = pool_keywords([(b, ref, 1.0) for b in blocks], store, embedder)
    assert labels == {1: [], 2: []}
    assert "unavailable" in notice


def test_kw_refuses_unfiltered_count_view(store: Store) -> None:
    with pytest.raises(BadInput, match="ranked paper pool"):
        PaperHandler(hub=Hub(store=store)).search(
            q="catalysis", kw="solar", view="count"
        )


@pytest.mark.parametrize("view", ["index", "ids"])
def test_kw_refuses_plain_listing_views(store: Store, view: str) -> None:
    with pytest.raises(BadInput, match="ranked paper pool"):
        PaperHandler(hub=Hub(store=store)).search(q="catalysis", kw="solar", view=view)
