"""gr514956: the rendered weak-match fold for memory search — weak
uncorroborated hits hide behind ``more(cursor=…)`` instead of listing,
``view='index'`` (the no-human-reads recall path) hard-cuts the same
rows, and kinds absent from ``SEMANTIC_ONLY_FOLD_DISTANCE`` never fold.

Distances are exact: a fixed-vector embedder plus stored unit-vector
embeddings at known cosine offsets, the same trick as
``test_search_fanout_honesty.py``.
"""

from __future__ import annotations

import math
import re

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.memory import MemoryHandler
from precis.runtime.core import PrecisRuntime
from precis.store import (
    SEMANTIC_ONLY_FOLD_DISTANCE,
    ChunkInsert,
    Store,
)


def _unit_vec(dim: int, pairs: list[tuple[int, float]]) -> list[float]:
    v = [0.0] * dim
    for i, x in pairs:
        v[i] = x
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class _FixedEmbedder:
    """``embed_one`` → one fixed vector, so every stored embedding's
    cosine distance to the query is set by the fixture, not the model."""

    def __init__(self, vec: list[float]) -> None:
        self._vec = list(vec)

    @property
    def dim(self) -> int:
        return len(self._vec)

    @property
    def model(self) -> str:
        return "fixed"

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [list(self._vec) for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return list(self._vec)

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def unload(self) -> None:
        pass


def _seed_corpus(store: Store) -> dict[str, int]:
    """Four memory refs at fixed distances from the query vector.

    ``q`` below is ``alpha beta`` — a two-word query, so the partial-term
    (OR) probe counts as corroboration. Returns ref ids by role.
    """
    dim = store.embedding_dim()
    near = _unit_vec(dim, [(0, 0.9), (1, math.sqrt(0.19))])  # distance ≈ 0.1
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])  # distance ≈ 0.5

    strong = store.insert_ref(kind="memory", slug=None, title="strong node")
    store.chunks.insert_chunks(
        strong.id,
        [ChunkInsert(ord=0, text="prose about quokkas entirely", embedding=near)],
    )
    weak = store.insert_ref(kind="memory", slug=None, title="weak node")
    store.chunks.insert_chunks(
        weak.id,
        [
            ChunkInsert(
                ord=0,
                text="prose about wombats entirely unrelated",
                embedding=far,
            )
        ],
    )
    corroborated = store.insert_ref(kind="memory", slug=None, title="corroborated node")
    store.chunks.insert_chunks(
        corroborated.id,
        [
            ChunkInsert(
                ord=0,
                # one OR-term only — the strict-AND leg misses this.
                text="alpha appears here without the other term",
                embedding=far,
            )
        ],
    )
    lex_only = store.insert_ref(kind="memory", slug=None, title="lex node")
    store.chunks.insert_chunks(
        lex_only.id,
        # both terms, no embedding — lex leg only, no distance at all.
        [ChunkInsert(ord=0, text="alpha and beta both appear here")],
    )
    return {
        "strong": strong.id,
        "weak": weak.id,
        "corroborated": corroborated.id,
        "lex_only": lex_only.id,
    }


def _handler(store: Store) -> MemoryHandler:
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    return MemoryHandler(hub=Hub(store=store, embedder=_FixedEmbedder(q_vec)))


def test_mixed_distances_fold_only_weak_uncorroborated(store: Store) -> None:
    """sem-only hits past 0.42 fold; near, OR-corroborated and lex-only
    hits all stay — and shown hits render ``(0.10)``-style distances."""
    ids = _seed_corpus(store)
    body = _handler(store).search(q="alpha beta").body

    # the headline counts shown hits — folded rows live in the hint.
    assert "# 3 of 4 memory matches for 'alpha beta'" in body
    assert f"memory {ids['strong']}" in body
    assert f"memory {ids['corroborated']}" in body
    assert f"memory {ids['lex_only']}" in body
    assert f"memory {ids['weak']}" not in body
    assert "(0.10)" in body
    # the lex-only hit has no semantic distance — rank= stays.
    assert "(rank=" in body
    assert "── weak matches below (distance > 0.42): 1 more, more(cursor=" in body
    assert body.rstrip().endswith("──")


def test_or_only_lexical_match_counts_as_corroboration(store: Store) -> None:
    """The OR probe marks a partial-term hit corroborated at the store
    level — the pin for what the render fold and the hard cut share."""
    ids = _seed_corpus(store)
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    info: dict[int, tuple[float | None, bool]] = {}
    hits = store.chunks.search_chunks(
        q="alpha beta",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
        fold_info_out=info,
    )
    by_ref = {ref.id: info[block.id] for block, ref, _ in hits}
    sem_d, corroborated = by_ref[ids["corroborated"]]
    assert corroborated is True
    assert sem_d == pytest.approx(0.5, abs=0.01)
    assert by_ref[ids["weak"]][1] is False
    assert by_ref[ids["lex_only"]][0] is None


