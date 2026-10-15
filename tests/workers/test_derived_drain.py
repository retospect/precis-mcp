"""The ``derived_drain`` job_type's bounded drain loop
(``docs/backlog/small-llm-derived-drain-band.md``).

Drives ``derived_drain._dispatch`` against a fake ``DispatchContext`` and a
SCRIPTED ``run_X_pass`` runner (monkeypatched into ``_RUNNER_FACTORIES``) so
these tests pin the loop LOGIC — bound, drain-to-empty, decreasing batch, lease
loss, no-progress breaker — without a DB, a real LLM, or the melchior slot. The
wrapped passes' own behaviour is covered by their existing suites; the executor
wiring (job_inproc + renew_own_lease) by ``test_job_inproc_executor.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

import pytest

from precis.store._resource_slots_ops import ResourceSlot
from precis.utils.llm import live_config
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import derived_drain as dd


class _FakeCtx:
    """The subset of ``DispatchContext`` ``_dispatch`` touches."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.store: Any = object()
        self.ref_id = 42
        self.meta: dict[str, Any] = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.chunks: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))


def _script_runner(
    monkeypatch: pytest.MonkeyPatch,
    pass_name: str,
    results: list[dict[str, Any]],
) -> list[dict[str, int]]:
    """Wire ``_RUNNER_FACTORIES[pass_name]`` to a runner that returns
    ``results`` in order (then repeats the last), recording each call's
    ``batch_size``/``concurrency`` into the returned ``calls`` list."""
    calls: list[dict[str, int]] = []
    seq = iter(results)
    last: dict[str, Any] = {"claimed": 0, "ok": 0, "failed": 0}

    def run_once(store: Any, *, batch_size: int, concurrency: int) -> dict[str, Any]:
        nonlocal last
        calls.append({"batch_size": batch_size, "concurrency": concurrency})
        try:
            last = next(seq)
        except StopIteration:
            pass
        return last

    monkeypatch.setitem(dd._RUNNER_FACTORIES, pass_name, lambda: run_once)
    monkeypatch.setattr(dd, "renew_own_lease", lambda *_a: True)
    return calls


def _summary(ctx: _FakeCtx) -> str | None:
    for kind, text in ctx.chunks:
        if kind == "job_summary":
            return text
    return None


def test_unknown_pass_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    ctx = _FakeCtx({"pass": "nope"})
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)
    assert ctx.failures and "unknown/absent" in ctx.failures[0][0]
    assert ctx.failures[0][1] == "infra"
    assert _summary(ctx) is None


def test_nonpositive_limit_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _script_runner(monkeypatch, "classify", [{"claimed": 0}])
    ctx = _FakeCtx({"pass": "classify", "limit": 0})
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)
    assert ctx.failures and "must be positive" in ctx.failures[0][0]


def test_drains_until_queue_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _script_runner(
        monkeypatch,
        "classify",
        [
            {"claimed": 16, "ok": 16, "failed": 0},
            {"claimed": 16, "ok": 15, "failed": 1},
            {"claimed": 0, "ok": 0, "failed": 0},  # queue empty → break
        ],
    )
    ctx = _FakeCtx({"pass": "classify", "limit": 500, "concurrency": 6})
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    assert not ctx.failures
    # 3 calls: two productive + the empty one that breaks the loop.
    assert len(calls) == 3
    assert all(c["concurrency"] == 6 for c in calls)
    summary = _summary(ctx)
    assert summary is not None and "drained 32 chunk(s)" in summary
    assert "31 ok" in summary


def test_respects_limit_with_decreasing_final_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # limit 20, default batch 16 → first call batch_size 16, second capped to 4.
    calls = _script_runner(
        monkeypatch,
        "classify",
        [{"claimed": 16, "ok": 16}, {"claimed": 4, "ok": 4}],
    )
    ctx = _FakeCtx({"pass": "classify", "limit": 20})
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    assert [c["batch_size"] for c in calls] == [16, 4]
    summary = _summary(ctx)
    assert summary is not None and "drained 20 chunk(s)" in summary


def test_lease_loss_stops_without_terminal_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_runner(monkeypatch, "classify", [{"claimed": 16, "ok": 16}])
    monkeypatch.setattr(dd, "renew_own_lease", lambda *_a: False)  # lease lost
    ctx = _FakeCtx({"pass": "classify", "limit": 500})
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    # No terminal job_summary — the new owner drives it; a job_event explains.
    assert _summary(ctx) is None
    assert any(k == "job_event" and "lease lost" in t for k, t in ctx.chunks)
    assert not ctx.failures


