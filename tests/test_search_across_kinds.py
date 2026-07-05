"""Cross-kind chunk search — the unified-item-view Slice-2 primitive.

``Store.search_chunks_across_kinds`` searches the chunks of a *set* of
kinds at once (semantic + lexical, RRF-fused), collapses to one best
chunk per ref, bounds by ``refs.created_at``, and orders by relevance
or recency. These tests pin the cross-kind reach, the per-ref collapse,
the date window, and the recency sort — all with the deterministic
``MockEmbedder`` so vectors are reproducible.
"""

from __future__ import annotations

from datetime import UTC, datetime

from precis.embedder import MockEmbedder
from precis.store import BlockInsert, Store, Tag


def _seed(store: Store, kind: str, slug: str, blocks: list[str], emb: MockEmbedder):
    ref = store.insert_ref(kind=kind, slug=slug, title=slug)
    store.insert_blocks(
        ref.id,
        [
            BlockInsert(pos=i, text=t, embedding=emb.embed_one(t))
            for i, t in enumerate(blocks)
        ],
    )
    return ref.id


def test_reaches_multiple_kinds_and_collapses_per_ref(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    pid = _seed(
        store,
        "paper",
        "paper-mof",
        ["MOF adsorbents capture carbon dioxide.", "MOF pore size tuning for CO2."],
        emb,
    )
    wid = _seed(store, "web", "web-mof", ["A blog on MOF carbon dioxide capture."], emb)
    # A third kind that should NOT appear when we scope to paper+web.
    _seed(store, "pres", "pres-mof", ["slide: MOF carbon dioxide idea."], emb)

    hits = store.search_chunks_across_kinds(
        kinds=["paper", "web"],
        q="MOF carbon dioxide capture",
        query_vec=emb.embed_one("MOF carbon dioxide capture"),
        max_distance=None,
    )
    got_refs = {ref.id for _, ref, _ in hits}
    assert pid in got_refs
    assert wid in got_refs
    assert all(ref.kind in ("paper", "web") for _, ref, _ in hits)
    # Per-ref collapse: the paper has two matching chunks but contributes
    # exactly one row (its best chunk).
    assert sum(1 for _, ref, _ in hits if ref.id == pid) == 1


def test_kind_scope_excludes_unlisted_kinds(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    pid = _seed(store, "paper", "p1", ["nitrate reduction on copper"], emb)
    _seed(store, "web", "w1", ["nitrate reduction on copper"], emb)

    hits = store.search_chunks_across_kinds(
        kinds=["paper"],
        q="nitrate copper",
        query_vec=emb.embed_one("nitrate copper"),
        max_distance=None,
    )
    assert {ref.id for _, ref, _ in hits} == {pid}


def test_recency_sort_orders_newest_first(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    text = "graphene field-effect transistor mobility"
    older = _seed(store, "paper", "older", [text], emb)
    newer = _seed(store, "web", "newer", [text], emb)  # inserted later → newer

    hits = store.search_chunks_across_kinds(
        kinds=["paper", "web"],
        q="graphene transistor mobility",
        query_vec=emb.embed_one("graphene transistor mobility"),
        sort="recency",
        max_distance=None,
    )
    ordered = [ref.id for _, ref, _ in hits]
    assert ordered[:2] == [newer, older]


def test_date_window_bounds_results(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    _seed(store, "paper", "d1", ["perovskite solar cell efficiency"], emb)
    qv = emb.embed_one("perovskite solar")

    # Far-past floor keeps everything; far-future floor drops everything.
    assert store.search_chunks_across_kinds(
        kinds=["paper"],
        q="perovskite solar",
        query_vec=qv,
        max_distance=None,
        since=datetime(2000, 1, 1, tzinfo=UTC),
    )
    assert not store.search_chunks_across_kinds(
        kinds=["paper"],
        q="perovskite solar",
        query_vec=qv,
        max_distance=None,
        since=datetime(2999, 1, 1, tzinfo=UTC),
    )
    # Far-past ceiling drops everything (created_at is "now").
    assert not store.search_chunks_across_kinds(
        kinds=["paper"],
        q="perovskite solar",
        query_vec=qv,
        max_distance=None,
        until=datetime(2000, 1, 1, tzinfo=UTC),
    )


def test_lexical_only_when_no_vector(store: Store) -> None:
    """No query_vec → lexical leg alone still answers (embedder-down path)."""
    emb = MockEmbedder(dim=store.embedding_dim())
    pid = _seed(store, "paper", "lex", ["thermoelectric figure of merit ZT"], emb)
    hits = store.search_chunks_across_kinds(kinds=["paper"], q="thermoelectric ZT")
    assert pid in {ref.id for _, ref, _ in hits}


def test_empty_kinds_returns_empty(store: Store) -> None:
    assert store.search_chunks_across_kinds(kinds=[], q="anything") == []


def test_recent_refs_newest_first_and_kind_scoped(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="rr-a", title="A")
    b = store.insert_ref(kind="web", slug="rr-b", title="B")  # inserted later
    c = store.insert_ref(kind="memory", slug=None, title="C")  # unlisted kind

    got = store.recent_refs(["paper", "web"], limit=10)
    ids = [r.id for r in got]
    assert ids[:2] == [b.id, a.id]  # newest first
    assert c.id not in ids
    assert store.recent_refs([], limit=10) == []


def test_ref_tags_bulk(store: Store) -> None:
    a = store.insert_ref(kind="paper", slug="tb-a", title="A")
    b = store.insert_ref(kind="paper", slug="tb-b", title="B")  # untagged
    store.add_tag(a.id, Tag.open("topic-x"))
    store.add_tag(a.id, Tag.closed("PRIO", "high"))

    got = store.ref_tags_bulk([a.id, b.id])
    assert set(got[a.id]) == {("OPEN", "topic-x"), ("PRIO", "high")}
    assert b.id not in got  # untagged refs are simply absent
    assert store.ref_tags_bulk([]) == {}


def test_refs_with_body_chunks(store: Store) -> None:
    emb = MockEmbedder(dim=store.embedding_dim())
    ingested = _seed(store, "paper", "has-chunks", ["some body text"], emb)
    stub = store.insert_ref(kind="paper", slug="no-chunks", title="stub")

    got = store.refs_with_body_chunks([ingested, stub.id])
    assert got == {ingested}
    assert store.refs_with_body_chunks([]) == set()