def test_more_cursor_returns_the_folded_rows(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On a long-lived runtime the hint's cursor is real and drains the
    folded rows verbatim through ``fetch_more``."""
    ids = _seed_corpus(store)
    rt = runtime_with_store
    rt.long_lived = True
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    monkeypatch.setattr(rt.hub, "embedder", _FixedEmbedder(q_vec))

    body = rt.dispatch("search", {"kind": "memory", "q": "alpha beta"})
    assert "__fold_tail__" not in body
    m = re.search(r"more\(cursor='([0-9a-f]+)'\)", body)
    assert m is not None
    page, is_error = rt.fetch_more(m.group(1))
    assert not is_error
    assert f"memory {ids['weak']}" in page
    assert "wombats" in page


def test_short_lived_runtime_unwraps_the_tail_inline(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A one-shot runtime can't redeem a cursor — the folded rows append
    under the unwrapped ``below`` hint instead (gr267466's rule)."""
    ids = _seed_corpus(store)
    rt = runtime_with_store
    assert not rt.long_lived
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    monkeypatch.setattr(rt.hub, "embedder", _FixedEmbedder(q_vec))

    body = rt.dispatch("search", {"kind": "memory", "q": "alpha beta"})
    assert "more(cursor=" not in body
    assert "__fold_tail__" not in body
    assert "── weak matches below (distance > 0.42): 1 more, below ──" in body
    # the appended tail starts on its own line — no "below ──me123" glue.
    assert f"below ──\n## memory {ids['weak']}" in body


def test_all_weak_prints_no_strong_match(store: Store) -> None:
    """Nothing under the bar → ``no strong match`` plus the same hint."""
    dim = store.embedding_dim()
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])
    ref = store.insert_ref(kind="memory", slug=None, title="only weak")
    store.chunks.insert_chunks(
        ref.id,
        [ChunkInsert(ord=0, text="unrelated prose entirely", embedding=far)],
    )
    body = _handler(store).search(q="alpha beta").body
    assert "no strong match" in body
    assert "weak matches below (distance > 0.42): 1 more" in body
    assert "of 1 memory match" not in body


def test_index_view_hard_cuts_without_hint(store: Store) -> None:
    """``view='index'`` — the recall path — drops weak uncorroborated
    hits outright: no hint line, no cursor, no folded tail."""
    ids = _seed_corpus(store)
    body = _handler(store).search(q="alpha beta", view="index").body
    assert f"me{ids['strong']}" in body
    assert f"me{ids['corroborated']}" in body
    # the lex-only row has no distance to cut on — it is kept.
    assert f"me{ids['lex_only']}" in body
    assert f"me{ids['weak']}" not in body
    assert "weak matches" not in body
    assert "more(cursor=" not in body


def test_gripe_never_folds(store: Store) -> None:
    """Kinds absent from the fold table render every hit the old way —
    a weak uncorroborated gripe hit still lists with ``(rank=…)``.
    (Papers can't fold either: the fold only exists on the
    ``search_body_chunks`` path, which paper never takes — the table is
    the assertion that pins it.)"""
    assert SEMANTIC_ONLY_FOLD_DISTANCE == {"memory": 0.42}
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    gh = GripeHandler(hub=Hub(store=store, embedder=_FixedEmbedder(q_vec)))
    # ``put`` files the STATUS tag the gripe invariant requires; the
    # chunk lands unembedded, so pin the distance directly.
    gh.put(text="unrelated gripe prose entirely")
    ref_id = next(
        r.id
        for r in store.list_refs(kind="gripe")
        if r.title == "unrelated gripe prose entirely"
    )
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])
    for chunk in store.chunks.list_chunks_for_ref(ref_id):
        store.chunks.update_chunk_embedding(chunk.id, far)

    body = gh.search(q="alpha beta").body
    assert f"gripe {ref_id}" in body
    assert "(rank=" in body
    assert "weak matches" not in body


def test_cross_kind_stream_hard_cuts(store: Store) -> None:
    """``search_hits`` (the cross-kind merge) drops the weak memory hit —
    the merge is machine-read, so no fold, just the cut."""
    ids = _seed_corpus(store)
    hits = _handler(store).search_hits(q="alpha beta")
    hit_ids = {h.ref_id for h in hits}
    assert ids["weak"] not in hit_ids
    assert ids["strong"] in hit_ids
    assert ids["corroborated"] in hit_ids


def test_compact_view_enumerates_weak_hits(store: Store) -> None:
    """The compact listing is the complete id enumeration (gr514477) —
    weak hits list beside the strong ones, no fold, no hint. Memory's
    public ``search()`` refuses ``view='ids'`` today, so this calls the
    body-search seam directly: the pin is for any folding kind that
    accepts the compact view."""
    ids = _seed_corpus(store)
    resp = _handler(store)._search_body_chunks(
        q="alpha beta", tags=None, page_size=10, compact="ids"
    )
    for rid in ids.values():
        assert f"me{rid}" in resp.body
    assert "weak matches" not in resp.body
    assert "more(cursor=" not in resp.body
    assert resp.fold_tail is None