def test_no_progress_breaker_bails_early(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every batch claims rows but produces 0 ok (slot saturated) — bail after
    # _MAX_NO_PROGRESS consecutive dry batches instead of spinning to limit.
    calls = _script_runner(
        monkeypatch,
        "llm_summarize",
        [{"claimed": 16, "ok": 0, "failed": 16}],  # repeats (last-value)
    )
    ctx = _FakeCtx({"pass": "llm_summarize", "limit": 5000})
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    assert len(calls) == dd._MAX_NO_PROGRESS
    assert any(k == "job_event" and "no progress" in t for k, t in ctx.chunks)
    summary = _summary(ctx)
    assert summary is not None and "drained 48 chunk(s)" in summary


# ── slot-derived default concurrency ─────────────────────────────────────
#
# ``_derived_concurrency`` resolves the pass's chain exactly as ``route()``
# does, then sums ``resource_slots.capacity`` over the ``llm:<model>`` rows
# behind its local rungs. With the env/app_settings inputs dark, the default
# SMALL chain is a single LOCAL rung with no model pin → ``llm:summarizer``.


class _SlotStore:
    """Store stand-in serving canned ``all_resource_slots()`` rows — the only
    Store surface ``_derived_concurrency`` touches."""

    def __init__(self, slots: list[ResourceSlot]) -> None:
        self._slots = slots

    def all_resource_slots(self) -> list[ResourceSlot]:
        return list(self._slots)


def _slot(resource: str, capacity: int, host: str = "h") -> ResourceSlot:
    return ResourceSlot(
        host=host,
        resource=resource,
        capacity=capacity,
        free=capacity,
        kind="hard",
        updated_at=datetime.now(UTC),
    )


def _clean_llm_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin every input chain/model resolution can read, so a dev shell's env
    (or a bound store's app_settings rows) can't leak into these tests."""
    for var in (
        "PRECIS_LLM_BACKEND",
        "PRECIS_LLM_BASE_URL",
        "PRECIS_LLM_FAILOVER",
        "PRECIS_LLM_CHAIN_SMALL",
        "PRECIS_LLM_MODEL_SMALL",
        "PRECIS_SUMMARIZE_MODEL",
        "PRECIS_CLASSIFY_MODEL",
    ):
        monkeypatch.delenv(var, raising=False)
    # app_settings reads stay dark regardless of a bound store or a stale TTL
    # cache entry an earlier test left.
    monkeypatch.setattr(live_config, "_cached_setting", lambda _key: None)


def test_concurrency_derives_from_advertised_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default SMALL chain's local rung resolves to ``llm:summarizer``;
    its advertised capacity becomes the drain's pool width."""
    _clean_llm_env(monkeypatch)
    calls = _script_runner(monkeypatch, "llm_summarize", [{"claimed": 0}])
    ctx = _FakeCtx({"pass": "llm_summarize"})
    ctx.store = _SlotStore([_slot("llm:summarizer", 4)])
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    assert calls and all(c["concurrency"] == 4 for c in calls)


def test_concurrency_fallback_when_nothing_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``llm:`` row for the chain's local model — unrelated rows don't
    count — keeps the plain ``_DEFAULT_CONCURRENCY`` fan-out."""
    _clean_llm_env(monkeypatch)
    calls = _script_runner(monkeypatch, "llm_summarize", [{"claimed": 0}])
    ctx = _FakeCtx({"pass": "llm_summarize"})
    ctx.store = _SlotStore(
        [_slot("llm:other-model", 8), _slot("gpu", 4)],
    )
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    assert calls and all(c["concurrency"] == dd._DEFAULT_CONCURRENCY for c in calls)


def test_concurrency_fallback_on_cloud_only_chain(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A chain whose only rung is cloud advertises no local capacity —
    advertised ``llm:`` rows for other chains are ignored."""
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv(
        "PRECIS_LLM_CHAIN_SMALL",
        '[{"placement": "cloud", "model": "some-cloud", "transport": "claude_p"}]',
    )
    calls = _script_runner(monkeypatch, "llm_summarize", [{"claimed": 0}])
    ctx = _FakeCtx({"pass": "llm_summarize"})
    ctx.store = _SlotStore([_slot("llm:summarizer", 4)])
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    assert calls and all(c["concurrency"] == dd._DEFAULT_CONCURRENCY for c in calls)


def test_concurrency_uses_only_the_chain_local_rungs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The prod shape: a local rung ahead of a cloud overflow rung — the
    cloud rung's model's slots (if any) must not add to the width."""
    _clean_llm_env(monkeypatch)
    monkeypatch.setenv(
        "PRECIS_LLM_CHAIN_SMALL",
        '[{"placement": "local", "model": "glm-4.7-flash", "transport": "local"},'
        ' {"placement": "cloud", "model": "z-ai/glm-4.7-flash",'
        ' "transport": "claude_p"}]',
    )
    ctx = _FakeCtx({"pass": "llm_summarize"})
    ctx.store = _SlotStore(
        [
            _slot("llm:glm-4.7-flash", 4, host="melchior"),
            _slot("llm:z-ai/glm-4.7-flash", 32, host="elsewhere"),
        ]
    )
    assert dd._derived_concurrency(ctx.store, "llm_summarize") == 4


def test_derived_concurrency_floor_sum_and_cap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _clean_llm_env(monkeypatch)
    # Floor: a single advertised slot still runs one thread.
    store: Any = _SlotStore([_slot("llm:summarizer", 1)])
    assert dd._derived_concurrency(store, "llm_summarize") == 1
    # Two serving hosts sum their advertised capacity.
    store = _SlotStore(
        [_slot("llm:summarizer", 2, "a"), _slot("llm:summarizer", 3, "b")]
    )
    assert dd._derived_concurrency(store, "llm_summarize") == 5
    # Cap: a fat advertised capacity is clamped.
    store = _SlotStore([_slot("llm:summarizer", 500)])
    assert (
        dd._derived_concurrency(store, "llm_summarize") == dd._MAX_DERIVED_CONCURRENCY
    )


def test_concurrency_param_beats_advertised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``params.concurrency`` always wins — the derivation only
    fills the unset default."""
    _clean_llm_env(monkeypatch)
    calls = _script_runner(monkeypatch, "llm_summarize", [{"claimed": 0}])
    ctx = _FakeCtx({"pass": "llm_summarize", "concurrency": 7})
    ctx.store = _SlotStore([_slot("llm:summarizer", 4)])
    dd._dispatch(cast(DispatchContext, ctx), dd.SPEC)

    assert calls and all(c["concurrency"] == 7 for c in calls)
