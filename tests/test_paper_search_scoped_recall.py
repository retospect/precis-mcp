"""gr510534 + gr614169: a paper search must not miss multi-word queries.

Prod repro (2026-10-23): ``search(kind='paper', q='AMOC strengthening
weakening response to regional MCB', scope='pa510467')`` returned "no
paper blocks match" while ``q='AMOC'`` in the same scope found the right
chunk. Two legs failed at once: the lexical leg ANDs every term through
``websearch_to_tsquery`` and the terms live in *different* chunks of the
paper, so no single chunk matched; and the semantic leg contributed
nothing because the paper's chunks were unembedded (ingested hours
earlier). The memory-ablation bench (gr614169) measured the same failure
corpus-wide: the AND query was empty for 11 of 25 QASPER quick
questions, so hybrid degraded to semantic-only exactly when the query
was a natural-language question.

One relaxation applies under either scope:

* when the strict AND query is empty, the lexical leg retries the same
  terms OR-ed (``_OR_TSQUERY`` / ``lex_any``) — under a single-ref
  ``scope=`` for any multi-word query, unscoped from three words up and
  only when an EXISTS probe shows the strict leg empty, so an AND that
  hits pays no OR scan;
* under ``scope=`` the semantic distance floor also lifts — the floor is
  a corpus-wide nonsense guard, and within one paper "the closest
  blocks it has" is an honest answer.

Strict AND hits are never reordered — the OR retry only fills an empty
leg — and operator queries (``-term`` / ``"phrase"``) never retry, since
``plainto_tsquery`` would flatten them into positive OR terms.
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


def _found_chunk_handles(body: str, chunk_ids: list[int]) -> list[bool]:
    return [
        handle_registry.format_handle("paper", cid, chunk=True) in body
        for cid in chunk_ids
    ]


def test_scoped_multiword_hybrid_finds_spread_terms(store: Store) -> None:
    """The prod repro shape: unembedded chunks, hybrid mode — the
    semantic leg is empty, so only the OR-ed lexical retry can hit."""
    ref_id, chunk_ids = _seed(store, embedded=False)
    scope = handle_registry.format_handle("paper", ref_id)
    body = _handler(store).search(q=_Q, scope=scope).body
    assert "no paper blocks match" not in body
    assert any(_found_chunk_handles(body, chunk_ids))


def test_scoped_multiword_lexical_finds_spread_terms(store: Store) -> None:
    """Same fallback through the pure lexical path (no embedder leg)."""
    ref_id, chunk_ids = _seed(store, embedded=False)
    scope = handle_registry.format_handle("paper", ref_id)
    body = _handler(store).search(q=_Q, scope=scope, mode="lexical").body
    assert "no paper blocks match" not in body
    assert any(_found_chunk_handles(body, chunk_ids))


def test_unscoped_multiword_lexical_finds_spread_terms(store: Store) -> None:
    """gr614169: the same AND-empty failure happens corpus-wide — a
    natural-language question's terms spread over different chunks, so
    the unscoped lexical leg OR-retries too instead of answering "no
    blocks" while each term alone hits."""
    _ref_id, chunk_ids = _seed(store, embedded=False)
    body = _handler(store).search(q=_Q, mode="lexical").body
    assert "no paper blocks match" not in body
    assert any(_found_chunk_handles(body, chunk_ids))


def test_unscoped_multiword_hybrid_finds_spread_terms(store: Store) -> None:
    """gr614169 through fused: the ``lex_any`` OR leg fills the empty
    AND leg corpus-wide, so the query's lexical leg stops degrading
    hybrid to semantic-only."""
    _ref_id, chunk_ids = _seed(store, embedded=False)
    body = _handler(store).search(q=_Q).body
    assert "no paper blocks match" not in body
    assert any(_found_chunk_handles(body, chunk_ids))


