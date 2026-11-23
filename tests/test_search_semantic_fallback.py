"""gr714744: an explicit ``mode='semantic'`` single-kind search whose
query-embed leg fails runs one disclosed ``mode='hybrid'`` retry at the
dispatch boundary — the caller gets the lexical candidates, flagged as
degraded, instead of a bare ``Upstream`` refusal that reads as a dead
end for a dedup check.

``query_vec_for`` itself keeps raising (gripe #254606 — the loud leg is
what stops a silent zero-hit page reading as "no matches"); only the
agent-facing dispatch boundary converts the flagged error into the
degrade the error's own ``next=`` names.
"""

from __future__ import annotations

import pytest

from precis.embedder import EmbedderUnavailable, MockEmbedder
from precis.errors import Upstream
from precis.runtime.core import PrecisRuntime
from precis.store import ChunkInsert, Store


class _DownEmbedder:
    """Wired but always failing — the outage shape the gripe reports."""

    def embed_one(self, q: str) -> list[float]:
        raise RuntimeError("remote embed endpoint down")

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("remote embed endpoint down")


class _RecoveringEmbedder:
    def __init__(self, vector: list[float]) -> None:
        self.vector = vector
        self.calls = 0

    def embed_one(self, q: str) -> list[float]:
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("remote embed endpoint down")
        return self.vector


class _CapacityEmbedder:
    """Busy-but-alive: the bounded admission queue timed out (gr450123)."""

    def embed_one(self, q: str) -> list[float]:
        raise EmbedderUnavailable(
            "embedder at capacity (429 after queueing)",
            retry_after_s=2,
            last_status=429,
        )


def _seed_finding(store: Store, title: str) -> int:
    return store.insert_ref(kind="finding", slug=None, title=title, meta={}).id


def _search(rt: PrecisRuntime, **args: object) -> str:
    return rt.dispatch(
        "search",
        {
            "kind": "finding",
            "q": "graph-network ablation",
            "status": "*",
            "mode": "semantic",
            "page_size": 3,
            **args,
        },
    )


def test_semantic_embed_outage_falls_back_to_hybrid_once(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gripe's exact call: dedup candidates must come back, degraded
    and disclosed, where the bare refusal used to land."""
    rt = runtime_with_store
    monkeypatch.setattr(rt.hub, "embedder", _DownEmbedder())
    _seed_finding(store, "graph-network ablation analysis of the mixture")
    body = _search(rt)
    assert "[error:" not in body
    assert "graph-network ablation analysis of the mixture" in body
    assert "semantic leg skipped" in body
    assert "lexical-only" in body
    assert "not absence" in body


def test_semantic_embedder_recovers_on_hybrid_retry(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    title = "Particle rearrangement in supercooled liquids"
    vector = MockEmbedder(dim=store.embedding_dim()).embed_one(title)
    ref_id = _seed_finding(store, title)
    store.chunks.insert_chunks(
        ref_id,
        [
            ChunkInsert(
                ord=0, text=title, embedding=vector, meta={"chunk_kind": "finding_body"}
            )
        ],
    )
    rt = runtime_with_store
    assert title not in _search(rt, mode="lexical")

    embedder = _RecoveringEmbedder(vector)
    monkeypatch.setattr(rt.hub, "embedder", embedder)
    body = _search(rt)

    assert embedder.calls == 2
    assert title in body
    assert "query embedding recovered on the hybrid retry" in body
    assert "semantic leg skipped" not in body
    assert "lexical-only" not in body


def test_semantic_embed_outage_empty_page_still_discloses(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A degraded zero-hit page must not read as absence — the note
    lands under the 'no matches' line, not instead of it."""
    rt = runtime_with_store
    monkeypatch.setattr(rt.hub, "embedder", _DownEmbedder())
    body = _search(rt)
    assert "[error:" not in body
    assert "no finding matches" in body
    assert "semantic leg skipped" in body
    assert "not absence" in body


def test_capacity_retry_window_is_carried_into_the_note(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr450123's retry-after survives the fallback: the disclose names
    the concrete window the caller was told to wait."""
    rt = runtime_with_store
    monkeypatch.setattr(rt.hub, "embedder", _CapacityEmbedder())
    _seed_finding(store, "graph-network ablation analysis of the mixture")
    body = _search(rt)
    assert "[error:" not in body
    assert "semantic leg skipped" in body
    assert "~2 s" in body


def test_nested_args_mode_spelling_gets_the_same_fallback(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``args={'mode': 'semantic'}`` is the same request — dispatch
    hoists it into the same clean kwargs the top-level spelling uses."""
    rt = runtime_with_store
    monkeypatch.setattr(rt.hub, "embedder", _DownEmbedder())
    _seed_finding(store, "graph-network ablation analysis of the mixture")
    body = rt.dispatch(
        "search",
        {
            "kind": "finding",
            "q": "graph-network ablation",
            "status": "*",
            "__extras__": {"mode": "semantic"},
        },
    )
    assert "[error:" not in body
    assert "graph-network ablation analysis of the mixture" in body
    assert "semantic leg skipped" in body


def test_unflagged_upstream_is_not_retried(
    runtime_with_store: PrecisRuntime, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the semantic-embed flag selects the retry; an unrelated
    Upstream from inside a search surfaces exactly as before."""
    rt = runtime_with_store
    handler = rt.hub.handler_for("finding")
    assert handler is not None

    def _boom(**_kw: object) -> object:
        raise Upstream("some other upstream failed")

    monkeypatch.setattr(handler, "search", _boom)
    body = _search(rt)
    assert "[error:Upstream]" in body
    assert "some other upstream failed" in body
    assert "semantic leg skipped" not in body


def test_hybrid_mode_embed_outage_behaves_as_before(
    runtime_with_store: PrecisRuntime, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Control: mode='hybrid' already degraded silently; no retry note
    is invented for a call that never asked for the vector leg."""
    rt = runtime_with_store
    monkeypatch.setattr(rt.hub, "embedder", _DownEmbedder())
    _seed_finding(store, "graph-network ablation analysis of the mixture")
    body = _search(rt, mode="hybrid")
    assert "[error:" not in body
    assert "graph-network ablation analysis of the mixture" in body
    assert "semantic leg skipped" not in body
