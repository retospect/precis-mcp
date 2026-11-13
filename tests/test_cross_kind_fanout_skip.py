"""gr628278: the cross-kind fan-out fits the ``search/-`` tool-call
budget — its deadline is capped under ``TOOL_LATENCY_BUDGET_MS``, a
non-production embedder's hash-noise semantic leg is skipped up front,
and a kind whose ANN leg can only take the global-index route runs
lexical-only (named in a footer) instead of dying at the per-kind
statement cap.

td649559: the ANN route probe and the fan-out share one call deadline —
the probe's pool checkout and gate read draw from the budget instead of
running unbounded before the fan-out's clock starts."""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.runtime import search as search_mod
from precis.runtime.core import PrecisRuntime
from precis.store import Store
from precis.store.pool import PrecisPool
from tests._timing import budget


class _ProductionStubEmbedder(MockEmbedder):
    """Deterministic vectors flagged production — a real backend
    stand-in without the network/torch dependency (same shape as
    test_mcp_critic_regressions' stub)."""

    @property
    def is_production(self) -> bool:
        return True


def _record_invoke(
    monkeypatch: pytest.MonkeyPatch, rt: PrecisRuntime
) -> dict[str, dict[str, Any]]:
    """Stub the per-kind invoke; record the kwargs each kind was handed."""
    seen: dict[str, dict[str, Any]] = {}

    def fake(self: Any, handler: Any, kind: str, kw: dict[str, Any]) -> list:
        seen[kind] = dict(kw)
        return []

    monkeypatch.setattr(type(rt), "_cross_kind_invoke_search_hits", fake)
    return seen


def _spy_embeds(monkeypatch: pytest.MonkeyPatch, rt: PrecisRuntime) -> list[str]:
    """Wrap the hub embedder's ``embed_one``; return the embedded texts."""
    emb = rt.hub.embedder
    assert emb is not None
    texts: list[str] = []
    orig = emb.embed_one

    def spy(text: str) -> list[float]:
        texts.append(text)
        return orig(text)

    monkeypatch.setattr(emb, "embed_one", spy)
    return texts


def test_fanout_deadline_fits_the_tool_call_budget() -> None:
    """``search/-`` reports against ``TOOL_LATENCY_BUDGET_MS`` (15 s);
    the fan-out deadline plus the embed/merge headroom must sit under
    it — the 30 s deadline could never fit (search/- p50 was 17.5 s)."""
    from precis.health_checks import TOOL_LATENCY_BUDGET_MS

    assert search_mod.CROSS_KIND_DEADLINE_S < TOOL_LATENCY_BUDGET_MS / 1000
    assert search_mod.CROSS_KIND_PER_KIND_S < search_mod.CROSS_KIND_DEADLINE_S


