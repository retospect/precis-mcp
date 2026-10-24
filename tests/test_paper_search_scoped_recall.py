"""gr510534: a scoped paper search must not miss multi-word queries.

Prod repro (2026-10-23): ``search(kind='paper', q='AMOC strengthening
weakening response to regional MCB', scope='pa510467')`` returned "no
paper blocks match" while ``q='AMOC'`` in the same scope found the right
chunk. Two legs failed at once: the lexical leg ANDs every term through
``websearch_to_tsquery`` and the terms live in *different* chunks of the
paper, so no single chunk matched; and the semantic leg contributed
nothing because the paper's chunks were unembedded (ingested hours
earlier).

Two relaxations apply under a single-ref ``scope=``:

* the lexical leg retries the same terms OR-ed when the strict AND
  query is empty (``_SCOPED_OR_TSQUERY`` / ``lex_any``);
* the semantic distance floor lifts — the floor is a corpus-wide
  nonsense guard, and within one paper "the closest blocks it has" is
  an honest answer.

Corpus-wide AND precision and the floor elsewhere are unchanged.
"""

from __future__ import annotations

from precis.dispatch import Hub
from precis.embedder import MockEmbedder
from precis.handlers.paper import PaperHandler
from precis.store import ChunkInsert, Store
from precis.utils import handle_registry

_Q = "AMOC strengthening weakening response regional MCB"


def _embedder(store: Store) -> MockEmbedder:
    return MockEmbedder(dim=store.embedding_dim())


def _seed(
    store: Store, *, embedded: bool, slug: str = "mcb-paper"
) -> tuple[int, list[int]]:
    """A paper whose query terms live in different chunks — no single
    chunk matches every term of the multi-word query."""
    e = _embedder(store)
    ref = store.insert_ref(
        kind="paper",
        slug=slug,
        title="Atlantic overturning under marine cloud brightening",
        year=2024,
    )
    texts = [
        "AMOC index increased; the overturning circulation strengthened.",
        "Regional MCB deployment weakens the precipitation response.",
    ]
    rows = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=i,
                text=t,
                embedding=e.embed_one(t) if embedded else None,
            )
            for i, t in enumerate(texts)
        ],
    )
    return ref.id, [r.id for r in rows]


def _handler(store: Store) -> PaperHandler:
    return PaperHandler(hub=Hub(store=store, embedder=_embedder(store)))


def test_scoped_multiword_hybrid_finds_spread_terms(store: Store) -> None:
    """The prod repro shape: unembedded chunks, hybrid mode — the
    semantic leg is empty, so only the OR-ed lexical retry can hit."""
    ref_id, chunk_ids = _seed(store, embedded=False)
    scope = handle_registry.format_handle("paper", ref_id)
    body = _handler(store).search(q=_Q, scope=scope).body
    assert "no paper blocks match" not in body
    found = [
        handle_registry.format_handle("paper", cid, chunk=True) in body
        for cid in chunk_ids
    ]
    assert any(found)


def test_scoped_multiword_lexical_finds_spread_terms(store: Store) -> None:
    """Same fallback through the pure lexical path (no embedder leg)."""
    ref_id, chunk_ids = _seed(store, embedded=False)
    scope = handle_registry.format_handle("paper", ref_id)
    body = _handler(store).search(q=_Q, scope=scope, mode="lexical").body
    assert "no paper blocks match" not in body
    found = [
        handle_registry.format_handle("paper", cid, chunk=True) in body
        for cid in chunk_ids
    ]
    assert any(found)


def test_unscoped_multiword_keeps_and_precision(store: Store) -> None:
    """The retry is scope-only: corpus-wide lexical search still ANDs
    every term, so the same query stays empty."""
    _seed(store, embedded=False)
    body = _handler(store).search(q=_Q, mode="lexical").body
    assert "no paper blocks match" in body


def test_scoped_multiword_embedded_also_answers(store: Store) -> None:
    """With embeddings present the lifted distance floor lets the
    semantic leg answer too — either way the search must not be empty."""
    ref_id, _ = _seed(store, embedded=True)
    scope = handle_registry.format_handle("paper", ref_id)
    body = _handler(store).search(q=_Q, scope=scope).body
    assert "no paper blocks match" not in body


def test_scoped_and_matches_keep_strict_results(store: Store) -> None:
    """The OR leg only fills when the AND leg is empty: a chunk matching
    every term still leads, and a one-term-only chunk stays OUT — the
    strict result is untouched."""
    e = _embedder(store)
    ref = store.insert_ref(
        kind="paper", slug="and-paper", title="AND ranking probe", year=2024
    )
    rows = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(
                ord=0,
                text="a paragraph about weakening only",
                embedding=e.embed_one("a paragraph about weakening only"),
            ),
            ChunkInsert(
                ord=1,
                text="AMOC weakening under MCB forcing, in one chunk.",
                embedding=e.embed_one(
                    "AMOC weakening under MCB forcing, in one chunk."
                ),
            ),
        ],
    )
    or_only = handle_registry.format_handle("paper", rows[0].id, chunk=True)
    and_hit = handle_registry.format_handle("paper", rows[1].id, chunk=True)
    scope = handle_registry.format_handle("paper", ref.id)
    body = (
        _handler(store).search(q="AMOC weakening MCB", scope=scope, mode="lexical").body
    )
    assert and_hit in body
    assert or_only not in body


def test_scoped_count_mirrors_the_or_retry(store: Store) -> None:
    """``count_chunks_lexical`` is the "N of K" denominator for the leg
    the search actually ran — it must count the OR universe when the
    strict AND universe is empty, not report "N of 0" over real hits."""
    ref_id, _ = _seed(store, embedded=False)
    strict = store.chunks.count_chunks_lexical(q=_Q, kind="paper")
    assert strict == 0
    scoped = store.chunks.count_chunks_lexical(q=_Q, kind="paper", scope_ref_id=ref_id)
    assert scoped == 2
