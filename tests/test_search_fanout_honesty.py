"""gr513663: honest cross-kind search — majority-failure banner, the
semantic-only relevance floor, and the title-match pointer on empty
numeric-ref searches."""

from __future__ import annotations

import math

import pytest

from precis.runtime.core import PrecisRuntime
from precis.store import ChunkInsert, Store, Tag
from precis.utils.search_merge import SearchHit


def _hit(kind: str, title: str, ref_id: int, score: float = 0.5) -> SearchHit:
    return SearchHit(
        score=score, kind=kind, title=title, preview="p", ref_id=ref_id, slug=None
    )


def _unit_vec(dim: int, pairs: list[tuple[int, float]]) -> list[float]:
    v = [0.0] * dim
    for i, x in pairs:
        v[i] = x
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


def _patch_fan_out(
    monkeypatch: pytest.MonkeyPatch,
    rt: PrecisRuntime,
    kinds: list[str],
    results: dict[str, object],
) -> None:
    monkeypatch.setattr(type(rt), "_cross_kind_fan_out", lambda self, ks, kw: results)
    monkeypatch.setattr(
        type(rt), "_resolve_cross_kind_request", lambda self, kind: list(kinds)
    )


def test_majority_failure_leads_with_incomplete_banner(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """2 of 3 kinds timing out must not open with a confident ranking —
    the hits that did answer stay, under an incompleteness banner."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:3]
    _patch_fan_out(
        monkeypatch,
        rt,
        kinds,
        {
            kinds[0]: [_hit(kinds[0], "real hit", 1)],
            kinds[1]: TimeoutError("no answer within 30s"),
            kinds[2]: TimeoutError("no answer within 30s"),
        },
    )
    resp = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    first = resp.body.splitlines()[0]
    assert first.startswith("# ⚠ search incomplete")
    assert "2 of 3" in first
    assert "real hit" in resp.body


def test_minority_failure_keeps_ranked_headline(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """1 of 3 kinds failing is a footnote (the existing errored tip),
    not a banner."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:3]
    _patch_fan_out(
        monkeypatch,
        rt,
        kinds,
        {
            kinds[0]: [_hit(kinds[0], "real hit", 1)],
            kinds[1]: TimeoutError("no answer within 30s"),
            kinds[2]: [_hit(kinds[2], "another hit", 2)],
        },
    )
    resp = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    first = resp.body.splitlines()[0]
    assert "search incomplete" not in first
    assert "errored, omitted" in resp.body


def test_majority_failure_with_zero_hits_says_incomplete(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The empty case is where a confident 'no matches' lies hardest:
    most kinds never answered."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:3]
    _patch_fan_out(
        monkeypatch,
        rt,
        kinds,
        {
            kinds[0]: [],
            kinds[1]: TimeoutError("no answer within 30s"),
            kinds[2]: TimeoutError("no answer within 30s"),
        },
    )
    resp = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    assert "search incomplete" in resp.body
    assert "nothing matched" in resp.body
    assert "narrow by kind" in resp.body


def test_semantic_only_floor_drops_uncorroborated_tail(store: Store) -> None:
    """A chunk found ONLY by the semantic leg must beat the stricter
    floor; a lexically-corroborated hit is never floor-cut (gr513663:
    memory returned seawater-economics entries for an MCB query purely
    because they were the nearest embedded text)."""
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    # cosine distance ≈ 0.5 — inside the 0.65 corpus floor, outside 0.45.
    mid = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])
    # cosine distance ≈ 0.9 — beyond every floor; lex leg only.
    far = _unit_vec(dim, [(0, 0.1), (1, math.sqrt(0.99))])

    ref = store.insert_ref(kind="memory", slug=None, title="m")
    store.chunks.insert_chunks(
        ref.id,
        [
            ChunkInsert(ord=0, text="unrelated filler words only", embedding=mid),
            ChunkInsert(ord=1, text="frobnicate token present", embedding=far),
        ],
    )
    floored = store.chunks.search_chunks(
        q="frobnicate",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
        semantic_only_max_distance=0.45,
    )
    assert [b.text for b, _r, _s in floored] == ["frobnicate token present"]

    unfloored = store.chunks.search_chunks(
        q="frobnicate",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
    )
    assert {b.text for b, _r, _s in unfloored} == {
        "unrelated filler words only",
        "frobnicate token present",
    }