def test_mock_embedder_skips_the_semantic_leg_up_front(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a non-production embedder the fan-out forces lexical for
    every kind: the query is never embedded, no ``query_vec`` reaches a
    handler, and the marker says the leg was skipped — no ANN scan pays
    for a ranking that can only be hash noise."""
    rt = runtime_with_store
    q = "gr628278uniquemarker"
    embedded = _spy_embeds(monkeypatch, rt)
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("*", {"q": q})

    assert q not in embedded
    assert seen, "every fanned kind should still run"
    assert all(kw.get("mode") == "lexical" for kw in seen.values())
    assert all("query_vec" not in kw for kw in seen.values())
    assert "non-production" in out.body
    assert "semantic leg was skipped" in out.body


def test_mock_embedder_honours_explicit_semantic_mode(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``mode='semantic'`` is caller intent — the leg runs even when its
    vector is mock noise."""
    rt = runtime_with_store
    q = "gr628278uniquemarker"
    embedded = _spy_embeds(monkeypatch, rt)
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("*", {"q": q, "mode": "semantic"})

    assert q in embedded
    assert all(kw.get("mode") == "semantic" for kw in seen.values())
    assert "semantic leg was skipped" not in out.body


def test_global_fallback_kind_gets_lexical_leg_and_is_named(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Wildcard under a production embedder: a ``_ANN_PARTIAL_KINDS``
    member whose index is missing/unready (none are built in the test
    DB) routes its ANN leg over the global index — the 10-30 s scan —
    so it is forced lexical up front and named in a footer, while
    ``paper`` (a designated global-index kind) and the small kinds keep
    the full hybrid legs."""
    from precis.store._chunks_ops import _ANN_PARTIAL_KINDS

    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("*", {"q": "gr628278uniquemarker"})

    skipped = set(seen) & set(_ANN_PARTIAL_KINDS)
    assert skipped, "the test DB builds no partial indexes — fallback expected"
    assert all(seen[k].get("mode") == "lexical" for k in skipped)
    kept = set(seen) - skipped
    assert kept and all(seen[k].get("mode") != "lexical" for k in kept)
    assert all("query_vec" in seen[k] for k in kept)
    assert "semantic leg skipped for" in out.body
    for k in sorted(skipped):
        assert k in out.body


def test_explicit_kind_list_keeps_the_slow_kind_on_full_legs(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit comma-list is caller intent: ``memory`` (a partial-
    index kind with no index in the test DB) keeps its hybrid legs —
    the per-kind statement cap remains the honest backstop there."""
    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    seen = _record_invoke(monkeypatch, rt)

    out = rt._dispatch_cross_kind("paper,memory", {"q": "gr628278uniquemarker"})

    assert "memory" in seen
    assert seen["memory"].get("mode") != "lexical"
    assert "query_vec" in seen["memory"]
    assert "semantic leg skipped for" not in out.body


def test_mock_embedder_keeps_an_explicit_list_on_full_legs(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An explicit comma-list is caller intent — even under a
    non-production embedder the named kinds keep their legs (the up-front
    skip is the wildcard default expansion's optimization, same gate the
    domain fence uses)."""
    rt = runtime_with_store
    seen = _record_invoke(monkeypatch, rt)

    rt._dispatch_cross_kind("paper,memory", {"q": "gr628278uniquemarker"})

    assert seen
    assert all(kw.get("mode") != "lexical" for kw in seen.values())
    assert all("query_vec" in kw for kw in seen.values())


def test_probe_and_fan_out_share_one_call_deadline(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """td649559: ``_dispatch_cross_kind`` hands the probe and the fan-out
    the same deadline instant — the probe can no longer spend an
    unbounded prefix before the fan-out's clock starts."""
    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    seen_deadlines: dict[str, float] = {}

    def probe(self: Any, kinds: list[str], deadline_at: float) -> list[str]:
        seen_deadlines["probe"] = deadline_at
        return []

    def fanout(
        self: Any,
        kinds: list[str],
        base_kwargs: dict[str, Any],
        kind_overrides: dict[str, dict[str, Any]] | None = None,
        *,
        deadline_at: float | None = None,
        prioritize: bool = False,
    ) -> dict[str, Any]:
        seen_deadlines["fanout"] = deadline_at if deadline_at is not None else -1.0
        return {}

    monkeypatch.setattr(type(rt), "_cross_kind_slow_semantic_kinds", probe)
    monkeypatch.setattr(type(rt), "_cross_kind_fan_out", fanout)

    t0 = time.monotonic()
    rt._dispatch_cross_kind("*", {"q": "gr628278uniquemarker"})

    assert seen_deadlines["probe"] == seen_deadlines["fanout"]
    assert t0 <= seen_deadlines["probe"] <= t0 + search_mod.CROSS_KIND_DEADLINE_S + 5.0


def test_stalled_probe_sql_dies_at_the_shared_deadline(
    runtime_with_store: PrecisRuntime,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """td649559: a stalled gate read used to block the dispatch thread
    for as long as the statement ran — past the tool latency budget,
    before the fan-out even started. The probe's statement timeout is
    now the remaining call budget: Postgres cancels the stall at the
    deadline, the probe fails open (no leg skip is claimed), and the
    call returns inside the budget — the kinds honestly report their
    TimeoutError once the probe has eaten the clock."""
    import precis.store._chunks_ops as chunks_ops
    from precis.errors import Internal

    rt = runtime_with_store
    monkeypatch.setattr(
        rt.hub,
        "embedder",
        _ProductionStubEmbedder(dim=store.embedding_dim()),
    )
    monkeypatch.setattr(search_mod, "CROSS_KIND_DEADLINE_S", 0.8)
    # The route gate read is replaced by an unconditional server-side
    # stall; a stale cached gate state would skip the SQL entirely.
    monkeypatch.setattr(
        chunks_ops, "_ANN_STATE_SQL", "SELECT pg_sleep(30) /* probe-stall-test */"
    )
    chunks_ops._reset_ann_state_cache()

    t0 = time.perf_counter()
    with pytest.raises(Internal, match="cross-kind search failed in every kind"):
        rt._dispatch_cross_kind("*", {"q": "gr628278uniquemarker"})
    elapsed = time.perf_counter() - t0

    assert elapsed < budget(5.0), f"stalled probe ate the call: {elapsed:.1f}s"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE query LIKE %s",
            ("%probe-stall-test%",),
        ).fetchone()
    assert row is not None and row[0] == 0, "stalled probe still running on the server"


def test_probe_with_no_remaining_budget_fails_open_without_the_pool(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A probe asked for after the deadline is already gone fails open
    without touching the pool — the fan-out keeps whatever is left.
    (Unbounded ``None`` timeouts recorded here belong to other threads'
    checkouts — only a bounded one could be this probe's.)"""
    rt = runtime_with_store
    checkouts: list[float | None] = []
    orig = PrecisPool.connection

    @contextmanager
    def spy(self: Any, timeout: float | None = None) -> Iterator[Any]:
        checkouts.append(timeout)
        with orig(self, timeout=timeout) as conn:
            yield conn

    monkeypatch.setattr(PrecisPool, "connection", spy)

    out = rt._cross_kind_slow_semantic_kinds(["memory"], time.monotonic() - 0.01)

    assert out == []
    assert all(t is None for t in checkouts)


def test_probe_checkout_is_bounded_by_the_remaining_budget(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe's pool checkout waits at most the remaining call budget
    (td649559: an unbounded checkout under pool contention could spend
    the whole tool budget before the fan-out began). Unbounded ``None``
    checkouts belong to other threads and are ignored."""
    rt = runtime_with_store
    checkouts: list[float | None] = []
    orig = PrecisPool.connection

    @contextmanager
    def spy(self: Any, timeout: float | None = None) -> Iterator[Any]:
        checkouts.append(timeout)
        with orig(self, timeout=timeout) as conn:
            yield conn

    monkeypatch.setattr(PrecisPool, "connection", spy)

    out = rt._cross_kind_slow_semantic_kinds(["memory"], time.monotonic() + 5.0)

    # The test DB builds no partial indexes — memory's ANN leg routes
    # over the global index, so the probe names it.
    assert out == ["memory"]
    bounded = [t for t in checkouts if t is not None]
    assert bounded, "the probe should check out a connection with a bound"
    assert all(0 < t <= 5.0 for t in bounded)
