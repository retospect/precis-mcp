"""The request path and the worker path must not share one embedder budget.

Gripe 244419: ``embedder_timeout`` (300s) × ``embedder_max_retries + 1``
(6 attempts) is a ~1800s worst case per embed call. That is the *right*
number for a worker riding out a warming model on a CPU host — it is what
``embedder-wedge-hardening.md`` §5 deliberately raised it to after the
2026-08-10 caspar/balthazar incident. On the MCP request path it is the
bug: ``server.py`` registers the seven verbs as plain sync callables so
FastMCP runs each in an anyio worker thread, nothing in ``src/`` sets
``current_default_thread_limiter``, and ~40 concurrent 30-minute waits
exhaust the default pool — after which *every* call to that ``precis
serve`` queues for a thread, including a static ``get(kind='skill')``
that touches neither DB nor embedder.

So these pin the split itself, in both directions: the factory must hand
the hub the interactive budget, ``cli/worker.py`` must keep handing the
worker the patient one, and the interactive worst case must stay bounded
far below the batch one.
"""

from __future__ import annotations

import argparse
import threading
import time
from typing import Any

import pytest

from precis.config import PrecisConfig


def _worst_case_seconds(timeout: float, max_retries: int) -> float:
    """Wall-clock ceiling for one ``RemoteEmbedder`` call against a single
    endpoint: every attempt burns its full socket timeout, plus the
    jittered backoff between them (``_backoff`` sleeps at most
    ``min(backoff_max, backoff_base * 2**attempt)``)."""
    attempts = max_retries + 1
    backoff = sum(min(15.0, 0.5 * (2**a)) for a in range(max_retries))
    return timeout * attempts + backoff


def test_interactive_budget_is_bounded_far_below_the_batch_one() -> None:
    """A regression guard on the numbers themselves — if someone raises the
    interactive budget back toward the batch one, the wedge returns."""
    cfg = PrecisConfig()
    batch = _worst_case_seconds(cfg.embedder_timeout, cfg.embedder_max_retries)
    interactive = _worst_case_seconds(
        cfg.embedder_interactive_timeout, cfg.embedder_interactive_max_retries
    )
    # The batch budget is the patient one and must stay patient.
    assert batch >= 1800.0
    # An interactive caller has a human attached and a correct fallback
    # (``utils/embed_query.py`` degrades to lexical-only), so it must fail
    # inside a minute rather than half an hour.
    assert interactive <= 60.0
    assert interactive < batch / 20


def test_interactive_timeout_clears_a_cold_model_reload() -> None:
    """``embedder_service`` unloads after ``idle_s`` and the next ``/embed``
    pays a synchronous lazy reload — ~7s for bge-m3 (see the eager-load note
    in ``precis/embedder.py``). A budget under that would flap every fleet
    to lexical-only each morning, which is a different bug, not a fix."""
    cfg = PrecisConfig()
    assert cfg.embedder_interactive_timeout >= 10.0
    # ...but still enough retries to absorb exactly one blip, not a storm:
    # the embedder answers 429 past ``max_inflight=4`` and RemoteEmbedder
    # treats 429 as retryable, so a patient budget here multiplies load on
    # the thing that is already saturated.
    assert 1 <= cfg.embedder_interactive_max_retries <= 2