def test_semantic_only_floor_keeps_close_neighbours(store: Store) -> None:
    """The floor cuts the 'nearest available' tail, not genuine
    neighbours — a sem-only hit inside the bar still surfaces."""
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    close = _unit_vec(dim, [(0, 0.8), (1, math.sqrt(0.36))])  # distance ≈ 0.2

    ref = store.insert_ref(kind="memory", slug=None, title="m")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="no shared terms here", embedding=close)],
    )
    hits = store.chunks.search_chunks(
        q="frobnicate",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
        semantic_only_max_distance=0.45,
    )
    assert [b.text for b, _r, _s in hits] == ["no shared terms here"]


def test_empty_search_names_title_matched_refs(hub, store: Store) -> None:
    """gr513663: 'nothing in memory' must point at the quest/todo whose
    title matches instead of reading as 'nothing anywhere'."""
    from precis.handlers.memory import MemoryHandler

    store.insert_ref(
        kind="quest", slug=None, title="marine cloud brightening programme"
    )
    h = MemoryHandler(hub=hub)
    out = h.search(q="marine cloud brightening")
    assert "no memory entries match" in out.body
    assert "title-matched refs" in out.body
    assert "marine cloud brightening programme" in out.body


def test_empty_search_without_title_matches_omits_pointer(hub, store: Store) -> None:
    """No title match anywhere → the empty body stays exactly as before."""
    from precis.handlers.memory import MemoryHandler

    h = MemoryHandler(hub=hub)
    out = h.search(q="zzznolexicalhit")
    assert "no memory entries match" in out.body
    assert "title-matched refs" not in out.body


