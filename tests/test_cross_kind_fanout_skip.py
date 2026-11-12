"""gr628278: the cross-kind fan-out fits the ``search/-`` tool-call
budget — its deadline is capped under ``TOOL_LATENCY_BUDGET_MS``, a
non-production embedder's hash-noise semantic leg is skipped up front,
and a kind whose ANN leg can only take the global-index route runs
lexical-only (named in a footer) instead of dying at the per-kind
statement cap."""

from __future__ import annotations

from typing import Any

import pytest

from precis.embedder import MockEmbedder
from precis.runtime import search as search_mod
from precis.runtime.core import PrecisRuntime
from precis.store import Store


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