def test_unscoped_two_word_keeps_strict_and(store: Store) -> None:
    """The unscoped retry starts at three words: a one- or two-word
    keyword query's partial match is noise more often than signal, so
    an AND-empty two-word answer stays empty."""
    _seed(store, embedded=False)
    body = _handler(store).search(q="AMOC weakening", mode="lexical").body
    assert "no paper blocks match" in body


def test_unscoped_operator_query_never_or_retries(store: Store) -> None:
    """``-term`` / ``"phrase"`` are intents the plainto OR retry would
    invert, so an operator query keeps strict-AND semantics: an empty
    AND answer stays empty."""
    _seed(store, embedded=False)
    body = (
        _handler(store)
        .search(q="AMOC -strengthening weakening response regional MCB", mode="lexical")
        .body
    )
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


def test_unscoped_and_matches_keep_strict_results(store: Store) -> None:
    """gr614169: unscoped the EXISTS probe sees the strict leg's hits,
    so no OR leg is built — a one-term-only chunk stays OUT, exactly as
    scoped."""
    e = _embedder(store)
    ref = store.insert_ref(
        kind="paper", slug="and-paper-unscoped", title="AND probe", year=2024
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
    body = _handler(store).search(q="AMOC weakening MCB", mode="lexical").body
    assert and_hit in body
    assert or_only not in body


def _seed_and_plus_or(store: Store, *, slug: str = "and-page-paper") -> tuple[int, int]:
    """One strict-AND chunk plus a one-term-only decoy — a strict
    universe of exactly one, with an OR-only row behind it."""
    ref = store.insert_ref(kind="paper", slug=slug, title="AND paging probe", year=2024)
    rows = store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text="a paragraph about weakening only"),
            ChunkInsert(ord=1, text="AMOC weakening under MCB forcing, in one chunk."),
        ],
    )
    return ref.id, rows[1].id


def test_scoped_page_past_strict_hits_stays_empty(store: Store) -> None:
    """td615185 regression: the OR trigger is strict-universe emptiness,
    not page emptiness. Page 2 sits past the one strict hit — an OR
    retry there would return the decoy page 1's strict leg excluded."""
    ref_id, and_hit = _seed_and_plus_or(store)
    page1 = store.chunks.search_chunks_lexical(
        q="AMOC weakening MCB", scope_ref_id=ref_id, limit=1
    )
    assert [b.id for b, _r, _s in page1] == [and_hit]
    page2 = store.chunks.search_chunks_lexical(
        q="AMOC weakening MCB", scope_ref_id=ref_id, limit=1, offset=1
    )
    assert page2 == []


def test_unscoped_page_past_strict_hits_stays_empty(store: Store) -> None:
    """Same regression corpus-wide: a page past the strict hits probes
    the strict universe (no LIMIT/OFFSET) and stays empty — the
    three-word query earns the OR retry, but only on a truly empty leg."""
    _ref_id, and_hit = _seed_and_plus_or(store, slug="and-page-unscoped")
    page1 = store.chunks.search_chunks_lexical(q="AMOC weakening MCB", limit=1)
    assert [b.id for b, _r, _s in page1] == [and_hit]
    page2 = store.chunks.search_chunks_lexical(
        q="AMOC weakening MCB", limit=1, offset=1
    )
    assert page2 == []


def test_scoped_count_mirrors_the_or_retry(store: Store) -> None:
    """``count_chunks_lexical`` is the "N of K" denominator for the leg
    the search actually ran — it must count the OR universe when the
    strict AND universe is empty, not report "N of 0" over real hits."""
    ref_id, _ = _seed(store, embedded=False)
    scoped = store.chunks.count_chunks_lexical(q=_Q, kind="paper", scope_ref_id=ref_id)
    assert scoped == 2


def test_unscoped_count_mirrors_the_or_retry(store: Store) -> None:
    """gr614169: same universe mirroring without a scope — the AND-empty
    count retries OR-ed corpus-wide."""
    _seed(store, embedded=False)
    assert store.chunks.count_chunks_lexical(q=_Q, kind="paper") == 2
