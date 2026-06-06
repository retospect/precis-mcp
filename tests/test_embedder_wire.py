"""Wire-schema contract tests for the embedder service.

Pins the (de)serialisation shapes shared by `RemoteEmbedder` and the
`precis serve-embeddings` service so the two sides cannot drift.
"""

from __future__ import annotations

import pytest

from precis.embedder_wire import (
    WIRE_VERSION,
    EmbedRequest,
    EmbedResponse,
    ModelInfo,
)


def test_embed_request_roundtrip() -> None:
    req = EmbedRequest(texts=["a", "b"], normalize=True)
    assert EmbedRequest.from_dict(req.to_dict()) == req


def test_embed_request_defaults_normalize_true() -> None:
    req = EmbedRequest.from_dict({"texts": ["x"]})
    assert req.normalize is True
    assert req.texts == ["x"]


def test_embed_request_rejects_missing_texts() -> None:
    with pytest.raises(ValueError, match="texts"):
        EmbedRequest.from_dict({})


def test_embed_request_rejects_non_str_text() -> None:
    with pytest.raises(ValueError, match=r"texts\[1\]"):
        EmbedRequest.from_dict({"texts": ["ok", 3]})


def test_embed_response_roundtrip() -> None:
    resp = EmbedResponse(model="bge-m3", dim=2, vectors=[[0.1, 0.2], [0.3, 0.4]])
    assert EmbedResponse.from_dict(resp.to_dict()) == resp


def test_embed_response_coerces_ints_to_float() -> None:
    resp = EmbedResponse.from_dict({"model": "m", "dim": 2, "vectors": [[1, 2]]})
    assert resp.vectors == [[1.0, 2.0]]
    assert all(isinstance(x, float) for x in resp.vectors[0])


def test_embed_response_rejects_bad_dim_type() -> None:
    with pytest.raises(ValueError, match="dim"):
        EmbedResponse.from_dict({"model": "m", "dim": "2", "vectors": []})


def test_model_info_roundtrip() -> None:
    info = ModelInfo(model="bge-m3", dim=1024, revision="abc123")
    back = ModelInfo.from_dict(info.to_dict())
    assert back == info
    assert back.wire_version == WIRE_VERSION


def test_model_info_revision_optional() -> None:
    info = ModelInfo.from_dict({"model": "bge-m3", "dim": 1024})
    assert info.revision is None


def test_model_info_rejects_bad_revision_type() -> None:
    with pytest.raises(ValueError, match="revision"):
        ModelInfo.from_dict({"model": "m", "dim": 1, "revision": 7})
