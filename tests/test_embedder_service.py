"""End-to-end embedder service ↔ client contract test.

Boots the real :class:`EmbedderService` on an ephemeral loopback port
with a :class:`MockEmbedder` (no torch, no weights) and drives it with
:class:`RemoteEmbedder` over the *default* urllib transport — so the
JSON wire, the HTTP routes, and the boundary check are all exercised
together. This is the CI contract test ADR 0020 calls for: the two
sides cannot drift without it going red.
"""

from __future__ import annotations

import threading
import urllib.error
import urllib.request
from collections.abc import Iterator

import pytest

from precis.embedder import MockEmbedder, RemoteEmbedder
from precis.embedder_service import EmbedderService, make_server

_DIM = 32


@pytest.fixture
def service_url() -> Iterator[str]:
    embedder = MockEmbedder(dim=_DIM, model="mock")
    service = EmbedderService(embedder, revision="testrev", max_inflight=4, warm=True)
    httpd = make_server(service, host="127.0.0.1", port=0)
    port = httpd.server_address[1]
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


def _get(url: str) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def test_model_endpoint(service_url: str) -> None:
    client = RemoteEmbedder(service_url, expected_dim=_DIM)
    assert client.model == "mock"
    assert client.dim == _DIM


def test_embed_roundtrip(service_url: str) -> None:
    client = RemoteEmbedder(service_url, expected_dim=_DIM)
    vectors = client.embed(["alpha", "beta", "gamma"])
    assert len(vectors) == 3
    assert all(len(v) == _DIM for v in vectors)
    # MockEmbedder is deterministic: same text → same vector.
    again = client.embed(["alpha"])
    assert again[0] == vectors[0]


def test_healthz_and_readyz(service_url: str) -> None:
    status, body = _get(service_url + "/healthz")
    assert status == 200 and body == "ok"
    status, _ = _get(service_url + "/readyz")
    assert status == 200  # mock warms instantly


def test_metrics_endpoint(service_url: str) -> None:
    client = RemoteEmbedder(service_url, expected_dim=_DIM)
    client.embed(["x"])
    status, body = _get(service_url + "/metrics")
    assert status == 200
    assert "precis_embedder_embed_total" in body


def test_dim_boundary_check_against_live_service(service_url: str) -> None:
    # Client expects a different dim than the service serves → loud fail.
    client = RemoteEmbedder(service_url, expected_dim=_DIM + 1)
    with pytest.raises(RuntimeError, match="dim"):
        client.embed(["x"])


def test_unknown_route_404(service_url: str) -> None:
    status, _ = _get(service_url + "/nope")
    assert status == 404


def test_warm_thread_calls_warmup_not_embed() -> None:
    """Regression: the warm thread must call ``warmup()``, never the
    public ``embed()``. Going via ``embed`` makes the warm thread
    fast-fail on the very ``_raise_if_warming`` gate it's meant to
    clear, leaving the service permanently in 503 / "warming" state
    (the 2026-06-15 → 2026-06-16 production regression).

    Use a probe Embedder that distinguishes the two code paths and
    boot a service with ``warm=True``; assert ``warmup()`` was the
    one called and the ready flag was set.
    """

    class WarmProbe:
        dim = 4
        model = "probe"

        def __init__(self) -> None:
            self.embed_calls = 0
            self.warmup_calls = 0

        def embed(self, texts: list[str]) -> list[list[float]]:
            self.embed_calls += 1
            return [[0.0] * 4 for _ in texts]

        def embed_one(self, text: str) -> list[float]:
            return [0.0] * 4

        def is_ready(self) -> bool:
            return True

        def warmup(self) -> None:
            self.warmup_calls += 1

    probe = WarmProbe()
    service = EmbedderService(probe, revision="t", max_inflight=4, warm=True)
    # Warm thread is daemonised; give it a moment to run.
    assert service._ready.wait(timeout=2.0)
    assert probe.warmup_calls == 1
    assert probe.embed_calls == 0
