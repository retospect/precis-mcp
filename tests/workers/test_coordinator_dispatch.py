"""``coordinator._run_one`` routes plugin job_types through their
``spec.dispatch`` callable and rejects specs without one.

Mirrors the unit-test pattern in ``test_claude_inproc_dispatch``:
we stub the store + claude_inproc helpers so the dispatch routing
can be asserted without a live Postgres.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, cast
from unittest.mock import MagicMock

import pytest

from precis.workers.executors import coordinator
from precis.workers.executors._context import DispatchContext
from precis.workers.executors._yield import Done, WakeKind, WakeWhen, Yield
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
        self.blocks = self  # blocks carve: flat fake doubles as its own sub-store
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
        # This test asserts routing only; persistence of the return has
        # its own suite below (TestCoordinatorPersistsReturn). Stub it so
        # the no-op dispatch's ``None`` return doesn't trip the
        # contract-violation path against the bare fake store.
        monkeypatch.setattr(
            coordinator, "_persist_dispatch_result", lambda *a, **k: None
        )

        store = _FakeStore()
        ctx = coordinator._build_dispatch_context(
            store,
            ref_id=101,
            title="campaign#101",
            meta={"job_type": "plugin_coordinator_demo"},
        )
        coordinator._run_one(ctx)

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
        ctx = coordinator._build_dispatch_context(
            store,
            ref_id=42,
            title="t",
            meta={"job_type": "no_dispatch_plugin"},
        )
        coordinator._run_one(ctx)

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
        ctx = coordinator._build_dispatch_context(
            store,
            ref_id=43,
            title="t",
            meta={"job_type": "totally_unknown"},
        )
        coordinator._run_one(ctx)

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
        ctx = coordinator._build_dispatch_context(
            store,
            ref_id=44,
            title="t",
            meta={"job_type": "plugin_coordinator_demo"},
        )
        coordinator._run_one(ctx)

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


# ── Behavioural: the dispatcher's return is PERSISTED ─────────────
#
# The structural tests above only prove dispatch is called and the
# dataclasses construct. They are exactly why the "return discarded"
# bug shipped green: nothing exercised what _run_one does with the
# return. These close that gap — Done terminates, Yield checkpoints +
# parks at a waiting status, and a contract violation fails loudly
# instead of hanging at STATUS:running.


class TestCoordinatorPersistsReturn:
    def _run_with(self, monkeypatch: pytest.MonkeyPatch, dispatch_fn: Any) -> dict:
        spec = _spec_with_dispatch(dispatch_fn)
        monkeypatch.setattr(
            coordinator,
            "get_job_type",
            lambda name: spec if name == "plugin_coordinator_demo" else None,
        )
        monkeypatch.setattr(coordinator, "_is_cancel_requested", lambda *_: False)
        calls: dict[str, list] = {
            "status": [],
            "chunks": [],
            "meta": [],
            "failures": [],
        }
        monkeypatch.setattr(
            coordinator,
            "_set_status",
            lambda store, ref_id, status, conn=None: calls["status"].append(status),
        )
        monkeypatch.setattr(
            coordinator,
            "_append_chunk",
            lambda store, ref_id, kind, text, conn=None: calls["chunks"].append(
                (kind, text)
            ),
        )
        monkeypatch.setattr(
            coordinator,
            "_set_meta",
            lambda conn, ref_id, **fields: calls["meta"].append(fields),
        )
        monkeypatch.setattr(
            coordinator,
            "_record_failure",
            lambda store, ref_id, reason, *, gripe_rollback: calls["failures"].append(
                reason
            ),
        )
        ctx = coordinator._build_dispatch_context(
            _FakeStore(),
            ref_id=7,
            title="t",
            meta={"job_type": "plugin_coordinator_demo"},
        )
        coordinator._run_one(ctx)
        return calls

    def test_done_success_writes_summary_merges_meta_succeeds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._run_with(
            monkeypatch,
            lambda ctx, spec: Done(
                summary="all good", success=True, summary_meta={"wall_seconds": 3}
            ),
        )
        assert ("job_summary", "all good") in calls["chunks"]
        assert {"wall_seconds": 3} in calls["meta"]
        assert calls["status"] == [coordinator._SUCCEEDED]
        assert calls["failures"] == []

    def test_done_failure_sets_failed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        calls = self._run_with(
            monkeypatch, lambda ctx, spec: Done(summary="broke", success=False)
        )
        assert calls["status"] == [coordinator._FAILED]

    def test_yield_checkpoints_and_parks_at_waiting_status(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._run_with(
            monkeypatch,
            lambda ctx, spec: Yield(
                state={"phase": 2},
                wake_when=WakeWhen(
                    kind="children_done", payload={"child_job_ids": [11, 12]}
                ),
            ),
        )
        merged = {k: v for m in calls["meta"] for k, v in m.items()}
        assert merged["coordinator_state"] == {"phase": 2}
        assert merged["wake_when"] == {
            "kind": "children_done",
            "payload": {"child_job_ids": [11, 12]},
        }
        # §H piece 5: a children_done park gets a wake_deadline stamped —
        # the FakeConn returns no rows, so this falls back to the default
        # (6h) — a roughly-6h-out unix timestamp, not None/absent.
        import time as _time

        assert "wake_deadline" in merged
        assert merged["wake_deadline"] > _time.time()
        assert merged["wake_deadline"] < _time.time() + 7 * 3600
        # parked at the status the wake_runner watches — NOT left running
        assert calls["status"] == [coordinator._WAITING_CHILDREN]
        assert calls["failures"] == []

    def test_non_children_done_yield_gets_no_wake_deadline(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """§H piece 5 is scoped to ``children_done`` only — an ``at_time``/
        ``tag_cleared``/``tag_added`` park has no children to deadlock on."""
        calls = self._run_with(
            monkeypatch,
            lambda ctx, spec: Yield(
                state={},
                wake_when=WakeWhen(kind="at_time", payload={"ts": 0}),
            ),
        )
        merged = {k: v for m in calls["meta"] for k, v in m.items()}
        assert "wake_deadline" not in merged
        assert calls["status"] == [coordinator._WAITING_TIME]

    def test_unknown_wake_kind_fails_loudly(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._run_with(
            monkeypatch,
            lambda ctx, spec: Yield(
                state={},
                wake_when=WakeWhen(kind=cast(WakeKind, "not_a_real_kind"), payload={}),
            ),
        )
        assert len(calls["failures"]) == 1
        assert calls["status"] == []  # never parked at a bogus status

    def test_non_done_yield_return_records_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls = self._run_with(monkeypatch, lambda ctx, spec: "oops not a Done")
        assert len(calls["failures"]) == 1
        assert "expected Done|Yield" in calls["failures"][0]
        assert calls["status"] == []  # not left at running


class _RowsConn:
    """Minimal fake ``Connection`` returning a fixed set of rows —
    enough to unit-test ``_children_wake_deadline_s``'s pure MAX+margin /
    fallback logic without a live database."""

    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    def execute(self, *_args: Any, **_kw: Any) -> _RowsConn:
        return self

    def fetchall(self) -> list[tuple]:
        return self._rows


def _rows_conn(rows: list[tuple]) -> Any:
    """`_RowsConn` typed as a real Connection for mypy — the fake only
    needs execute/fetchall, and `_children_wake_deadline_s` is unit-pure."""
    from psycopg import Connection

    return cast(Connection, _RowsConn(rows))


class TestChildrenWakeDeadline:
    """§H piece 5: MAX (not sum) of the children's wall_seconds + margin,
    FLOORED at PRECIS_WAKE_DEADLINE_HOURS (default 6h) — review Finding 5:
    a resource-serialized fan-out (executor concurrency limit / slot
    scarcity) can legitimately take far longer than any single child's own
    wall_seconds, so a tight MAX alone must never trip the deadline early.
    The floor also covers the "no child declares a budget at all" case."""

    def test_large_known_wall_seconds_beats_the_floor(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When MAX(wall)+margin exceeds the default 6h floor, the
        computed value wins — a genuinely long-running child's own budget
        is respected, never clamped down."""
        monkeypatch.delenv("PRECIS_WAKE_DEADLINE_HOURS", raising=False)
        conn = _rows_conn([("18000",), ("72000",), (None,)])
        deadline_s = coordinator._children_wake_deadline_s(conn, [1, 2, 3])
        assert deadline_s == 72000 + coordinator._WAKE_DEADLINE_MARGIN_S

    def test_small_known_wall_seconds_floors_at_default_hours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A KNOWN but small per-child wall_seconds budget still floors at
        PRECIS_WAKE_DEADLINE_HOURS — the master's "resource-serialized
        fan-out isn't tripped early" fix. Pre-fix, this would have
        returned 900 + margin (~1h15m), well under the 6h floor."""
        monkeypatch.delenv("PRECIS_WAKE_DEADLINE_HOURS", raising=False)
        conn = _rows_conn([("900",)])
        deadline_s = coordinator._children_wake_deadline_s(conn, [1])
        assert deadline_s == 6.0 * 3600
        assert deadline_s > 900 + coordinator._WAKE_DEADLINE_MARGIN_S

    def test_no_known_budget_falls_back_to_default_hours(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRECIS_WAKE_DEADLINE_HOURS", raising=False)
        conn = _rows_conn([(None,), (None,)])
        assert coordinator._children_wake_deadline_s(conn, [1, 2]) == 6.0 * 3600

    def test_empty_child_list_falls_back_to_default_hours(self) -> None:
        conn = _rows_conn([])
        assert coordinator._children_wake_deadline_s(conn, []) == 6.0 * 3600

    def test_default_hours_env_overridable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_WAKE_DEADLINE_HOURS", "2")
        conn = _rows_conn([])
        assert coordinator._children_wake_deadline_s(conn, []) == 2.0 * 3600

    def test_floor_itself_is_env_overridable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The floor a small known budget lands on is the SAME env-
        overridable default — not a hardcoded 6h. A 0s known child budget
        computes to exactly the margin (3600s); a floor set well above
        that (2h) must win."""
        monkeypatch.setenv("PRECIS_WAKE_DEADLINE_HOURS", "2")
        conn = _rows_conn([("0",)])
        deadline_s = coordinator._children_wake_deadline_s(conn, [1])
        assert deadline_s == 2.0 * 3600

    def test_malformed_wall_seconds_ignored(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A malformed wall_seconds value is ignored (not treated as
        known); the one valid small value present still floors at the
        default hours rather than surfacing 900 + margin directly."""
        monkeypatch.delenv("PRECIS_WAKE_DEADLINE_HOURS", raising=False)
        conn = _rows_conn([("not-a-number",), ("900",)])
        deadline_s = coordinator._children_wake_deadline_s(conn, [1, 2])
        assert deadline_s == 6.0 * 3600


class TestWaitingStatusVocab:
    """The ``STATUS:waiting_*`` values a Yield parks at must be in the
    closed STATUS vocabulary — ``_common.set_status`` validates via
    ``Tag.parse_strict``, so a missing value fails the first *real*
    (unstubbed) Yield at persist time. Regression for the gap where
    all four were absent from ``_CLOSED_VOCAB``."""

    def test_all_waiting_statuses_parse_strict(self) -> None:
        from precis.store.types import Tag
        from precis.workers.executors import _common

        for value in (
            _common.WAITING_CHILDREN,
            _common.WAITING_TIME,
            _common.WAITING_ASK_USER,
            _common.WAITING_MANUAL_KICK,
        ):
            tag = Tag.parse_strict(f"STATUS:{value}")
            assert tag.value == value


# ── Lease keepalive (gr204309) ─────────────────────────────────────
#
# The coordinator sets meta.lease_until ONCE at claim, sized to
# _LEASE_MINUTES — a slice that genuinely runs longer than that must not
# be mistaken for dead by an external reaper (the quest-loop reconciler
# cancelled live work on exactly this false signal, gr204309). These
# tests exercise _LeaseKeepalive directly against a fake store/pool, and
# then the wiring that engages it around every claimed slice.


class TestLeaseKeepalive:
    def test_keepalive_renews_the_lease_repeatedly_while_the_slice_runs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(coordinator, "_LEASE_KEEPALIVE_INTERVAL_S", 0.02)
        renewals: list[dict[str, Any]] = []

        def _fake_renew(
            conn: Any, ref_id: int, meta: dict[str, Any], lease_seconds: int
        ) -> bool:
            renewals.append({"ref_id": ref_id, "lease_seconds": lease_seconds})
            return True

        monkeypatch.setattr(coordinator, "_renew_lease_if_mine", _fake_renew)

        store = _FakeStore()
        with coordinator._LeaseKeepalive(store, 55, {"lease_boot_id": "b"}):
            # Wait for the ticks rather than sleeping a fixed span: at a
            # 0.02s interval a loaded/oversubscribed runner can schedule the
            # keepalive thread just once inside a 0.09s window (a macOS CI
            # flake). Generous deadline, exits as soon as the point is proven.
            deadline = time.monotonic() + 10.0
            while len(renewals) < 2 and time.monotonic() < deadline:
                time.sleep(0.01)

        # Renewed more than once (the whole point — a single claim-time
        # stamp wouldn't need a keepalive at all), always for the right
        # job, always sized to the full lease window (not the interval).
        assert len(renewals) >= 2
        assert all(r["ref_id"] == 55 for r in renewals)
        assert all(
            r["lease_seconds"] == coordinator._LEASE_MINUTES * 60 for r in renewals
        )

    def test_keepalive_thread_is_not_left_running_after_the_slice_ends(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(coordinator, "_LEASE_KEEPALIVE_INTERVAL_S", 0.02)
        monkeypatch.setattr(coordinator, "_renew_lease_if_mine", lambda *_a, **_k: True)

        store = _FakeStore()
        ka = coordinator._LeaseKeepalive(store, 1, {})
        with ka:
            time.sleep(0.05)

        # __exit__ joins and clears the thread — nothing left running.
        assert ka._thread is None

    def test_keepalive_stops_renewing_once_identity_is_lost(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """renew_lease_if_mine returning False means another worker
        generation already reclaimed the job — the keepalive must stop
        calling it (and must NOT touch the still-running slice itself)."""
        monkeypatch.setattr(coordinator, "_LEASE_KEEPALIVE_INTERVAL_S", 0.02)
        calls: list[int] = []

        def _fake_renew(*_a: Any, **_k: Any) -> bool:
            calls.append(1)
            return False

        monkeypatch.setattr(coordinator, "_renew_lease_if_mine", _fake_renew)

        store = _FakeStore()
        with coordinator._LeaseKeepalive(store, 9, {}):
            time.sleep(0.09)  # would be several more ticks if it kept going

        assert len(calls) == 1

    def test_run_coordinator_pass_wraps_each_slice_in_a_keepalive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The wiring itself: run_coordinator_pass's sequential loop must
        actually engage `_LeaseKeepalive` around `_run_one` — not merely
        have the class exist unused."""
        entered: list[int] = []
        exited: list[int] = []

        class _SpyKeepalive:
            def __init__(self, _store: Any, ref_id: int, _meta: dict[str, Any]) -> None:
                self._ref_id = ref_id

            def __enter__(self) -> _SpyKeepalive:
                entered.append(self._ref_id)
                return self

            def __exit__(self, *_exc_info: Any) -> None:
                exited.append(self._ref_id)

        monkeypatch.setattr(coordinator, "_LeaseKeepalive", _SpyKeepalive)
        monkeypatch.setattr(
            coordinator,
            "_claim_jobs",
            lambda conn, *, limit: [(1, "t", {"job_type": "x"})],
        )
        monkeypatch.setattr(coordinator, "_set_status", lambda *_a, **_k: None)
        monkeypatch.setattr(coordinator, "_run_one", lambda *_a, **_k: None)

        store = _FakeStore()
        out = coordinator.run_coordinator_pass(store)

        assert entered == [1]
        assert exited == [1]
        assert out == {"claimed": 1, "ok": 1, "failed": 0}
