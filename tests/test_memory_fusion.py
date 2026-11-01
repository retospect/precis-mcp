"""Memory recall fusion (gr548265, absorbs gr515867): semantic rank-1
rescue, band recency prior, gotcha quota — each on synthetic legs, each
flag off restoring the plain fused order, and the rescue and quota end to
end through ``search(kind='memory', view='index')``."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import pytest

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import ChunkInsert, Store
from precis.store._search_cut import GAP_CUT_DELTA
from precis.utils.memory_fusion import (
    QUOTA_FLAG,
    RECENCY_FLAG,
    RESCUE_DELTA,
    RESCUE_FLAG,
    FusionFlags,
    apply_quota,
    apply_recency,
    apply_rescue,
    rerank,
)
from precis.utils.memory_hubs import (
    BAND_FACTOR_ACTIVE,
    BAND_FACTOR_OLD,
    BAND_FACTOR_SETTLED,
    band_factors,
)

CUT = 0.42


@pytest.fixture(autouse=True)
def _default_gap_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the shared cut to its defaults: gap cut on, stock delta."""
    monkeypatch.delenv("PRECIS_SEARCH_FIXED_FLOORS", raising=False)
    monkeypatch.delenv("PRECIS_SEARCH_GAP_CUT", raising=False)


@dataclass(frozen=True)
class _B:
    id: int


@dataclass(frozen=True)
class _R:
    id: int


def _hits(n: int) -> list[tuple[_B, _R, float]]:
    """``n`` fused hits, ref i / block 100+i, scores strictly falling."""
    return [(_B(100 + i), _R(i), 1.0 - i / 100) for i in range(n)]


def _ids(hits: list[tuple[_B, _R, float]]) -> list[int]:
    return [r.id for _b, r, _s in hits]


def _both(n: int) -> dict[int, tuple[float | None, bool]]:
    """Every hit corroborated at distance 0.30."""
    return {100 + i: (0.30, True) for i in range(n)}


# ── rescue ────────────────────────────────────────────────────────────


def test_rescue_lifts_semantic_rank_one_into_top_k() -> None:
    # gr515867 shape: ref 7 is semantic rank 1 with no lexical match and
    # fuses below six both-leg hits; at k=5 it takes the last slot.
    hits = _hits(8)
    info = _both(8) | {107: (0.12, False)}
    out = apply_rescue(hits, info, k=5, cut=CUT)
    assert _ids(out)[:5] == [0, 1, 2, 3, 7]
    assert _ids(out)[5:] == [4, 5, 6]


def test_rescue_takes_near_ties_but_at_most_half_the_page() -> None:
    hits = _hits(10)
    near = 0.12 + RESCUE_DELTA / 2
    info = _both(10) | {107: (0.12, False), 108: (near, False), 109: (near, False)}
    out = apply_rescue(hits, info, k=4, cut=CUT)
    assert _ids(out)[:4] == [0, 1, 7, 8]
    assert sorted(_ids(out)) == list(range(10))


def test_rescue_skips_hits_past_the_cut() -> None:
    hits = _hits(8)
    past_cut = _both(8) | {107: (0.45, False)}
    assert _ids(apply_rescue(hits, past_cut, k=5, cut=CUT)) == list(range(8))


def test_rescue_window_is_the_gap_cut() -> None:
    # gr548264 landed: the window is best + GAP_CUT_DELTA, so a hit 0.10
    # behind rank 1 still shares the rescue; one past the gap does not.
    hits = _hits(8)
    inside = _both(8) | {100: (0.10, True), 107: (0.10 + GAP_CUT_DELTA - 0.01, False)}
    assert _ids(apply_rescue(hits, inside, k=5, cut=CUT))[:5] == [0, 1, 2, 3, 7]
    outside = _both(8) | {100: (0.10, True), 107: (0.10 + GAP_CUT_DELTA + 0.01, False)}
    assert _ids(apply_rescue(hits, outside, k=5, cut=CUT)) == list(range(8))


def test_fixed_floor_fallback_keeps_the_narrow_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_SEARCH_FIXED_FLOORS", "1")
    hits = _hits(8)
    far = _both(8) | {100: (0.10, True), 107: (0.10 + RESCUE_DELTA + 0.01, False)}
    assert _ids(apply_rescue(hits, far, k=5, cut=CUT)) == list(range(8))
    near = _both(8) | {100: (0.10, True), 107: (0.10 + RESCUE_DELTA - 0.01, False)}
    assert _ids(apply_rescue(hits, near, k=5, cut=CUT))[:5] == [0, 1, 2, 3, 7]


# ── recency ───────────────────────────────────────────────────────────


def test_recency_factor_reorders_close_scores_only() -> None:
    hits = [(_B(1), _R(1), 1.00), (_B(2), _R(2), 0.95), (_B(3), _R(3), 0.50)]
    factors = {1: BAND_FACTOR_OLD, 2: BAND_FACTOR_ACTIVE, 3: BAND_FACTOR_ACTIVE}
    assert _ids(apply_recency(hits, factors)) == [2, 1, 3]
    assert _ids(apply_recency(hits, {})) == [1, 2, 3]