def test_factory_wires_the_interactive_budget_onto_the_hub(
    store: Any, monkeypatch: Any
) -> None:
    """``build_runtime`` is the composition root for every request-path
    caller (MCP server, precis_web, CLI tool paths) — it must pass the
    interactive pair, not the batch pair."""
    import precis.embedder as embedder_mod
    import precis.secrets as secrets_mod
    from precis.runtime import build_runtime

    seen: dict[str, Any] = {}

    def _capture(name: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return embedder_mod.MockEmbedder(dim=kwargs.get("dim") or 1024)

    monkeypatch.setattr(embedder_mod, "make_embedder", _capture)
    # ``build_runtime`` calls ``secrets.adopt_process_store``, which sets
    # PROCESS-GLOBAL ``_STORE`` / ``_ADOPTED_DSN`` and pops the DSN from the
    # env. Left alone, that leaks into any later test asserting a *storeless*
    # ``build_runtime()`` — it recovers the adopted DSN and comes up with a
    # store (``test_runtime.py::test_build_runtime_no_database`` is the one
    # that catches it). Routing both through ``monkeypatch`` restores them at
    # teardown; same guard as
    # ``test_build_runtime_falls_back_to_adopted_dsn_after_env_scrubbed``.
    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", None)
    monkeypatch.setenv("PRECIS_DATABASE_URL", store.dsn)

    rt = build_runtime()
    try:
        cfg = PrecisConfig()
        assert seen["timeout"] == cfg.embedder_interactive_timeout
        assert seen["max_retries"] == cfg.embedder_interactive_max_retries
        # ...and specifically NOT the batch pair.
        assert seen["timeout"] != cfg.embedder_timeout
    finally:
        if rt.store is not None:
            rt.store.close()


def test_worker_still_gets_the_patient_batch_budget(monkeypatch: Any) -> None:
    """The other half of the split: nothing about the request-path fix may
    shorten the worker's leash, or the 2026-08-10 recompute-storm returns."""
    import precis.cli.worker as worker_mod

    seen: dict[str, Any] = {}

    def _capture(name: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return object()

    monkeypatch.setattr(worker_mod, "make_embedder", _capture)
    args = argparse.Namespace(
        embedder="remote",
        embedder_url="http://127.0.0.1:8181",
        embedder_timeout=300.0,
        embedder_max_retries=5,
    )
    worker_mod._resolve_embedder(args)

    assert seen["timeout"] == 300.0
    assert seen["max_retries"] == 5


# ---------------------------------------------------------------------------
# The bulkhead: bound how many threads park inside an embed at once
# ---------------------------------------------------------------------------


class _BlockingEmbedder:
    """Backend whose embeds park until released — stands in for a struggling
    remote embedder without needing one."""

    def __init__(self, dim: int = 8) -> None:
        self._dim = dim
        self.gate = threading.Event()
        self.entered = threading.Semaphore(0)
        self.warmups = 0

    @property
    def dim(self) -> int:
        return self._dim

    @property
    def model(self) -> str:
        return "blocking"

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        self.warmups += 1

    def unload(self) -> None:
        return None

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.entered.release()
        self.gate.wait(timeout=10.0)
        return [[0.0] * self._dim for _ in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


def test_bulkhead_sheds_past_max_concurrency_instead_of_queueing() -> None:
    """The load-bearing property. Queueing would hold the very thread the
    bulkhead exists to protect."""
    from precis.embedder import BoundedConcurrencyEmbedder
    from precis.errors import Upstream

    inner = _BlockingEmbedder()
    emb = BoundedConcurrencyEmbedder(inner, 2)

    threads = [
        threading.Thread(target=lambda: emb.embed_one("x"), daemon=True)
        for _ in range(2)
    ]
    for t in threads:
        t.start()
    # Both slots are now occupied and parked inside the backend.
    assert inner.entered.acquire(timeout=10.0)
    assert inner.entered.acquire(timeout=10.0)

    # The third caller must fail FAST rather than wait for a slot.
    started = time.monotonic()
    with pytest.raises(Upstream):
        emb.embed_one("third")
    assert time.monotonic() - started < 1.0

    inner.gate.set()
    for t in threads:
        t.join(timeout=10.0)


def test_bulkhead_releases_slots_after_each_call() -> None:
    """A slot must come back on both the success and the failure path, or the
    bulkhead degrades into a permanent outage of its own."""
    from precis.embedder import BoundedConcurrencyEmbedder, MockEmbedder

    emb = BoundedConcurrencyEmbedder(MockEmbedder(dim=8), 1)
    for _ in range(5):
        assert len(emb.embed_one("x")) == 8

    class _Boom(MockEmbedder):
        def embed_one(self, text: str) -> list[float]:
            raise RuntimeError("backend blew up")

    emb = BoundedConcurrencyEmbedder(_Boom(dim=8), 1)
    for _ in range(3):
        with pytest.raises(RuntimeError, match="blew up"):
            emb.embed_one("x")
    # Slot was returned each time — a shed would raise Upstream instead.


def test_bulkhead_embed_one_takes_a_single_slot() -> None:
    """``embed_one`` must delegate to the INNER ``embed_one``, not to
    ``self.embed`` — routing through the wrapper would take a second slot for
    one logical embed and let a single caller shed itself."""
    from precis.embedder import BoundedConcurrencyEmbedder, MockEmbedder

    emb = BoundedConcurrencyEmbedder(MockEmbedder(dim=8), 1)
    assert len(emb.embed_one("only one caller")) == 8


def test_bulkhead_forwards_the_protocol_and_leaves_warmup_ungated() -> None:
    """It must still satisfy ``Embedder`` (``skill_index`` runtime-checks it),
    and ``warmup`` must bypass the gate for the same reason
    ``BgeM3Embedder.warmup`` bypasses its warming gate."""
    from precis.embedder import BoundedConcurrencyEmbedder, Embedder

    inner = _BlockingEmbedder(dim=8)
    emb = BoundedConcurrencyEmbedder(inner, 1)

    assert isinstance(emb, Embedder)
    assert emb.dim == 8
    assert emb.model == "blocking"
    assert emb.is_ready() is True
    assert emb.inner is inner

    # Occupy the only slot, then confirm warmup/unload still get through.
    t = threading.Thread(target=lambda: emb.embed_one("x"), daemon=True)
    t.start()
    assert inner.entered.acquire(timeout=10.0)
    emb.warmup()
    emb.unload()
    assert inner.warmups == 1

    inner.gate.set()
    t.join(timeout=10.0)


def test_bulkhead_rejects_a_nonsense_ceiling() -> None:
    from precis.embedder import BoundedConcurrencyEmbedder, MockEmbedder

    with pytest.raises(ValueError):
        BoundedConcurrencyEmbedder(MockEmbedder(dim=8), 0)


def test_bulkhead_leaves_most_of_the_anyio_pool_for_other_verbs() -> None:
    """The whole point (gripe 244419): a static ``get(kind='skill')`` must stay
    answerable while semantic search is struggling. anyio's default limiter is
    40 threads, so the ceiling has to be a small fraction of it."""
    cfg = PrecisConfig()
    assert cfg.embedder_interactive_max_concurrency >= 1
    assert cfg.embedder_interactive_max_concurrency <= 8


def test_factory_applies_the_bulkhead(store: Any, monkeypatch: Any) -> None:
    """Same seam as the timeout split — the request-path composition root."""
    import precis.embedder as embedder_mod
    import precis.secrets as secrets_mod
    from precis.runtime import build_runtime

    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", None)
    monkeypatch.setenv("PRECIS_DATABASE_URL", store.dsn)
    monkeypatch.setenv("PRECIS_EMBEDDER", "mock")

    rt = build_runtime()
    try:
        emb = rt.hub.embedder
        assert isinstance(emb, embedder_mod.BoundedConcurrencyEmbedder)
        assert emb.max_concurrency == (
            PrecisConfig().embedder_interactive_max_concurrency
        )
        # Transparent to callers: dim still matches the corpus.
        assert rt.store is not None
        assert emb.dim == rt.store.embedding_dim()
    finally:
        if rt.store is not None:
            rt.store.close()


def test_batch_callers_opt_out_of_both_halves(store: Any, monkeypatch: Any) -> None:
    """``build_runtime(interactive=False)`` is for unattended BULK passes that
    happen to want a runtime — ``cli/taproot.py``'s backfill / direct-mint.

    They loop through ``taproot/canon.py::block``, which embeds *unguarded*:
    on the interactive budget one transient blip aborts the run after ~31s
    and discards everything computed so far, and a shed would do the same.
    So a batch caller must get the patient pair AND no bulkhead.
    """
    import precis.embedder as embedder_mod
    import precis.secrets as secrets_mod
    from precis.runtime import build_runtime

    seen: dict[str, Any] = {}

    def _capture(name: str, **kwargs: Any) -> Any:
        seen.update(kwargs)
        return embedder_mod.MockEmbedder(dim=kwargs.get("dim") or 1024)

    monkeypatch.setattr(embedder_mod, "make_embedder", _capture)
    monkeypatch.setattr(secrets_mod, "_STORE", None)
    monkeypatch.setattr(secrets_mod, "_ADOPTED_DSN", None)
    monkeypatch.setenv("PRECIS_DATABASE_URL", store.dsn)

    rt = build_runtime(interactive=False)
    try:
        cfg = PrecisConfig()
        assert seen["timeout"] == cfg.embedder_timeout
        assert seen["max_retries"] == cfg.embedder_max_retries
        # No bulkhead: shedding assumes a fallback the bulk path doesn't have.
        assert not isinstance(rt.hub.embedder, embedder_mod.BoundedConcurrencyEmbedder)
    finally:
        if rt.store is not None:
            rt.store.close()


def test_taproot_bulk_subcommands_declare_themselves_batch() -> None:
    """The two in-tree callers that actually need it. A future
    ``build_runtime(cfg)`` added to a bulk taproot pass silently re-inherits
    the interactive budget, so pin the call form itself."""
    import inspect

    import precis.cli.taproot as taproot_mod

    for fn in (taproot_mod._run_backfill, taproot_mod._run_direct_mint):
        src = inspect.getsource(fn)
        assert "build_runtime(cfg, interactive=False)" in src, (
            f"{fn.__name__} must build its runtime in batch mode — it embeds "
            "unguarded per chunk via canon.block()"
        )
