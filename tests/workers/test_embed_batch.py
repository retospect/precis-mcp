"""``embed_batch`` job_type (§F cycle a) — a bounded work order draining
the SAME derived embed queue the standing ``embed`` pass uses (ADR 0007).

Drives ``embed_batch._dispatch`` directly against a minimal fake
``DispatchContext`` (only the attributes/callables the dispatcher actually
reads — ``store``/``meta``/``record_failure``/``append_chunk``) rather
than a full ``job_inproc`` claim/run cycle, so these tests pin the
job_type's OWN logic; ``test_job_inproc_executor.py`` covers the executor
wiring around it.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.embedder import EmbedderUnavailable, MockEmbedder
from precis.store import Store
from precis.workers.embed import EmbedHandler
from precis.workers.job_types import embed_batch
from tests.workers._helpers import make_mock_bge_m3, seed_chunks

pytestmark = pytest.mark.db


class _FakeCtx:
    """The slice of ``DispatchContext`` ``embed_batch._dispatch`` reads.

    Backed by a REAL ``kind='job'`` ref (rather than a bare hardcoded id) —
    the drain loop's per-iteration ``renew_own_lease`` does a genuine
    ``UPDATE refs ... WHERE ref_id = ...`` null-safe-matched against
    ``meta``'s (absent, i.e. ``None``) lease identity fields, which only
    succeeds against a row that actually exists.
    """

    def __init__(self, store: Store, *, params: dict[str, Any] | None = None) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="embed_batch test",
            meta={"executor": "job_inproc", "job_type": "embed_batch"},
        )
        self.ref_id = int(ref.id)
        self.title = "embed_batch test"
        self.meta: dict[str, Any] = {"params": params or {}}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:  # pragma: no cover — unused here
        pass

    def set_meta(self, **_kw: Any) -> None:  # pragma: no cover — unused here
        pass

    def is_cancel_requested(self) -> bool:  # pragma: no cover — unused here
        return False


def _mk_ctx(store: Store, *, params: dict[str, Any] | None = None) -> Any:
    # Typed ``Any`` on purpose: ``_FakeCtx`` structurally satisfies the
    # slice of ``DispatchContext`` the dispatcher reads, but isn't (and
    # needn't be) a real ``DispatchContext`` instance.
    return _FakeCtx(store, params=params)


def _patch_embedder(monkeypatch: pytest.MonkeyPatch, embedder: Any) -> None:
    monkeypatch.setattr(embed_batch, "resolve_embedder", lambda **_kw: embedder)


def _embedding_count(store: Store, *, model: str = "bge-m3") -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunk_embeddings WHERE embedder = %s", (model,)
        ).fetchone()
    return int(row[0]) if row else 0


def _claim_count(store: Store, *, model: str = "bge-m3") -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM chunk_claims WHERE artifact = %s", (model,)
        ).fetchone()
    return int(row[0]) if row else 0


# ── drains + stops-at-limit + stops-at-empty-queue ───────────────────────


def test_drains_all_chunks_and_reports_zero_remaining(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, [f"chunk {i}" for i in range(5)])

    ctx = _mk_ctx(store, params={"limit": 100})
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    assert ctx.failures == []
    assert _embedding_count(store) == 5
    assert len(ctx.summaries) == 1
    kind, text = ctx.summaries[0]
    assert kind == "job_summary"
    assert "embedded 5 chunk(s)" in text
    assert "queue_remaining≈0" in text


def test_stops_at_limit_below_backlog_size(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, [f"chunk {i}" for i in range(10)])

    ctx = _mk_ctx(store, params={"limit": 3})
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    assert ctx.failures == []
    assert _embedding_count(store) == 3
    assert "embedded 3 chunk(s)" in ctx.summaries[0][1]
    # 7 chunks were never claimed — the backlog is still there.
    assert "queue_remaining≈7" in ctx.summaries[0][1]


def test_default_limit_when_params_omit_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, [f"chunk {i}" for i in range(3)])

    ctx = _mk_ctx(store, params={})  # no limit → embed_batch._DEFAULT_LIMIT
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    assert ctx.failures == []
    assert _embedding_count(store) == 3  # queue emptied well under the default


# ── EmbedderUnavailable → failed(infra) ──────────────────────────────────


class _DownEmbedder(MockEmbedder):
    def embed(self, texts: list[str]) -> list[list[float]]:
        raise EmbedderUnavailable("embedder daemon unreachable")


def test_embedder_unavailable_fails_infra_and_releases_claims(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_embedder(monkeypatch, _DownEmbedder(dim=1024, model="bge-m3"))
    seed_chunks(store, ["a", "b"])

    ctx = _mk_ctx(store, params={"limit": 100})
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    assert len(ctx.failures) == 1
    reason, failure_class = ctx.failures[0]
    assert failure_class == "infra"
    assert "unreachable" in reason
    assert _embedding_count(store) == 0
    # The claims were released — the chunks are immediately re-claimable,
    # not stuck holding a lease until the cooldown.
    assert _claim_count(store) == 0


# ── concurrent-with-standing-pass safety ─────────────────────────────────


def test_concurrent_with_standing_pass_never_double_claims(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The standing ``embed`` pass and an ``embed_batch`` job share the SAME
    ``chunk_claims`` lease (keyed on ``artifact = model_name``, not on
    which handler *instance* claimed it) — a chunk the standing pass has
    already leased this cycle is invisible to embed_batch's own claim, so
    the two never double-write. One assertion, not a stress test."""
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, [f"chunk {i}" for i in range(4)])

    standing_handler = EmbedHandler(make_mock_bge_m3())
    with store.pool.connection() as conn:
        leased = standing_handler.claim_batch(conn, limit=2)
        conn.commit()
    assert len(leased) == 2

    ctx = _mk_ctx(store, params={"limit": 100})
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    # embed_batch only ever saw the other 2 (unleased) chunks.
    assert _embedding_count(store) == 2
    # The standing pass's 2 leases are still held — embed_batch neither
    # stole nor duplicated them.
    assert _claim_count(store) == 2