def test_band_factors_from_touch_age(store: Store) -> None:
    ref = store.insert_ref(kind="memory", slug=None, title="aging node")
    now = datetime.now(UTC)
    assert band_factors(store, [ref.id], now=now) == {ref.id: BAND_FACTOR_ACTIVE}
    later = now + timedelta(days=30)
    assert band_factors(store, [ref.id], now=later) == {ref.id: BAND_FACTOR_SETTLED}
    much_later = now + timedelta(days=90)
    assert band_factors(store, [ref.id], now=much_later) == {ref.id: BAND_FACTOR_OLD}


# ── quota ─────────────────────────────────────────────────────────────


def test_quota_appends_best_gotcha_as_row_k_plus_one() -> None:
    hits = _hits(9)
    out, extra = apply_quota(hits, _both(9), k=5, cut=CUT, gotchas={7, 8})
    assert extra == 1
    assert _ids(out)[:6] == [0, 1, 2, 3, 4, 7]
    assert sorted(_ids(out)) == list(range(9))


def test_quota_noop_when_a_gotcha_made_the_page_or_none_clears() -> None:
    hits = _hits(9)
    assert apply_quota(hits, _both(9), k=5, cut=CUT, gotchas={2, 7})[1] == 0
    weak = _both(9) | {107: (0.50, False)}
    out, extra = apply_quota(hits, weak, k=5, cut=CUT, gotchas={7})
    assert (extra, _ids(out)) == (0, list(range(9)))


# ── flags ─────────────────────────────────────────────────────────────


def test_every_flag_off_restores_the_fused_order() -> None:
    hits = _hits(9)
    info = _both(9) | {108: (0.05, False)}
    factors = {8: 1.15, 0: 0.9}

    def run(flags: FusionFlags) -> tuple[list[tuple[_B, _R, float]], int]:
        return rerank(
            hits, info, k=5, cut=CUT, factors=factors, gotchas={7}, flags=flags
        )

    on, extra = run(FusionFlags())
    assert extra == 1 and _ids(on) != list(range(9))
    assert run(FusionFlags(rescue=False, recency=False, quota=False)) == (hits, 0)


def test_flags_read_env_default_on(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in (RESCUE_FLAG, RECENCY_FLAG, QUOTA_FLAG):
        monkeypatch.delenv(var, raising=False)
    assert FusionFlags.from_env() == FusionFlags(True, True, True)
    monkeypatch.setenv(RECENCY_FLAG, "0")
    assert FusionFlags.from_env() == FusionFlags(True, False, True)


# ── end to end through view='index' ───────────────────────────────────


def _unit_vec(dim: int, pairs: list[tuple[int, float]]) -> list[float]:
    v = [0.0] * dim
    for i, x in pairs:
        v[i] = x
    norm = math.sqrt(sum(x * x for x in v))
    return [x / norm for x in v]


class _FixedEmbedder:
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


def _bullets(body: str) -> list[str]:
    """The index rows, without any narrowing-hint tail."""
    return [ln for ln in body.splitlines() if ln.startswith("- ")]


def _handler(store: Store) -> MemoryHandler:
    q_vec = _unit_vec(store.embedding_dim(), [(0, 1.0)])
    return MemoryHandler(hub=Hub(store=store, embedder=_FixedEmbedder(q_vec)))


def test_index_view_rescues_semantic_only_rank_one(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    dim = store.embedding_dim()
    near = _unit_vec(dim, [(0, 0.9), (1, math.sqrt(0.19))])
    mid = _unit_vec(dim, [(0, 0.7), (1, math.sqrt(0.51))])
    for i in range(3):
        ref = store.insert_ref(kind="memory", slug=None, title=f"both legs {i}")
        store.chunks.insert_chunks(
            ref.id, [ChunkInsert(ord=0, text="alpha beta prose here", embedding=mid)]
        )
    rescued = store.insert_ref(kind="memory", slug=None, title="semantic only")
    store.chunks.insert_chunks(
        rescued.id,
        [ChunkInsert(ord=0, text="quokkas wander entirely", embedding=near)],
    )
    handle = f"(me{rescued.id})"
    monkeypatch.setenv(RESCUE_FLAG, "0")
    off = _bullets(
        _handler(store).search(q="alpha beta", view="index", page_size=2).body
    )
    assert len(off) == 2 and not any(handle in ln for ln in off)
    monkeypatch.setenv(RESCUE_FLAG, "1")
    on = _bullets(
        _handler(store).search(q="alpha beta", view="index", page_size=2).body
    )
    assert len(on) == 2
    assert handle in on[1]


def test_index_view_admits_one_gotcha_past_k(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = _handler(store)
    for i in range(3):
        handler.put(
            text="alpha beta alpha beta alpha beta thread state",
            title=f"Thread {i} alpha beta",
            tags=["section:threads"],
        )
    handler.put(
        text="alpha beta once, then a long gotcha body about something else "
        "that dilutes the term density of this chunk considerably",
        title="The gotcha",
        tags=["section:gotchas"],
    )
    monkeypatch.setenv(QUOTA_FLAG, "0")
    off = _bullets(handler.search(q="alpha beta", view="index", page_size=2).body)
    assert len(off) == 2 and not any("The gotcha" in ln for ln in off)
    monkeypatch.setenv(QUOTA_FLAG, "1")
    on = _bullets(handler.search(q="alpha beta", view="index", page_size=2).body)
    assert len(on) == 3
    assert "The gotcha" in on[2]
