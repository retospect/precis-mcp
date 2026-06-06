"""`RemoteEmbedder` client contract tests.

Exercises the HTTP client's retry / endpoint-fallback / boundary-check
logic via an injected transport — no live server, no torch. The
transport seam is ``(method, url, json_body, timeout) -> (status,
dict)``; fakes here script responses by route + endpoint.
"""

from __future__ import annotations

import urllib.error

import pytest

from precis.embedder import Embedder, RemoteEmbedder, make_embedder
from precis.embedder_wire import (
    WIRE_VERSION,
    EmbedResponse,
    ModelInfo,
)

_DIM = 8


def _model_body(*, model: str = "bge-m3", dim: int = _DIM, wire: str = WIRE_VERSION):
    return ModelInfo(model=model, dim=dim, revision="rev0", wire_version=wire).to_dict()


def _embed_body(n_texts: int, *, model: str = "bge-m3", dim: int = _DIM):
    return EmbedResponse(
        model=model, dim=dim, vectors=[[0.0] * dim for _ in range(n_texts)]
    ).to_dict()


def _noop_sleep(_: float) -> None:
    return None


def _ok_transport():
    """A transport that answers /model and /embed correctly."""

    def transport(method, url, body, timeout):
        if url.endswith("/model"):
            return 200, _model_body()
        if url.endswith("/embed"):
            return 200, _embed_body(len(body["texts"]))
        return 404, {}

    return transport


# ── Protocol + happy path ───────────────────────────────────────────


def test_is_embedder_protocol() -> None:
    emb = RemoteEmbedder("http://x:1", transport=_ok_transport())
    assert isinstance(emb, Embedder)


def test_embed_returns_vectors() -> None:
    emb = RemoteEmbedder("http://x:1", expected_dim=_DIM, transport=_ok_transport())
    vecs = emb.embed(["a", "b", "c"])
    assert len(vecs) == 3
    assert all(len(v) == _DIM for v in vecs)


def test_embed_empty_is_no_network() -> None:
    def boom(*a, **k):  # pragma: no cover - must not be called
        raise AssertionError("transport should not be called for empty input")

    emb = RemoteEmbedder("http://x:1", transport=boom)
    assert emb.embed([]) == []


def test_dim_uses_expected_without_network() -> None:
    def boom(*a, **k):  # pragma: no cover
        raise AssertionError("dim must not hit the network when expected_dim set")

    emb = RemoteEmbedder("http://x:1", expected_dim=_DIM, transport=boom)
    assert emb.dim == _DIM


def test_model_property_fetches_model_info() -> None:
    emb = RemoteEmbedder("http://x:1", transport=_ok_transport())
    assert emb.model == "bge-m3"


# ── boundary checks ───────────────────────────────────────────────────


def test_dim_mismatch_raises() -> None:
    def transport(method, url, body, timeout):
        if url.endswith("/model"):
            return 200, _model_body(dim=999)
        return 200, _embed_body(len(body["texts"]))

    emb = RemoteEmbedder("http://x:1", expected_dim=_DIM, transport=transport)
    with pytest.raises(RuntimeError, match="dim 999 != corpus dim 8"):
        emb.embed(["a"])


def test_wire_version_skew_raises() -> None:
    def transport(method, url, body, timeout):
        return 200, _model_body(wire="999")

    emb = RemoteEmbedder("http://x:1", transport=transport)
    with pytest.raises(RuntimeError, match="wire version"):
        emb.model  # noqa: B018 - property access triggers fetch


def test_vector_count_mismatch_raises() -> None:
    def transport(method, url, body, timeout):
        if url.endswith("/model"):
            return 200, _model_body()
        return 200, _embed_body(1)  # always one vector regardless of input

    emb = RemoteEmbedder("http://x:1", expected_dim=_DIM, transport=transport)
    with pytest.raises(RuntimeError, match="1 vectors for 2 texts"):
        emb.embed(["a", "b"])


# ── retry / fallback ──────────────────────────────────────────────────


def test_retries_then_succeeds_on_429() -> None:
    calls = {"n": 0}

    def transport(method, url, body, timeout):
        if url.endswith("/model"):
            return 200, _model_body()
        calls["n"] += 1
        if calls["n"] < 3:
            return 429, {}
        return 200, _embed_body(len(body["texts"]))

    emb = RemoteEmbedder(
        "http://x:1", expected_dim=_DIM, transport=transport, sleep=_noop_sleep
    )
    assert len(emb.embed(["a"])) == 1
    assert calls["n"] == 3


def test_falls_back_to_second_endpoint() -> None:
    def transport(method, url, body, timeout):
        if url.startswith("http://dead:1"):
            raise urllib.error.URLError("connection refused")
        if url.endswith("/model"):
            return 200, _model_body()
        return 200, _embed_body(len(body["texts"]))

    emb = RemoteEmbedder(
        "http://dead:1,http://live:2",
        expected_dim=_DIM,
        transport=transport,
        sleep=_noop_sleep,
    )
    assert len(emb.embed(["a"])) == 1


def test_all_endpoints_down_raises() -> None:
    def transport(method, url, body, timeout):
        raise urllib.error.URLError("down")

    emb = RemoteEmbedder(
        "http://a:1,http://b:2",
        transport=transport,
        max_retries=1,
        sleep=_noop_sleep,
    )
    with pytest.raises(RuntimeError, match="all embedder endpoints failed"):
        emb.embed(["a"])


def test_non_retryable_4xx_returns_status() -> None:
    def transport(method, url, body, timeout):
        if url.endswith("/model"):
            return 200, _model_body()
        return 400, {}

    emb = RemoteEmbedder(
        "http://x:1", expected_dim=_DIM, transport=transport, sleep=_noop_sleep
    )
    with pytest.raises(RuntimeError, match="returned HTTP 400"):
        emb.embed(["a"])


# ── construction / factory ────────────────────────────────────────────


def test_empty_url_raises() -> None:
    with pytest.raises(ValueError, match="at least one URL"):
        RemoteEmbedder("   ")


def test_make_embedder_remote_requires_url() -> None:
    with pytest.raises(ValueError, match="requires a URL"):
        make_embedder("remote", dim=_DIM)


def test_make_embedder_remote_builds_client() -> None:
    emb = make_embedder("remote", dim=_DIM, url="http://x:1")
    assert isinstance(emb, RemoteEmbedder)
    assert emb.dim == _DIM


def test_make_embedder_unknown_name_raises() -> None:
    with pytest.raises(ValueError, match="unknown embedder name"):
        make_embedder("nope")
