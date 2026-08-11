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

from typing import Any

import pytest

from precis.workers.job_types import derived_drain as dd


class _FakeCtx:
    """The subset of ``DispatchContext`` ``_dispatch`` touches."""

    def __init__(self, params: dict[str, Any]) -> None:
        self.store = object()
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
    dd._dispatch(ctx, dd.SPEC)  # type: ignore[arg-type]
    assert ctx.failures and "unknown/absent" in ctx.failures[0][0]
    assert ctx.failures[0][1] == "infra"
    assert _summary(ctx) is None


def test_nonpositive_limit_records_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    _script_runner(monkeypatch, "classify", [{"claimed": 0}])
    ctx = _FakeCtx({"pass": "classify", "limit": 0})
    dd._dispatch(ctx, dd.SPEC)  # type: ignore[arg-type]
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
    dd._dispatch(ctx, dd.SPEC)  # type: ignore[arg-type]

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
    dd._dispatch(ctx, dd.SPEC)  # type: ignore[arg-type]

    assert [c["batch_size"] for c in calls] == [16, 4]
    summary = _summary(ctx)
    assert summary is not None and "drained 20 chunk(s)" in summary


def test_lease_loss_stops_without_terminal_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _script_runner(monkeypatch, "classify", [{"claimed": 16, "ok": 16}])
    monkeypatch.setattr(dd, "renew_own_lease", lambda *_a: False)  # lease lost
    ctx = _FakeCtx({"pass": "classify", "limit": 500})
    dd._dispatch(ctx, dd.SPEC)  # type: ignore[arg-type]

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
    dd._dispatch(ctx, dd.SPEC)  # type: ignore[arg-type]

    assert len(calls) == dd._MAX_NO_PROGRESS
    assert any(k == "job_event" and "no progress" in t for k, t in ctx.chunks)
    summary = _summary(ctx)
    assert summary is not None and "drained 48 chunk(s)" in summary
