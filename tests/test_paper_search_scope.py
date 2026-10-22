"""gr503955: the title-match callout honours the include scope.

``search(kind='paper', q=<a paper's title>, serves='quest:<id>')`` used to
name an out-of-scope paper in its ``Title match`` callout:
``FusedBlockSearch._inject_title_matches`` trigram-matched raw titles
without seeing ``include_ref_ids`` (the dispatch-resolved ``serves=``/
``cited=``/``hubbed=`` inclusion set the search legs already honour), so a
scoped search could name — and promote — a paper outside the serving set.
The fix filters the title matches by the include set; an emptied match
list returns the hits untouched (no callout).
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry

_QUERY = "quantum annealing for spin glass ground states"
_SERVING_TITLE = "Quantum Annealing for Spin Glass Ground States"
_OTHER_TITLE = "Quantum Annealing for Spin Glass Ground States Revisited"


def _seed(
    store: Store,
    *,
    slug: str,
    title: str,
    embedder: MockEmbedder,
) -> int:
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title=title,
        authors=[{"family": "Ising", "given": "Ernst"}],
        year=2024,
    )
    text = f"{title.lower()} — methods and benchmarks"
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text=text, embedding=embedder.embed_one(text))]
    )
    return ref.id


def _mk(store: Store) -> tuple[PaperHandler, int, int, int]:
    """Two papers whose titles both trigram-match the query; only the
    first carries a ``serves`` edge to the quest."""
    e = MockEmbedder(dim=store.embedding_dim())
    quest = store.insert_ref(kind="quest", slug=None, title="anneal the glass")
    serving = _seed(store, slug="serving", title=_SERVING_TITLE, embedder=e)
    other = _seed(store, slug="other", title=_OTHER_TITLE, embedder=e)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)"
            " VALUES (%s,%s,'serves','system')",
            (serving, quest.id),
        )
        conn.commit()
    return PaperHandler(hub=Hub(store=store, embedder=e)), quest.id, serving, other


def test_scoped_search_names_only_the_serving_title_match(store: Store) -> None:
    ph, qid, serving, other = _mk(store)
    body = ph.search(q=_QUERY, serves=f"quest:{qid}").body
    assert "Title match" in body
    assert _SERVING_TITLE in body
    assert _OTHER_TITLE not in body
    assert handle_registry.format_handle("paper", serving) in body
    assert handle_registry.format_handle("paper", other) not in body


def test_unscoped_search_names_both_title_matches(store: Store) -> None:
    ph, _qid, _serving, _other = _mk(store)
    body = ph.search(q=_QUERY).body
    assert "Title match" in body
    assert _SERVING_TITLE in body
    assert _OTHER_TITLE in body


def test_scope_with_no_serving_title_match_drops_the_callout(
    store: Store,
) -> None:
    """When the include set holds no title match the callout must go
    away entirely — not render an empty or out-of-scope line."""
    e = MockEmbedder(dim=store.embedding_dim())
    quest = store.insert_ref(kind="quest", slug=None, title="q")
    unrelated = store.insert_ref(kind="paper", slug="srv", title="unrelated srv")
    text = "quantum annealing for spin glass ground states body text"
    store.chunks.insert_chunks(
        unrelated.id,
        [ChunkInsert(ord=0, text=text, embedding=e.embed_one(text))],
    )
    _seed(store, slug="other", title=_OTHER_TITLE, embedder=e)
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO links (src_ref_id, dst_ref_id, relation, set_by)"
            " VALUES (%s,%s,'serves','system')",
            (unrelated.id, quest.id),
        )
        conn.commit()
    ph = PaperHandler(hub=Hub(store=store, embedder=e))
    body = ph.search(q=_QUERY, serves=f"quest:{quest.id}").body
    assert "Title match" not in body
    assert _OTHER_TITLE not in body
