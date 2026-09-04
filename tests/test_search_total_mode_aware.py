"""gr311338: the ``search(kind='paper')`` "N of K" header total must
reflect the actual retrieval pool of the requested ``mode=``, not always
the plain-lexical universe.

Store-level: :meth:`Store.chunks.count_chunks_keywords` (the
``mode='verbatim'`` companion to ``count_chunks_lexical``) counts the same
``c.keywords @> terms`` pool :meth:`Store.chunks.search_chunks_keywords`
retrieves from.

Handler-level: :class:`~precis.handlers._paper_search.FusedBlockSearch`
renders a header total that tracks the requested mode — lexical and
verbatim report their own (genuinely different) pool sizes for the same
query, and semantic reports no total at all (honest — cosine hits aren't
gated by the lexical tsquery, so no cheap accurate count exists), mirroring
the existing ``broad=True`` -> ``total=None`` precedent.
"""

from __future__ import annotations

from uuid import uuid4

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store


def _seed(store: Store, *, slug: str, blocks: list[str], embed: bool = False) -> int:
    ref = store.insert_ref(kind="paper", slug=slug, title=slug)
    e = MockEmbedder(dim=1024)
    rows = [
        ChunkInsert(ord=i, text=t, embedding=(e.embed_one(t) if embed else None))
        for i, t in enumerate(blocks)
    ]
    store.chunks.insert_chunks(ref.id, rows)
    return ref.id


# ── store-level: count_chunks_keywords mirrors search_chunks_keywords ──


def test_count_chunks_keywords_matches_search(store: Store) -> None:
    tag = uuid4().hex[:8]
    rid = _seed(
        store,
        slug=f"cnt-kw-{tag}",
        blocks=[f"photocatalysis{tag} mechanism{tag} chunk {i}" for i in range(5)],
    )
    with store.pool.connection() as conn:
        for i in range(3):
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = %s",
                ([f"photocatalysis{tag}", f"mechanism{tag}"], rid, i),
            )
        conn.commit()

    terms = [f"photocatalysis{tag}", f"mechanism{tag}"]
    total = store.chunks.count_chunks_keywords(terms=terms, kind="paper")
    all_hits = store.chunks.search_chunks_keywords(terms=terms, kind="paper", limit=100)
    assert total == len(all_hits) == 3


def test_count_chunks_keywords_empty_terms_is_zero(store: Store) -> None:
    assert store.chunks.count_chunks_keywords(terms=[], kind="paper") == 0
    assert store.chunks.count_chunks_keywords(terms=["   "], kind="paper") == 0


# ── handler-level: the rendered header tracks mode's own pool ──────────


def test_verbatim_total_differs_from_lexical_total(store: Store) -> None:
    """Same query, same corpus: lexical's pool is every chunk containing
    both words; verbatim's pool is only the chunks whose own KeyBERT
    keywords contain both words verbatim — genuinely smaller here, and
    the header must say so (not silently repeat the lexical K, the
    gr311338 defect)."""
    tag = uuid4().hex[:8]
    rid = _seed(
        store,
        slug=f"totals-{tag}",
        blocks=[f"photocatalysis{tag} mechanism{tag} paragraph {i}" for i in range(5)],
    )
    with store.pool.connection() as conn:
        # Only 3 of the 5 lexically-matching chunks carry both terms as
        # their own extracted keywords — verbatim's honest pool is 3,
        # lexical's is 5.
        for i in range(3):
            conn.execute(
                "UPDATE chunks SET keywords = %s WHERE ref_id = %s AND ord = %s",
                ([f"photocatalysis{tag}", f"mechanism{tag}"], rid, i),
            )
        conn.commit()

    h = PaperHandler(hub=Hub(store=store))
    q = f"photocatalysis{tag} mechanism{tag}"

    lex = h.search(q=q, mode="lexical", page_size=2)
    verbatim = h.search(q=q, mode="verbatim", page_size=2)

    assert "2 of 5" in lex.body
    assert "2 of 3" in verbatim.body
    # The old defect: verbatim's header echoed the lexical K verbatim.
    assert "2 of 5" not in verbatim.body


def test_semantic_total_is_honest_none(store: Store) -> None:
    """``mode='semantic'`` has no cheap-honest count (cosine hits aren't
    gated by the lexical tsquery) — the header must render the plain
    count with no "of K" claim, mirroring broad mode's existing
    ``total=None`` precedent, rather than reusing the lexical count."""
    tag = uuid4().hex[:8]
    _seed(
        store,
        slug=f"sem-{tag}",
        blocks=[f"photocatalysis{tag} mechanism{tag} paragraph {i}" for i in range(5)],
        embed=True,
    )

    h = PaperHandler(
        hub=Hub(store=store, embedder=MockEmbedder(dim=store.embedding_dim()))
    )
    out = h.search(
        q=f"photocatalysis{tag} mechanism{tag}",
        mode="semantic",
        page_size=2,
    )
    assert " of " not in out.body.splitlines()[0]
