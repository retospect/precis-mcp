"""``claude_inproc._run_one`` routes plugin job_types through
their ``spec.dispatch`` callable.

These tests stub the store + helpers so we can assert that
``DispatchContext`` is built with the right closures and that
``spec.dispatch`` is what gets called for a plugin spec — while
``fix_gripe`` / ``plan_tick`` still go through the in-tree
``_run_fix_gripe`` / ``_run_plan_tick`` path.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock

import pytest

from precis.workers.executors import claude_inproc
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import JobTypeSpec, _reset_plugin_cache


@pytest.fixture(autouse=True)
def _reset_plugin_cache_fixture() -> Any:
    _reset_plugin_cache()
    yield
    _reset_plugin_cache()


@dataclass
class _FakeConn:
    """Minimal connection stub that no-ops everything our helpers do."""

    def execute(self, *_args: Any, **_kw: Any) -> _FakeRow:
        return _FakeRow()

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


@dataclass
class _FakeRow:
    def fetchone(self) -> tuple[int]:
        return (0,)

    def fetchall(self) -> list[tuple[int]]:
        return []


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


# ── Dispatch routing ──────────────────────────────────────────────


class TestPluginDispatchRouting:
    def test_plugin_spec_with_dispatch_is_called(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plugin spec with ``dispatch`` set short-circuits the
        built-in ``if/elif`` and receives a DispatchContext."""

        received: dict[str, Any] = {}

        def _dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None:
            received["ctx"] = ctx
            received["spec"] = spec

        spec = JobTypeSpec(
            name="plugin_demo",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
            dispatch=_dispatch,
        )

        # Patch the registry lookup so _run_one resolves our spec
        # without us having to wire entry points for this test.
        monkeypatch.setattr(
            claude_inproc,
            "get_job_type",
            lambda name: spec if name == "plugin_demo" else None,
        )
        # Disable the pre-run cancel check so the dispatch is
        # reached. Cancel handling has its own coverage above the
        # dispatch path; this test only asserts routing.
        monkeypatch.setattr(claude_inproc, "_is_cancel_requested", lambda *_: False)

        store = _FakeStore()
        claude_inproc._run_one(
            store,
            ref_id=42,
            title="job#42",
            meta={"job_type": "plugin_demo"},
        )

        assert received["spec"] is spec
        assert isinstance(received["ctx"], DispatchContext)
        assert received["ctx"].ref_id == 42
        assert received["ctx"].title == "job#42"
        assert received["ctx"].meta == {"job_type": "plugin_demo"}

    def test_builtin_without_dispatch_falls_through(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A spec with ``dispatch=None`` (built-ins) goes through
        the in-tree ``_run_fix_gripe`` / ``_run_plan_tick`` path."""

        spec = JobTypeSpec(
            name="fix_gripe",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
            # dispatch=None (the default) — built-in path
        )

        monkeypatch.setattr(claude_inproc, "get_job_type", lambda name: spec)
        monkeypatch.setattr(claude_inproc, "_is_cancel_requested", lambda *_: False)

        captured: list[tuple[int, Any]] = []
        monkeypatch.setattr(
            claude_inproc,
            "_run_fix_gripe",
            lambda store, ref_id, s: captured.append((ref_id, s)),
        )

        store = _FakeStore()
        claude_inproc._run_one(
            store,
            ref_id=43,
            title="job#43",
            meta={"job_type": "fix_gripe"},
        )

        assert captured == [(43, spec)]

    def test_plugin_dispatch_marks_succeeded_when_not_terminal(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A plugin dispatcher that returns without driving a terminal
        status leaves the job ``RUNNING``; the executor must finalize it
        to ``SUCCEEDED`` (else it lingers until the stuck-job sweeper
        reaps it as claim-orphaned → failed → wedges a recurring)."""
        spec = JobTypeSpec(
            name="plugin_demo",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
            dispatch=lambda ctx, spec: None,  # does its work, sets no status
        )
        monkeypatch.setattr(claude_inproc, "get_job_type", lambda name: spec)
        monkeypatch.setattr(claude_inproc, "_is_cancel_requested", lambda *_: False)
        # Job is still RUNNING after the dispatch returns.
        monkeypatch.setattr(
            claude_inproc,
            "_current_status",
            lambda conn, ref_id: claude_inproc._RUNNING,
        )
        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            claude_inproc,
            "_set_status",
            lambda store, ref_id, value, *, conn: calls.append((ref_id, value)),
        )

        claude_inproc._run_one(
            _FakeStore(), ref_id=50, title="t", meta={"job_type": "plugin_demo"}
        )

        assert calls == [(50, claude_inproc._SUCCEEDED)]

    def test_plugin_dispatch_leaves_terminal_status_untouched(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A dispatcher that already recorded a failure
        (``STATUS:failed``) must NOT be overwritten with ``succeeded``."""
        spec = JobTypeSpec(
            name="plugin_demo",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
            dispatch=lambda ctx, spec: None,  # imagine it called record_failure
        )
        monkeypatch.setattr(claude_inproc, "get_job_type", lambda name: spec)
        monkeypatch.setattr(claude_inproc, "_is_cancel_requested", lambda *_: False)
        # Dispatcher already drove the job terminal.
        monkeypatch.setattr(
            claude_inproc, "_current_status", lambda conn, ref_id: claude_inproc._FAILED
        )
        calls: list[tuple[int, str]] = []
        monkeypatch.setattr(
            claude_inproc,
            "_set_status",
            lambda store, ref_id, value, *, conn: calls.append((ref_id, value)),
        )

        claude_inproc._run_one(
            _FakeStore(), ref_id=51, title="t", meta={"job_type": "plugin_demo"}
        )

        assert calls == []  # finalize must not override a terminal status


class TestDispatchContextClosures:
    """The closures in DispatchContext call the executor helpers
    with the right ref_id, without leaking the executor's
    connection-management responsibilities to plugins."""

    def test_set_status_uses_set_status_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[Any, int, str]] = []

        def _spy(store: Any, ref_id: int, value: str, *, conn: Any) -> None:
            calls.append((store, ref_id, value))

        monkeypatch.setattr(claude_inproc, "_set_status", _spy)

        store = _FakeStore()
        ctx = claude_inproc._build_dispatch_context(store, ref_id=7, title="t", meta={})
        ctx.set_status("running")

        assert calls == [(store, 7, "running")]

    def test_append_chunk_uses_append_chunk_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[int, str, str]] = []

        def _spy(store: Any, ref_id: int, kind: str, text: str, *, conn: Any) -> None:
            calls.append((ref_id, kind, text))

        monkeypatch.setattr(claude_inproc, "_append_chunk", _spy)

        store = _FakeStore()
        ctx = claude_inproc._build_dispatch_context(store, ref_id=8, title="t", meta={})
        ctx.append_chunk("job_event", "hello")

        assert calls == [(8, "job_event", "hello")]

    def test_record_failure_uses_record_failure_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        calls: list[tuple[int, str]] = []

        def _spy(
            store: Any,
            ref_id: int,
            reason: str,
            *,
            gripe_rollback: Any,
            failure_class: str | None = None,
        ) -> None:
            calls.append((ref_id, reason))

        monkeypatch.setattr(claude_inproc, "_record_failure", _spy)

        store = _FakeStore()
        ctx = claude_inproc._build_dispatch_context(store, ref_id=9, title="t", meta={})
        ctx.record_failure("plugin said no")

        assert calls == [(9, "plugin said no")]

    def test_record_failure_threads_failure_class(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ctx.record_failure(reason, failure_class=...)`` must reach the
        ``_common.record_failure`` helper — the seam
        ``struct_relax``/``ssh_node`` rely on to distinguish an infra failure
        from a genuine physical (non-convergence) one."""
        calls: list[tuple[int, str, str | None]] = []

        def _spy(
            store: Any,
            ref_id: int,
            reason: str,
            *,
            gripe_rollback: Any,
            failure_class: str | None = None,
        ) -> None:
            calls.append((ref_id, reason, failure_class))

        monkeypatch.setattr(claude_inproc, "_record_failure", _spy)

        store = _FakeStore()
        ctx = claude_inproc._build_dispatch_context(store, ref_id=9, title="t", meta={})
        ctx.record_failure("container died", failure_class="infra")

        assert calls == [(9, "container died", "infra")]

    def test_is_cancel_requested_uses_helper(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        seen: list[int] = []

        def _spy(conn: Any, ref_id: int) -> bool:
            seen.append(ref_id)
            return False

        monkeypatch.setattr(claude_inproc, "_is_cancel_requested", _spy)

        store = _FakeStore()
        ctx = claude_inproc._build_dispatch_context(
            store, ref_id=10, title="t", meta={}
        )
        assert ctx.is_cancel_requested() is False
        assert seen == [10]