def test_boundary_distance_kept_by_both_paths(store: Store) -> None:
    """At the 0.42 boundary the render fold and the recall hard cut
    agree: ``d <= 0.42`` keeps everywhere, ``d > 0.42`` weakens
    everywhere. The pair sits a milliunit either side — both render
    ``(0.42)`` — close enough that a ``<``/``>`` disagreement on either
    path would show up as a kept-vs-cut split."""
    dim = store.embedding_dim()

    def at(c: float) -> list[float]:
        return _unit_vec(dim, [(0, c), (1, math.sqrt(1 - c * c))])

    just_under = at(0.581)  # d ≈ 0.419
    just_over = at(0.579)  # d ≈ 0.421
    under = store.insert_ref(kind="memory", slug=None, title="under boundary")
    store.chunks.insert_chunks(
        under.id,
        [ChunkInsert(ord=0, text="unrelated prose entirely", embedding=just_under)],
    )
    over = store.insert_ref(kind="memory", slug=None, title="over boundary")
    store.chunks.insert_chunks(
        over.id,
        [ChunkInsert(ord=0, text="unrelated prose entirely", embedding=just_over)],
    )
    handler = _handler(store)

    body = handler.search(q="alpha beta").body
    assert f"memory {under.id}" in body
    assert "(0.42)" in body
    assert f"memory {over.id}" not in body
    assert "weak matches below (distance > 0.42): 1 more" in body

    index = handler.search(q="alpha beta", view="index").body
    assert f"me{under.id}" in index
    assert f"me{over.id}" not in index


def test_fused_order_unchanged_by_fold_info(store: Store) -> None:
    """``fold_info_out`` is a read-only probe: requesting it must not
    change which rows the fused query returns or their order."""
    _seed_corpus(store)
    dim = store.embedding_dim()
    q_vec = _unit_vec(dim, [(0, 1.0)])
    plain = store.chunks.search_chunks(
        q="alpha beta",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
    )
    info: dict[int, tuple[float | None, bool]] = {}
    probed = store.chunks.search_chunks(
        q="alpha beta",
        query_vec=q_vec,
        kind="memory",
        limit=10,
        max_distance=0.65,
        fold_info_out=info,
    )
    assert [block.id for block, _, _ in plain] == [block.id for block, _, _ in probed]
    assert len(info) == len(probed)


def test_fold_tail_count_and_drain_order(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three weak hits fold in fused order — the hint counts them and
    the drained page replays nearest-first."""
    dim = store.embedding_dim()
    weak_ids = []
    for i, first in enumerate([0.45, 0.40, 0.36]):  # d = 0.55 / 0.60 / 0.64
        ref = store.insert_ref(kind="memory", slug=None, title=f"weak {i}")
        store.chunks.insert_chunks(
            ref.id,
            [
                ChunkInsert(
                    ord=0,
                    text=f"unrelated prose number {i} entirely",
                    embedding=_unit_vec(
                        dim, [(0, first), (1, math.sqrt(1 - first**2))]
                    ),
                )
            ],
        )
        weak_ids.append(ref.id)
    strong = store.insert_ref(kind="memory", slug=None, title="strong")
    store.chunks.insert_chunks(
        strong.id,
        [
            ChunkInsert(
                ord=0,
                text="prose about quokkas entirely",
                embedding=_unit_vec(dim, [(0, 0.9), (1, math.sqrt(0.19))]),
            )
        ],
    )
    rt = runtime_with_store
    rt.long_lived = True
    q_vec = _unit_vec(dim, [(0, 1.0)])
    monkeypatch.setattr(rt.hub, "embedder", _FixedEmbedder(q_vec))

    body = rt.dispatch("search", {"kind": "memory", "q": "alpha beta"})
    assert "# 1 of 4 memory match for 'alpha beta'" in body
    assert "weak matches below (distance > 0.42): 3 more" in body
    m = re.search(r"more\(cursor='([0-9a-f]+)'\)", body)
    assert m is not None
    page, is_error = rt.fetch_more(m.group(1))
    assert not is_error
    positions = [page.index(f"## memory {rid}") for rid in weak_ids]
    assert positions == sorted(positions)


def test_or_probe_splits_on_nonword(store: Store) -> None:
    """``cloud-brightening`` is two terms, not one: the OR probe counts a
    hit on either side of a hyphen, so the partial-term match is
    corroborated and the far hit stays on the page."""
    from precis.store._chunks_ops import _or_corroborates

    assert _or_corroborates("cloud-brightening")
    assert _or_corroborates("alpha beta")
    assert not _or_corroborates("alpha")
    assert not _or_corroborates("alpha_beta")  # \w joins it into one term

    dim = store.embedding_dim()
    far = _unit_vec(dim, [(0, 0.5), (1, math.sqrt(0.75))])
    ref = store.insert_ref(kind="memory", slug=None, title="wombat node")
    store.chunks.insert_chunks(
        ref.id,
        # strict AND ('quokka' & 'wombat') misses; the OR probe hits.
        [ChunkInsert(ord=0, text="wombats roam the fields", embedding=far)],
    )
    body = _handler(store).search(q="quokka-wombat").body
    assert f"memory {ref.id}" in body
    assert "(0.50)" in body