def test_boundary_half_failure_trips_banner(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Exactly 50% failing (1 of 2) is 'at least half' — the banner trips."""
    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:2]
    _patch_fan_out(
        monkeypatch,
        rt,
        kinds,
        {kinds[0]: [_hit(kinds[0], "real hit", 1)], kinds[1]: TimeoutError("t")},
    )
    resp = rt._dispatch_cross_kind("*", {"q": "dogfood", "mode": "lexical"})
    assert resp.body.splitlines()[0].startswith("# ⚠ search incomplete")
    assert "1 of 2" in resp.body.splitlines()[0]


def test_degraded_embedder_plus_majority_failure_says_incomplete(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Embedder down AND majority failure: the incompleteness wins the
    empty-body branch, with the degraded note attached."""
    from precis.errors import Upstream

    rt = runtime_with_store

    class _Down:
        def embed_one(self, q: str) -> list[float]:
            raise Upstream("embedder down")

    monkeypatch.setattr(rt.hub, "embedder", _Down())
    kinds = rt._cross_kind_kinds()[:3]
    _patch_fan_out(
        monkeypatch,
        rt,
        kinds,
        {kinds[0]: [], kinds[1]: TimeoutError("t"), kinds[2]: TimeoutError("t")},
    )
    resp = rt._dispatch_cross_kind("*", {"q": "dogfood"})
    assert "search incomplete" in resp.body
    assert "lexical-only" in resp.body


def test_min_start_budget_returns_timeout(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A kind dequeued inside the min-start budget reports the timeout
    without touching the DB (CROSS_KIND_MIN_START_MS early return)."""
    import time as _time

    from precis.runtime import search as search_mod

    rt = runtime_with_store
    kinds = rt._cross_kind_kinds()[:5]
    monkeypatch.setattr(search_mod, "CROSS_KIND_DEADLINE_S", 0.45)
    monkeypatch.setattr(search_mod, "CROSS_KIND_MIN_START_MS", 2000)
    # min_start = min(2000, 45) = 45ms. The first 4 kinds hold all 4
    # workers for 0.42 s; the 5th dequeues with ~30 ms left → early return.

    def fake(k: str) -> list:
        if k != kinds[4]:
            _time.sleep(0.42)
        return []

    monkeypatch.setattr(
        type(rt), "_cross_kind_invoke_search_hits", lambda self, h, k, kw: fake(k)
    )
    out = rt._cross_kind_fan_out(kinds, {"q": "x"})
    assert isinstance(out[kinds[4]], TimeoutError)
    assert "too late" in str(out[kinds[4]])


def test_pointer_skipped_under_explicit_scope(
    runtime_with_store: PrecisRuntime, store: Store
) -> None:
    """scope='body' excludes the title surface — the pointer must not
    name a title match the caller scoped out."""
    rt = runtime_with_store
    h = rt.hub.handler_for("memory")
    assert h is not None
    ref = store.insert_ref(
        kind="memory", slug=None, title="scopedterm lives in the title"
    )
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=0, text="body has no such term")]
    )
    out = h.search(q="scopedterm", mode="lexical", scope="body")
    assert "no memory entries match" in out.body
    assert "title-matched refs" not in out.body


def test_pointer_honours_caller_filters(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """include_ref_ids, exclude_ref_ids, tags and profile ownership all
    constrain what the pointer may name."""
    rt = runtime_with_store
    h = rt.hub.handler_for("memory")
    assert h is not None
    a = store.insert_ref(kind="quest", slug=None, title="marine cloud brightening")
    b = store.insert_ref(kind="todo", slug=None, title="marine cloud brightening")
    store.add_tag(b.id, Tag.open("topic-mcb"), set_by="system")

    named = h._title_match_pointer("marine cloud brightening")
    assert named and str(a.id) in named and str(b.id) in named
    only_a = h._title_match_pointer("marine cloud brightening", include_ref_ids=[a.id])
    assert only_a and str(a.id) in only_a and str(b.id) not in only_a
    not_a = h._title_match_pointer("marine cloud brightening", exclude_ref_ids=[a.id])
    assert not_a and str(a.id) not in not_a and str(b.id) in not_a
    tagged = h._title_match_pointer("marine cloud brightening", tags=["topic-mcb"])
    assert tagged and str(b.id) in tagged and str(a.id) not in tagged

    # owner-private profiles never surface to a different identified caller.
    from precis.users import hash_password

    store.create_web_user(login="alice", abbrev="al", password=hash_password("pw"))
    store.insert_ref(
        kind="profile",
        slug=None,
        title="marine cloud brightening",
        owner_login="alice",
    )
    monkeypatch.setattr("precis.profiles.acting_login", lambda _s: "bob")
    after = h._title_match_pointer("marine cloud brightening")
    assert after is not None and "alice" not in after


def test_pointer_drops_cleanly_when_probe_fails(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled/slow probe yields no pointer and no broken search."""
    from psycopg.errors import QueryCanceled

    rt = runtime_with_store
    h = rt.hub.handler_for("memory")
    assert h is not None
    store.insert_ref(kind="quest", slug=None, title="marine cloud brightening")

    monkeypatch.setattr(
        store,
        "search_refs_lexical",
        lambda **kw: (_ for _ in ()).throw(QueryCanceled("cancelled")),
    )
    out = h.search(q="marine cloud brightening", mode="lexical")
    assert "no memory entries match" in out.body
    assert "title-matched refs" not in out.body

    monkeypatch.setattr(
        store,
        "search_refs_lexical",
        lambda **kw: (_ for _ in ()).throw(RuntimeError("db gone")),
    )
    out = h.search(q="marine cloud brightening", mode="lexical")
    assert "no memory entries match" in out.body
    assert "title-matched refs" not in out.body