# ── mid-drain lease renewal ───────────────────────────────────────────────


def test_lease_lost_mid_drain_stops_without_terminal_status(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A renewal failure partway through the drain (another worker
    generation reclaimed the job) stops the loop immediately — the
    remaining backlog is left untouched and no failure is recorded (the
    new owner drives the job to its own terminal status)."""
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, [f"chunk {i}" for i in range(2 * embed_batch._MICRO_BATCH)])

    calls = {"n": 0}

    def _renew(_store: Store, _ref_id: int, _meta: dict[str, Any]) -> bool:
        calls["n"] += 1
        return calls["n"] == 1  # first iteration's renewal succeeds, then lost

    monkeypatch.setattr(embed_batch, "renew_own_lease", _renew)

    ctx = _mk_ctx(store, params={"limit": 2 * embed_batch._MICRO_BATCH})
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    assert calls["n"] == 2
    assert ctx.failures == []  # NOT recorded as a failure
    # Only the first micro-batch (renewed OK) was embedded; the rest of
    # the backlog is untouched for the new owner.
    assert _embedding_count(store) == embed_batch._MICRO_BATCH
    assert len(ctx.summaries) == 1
    kind, text = ctx.summaries[0]
    assert kind == "job_event"
    assert "lease lost" in text


def test_lease_renewed_each_iteration_while_draining(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The happy path calls the renewal once per drain-loop iteration."""
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, [f"chunk {i}" for i in range(2 * embed_batch._MICRO_BATCH)])

    calls: list[int] = []

    def _renew(_store: Store, ref_id: int, _meta: dict[str, Any]) -> bool:
        calls.append(ref_id)
        return True

    monkeypatch.setattr(embed_batch, "renew_own_lease", _renew)

    ctx = _mk_ctx(store, params={"limit": 2 * embed_batch._MICRO_BATCH})
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    assert ctx.failures == []
    assert _embedding_count(store) == 2 * embed_batch._MICRO_BATCH
    assert calls == [ctx.ref_id, ctx.ref_id]  # once per (of the 2) iterations


# ── bad params ────────────────────────────────────────────────────────────


def test_nonpositive_limit_fails_infra_without_touching_the_queue(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_embedder(monkeypatch, make_mock_bge_m3())
    seed_chunks(store, ["a"])

    ctx = _mk_ctx(store, params={"limit": 0})
    embed_batch._dispatch(ctx, embed_batch.SPEC)

    assert len(ctx.failures) == 1
    assert ctx.failures[0][1] == "infra"
    assert _embedding_count(store) == 0
