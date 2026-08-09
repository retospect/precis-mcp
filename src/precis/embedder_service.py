"""HTTP embedding service — the server side of ADR 0020.

Wraps an in-process :class:`precis.embedder.BgeM3Embedder` (or any
`Embedder`) behind the wire schema in :mod:`precis.embedder_wire`, so
torch-free ``serve`` / ``worker`` processes can embed over HTTP via
:class:`precis.embedder.RemoteEmbedder`.

Deliberately stdlib-only (``http.server``): the embedder image's only
heavy dependency is ``sentence-transformers``; the service adds no web
framework on top. A ``ThreadingHTTPServer`` plus a bounded admission
semaphore gives backpressure — when the in-flight ceiling is hit, the
service returns ``429`` + ``Retry-After`` rather than queueing
unboundedly, and the client's backoff (ADR 0020) does the rest.

Endpoints (paths from :mod:`precis.embedder_wire`):

- ``GET  /healthz`` — process is up (always 200 once serving).
- ``GET  /readyz``  — the model CAN serve: loaded (200) or idle (200,
  ``state`` field says so — a lazy reload happens transparently on the
  next ``/embed``) or still warming/wedged (503).
- ``GET  /model``   — :class:`ModelInfo` (name, dim, revision, wire).
  Answers without loading (name/dim are static per backend).
- ``POST /embed``   — :class:`EmbedRequest` → :class:`EmbedResponse`.
  Triggers a synchronous lazy reload first if the model is idle.
- ``GET  /metrics`` — plaintext counters for scraping.

**Idle-unload (§F cycle b, ``docs/backlog/cluster-scheduling.md`` §F,
the "elastic residency" amendment).** The RAM/GPU an idle-but-resident
model holds is the scarce resource, not the process — so rather than the
worker spinning the whole daemon up/down per batch, the ALREADY-standing
daemon (still launchd/systemd + watchdog-supervised) elastically
unloads its own model weights after ``PRECIS_EMBEDDER_IDLE_S`` (default
1800s; ``0`` disables — the never-unload opt-out) of no ``POST /embed``
activity, and lazily reloads on the next one. The existing self-probe
thread (below) carries the idle check too — no second thread. The
self-probe itself must never wake an idle model (it would just reset
the idle clock every tick and the unload would never hold), so it
skips its own probe-encode while idle; ``/readyz`` staying 200 in that
state is what keeps the capability-probe's ``embedder`` resource_slots
row advertised regardless.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import TYPE_CHECKING

from precis.embedder_wire import (
    DEFAULT_PORT,
    PATH_EMBED,
    PATH_HEALTH,
    PATH_METRICS,
    PATH_MODEL,
    PATH_READY,
    EmbedRequest,
    EmbedResponse,
    ModelInfo,
)

if TYPE_CHECKING:
    from precis.embedder import Embedder

log = logging.getLogger(__name__)


class _Metrics:
    """Tiny thread-safe counter bag exposed at ``/metrics``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.requests = 0
        self.embeds = 0
        self.texts = 0
        self.rejected_429 = 0
        self.errors = 0
        self.inflight = 0

    def render(self) -> str:
        with self._lock:
            return (
                f"precis_embedder_requests_total {self.requests}\n"
                f"precis_embedder_embed_total {self.embeds}\n"
                f"precis_embedder_texts_total {self.texts}\n"
                f"precis_embedder_rejected_429_total {self.rejected_429}\n"
                f"precis_embedder_errors_total {self.errors}\n"
                f"precis_embedder_inflight {self.inflight}\n"
            )


