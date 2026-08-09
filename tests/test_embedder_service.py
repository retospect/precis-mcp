"""End-to-end embedder service ↔ client contract test.

Boots the real :class:`EmbedderService` on an ephemeral loopback port
with a :class:`MockEmbedder` (no torch, no weights) and drives it with
:class:`RemoteEmbedder` over the *default* urllib transport — so the
JSON wire, the HTTP routes, and the boundary check are all exercised
together. This is the CI contract test embedder-as-service calls for: the two
sides cannot drift without it going red.
"""

from __future__ import annotations

import json
import threading
import time
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
        service.stop_probe()
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
    status, body = _get(service_url + "/readyz")
    assert status == 200  # mock warms instantly
    # §F cycle b: /readyz is now JSON with a `state` field (a plain
    # status-code check — the watchdog, capability_probe — still works
    # unchanged; this pins the richer body for the state-aware readers).
    payload = json.loads(body)
    assert payload["state"] == "loaded"


def test_metrics_endpoint(service_url: str) -> None:
    client = RemoteEmbedder(service_url, expected_dim=_DIM)
    client.embed(["x"])
    status, body = _get(service_url + "/metrics")
    assert status == 200
    assert "precis_embedder_embed_total" in body
    # §F cycle b residency signals.
    assert "precis_embedder_loaded 1" in body
    assert "precis_embedder_last_activity_age_seconds" in body


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

        def unload(self) -> None:
            pass

    probe = WarmProbe()
    service = EmbedderService(probe, revision="t", max_inflight=4, warm=True)
    # Warm thread is daemonised; give it a moment to run.
    assert service._ready.wait(timeout=2.0)
    assert probe.warmup_calls == 1
    assert probe.embed_calls == 0
    service.stop_probe()


class _TogglableEmbedder:
    """Fake ``Embedder`` whose ``embed()`` can be flipped between
    succeeding, raising, and hanging — for driving the background
    self-probe (gripe 51394).
    """

    dim = 4
    model = "toggle"

    def __init__(self) -> None:
        self.mode = "ok"  # "ok" | "raise" | "hang"
        self.calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.mode == "raise":
            raise RuntimeError("embedder wedged")
        if self.mode == "hang":
            time.sleep(10)
        return [[0.0] * self.dim for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return [0.0] * self.dim

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def unload(self) -> None:
        pass


def test_self_probe_flips_ready_false_then_recovers() -> None:
    """Regression for gripe 51394 ('embedder health signals lie'):
    ``_ready`` used to be a set-once-never-cleared latch, so /readyz
    stayed 200 forever even once real embeds started failing. The
    background self-probe must clear ``_ready`` after
    ``probe_fail_threshold`` consecutive failures, and set it again
    on the next success.
    """
    embedder = _TogglableEmbedder()
    service = EmbedderService(
        embedder,
        revision="t",
        max_inflight=4,
        warm=True,
        # Long interval: drive the probe synchronously via
        # ``_run_probe_once`` instead of racing the background loop.
        probe_interval_s=1000.0,
        probe_timeout_s=0.5,
        probe_fail_threshold=2,
    )
    try:
        assert service._ready.wait(timeout=2.0)  # warmup succeeded

        embedder.mode = "raise"
        service._run_probe_once()
        assert service.ready is True  # 1 failure < threshold(2)
        service._run_probe_once()
        assert service.ready is False  # 2 consecutive failures -> unready

        embedder.mode = "ok"
        service._run_probe_once()
        assert service.ready is True  # recovers on next success
    finally:
        service.stop_probe()


def test_self_probe_detects_hung_embed_without_blocking() -> None:
    """A wedged embedder that never returns (not just one that raises)
    must also flip readiness off — the probe encode runs in its own
    thread with a timeout, so a hang doesn't block the probe loop.
    """
    embedder = _TogglableEmbedder()
    service = EmbedderService(
        embedder,
        revision="t",
        max_inflight=4,
        warm=True,
        probe_interval_s=1000.0,
        probe_timeout_s=0.2,
        probe_fail_threshold=1,
    )
    try:
        assert service._ready.wait(timeout=2.0)

        embedder.mode = "hang"
        start = time.monotonic()
        service._run_probe_once()
        elapsed = time.monotonic() - start

        assert elapsed < 2.0  # did not wait out the 10s hang
        assert service.ready is False
    finally:
        service.stop_probe()


def test_probe_does_not_pile_up_threads_on_wedge() -> None:
    """Pre-ship review fix: a genuinely wedged encode holds
    ``_encode_lock`` forever, so a NEW probe-encode thread spawned on
    every tick would pile up unboundedly. At most one probe-encode
    thread may be outstanding at a time — a tick that finds the
    previous one still alive must record a failure without spawning
    another.
    """
    embedder = _TogglableEmbedder()
    service = EmbedderService(
        embedder,
        revision="t",
        max_inflight=4,
        warm=True,
        probe_interval_s=1000.0,
        probe_timeout_s=0.2,
        probe_fail_threshold=1,
    )
    try:
        assert service._ready.wait(timeout=2.0)
        embedder.mode = "hang"

        service._run_probe_once()  # tick 1: spawns + detects hang
        assert service.ready is False
        first_thread = service._probe_encode_thread
        assert first_thread is not None and first_thread.is_alive()

        service._run_probe_once()  # tick 2: previous still hung
        # Bounded to at most one outstanding probe-encode thread —
        # tick 2 must NOT have spawned a second one. Compare thread
        # *identity* (not a process-wide name count, which other
        # tests' abandoned hung-probe threads can pollute since this
        # suite runs single-process).
        assert service._probe_encode_thread is first_thread
        assert embedder.calls == 1  # only the first thread ever called embed()
    finally:
        service.stop_probe()


def test_probe_treats_lock_contention_as_healthy_not_a_failure() -> None:
    """Pre-ship review fix: a healthy-but-busy embedder legitimately
    holds ``_encode_lock`` for a large real batch. The probe must
    distinguish that from a wedge — failing to acquire the lock within
    the timeout means real traffic is actively encoding, so it must
    NOT count as a failure or flip ``/readyz`` unready.
    """
    embedder = _TogglableEmbedder()
    service = EmbedderService(
        embedder,
        revision="t",
        max_inflight=4,
        warm=True,
        probe_interval_s=1000.0,
        probe_timeout_s=0.2,
        probe_fail_threshold=1,  # even 1 tick would flip it if miscounted
    )
    try:
        assert service._ready.wait(timeout=2.0)

        # Simulate a legitimate slow real embed holding the lock —
        # not calling embedder.embed() at all, just holding the lock
        # the way EmbedderService.embed() would while encoding.
        release = threading.Event()

        def _hold_lock() -> None:
            with service._encode_lock:
                release.wait(timeout=2.0)

        holder = threading.Thread(target=_hold_lock, daemon=True)
        holder.start()
        time.sleep(0.05)  # let the holder grab the lock first

        try:
            service._run_probe_once()
            assert service.ready is True  # contention, not a failure
            service._run_probe_once()
            assert service.ready is True  # still fine on a second tick
        finally:
            release.set()
            holder.join(timeout=2.0)

        # Lock is free again — a normal probe still succeeds.
        service._run_probe_once()
        assert service.ready is True
        assert embedder.calls == 1  # only the post-release probe ever encoded
    finally:
        service.stop_probe()
