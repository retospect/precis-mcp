"""``coordinator._run_one`` routes plugin job_types through their
``spec.dispatch`` callable and rejects specs without one.

Mirrors the unit-test pattern in ``test_claude_inproc_dispatch``:
we stub the store + claude_inproc helpers so the dispatch routing
can be asserted without a live Postgres.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.workers.executors import coordinator
from precis.workers.executors._context import DispatchContext
from precis.workers.executors._yield import Done, WakeWhen, Yield
from precis.workers.job_types import JobTypeSpec, _reset_plugin_cache


@pytest.fixture(autouse=True)
def _reset_plugin_cache_fixture() -> Any:
    _reset_plugin_cache()
    yield
    _reset_plugin_cache()


@dataclass
class _FakeRow:
    def fetchone(self) -> tuple[int]:
        return (0,)

    def fetchall(self) -> list[tuple[int]]:
        return []


@dataclass
class _FakeConn:
    def execute(self, *_args: Any, **_kw: Any) -> _FakeRow:
        return _FakeRow()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _FakePool:
    @contextmanager
    def connection(self) -> Any:
        yield _FakeConn()


class _FakeStore:
    def __init__(self) -> None:
        self.pool = _FakePool()
        self.add_tag = MagicMock()
        self.insert_blocks = MagicMock()
        self.list_blocks_for_ref = MagicMock(return_value=[])


def _spec_with_dispatch(dispatch_fn: Any) -> JobTypeSpec:
    return JobTypeSpec(
        name="plugin_coordinator_demo",
        params_schema={"type": "object", "properties": {}},
        compatible_executors=frozenset({"coordinator"}),
        requires=frozenset(),
        description="d",
        run=lambda **_: None,
        dispatch=dispatch_fn,
    )


# ── Dispatch routing ──────────────────────────────────────────────


class TestCoordinatorDispatch:
    def test_plugin_spec_dispatch_called_with_ctx(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        captured: dict[str, Any] = {}

        def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
            captured["ctx"] = ctx
            captured["spec"] = spec

        spec = _spec_with_dispatch(_dispatch)
        monkeypatch.setattr(
            coordinator,
            "get_job_type",
            lambda name: spec if name == "plugin_coordinator_demo" else None,
        )
        monkeypatch.setattr(coordinator, "_is_cancel_requested", lambda *_: False)

        store = _FakeStore()
        coordinator._run_one(
            store,
            ref_id=101,
            title="campaign#101",
            meta={"job_type": "plugin_coordinator_demo"},
        )

        assert captured["spec"] is spec
        assert isinstance(captured["ctx"], DispatchContext)
        assert captured["ctx"].ref_id == 101

    def test_spec_without_dispatch_records_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The coordinator path has no built-in fallback — a spec
        without ``dispatch`` must be rejected with a clear reason."""
        spec = JobTypeSpec(
            name="no_dispatch_plugin",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"coordinator"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
            # dispatch=None (default) — illegal for coordinator
        )
        monkeypatch.setattr(coordinator, "get_job_type", lambda name: spec)
        monkeypatch.setattr(coordinator, "_is_cancel_requested", lambda *_: False)

        failures: list[str] = []
        monkeypatch.setattr(
            coordinator,
            "_record_failure",
            lambda store, ref_id, reason, *, gripe_rollback: failures.append(reason),
        )

        store = _FakeStore()
        coordinator._run_one(
            store,
            ref_id=42,
            title="t",
            meta={"job_type": "no_dispatch_plugin"},
        )

        assert len(failures) == 1
        assert "no spec.dispatch callable" in failures[0]

    def test_missing_job_type_records_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(coordinator, "get_job_type", lambda name: None)

        failures: list[str] = []
        monkeypatch.setattr(
            coordinator,
            "_record_failure",
            lambda store, ref_id, reason, *, gripe_rollback: failures.append(reason),
        )

        store = _FakeStore()
        coordinator._run_one(
            store,
            ref_id=43,
            title="t",
            meta={"job_type": "totally_unknown"},
        )

        assert len(failures) == 1
        assert "unknown job_type" in failures[0]

    def test_cancel_before_run_sets_cancelled(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = _spec_with_dispatch(lambda ctx, s: None)
        monkeypatch.setattr(coordinator, "get_job_type", lambda name: spec)
        monkeypatch.setattr(coordinator, "_is_cancel_requested", lambda *_: True)

        statuses: list[tuple[int, str]] = []
        monkeypatch.setattr(
            coordinator,
            "_set_status",
            lambda store, ref_id, value, **_kw: statuses.append((ref_id, value)),
        )

        store = _FakeStore()
        coordinator._run_one(
            store,
            ref_id=44,
            title="t",
            meta={"job_type": "plugin_coordinator_demo"},
        )

        assert (44, "cancelled") in statuses


# ── Status mapping ────────────────────────────────────────────────


class TestStatusForWakeKind:
    """The closed STATUS:waiting_* vocabulary aligns with the
    WakeKind enum so the wake_runner can index on exact match."""

    def test_every_wake_kind_has_a_status(self) -> None:
        # Sanity: every kind WakeWhen accepts maps to a status.
        # If a future WakeKind is added, the mapping must extend.
        for kind in ("children_done", "at_time", "tag_cleared", "tag_added"):
            assert kind in coordinator._STATUS_FOR_WAKE_KIND


# ── Yield types are usable ────────────────────────────────────────


class TestYieldTypes:
    def test_done_constructs(self) -> None:
        d = Done(summary="ok", success=True, summary_meta={"wall_seconds": 1.0})
        assert d.summary == "ok"
        assert d.summary_meta["wall_seconds"] == 1.0

    def test_yield_constructs(self) -> None:
        y = Yield(
            state={"phase": "screen", "batch_n": 3},
            wake_when=WakeWhen(
                kind="children_done",
                payload={"child_job_ids": [101, 102, 103]},
            ),
        )
        assert y.state["phase"] == "screen"
        assert y.wake_when.kind == "children_done"
        assert y.wake_when.payload["child_job_ids"] == [101, 102, 103]