class EmbedderService:
    """Holds the embedder, readiness state, backpressure, and metrics.

    Shared (by reference) with every request-handler instance the
    ``ThreadingHTTPServer`` spawns. The embedder is warmed on a
    background thread so ``/readyz`` stays 503 until the first encode
    succeeds — a load-balanced rollout can gate traffic on it.
    """

    def __init__(
        self,
        embedder: Embedder,
        *,
        revision: str | None = None,
        max_inflight: int = 4,
        warm: bool = True,
        probe_interval_s: float = 30.0,
        probe_timeout_s: float = 20.0,
        probe_fail_threshold: int = 2,
        idle_s: float = 1800.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._embedder = embedder
        self._revision = revision
        # Admission control: at most ``max_inflight`` concurrent embed
        # calls; beyond that callers get 429 + Retry-After.
        self._sem = threading.BoundedSemaphore(max_inflight)
        # Serialise actual encode calls — the underlying model is not
        # guaranteed thread-safe and a single GPU/MPS stream is the
        # bottleneck anyway.
        self._encode_lock = threading.Lock()
        self._ready = threading.Event()
        self.metrics = _Metrics()
        # Idle-unload (§F cycle b). ``idle_s <= 0`` disables — never
        # unload. ``clock`` is injectable (mirrors ``RemoteEmbedder``'s
        # ``sleep`` seam) so tests can fake elapsed time instead of
        # sleeping for real. ``_loaded`` tracks whether the underlying
        # backend currently holds weights; distinct from ``_ready``,
        # which the self-probe uses for "can serve at all" — a model can
        # be unloaded (idle) yet still ``ready`` (it serves via a
        # transparent lazy reload). ``_idle_lock`` serialises the
        # unload/reload transition; concurrent ``/embed`` callers block
        # on it during a reload, then proceed (max_inflight bounds them
        # once loaded).
        self._idle_s = idle_s
        self._clock = clock
        self._idle_lock = threading.Lock()
        self._loaded = not warm
        self._last_activity = clock()
        # Self-probe config (gripe 51394: "embedder health signals
        # lie" — ``_ready`` used to be a set-once-never-cleared latch,
        # so /readyz stayed 200 forever even after the embedder wedged
        # or every real embed started failing). A background thread
        # periodically performs a *real* tiny embed and flips
        # ``_ready`` off after consecutive failures/hangs, back on
        # after a subsequent success — so the signal reflects "I
        # actually embedded recently", not just "I embedded once at
        # boot".
        self._probe_interval_s = probe_interval_s
        self._probe_timeout_s = probe_timeout_s
        self._probe_fail_threshold = probe_fail_threshold
        self._probe_fail_count = 0
        self._probe_stop = threading.Event()
        self._probe_thread: threading.Thread | None = None
        # The most recently spawned probe-encode thread (Fix for
        # gripe 51394 review: at most ONE outstanding probe-encode
        # thread at a time — see ``_run_probe_once``).
        self._probe_encode_thread: threading.Thread | None = None
        if warm:
            threading.Thread(
                target=self._warm, name="embedder-warm", daemon=True
            ).start()
        else:
            self._ready.set()
            self._start_probe()

    @property
    def ready(self) -> bool:
        return self._ready.is_set()

    def _start_probe(self) -> None:
        """Start the background self-probe thread (idempotent).

        Publishes ``self._probe_thread`` only *after* ``start()``
        returns, so a concurrent ``stop_probe()`` (e.g. a test racing
        the warm thread) never observes a thread object that hasn't
        actually started yet — ``Thread.join()`` raises on that.
        """
        if self._probe_thread is not None:
            return
        thread = threading.Thread(
            target=self._probe_loop, name="embedder-probe", daemon=True
        )
        thread.start()
        self._probe_thread = thread

    def stop_probe(self, timeout: float = 5.0) -> None:
        """Signal the probe thread to stop and join it (best-effort)."""
        self._probe_stop.set()
        if self._probe_thread is not None:
            self._probe_thread.join(timeout=timeout)

    def _probe_loop(self) -> None:
        while not self._probe_stop.wait(self._probe_interval_s):
            self._probe_tick()

    def _probe_tick(self) -> None:
        """One iteration of the probe loop's periodic work: the idle
        check first, then (only if still loaded) the self-probe encode.
        Split out from :meth:`_probe_loop` so tests can drive a single
        tick synchronously against a fake clock instead of waiting out
        ``probe_interval_s`` for real."""
        self._maybe_idle_unload()
        if not self._loaded:
            # Idle: skip the self-probe entirely. It would call the RAW
            # embedder's ``embed()`` directly (bypassing this service's
            # own lazy-reload path), which on an unloaded BgeM3Embedder
            # raises ``Upstream("embedder warming")`` — a probe
            # "failure" that would eventually flip ``/readyz`` to 503
            # for a model that's merely idle by design, and would also
            # reload the model every tick, defeating the whole point of
            # idle-unload.
            return
        self._run_probe_once()

    def state(self) -> str:
        """One of ``"warming"`` / ``"loaded"`` / ``"idle"`` — surfaced on
        ``/readyz`` and ``/metrics``. Independent of :attr:`ready`: a
        self-probe-detected wedge clears ``ready`` (503) regardless of
        load state, while "idle" is still ``ready`` — the service CAN
        serve, via a transparent lazy reload on the next ``/embed``."""
        if not self._ready.is_set():
            return "warming"
        return "loaded" if self._loaded else "idle"

    def _idle_age_s(self) -> float:
        """Seconds since the last ``POST /embed`` (or boot, if none yet)."""
        return self._clock() - self._last_activity

    def _maybe_idle_unload(self) -> None:
        """Unload the model if idle for ``idle_s`` — no-op if disabled
        (``idle_s <= 0``), already idle, still warming (nothing loaded
        yet to release), or an encode is currently in flight.

        Encodes are serialised under ``_encode_lock`` (:meth:`embed`),
        so a non-blocking acquire of it is the guard against unloading
        mid-encode: an in-flight encode holds the lock, this tick's
        ``acquire(blocking=False)`` fails, and we skip — no busy-wait,
        no risk of yanking the model out from under a real request; the
        next probe tick (``probe_interval_s`` later) tries again.

        Lock ordering: ``_idle_lock`` first, then (while still holding
        it) a non-blocking try of ``_encode_lock`` — never the reverse.
        :meth:`embed` never nests the two: it takes ``_idle_lock``
        (inside ``_ensure_loaded_for_request``) and fully releases it
        BEFORE separately taking ``_encode_lock`` later in the same
        call — so there is no path that could deadlock against this
        method's ordering.
        """
        if self._idle_s <= 0:
            return
        with self._idle_lock:
            if not self._loaded or not self._ready.is_set():
                return
            if self._idle_age_s() < self._idle_s:
                return
            if not self._encode_lock.acquire(blocking=False):
                # A real encode is in flight right now — leave it alone;
                # try again next tick.
                return
            try:
                self._embedder.unload()
                self._loaded = False
                log.info(
                    "embedder idle-unload: released after %.0fs with no "
                    "/embed activity (idle_s=%.0f)",
                    self._idle_age_s(),
                    self._idle_s,
                )
            finally:
                self._encode_lock.release()

    def _ensure_loaded_for_request(self) -> None:
        """Synchronous lazy reload — but ONLY in the ready-but-unloaded
        ("idle") state. Called at the top of :meth:`embed`, before
        admission control — concurrent ``/embed`` callers arriving
        during a reload block on ``_idle_lock`` (not the admission
        semaphore) and proceed the instant it completes, rather than
        racing a second reload.

        Still-warming (cold boot, ``_ready`` not yet set) is
        deliberately left untouched: this method's job is the
        idle<->loaded transition, not "wait for or duplicate the
        initial boot warmup". The background ``_warm()`` thread owns
        that; if a request thread ALSO called ``self._embedder.warmup()``
        here during that window, two concurrent model loads would race
        (real backends: two competing SentenceTransformer
        constructions) and this method would set ``_loaded = True``
        while ``_ready`` was still False, so :meth:`state` would report
        "warming" while ``_loaded`` disagreed (review finding, §F cycle
        b). Returning immediately instead falls through to ``embed()``'s
        normal ``self._embedder.embed(texts)`` call under
        ``_encode_lock`` — EXACTLY the pre-idle-unload code path — so a
        request arriving during boot warmup gets precisely the error
        the raw backend has always raised there
        (``BgeM3Embedder._raise_if_warming`` -> ``Upstream``), mapped by
        the HTTP handler to the same status a client's backoff already
        tolerates (the ansible role docs "the worker tolerates a
        warming embedder").

        Lock ordering: this acquires ``_idle_lock`` and releases it
        BEFORE ``embed()`` goes on to acquire ``_encode_lock`` — the two
        are never nested, only sequential, so there's no ordering
        hazard with :meth:`_maybe_idle_unload` (which acquires
        ``_idle_lock`` then, while still holding it, non-blockingly
        tries ``_encode_lock``).
        """
        if not self._ready.is_set():
            return
        with self._idle_lock:
            if self._loaded:
                return
            self._embedder.warmup()
            self._loaded = True
            log.info("embedder lazy-reload: model reloaded after idle")

    def _run_probe_once(self) -> None:
        """Perform one real, timeout-guarded probe embed and update
        ``_ready``/failure count accordingly. Split out from
        ``_probe_loop`` so tests can drive it synchronously.

        Two things this deliberately gets right (2026-07 pre-ship
        review of gripe 51394):

        - **Bounded pileup.** At most ONE probe-encode thread is ever
          outstanding. A genuinely wedged encode holds
          ``_encode_lock`` forever; without this guard, every
          subsequent tick would spawn another thread that also blocks
          on the held lock forever — unbounded thread growth. If the
          previous probe-encode thread is still alive when a new tick
          fires, we don't spawn a second one — we just record the
          tick as a failure.
        - **Contention isn't a wedge.** The probe shares
          ``_encode_lock`` with real traffic (so it also observes
          genuine encode contention/GPU-serialisation issues, not
          just "the model object raises"). But a *healthy*, busy
          embedder legitimately holds that lock for large real
          batches — that must NOT flip ``/readyz`` to 503 and get the
          instance drained from the LB. So the probe first attempts
          ``_encode_lock.acquire(timeout=...)``; failing to acquire
          within the timeout means real traffic is actively encoding,
          which is evidence the embedder IS alive — that tick is
          treated as a healthy no-op (no failure counted, ``_ready``
          left as-is). Only a probe that DOES acquire the lock and
          then has the encode itself raise or hang past the timeout
          counts as a failure.

        Note: a genuinely wedged encode holding ``_encode_lock`` will
        still block real ``embed()`` calls too — that's the
        pre-existing failure mode and this probe does not (and
        cannot) unwedge the lock. Its only job is to make
        ``/readyz`` honest (503) so a supervisor restarts the
        process.
        """
        prev = self._probe_encode_thread
        if prev is not None and prev.is_alive():
            # A previous probe-encode thread is still running past a
            # full tick later. Its own lock-acquire attempt below is
            # itself timeout-bounded, so "still alive" this much later
            # can only mean it acquired the lock and the encode call
            # itself is hung — a genuine wedge, not mere contention.
            # Count it without spawning a second thread on top of it.
            self._record_probe_failure(
                "embedder self-probe: previous probe-encode thread "
                "still hung — not spawning another"
            )
            return

        result: dict[str, object] = {}

        def _do_probe() -> None:
            # Attempt the lock FIRST, with its own timeout, so we can
            # tell "real traffic has it" (healthy) apart from "we got
            # it and then hung/raised" (unhealthy).
            acquired = self._encode_lock.acquire(timeout=self._probe_timeout_s)
            if not acquired:
                result["outcome"] = "contended"
                return
            try:
                self._embedder.embed(["health probe"])
                result["outcome"] = "ok"
            except Exception as exc:
                result["outcome"] = "error"
                result["error"] = exc
            finally:
                self._encode_lock.release()

        thread = threading.Thread(
            target=_do_probe, name="embedder-probe-encode", daemon=True
        )
        thread.start()
        self._probe_encode_thread = thread
        # Slack over the lock-acquire budget so we don't race the
        # thread's own acquire-timeout when classifying contended vs.
        # hung — a "contended" outcome should already be set by the
        # time we get here in the common case.
        thread.join(self._probe_timeout_s * 1.5)

        if thread.is_alive():
            # Past the acquire budget (plus slack) and still running —
            # it must have acquired the lock and the encode call
            # itself is hung. Genuine wedge; the thread is abandoned
            # (daemon) and the pileup guard above keeps it capped at
            # one.
            self._record_probe_failure(
                f"embedder self-probe encode hung past {self._probe_timeout_s:.1f}s"
            )
            return

        outcome = result.get("outcome")
        if outcome == "contended":
            log.debug(
                "embedder self-probe: _encode_lock busy with real "
                "traffic for %.1fs — treated as healthy, not a failure",
                self._probe_timeout_s,
            )
            return
        if outcome == "ok":
            if self._probe_fail_count >= self._probe_fail_threshold and not self.ready:
                log.info("embedder self-probe recovered; /readyz back to 200")
            self._probe_fail_count = 0
            self._ready.set()
            return
        self._record_probe_failure(f"embedder self-probe failed: {result.get('error')}")

    def _record_probe_failure(self, message: str) -> None:
        self._probe_fail_count += 1
        log.warning(
            "%s (failure %d/%d)",
            message,
            self._probe_fail_count,
            self._probe_fail_threshold,
        )
        if self._probe_fail_count >= self._probe_fail_threshold and self.ready:
            log.warning(
                "embedder self-probe: %d consecutive failures, flipping /readyz to 503",
                self._probe_fail_count,
            )
            self._ready.clear()

    def model_info(self) -> ModelInfo:
        return ModelInfo(
            model=self._embedder.model,
            dim=self._embedder.dim,
            revision=self._revision,
        )

    def render_metrics(self) -> str:
        """``self.metrics.render()`` plus the idle-unload residency
        signals (§F cycle b) — cheap, aids ops (is this instance
        currently paying to hold weights? how long since it last did
        real work?)."""
        return (
            self.metrics.render()
            + f"precis_embedder_loaded {int(self._loaded)}\n"
            + f"precis_embedder_last_activity_age_seconds {self._idle_age_s():.1f}\n"
        )

    def _warm(self) -> None:
        try:
            # Call ``warmup()`` (not ``embed()``) — the public
            # ``embed`` is gated by ``_raise_if_warming`` which raises
            # while ``self._st is None``, but THIS thread is the one
            # supposed to clear that gate. Routing through ``embed``
            # caused the warm thread to fast-fail on the very gate it
            # was meant to clear (2026-06-15 → 2026-06-16 regression).
            # Belt-and-braces (§F cycle b review): hold ``_idle_lock``
            # across the call + the ``_loaded`` flip, same as
            # ``_ensure_loaded_for_request`` — this thread only ever
            # runs before ``_ready`` is set, so ``_ensure_loaded_for_
            # request`` never contends for the lock at the same time in
            # practice, but the lock makes that invariant robust rather
            # than implicit.
            with self._idle_lock:
                self._embedder.warmup()
                self._loaded = True
            self._ready.set()
            log.info(
                "embedder warm: model=%s dim=%d",
                self._embedder.model,
                self._embedder.dim,
            )
            self._start_probe()
        except Exception:  # pragma: no cover - depends on real model
            log.exception("embedder warmup failed; /readyz stays 503")

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Admission-controlled, serialised encode. Raises `Busy` when full.

        Any call is "activity" for the idle clock, and reloads the model
        first if it's currently idle (§F cycle b) — both BEFORE admission
        control, so a burst of callers arriving during a reload queue on
        the reload lock rather than each independently discovering
        ``Busy``/racing a duplicate reload.
        """
        self._last_activity = self._clock()
        self._ensure_loaded_for_request()
        if not self._sem.acquire(blocking=False):
            with self.metrics._lock:
                self.metrics.rejected_429 += 1
            raise Busy
        with self.metrics._lock:
            self.metrics.inflight += 1
        try:
            with self._encode_lock:
                vectors = self._embedder.embed(texts)
            with self.metrics._lock:
                self.metrics.embeds += 1
                self.metrics.texts += len(texts)
            return vectors
        finally:
            with self.metrics._lock:
                self.metrics.inflight -= 1
            self._sem.release()


class Busy(Exception):
    """Raised by :meth:`EmbedderService.embed` when at capacity."""


def _make_handler(service: EmbedderService) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        # Quieter logs — the access line per request is noise; errors
        # still surface via log.exception below.
        def log_message(self, *args: object) -> None:
            return

        def _send_json(
            self, status: int, obj: dict, extra_headers: dict | None = None
        ) -> None:
            body = json.dumps(obj).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            for k, v in (extra_headers or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _send_text(self, status: int, text: str) -> None:
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            service.metrics.requests += 1
            if self.path == PATH_HEALTH:
                self._send_text(200, "ok")
            elif self.path == PATH_READY:
                # §F cycle b: idle is still 200 — the JSON `state` field
                # is what distinguishes "loaded" from "idle" (both
                # serve); only "warming"/wedged is 503. A plain
                # status-code check (the watchdog, capability_probe)
                # keeps working unchanged.
                state = service.state()
                if service.ready:
                    self._send_json(200, {"status": "ready", "state": state})
                else:
                    self._send_json(503, {"status": "warming", "state": state})
            elif self.path == PATH_MODEL:
                self._send_json(200, service.model_info().to_dict())
            elif self.path == PATH_METRICS:
                self._send_text(200, service.render_metrics())
            else:
                self._send_json(404, {"error": "not found"})

        def do_POST(self) -> None:
            service.metrics.requests += 1
            if self.path != PATH_EMBED:
                self._send_json(404, {"error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length) if length else b""
                payload = json.loads(raw) if raw else {}
                req = EmbedRequest.from_dict(payload)
            except (ValueError, json.JSONDecodeError) as exc:
                self._send_json(400, {"error": f"bad request: {exc}"})
                return
            try:
                vectors = service.embed(req.texts)
            except Busy:
                self._send_json(
                    429, {"error": "busy"}, extra_headers={"Retry-After": "1"}
                )
                return
            except Exception as exc:  # pragma: no cover - model failure path
                with service.metrics._lock:
                    service.metrics.errors += 1
                log.exception("embed failed")
                self._send_json(500, {"error": f"embed failed: {exc}"})
                return
            info = service.model_info()
            resp = EmbedResponse(model=info.model, dim=info.dim, vectors=vectors)
            self._send_json(200, resp.to_dict())

    return Handler


def make_server(
    service: EmbedderService, *, host: str = "127.0.0.1", port: int = DEFAULT_PORT
) -> ThreadingHTTPServer:
    """Build (but don't start) the HTTP server bound to ``host:port``.

    Pass ``port=0`` for an ephemeral port (tests read the chosen port
    from ``server.server_address[1]``).
    """
    return ThreadingHTTPServer((host, port), _make_handler(service))


def serve(
    embedder: Embedder,
    *,
    host: str = "127.0.0.1",
    port: int = DEFAULT_PORT,
    revision: str | None = None,
    max_inflight: int = 4,
    warm: bool = True,
    idle_s: float = 1800.0,
) -> None:
    """Run the embedding service until interrupted (blocking)."""
    service = EmbedderService(
        embedder,
        revision=revision,
        max_inflight=max_inflight,
        warm=warm,
        idle_s=idle_s,
    )
    httpd = make_server(service, host=host, port=port)
    log.info(
        "serving embeddings on http://%s:%d (model=%s, max_inflight=%d, idle_s=%.0f)",
        host,
        port,
        embedder.model,
        max_inflight,
        idle_s,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:  # pragma: no cover
        log.info("shutting down embedder service")
    finally:
        service.stop_probe()
        httpd.server_close()


__all__ = ["Busy", "EmbedderService", "make_server", "serve"]
