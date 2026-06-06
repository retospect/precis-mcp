"""Shared wire schema for the embedder service.

Single source of truth imported by **both** the HTTP client
(:class:`precis.embedder.RemoteEmbedder`) and the service
(``precis serve-embeddings``). Keeping the request/response shapes here
means the two sides cannot drift.

Deliberately dependency-light — stdlib dataclasses + plain dict
(de)serialisation, no pydantic, no torch. The serve/worker images that
carry the client must stay tiny (ADR 0021), so nothing heavy is
importable from this module.

JSON is the v1 transport. ``msgpack`` for the float payload is a
possible later optimisation (a 32-chunk batch is ~128 KB of float32);
it would be a new field/format negotiated via the ``version`` constant
below, not a breaking change.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

#: Bumped when the wire shape changes incompatibly. Client and service
#: compare this on ``/model`` so a version skew fails loud.
WIRE_VERSION = "1"

#: Default port the embedder service binds (loopback). Overridable via
#: ``PRECIS_EMBEDDER_PORT`` on the service side and the URL on the
#: client side.
DEFAULT_PORT = 8181

# Route paths — referenced by both sides so a typo can't desync them.
PATH_EMBED = "/embed"
PATH_MODEL = "/model"
PATH_HEALTH = "/healthz"
PATH_READY = "/readyz"
PATH_METRICS = "/metrics"


def _require(obj: Mapping[str, Any], key: str, typ: type) -> Any:
    """Fetch ``key`` from ``obj`` asserting it is present and of ``typ``.

    Raises ``ValueError`` with a precise message — used on both the
    service (rejecting a bad request) and the client (rejecting a bad
    response), so malformed payloads fail at the boundary rather than
    deep in the encoder.
    """
    if key not in obj:
        raise ValueError(f"missing required field {key!r}")
    val = obj[key]
    if not isinstance(val, typ):
        raise ValueError(
            f"field {key!r} must be {typ.__name__}, got {type(val).__name__}"
        )
    return val


@dataclass(frozen=True)
class EmbedRequest:
    """A batch embed request: a list of texts to encode."""

    texts: list[str]
    normalize: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {"texts": list(self.texts), "normalize": self.normalize}

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> EmbedRequest:
        texts = _require(obj, "texts", list)
        for i, t in enumerate(texts):
            if not isinstance(t, str):
                raise ValueError(f"texts[{i}] must be str, got {type(t).__name__}")
        normalize = obj.get("normalize", True)
        if not isinstance(normalize, bool):
            raise ValueError("field 'normalize' must be bool")
        return cls(texts=list(texts), normalize=normalize)


@dataclass(frozen=True)
class EmbedResponse:
    """A batch embed response: one vector per input text.

    The ``len(vectors) == len(request.texts)`` contract is the caller's
    invariant (the store maps chunks↔vectors positionally); the client
    asserts it on receipt.
    """

    model: str
    dim: int
    vectors: list[list[float]]

    def to_dict(self) -> dict[str, Any]:
        return {"model": self.model, "dim": self.dim, "vectors": self.vectors}

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> EmbedResponse:
        model = _require(obj, "model", str)
        dim = _require(obj, "dim", int)
        vectors = _require(obj, "vectors", list)
        out: list[list[float]] = []
        for i, vec in enumerate(vectors):
            if not isinstance(vec, list):
                raise ValueError(f"vectors[{i}] must be a list")
            out.append([float(x) for x in vec])
        return cls(model=model, dim=dim, vectors=out)


@dataclass(frozen=True)
class ModelInfo:
    """Identity of the model the service is serving.

    The client asserts ``dim`` and ``model`` against the corpus's
    embedder-table contract before its first encode (ADR 0020) so a
    wrong/upgraded model fails loud instead of silently writing
    incompatible vectors.
    """

    model: str
    dim: int
    revision: str | None = None
    wire_version: str = WIRE_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "dim": self.dim,
            "revision": self.revision,
            "wire_version": self.wire_version,
        }

    @classmethod
    def from_dict(cls, obj: Mapping[str, Any]) -> ModelInfo:
        model = _require(obj, "model", str)
        dim = _require(obj, "dim", int)
        revision = obj.get("revision")
        if revision is not None and not isinstance(revision, str):
            raise ValueError("field 'revision' must be str or null")
        wire_version = obj.get("wire_version", WIRE_VERSION)
        if not isinstance(wire_version, str):
            raise ValueError("field 'wire_version' must be str")
        return cls(model=model, dim=dim, revision=revision, wire_version=wire_version)


__all__ = [
    "DEFAULT_PORT",
    "PATH_EMBED",
    "PATH_HEALTH",
    "PATH_METRICS",
    "PATH_MODEL",
    "PATH_READY",
    "WIRE_VERSION",
    "EmbedRequest",
    "EmbedResponse",
    "ModelInfo",
]
