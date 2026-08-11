"""Idle-unload + lazy-reload of the embedder daemon (§F cycle b).

The master proposal's "residency is hysteretic" acceptance ("the model
is warm for a batch and released after") is met here NOT by the
worker spinning the daemon up/down, but by the ALREADY-standing daemon
elastically unloading/reloading its own model weights (the deliberate
amendment recorded in the round's spec) — see
:mod:`precis.embedder_service`'s module docstring.

Drives :class:`~precis.embedder_service.EmbedderService` directly
against a controllable fake ``Embedder`` + an injectable fake clock —
no real torch model, no real sleep — so these tests pin the residency
state machine (loaded -> idle -> reloaded) deterministically.
:meth:`~precis.embedder_service.EmbedderService._probe_tick` (one probe
iteration) is driven synchronously instead of waiting out
``probe_interval_s``/``idle_s`` for real, mirroring the existing
self-probe tests' ``_run_probe_once()`` pattern in
``test_embedder_service.py``.
"""

from __future__ import annotations

import threading
import time

import pytest

from precis.embedder_service import Busy, EmbedderService
from precis.errors import Upstream


class _FakeEmbedder:
    """A fake ``Embedder`` that counts warmup/unload/embed calls and
    behaves like ``BgeM3Embedder`` between ``unload()`` and the next
    ``warmup()``: unloaded, ``embed()`` raises (the real backend raises
    ``Upstream`` via ``_raise_if_warming``; the exact type doesn't
    matter here — only that an unloaded model can't silently serve)."""

    dim = 4
    model = "fake"

    def __init__(self) -> None:
        self.is_loaded = True
        self.warmup_calls = 0
        self.unload_calls = 0
        self.embed_calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        if not self.is_loaded:
            raise RuntimeError("fake embedder: not loaded — call warmup() first")
        return [[1.0, 2.0, 3.0, 4.0] for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        self.warmup_calls += 1
        self.is_loaded = True

    def unload(self) -> None:
        self.unload_calls += 1
        self.is_loaded = False


class _SlowWarmupEmbedder:
    """A fake ``Embedder`` whose ``warmup()`` blocks until the test
    releases it — lets a test hold the service in the "warming" state
    (``_ready`` unset) deterministically, to drive the boot-warmup race
    (review finding 1) without a real multi-second model load."""

    dim = 4
    model = "slow"

    def __init__(self) -> None:
        self._loaded = threading.Event()
        self.release_warmup = threading.Event()
        self.warmup_calls = 0
        self.embed_calls = 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.embed_calls += 1
        if not self._loaded.is_set():
            # Mirrors BgeM3Embedder._raise_if_warming's contract: the
            # RAW backend raises Upstream when asked to embed before
            # it's loaded. The exact wording differs; the TYPE is what
            # the HTTP handler's status mapping depends on.
            raise Upstream("embedder warming — test fake")
        return [[0.0, 0.0, 0.0, 0.0] for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]

    def is_ready(self) -> bool:
        return self._loaded.is_set()

    def warmup(self) -> None:
        self.warmup_calls += 1
        self.release_warmup.wait(timeout=5.0)
        self._loaded.set()

    def unload(self) -> None:
        self._loaded.clear()


class _FakeClock:
    """Injectable monotonic-like clock — advances only when told to."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, seconds: float) -> None:
        self.t += seconds


def _mk_service(
    *, idle_s: float = 100.0, clock: _FakeClock | None = None
) -> tuple[EmbedderService, _FakeEmbedder, _FakeClock]:
    clock = clock or _FakeClock()
    embedder = _FakeEmbedder()
    # warm=False: skip the background warm thread (nondeterministic
    # timing); the service is immediately ready+loaded, matching a
    # freshly-booted, already-warm daemon. The probe thread the ctor
    # also starts is never exercised here — ticks are driven directly
    # via ``_probe_tick()``.
    service = EmbedderService(
        embedder,
        max_inflight=4,
        warm=False,
        idle_s=idle_s,
        clock=clock,
    )
    return service, embedder, clock


@pytest.fixture
def _stop_probe():
    services: list[EmbedderService] = []
    yield services
    for s in services:
        s.stop_probe()


# ── idle-unload after idle_s ──────────────────────────────────────────


def test_idle_unload_after_idle_s_releases_the_model(_stop_probe) -> None:
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(150.0)  # past idle_s
    service._probe_tick()

    assert embedder.unload_calls == 1
    assert service.state() == "idle"
    assert service.ready is True  # idle still serves (see /readyz test)


def test_below_idle_s_does_not_unload(_stop_probe) -> None:
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(50.0)  # under idle_s
    service._probe_tick()

    assert embedder.unload_calls == 0
    assert service.state() == "loaded"


def test_activity_resets_the_idle_clock(_stop_probe) -> None:
    """A real ``/embed`` in between resets the clock — the model does
    NOT unload just because idle_s has elapsed since BOOT if there was
    recent activity."""
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(80.0)
    service.embed(["hello"])  # activity — resets the idle clock
    clock.advance(80.0)  # 80s since the embed, still < idle_s(100)
    service._probe_tick()

    assert embedder.unload_calls == 0
    assert service.state() == "loaded"


# ── idle_s == 0 -> never unload ────────────────────────────────────────


def test_idle_s_zero_never_unloads(_stop_probe) -> None:
    service, embedder, clock = _mk_service(idle_s=0.0)
    _stop_probe.append(service)

    clock.advance(10_000_000.0)  # absurdly long — still must not unload
    service._probe_tick()

    assert embedder.unload_calls == 0
    assert service.state() == "loaded"


# ── /readyz stays 200 while idle; lazy reload on the next /embed ────────


def test_readyz_reports_idle_state_but_stays_ready(_stop_probe) -> None:
    service, _embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(150.0)
    service._probe_tick()

    assert service.ready is True
    assert service.state() == "idle"


def test_embed_while_idle_reloads_and_returns_correct_vectors(_stop_probe) -> None:
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(150.0)
    service._probe_tick()
    assert service.state() == "idle"

    vectors = service.embed(["hello"])

    assert embedder.warmup_calls == 1
    assert service.state() == "loaded"
    assert vectors == [[1.0, 2.0, 3.0, 4.0]]


def test_second_embed_after_reload_does_not_reload_again(_stop_probe) -> None:
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(150.0)
    service._probe_tick()
    service.embed(["first"])
    assert embedder.warmup_calls == 1

    service.embed(["second"])

    assert embedder.warmup_calls == 1  # no second reload


def test_model_endpoint_answers_without_loading_while_idle(_stop_probe) -> None:
    """A worker boot / ``/model`` poll must not trigger a load — the
    metadata (name/dim) is static per backend, independent of whether
    the weights are currently resident."""
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(150.0)
    service._probe_tick()
    assert service.state() == "idle"

    info = service.model_info()

    assert info.model == "fake"
    assert info.dim == 4
    assert embedder.warmup_calls == 0  # /model never reloads


# ── self-probe must not wake an idle model ──────────────────────────────


def test_self_probe_skipped_while_idle(_stop_probe) -> None:
    """The probe tick's own self-probe encode must not run while idle —
    it would call the raw embedder directly (bypassing the lazy-reload
    path), reloading the model every tick and defeating idle-unload."""
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(150.0)
    service._probe_tick()  # this tick performs the unload itself
    assert embedder.unload_calls == 1
    embed_calls_after_unload = embedder.embed_calls

    # Further ticks while still idle must not touch the raw embedder at
    # all (no probe-encode, no reload) — the idle clock never resets.
    service._probe_tick()
    service._probe_tick()

    assert embedder.embed_calls == embed_calls_after_unload
    assert embedder.warmup_calls == 0
    assert service.state() == "idle"


# ── /metrics exposes residency signals ───────────────────────────────────


def test_metrics_expose_loaded_flag_and_activity_age(_stop_probe) -> None:
    service, _embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    rendered = service.render_metrics()
    assert "precis_embedder_loaded 1" in rendered
    assert "precis_embedder_last_activity_age_seconds" in rendered

    clock.advance(150.0)
    service._probe_tick()

    rendered = service.render_metrics()
    assert "precis_embedder_loaded 0" in rendered


# ── busy admission control still applies once loaded ─────────────────────


def test_busy_still_raised_when_loaded_and_over_capacity(_stop_probe) -> None:
    """Sanity check the idle-unload plumbing didn't disturb the
    pre-existing admission-control contract."""
    service, _embedder, _clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)
    service._sem = threading.BoundedSemaphore(0)  # force full

    with pytest.raises(Busy):
        service.embed(["x"])


# ── review finding 1 (HIGH): boot-warmup race ─────────────────────────────


def test_embed_during_boot_warmup_raises_and_never_double_warms(_stop_probe) -> None:
    """A request arriving while the background ``_warm()`` thread is
    still mid-load (``_ready`` unset) must get the SAME error the raw
    backend has always raised there, WITHOUT the request thread ever
    calling ``warmup()`` itself — a second concurrent
    ``SentenceTransformer`` construction racing the background thread's
    own, plus ``_loaded=True`` set while ``_ready`` stayed False (state()
    then disagreeing with reality), was the pre-fix bug.
    """
    embedder = _SlowWarmupEmbedder()
    service = EmbedderService(embedder, max_inflight=4, warm=True, idle_s=100.0)
    _stop_probe.append(service)
    try:
        # Wait for the background _warm thread to actually enter
        # warmup() (it blocks there until we release it below) so the
        # race window is genuinely open before we probe it.
        deadline = time.monotonic() + 2.0
        while embedder.warmup_calls == 0 and time.monotonic() < deadline:
            time.sleep(0.01)
        assert embedder.warmup_calls == 1  # the background thread, only

        assert service.state() == "warming"
        with pytest.raises(Upstream):
            service.embed(["hello"])

        # The request path must NOT have called warmup() itself.
        assert embedder.warmup_calls == 1
        # And must not have flipped _loaded out of step with _ready.
        assert service.state() == "warming"
    finally:
        embedder.release_warmup.set()
        assert service._ready.wait(timeout=2.0)

    assert service.state() == "loaded"
    assert embedder.warmup_calls == 1  # still just the one, ever


# ── review finding 2 (MEDIUM): unload must not race an in-flight encode ──


def test_idle_unload_skipped_while_encode_in_flight(_stop_probe) -> None:
    """A probe tick landing exactly when a real encode holds
    ``_encode_lock`` must skip the unload (non-blocking guard) rather
    than yank the model out from under the in-flight request; the next
    tick, once the encode has released the lock, unloads normally."""
    service, embedder, clock = _mk_service(idle_s=100.0)
    _stop_probe.append(service)

    clock.advance(150.0)  # past idle_s
    service._encode_lock.acquire()  # simulate an in-flight encode
    try:
        service._probe_tick()
        assert embedder.unload_calls == 0
        assert service.state() == "loaded"
    finally:
        service._encode_lock.release()

    service._probe_tick()

    assert embedder.unload_calls == 1
    assert service.state() == "idle"
