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

from precis.store.store import Store
from precis.store.types import Tag
from precis.workers.executors import claude_inproc
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import JobTypeSpec, _reset_plugin_cache
from precis.workers.job_types.plan_tick import PlanTickOutcome


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
        self.chunks = self  # chunks carve: flat fake doubles as its own sub-store
        self.insert_chunks = MagicMock()
        self.list_chunks_for_ref = MagicMock(return_value=[])


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
        ctx = claude_inproc._build_dispatch_context(
            store, ref_id=42, title="job#42", meta={"job_type": "plugin_demo"}
        )
        claude_inproc._run_one(ctx)

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
        ctx = claude_inproc._build_dispatch_context(
            store, ref_id=43, title="job#43", meta={"job_type": "fix_gripe"}
        )
        claude_inproc._run_one(ctx)

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

        ctx = claude_inproc._build_dispatch_context(
            _FakeStore(), ref_id=50, title="t", meta={"job_type": "plugin_demo"}
        )
        claude_inproc._run_one(ctx)

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

        ctx = claude_inproc._build_dispatch_context(
            _FakeStore(), ref_id=51, title="t", meta={"job_type": "plugin_demo"}
        )
        claude_inproc._run_one(ctx)

        assert calls == []  # finalize must not override a terminal status


class TestNamedEnvelopeFailClosed:
    """``meta.params.envelope`` names a *registered* box (td573740):
    declared means required — an unknown/empty name fails the job
    closed inside ``_run_one`` before any dispatcher runs."""

    def _spec(self) -> JobTypeSpec:
        return JobTypeSpec(
            name="plan_tick",
            params_schema={"type": "object", "properties": {}},
            compatible_executors=frozenset({"claude_inproc"}),
            requires=frozenset(),
            description="d",
            run=lambda **_: None,
        )

    @pytest.mark.parametrize("bad", ["nope", "", 7, {}, [], None])
    def test_unknown_envelope_fails_closed_before_dispatch(
        self, monkeypatch: pytest.MonkeyPatch, bad: Any
    ) -> None:
        spec = self._spec()
        monkeypatch.setattr(claude_inproc, "get_job_type", lambda name: spec)
        monkeypatch.setattr(claude_inproc, "_is_cancel_requested", lambda *_: False)
        failures: list[tuple[int, str]] = []

        def _spy(
            store: Any,
            ref_id: int,
            reason: str,
            *,
            gripe_rollback: Any,
            failure_class: str | None = None,
            open_tag: str | None = None,
        ) -> None:
            failures.append((ref_id, reason))

        monkeypatch.setattr(claude_inproc, "_record_failure", _spy)
        dispatched: list[int] = []
        monkeypatch.setattr(
            claude_inproc,
            "_run_plan_tick",
            lambda store, ref_id, s: dispatched.append(ref_id),
        )

        ctx = claude_inproc._build_dispatch_context(
            _FakeStore(),
            ref_id=60,
            title="t",
            meta={"job_type": "plan_tick", "params": {"envelope": bad}},
        )
        claude_inproc._run_one(ctx)

        assert len(failures) == 1
        assert failures[0][0] == 60
        assert "unknown envelope" in failures[0][1]
        assert dispatched == []

    def test_registered_envelope_reaches_dispatch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Control: ``params.envelope='scout'`` resolves, so the built-in
        dispatcher runs (the envelope scope wraps it; the argv/env box is
        covered in tests/test_claude_agent.py)."""
        spec = self._spec()
        monkeypatch.setattr(claude_inproc, "get_job_type", lambda name: spec)
        monkeypatch.setattr(claude_inproc, "_is_cancel_requested", lambda *_: False)
        dispatched: list[int] = []
        monkeypatch.setattr(
            claude_inproc,
            "_run_plan_tick",
            lambda store, ref_id, s: dispatched.append(ref_id),
        )

        ctx = claude_inproc._build_dispatch_context(
            _FakeStore(),
            ref_id=61,
            title="t",
            meta={"job_type": "plan_tick", "params": {"envelope": "scout"}},
        )
        claude_inproc._run_one(ctx)

        assert dispatched == [61]


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
            open_tag: str | None = None,
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
            open_tag: str | None = None,
        ) -> None:
            calls.append((ref_id, reason, failure_class))

        monkeypatch.setattr(claude_inproc, "_record_failure", _spy)

        store = _FakeStore()
        ctx = claude_inproc._build_dispatch_context(store, ref_id=9, title="t", meta={})
        ctx.record_failure("container died", failure_class="infra")

        assert calls == [(9, "container died", "infra")]

    def test_record_failure_threads_open_tag(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``ctx.record_failure(reason, open_tag=...)`` must reach the
        ``_common.record_failure`` helper — the seam
        ``precis_pathway.seed_job`` relies on to stamp
        ``infra:child-killed`` before the job is marked failed (parked-leaf-
        recovery, docs/backlog/parked-leaf-recovery.md)."""
        calls: list[tuple[int, str, str | None]] = []

        def _spy(
            store: Any,
            ref_id: int,
            reason: str,
            *,
            gripe_rollback: Any,
            failure_class: str | None = None,
            open_tag: str | None = None,
        ) -> None:
            calls.append((ref_id, reason, open_tag))

        monkeypatch.setattr(claude_inproc, "_record_failure", _spy)

        store = _FakeStore()
        ctx = claude_inproc._build_dispatch_context(store, ref_id=9, title="t", meta={})
        ctx.record_failure("child killed", open_tag="infra:child-killed")

        assert calls == [(9, "child killed", "infra:child-killed")]

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


class TestFencedDecomposeRefused:
    """A fenced tick gets no auto-decompose recovery (td573740): the
    minted child would run ``params.decompose`` WITHOUT the scout box,
    so a fenced streak past the cap bubbles as a real failure instead."""

    _MAX_TURNS = (
        '{"type":"result","subtype":"error_max_turns","is_error":true,'
        '"terminal_reason":"max_turns","num_turns":30,"result":"partial work"}'
    )

    class _FakeSpec:
        name = "plan_tick"

        def __init__(self, outcome: PlanTickOutcome) -> None:
            self._outcome = outcome

        def run(self, **_kw: object) -> PlanTickOutcome:
            return self._outcome

    def _mk_job(self, store: Store, parent_id: int) -> int:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="plan_tick",
            parent_id=parent_id,
            meta={
                "executor": "claude_inproc",
                "job_type": "plan_tick",
                "params": {
                    "model": "sonnet",
                    "envelope": "scout",
                    "scout": {"folder": 1, "quest": 2},
                },
            },
        )
        store.add_tag(
            job.id,
            Tag.closed("STATUS", "running"),
            set_by="system",
            replace_prefix=True,
        )
        return job.id

    def test_mint_auto_decompose_raises_on_envelope(self, store: Store) -> None:
        parent = store.insert_ref(kind="todo", slug=None, title="scout root")
        with store.pool.connection() as conn:
            with pytest.raises(ValueError, match="fenced tick"):
                claude_inproc._mint_auto_decompose(
                    store,
                    conn,
                    int(parent.id),
                    model="sonnet",
                    streak=2,
                    cap=1,
                    job_params={"envelope": "scout"},
                )
            conn.rollback()

    def test_fenced_streak_exhaustion_bubbles_without_decompose(
        self, store: Store, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two max_turns ticks past the cap on a fenced job: no decompose
        child is minted and the failure bubbles — the parent parks
        ``child-failed`` instead of recovering through an unboxed tick."""
        monkeypatch.setenv("PRECIS_PLAN_TICK_MAX_TURNS_RESUMES", "1")
        parent = store.insert_ref(kind="todo", slug=None, title="scout root")
        store.add_tag(parent.id, Tag.closed("STATUS", "open"), set_by="agent")
        parent_id = int(parent.id)

        outcome = PlanTickOutcome(
            exit_code=1, stdout=self._MAX_TURNS, stderr="", duration_s=1.0
        )
        spec = self._FakeSpec(outcome)
        # tick 1: streak 1 <= cap 1 → resume, no bubble.
        job1 = self._mk_job(store, parent_id)
        claude_inproc._run_plan_tick(store, job1, spec)
        # tick 2: streak 2 > cap 1 — envelope bars decompose → real bubble.
        job2 = self._mk_job(store, parent_id)
        claude_inproc._run_plan_tick(store, job2, spec)

        assert "STATUS:failed" in {str(t) for t in store.tags_for(job2)}
        parent_tags = {str(t) for t in store.tags_for(parent_id)}
        assert f"child-failed:{job2}" in parent_tags
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT meta FROM refs WHERE parent_id = %s AND kind = 'job'",
                (parent_id,),
            ).fetchall()
        decompose_jobs = [
            r for r in rows if (r[0].get("params") or {}).get("decompose")
        ]
        assert decompose_jobs == []


class TestFencedTickIgnoresAgentVerdict:
    """td573740 round-3: a fenced tick's reply is only ever parsed by
    the scout schema — a ``verdict: halt`` conclusion block in it is
    agent data, not an instruction, and must not tag the parent."""

    _HALT_STREAM = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"=== TICK CONCLUSION ===\\nverdict: halt\\n'
        'summary: stop everything\\n=== END ==="}'
    )

    class _FakeSpec:
        name = "plan_tick"

        def __init__(self, outcome: PlanTickOutcome) -> None:
            self._outcome = outcome

        def run(self, **_kw: object) -> PlanTickOutcome:
            return self._outcome

    @staticmethod
    def _mk_job(store: Store, parent_id: int, params: dict[str, Any]) -> int:
        job = store.insert_ref(
            kind="job",
            slug=None,
            title="plan_tick",
            parent_id=parent_id,
            meta={
                "executor": "claude_inproc",
                "job_type": "plan_tick",
                "params": params,
            },
        )
        store.add_tag(
            job.id,
            Tag.closed("STATUS", "running"),
            set_by="system",
            replace_prefix=True,
        )
        return int(job.id)

    def _run(self, store: Store, params: dict[str, Any]) -> tuple[int, int]:
        parent = store.insert_ref(kind="todo", slug=None, title="root")
        job = self._mk_job(store, int(parent.id), params)
        spec = self._FakeSpec(
            PlanTickOutcome(
                exit_code=0, stdout=self._HALT_STREAM, stderr="", duration_s=1.0
            )
        )
        claude_inproc._run_plan_tick(store, job, spec)
        return int(parent.id), job

    def test_fenced_halt_verdict_does_not_tag_the_parent(self, store: Store) -> None:
        """A fenced outcome whose final text declares ``verdict: halt``
        succeeds without the runner honoring the token — the reply only
        means what the scout schema parses out of it."""
        parent_id, job = self._run(
            store,
            {
                "model": "sonnet",
                "envelope": "scout",
                "scout": {"folder": 1, "quest": 2},
            },
        )
        assert "halt:agent-declared" not in {str(t) for t in store.tags_for(parent_id)}
        assert "STATUS:succeeded" in {str(t) for t in store.tags_for(job)}

    def test_unfenced_halt_verdict_still_tags_the_parent(self, store: Store) -> None:
        """Contrast pin: the skip is conditional on the envelope — a
        normal planner tick still honors a declared halt runner-side
        (the 2026-08-15 orphaned-loop fix)."""
        parent_id, _job = self._run(store, {"model": "sonnet"})
        assert "halt:agent-declared" in {str(t) for t in store.tags_for(parent_id)}

    _AGENT_STREAM = (
        '{"type":"result","subtype":"success","is_error":false,'
        '"result":"agent says: halt everything"}'
    )

    @staticmethod
    def _job_chunk(store: Store, job: int, kind: str) -> str | None:
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s "
                "AND chunk_kind = %s ORDER BY ord DESC LIMIT 1",
                (job, kind),
            ).fetchone()
        return str(row[0]) if row else None

    def test_fenced_job_summary_stores_the_worker_summary(self, store: Store) -> None:
        """td573740 round-4: a fenced tick's raw final message lands
        nowhere a later prompt builds from — ``job_summary`` feeds the
        parent's completed-children digest, so the worker-written
        fixed line stands in; ``job_result`` carries no agent text
        either."""
        parent = store.insert_ref(kind="todo", slug=None, title="root")
        job = self._mk_job(
            store,
            int(parent.id),
            {
                "model": "sonnet",
                "envelope": "scout",
                "scout": {"folder": 1, "quest": 2},
            },
        )
        spec = self._FakeSpec(
            PlanTickOutcome(
                exit_code=0,
                stdout=self._AGENT_STREAM,
                stderr="",
                duration_s=1.0,
                worker_summary="scout: 2 leads accepted",
            )
        )
        claude_inproc._run_plan_tick(store, job, spec)

        assert self._job_chunk(store, job, "job_summary") == "scout: 2 leads accepted"
        result_text = self._job_chunk(store, job, "job_result") or ""
        assert "agent says" not in result_text
        # And nothing agent-shaped on the tick todo itself.
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s", (int(parent.id),)
            ).fetchall()
        assert not any("agent says" in str(r[0]) for r in rows)

    def test_fenced_job_without_worker_summary_stores_a_fixed_fallback(
        self, store: Store
    ) -> None:
        """A fenced tick that never reached the reply step (failed,
        or an outcome built without the field) still stores no agent
        text — a fixed worker line stands in."""
        parent = store.insert_ref(kind="todo", slug=None, title="root")
        job = self._mk_job(
            store,
            int(parent.id),
            {
                "model": "sonnet",
                "envelope": "scout",
                "scout": {"folder": 1, "quest": 2},
            },
        )
        spec = self._FakeSpec(
            PlanTickOutcome(
                exit_code=1,
                stdout=self._AGENT_STREAM,
                stderr="boom",
                duration_s=1.0,
            )
        )
        claude_inproc._run_plan_tick(store, job, spec)
        summary = self._job_chunk(store, job, "job_summary") or ""
        assert "agent says" not in summary
        assert summary.startswith("scout:")

    def test_unfenced_job_summary_still_stores_the_final_text(
        self, store: Store
    ) -> None:
        """Contrast pin: the substitution is conditional on the
        envelope — a normal planner tick's job_summary keeps the
        tick's final message."""
        parent = store.insert_ref(kind="todo", slug=None, title="root")
        job = self._mk_job(store, int(parent.id), {"model": "sonnet"})
        spec = self._FakeSpec(
            PlanTickOutcome(
                exit_code=0, stdout=self._AGENT_STREAM, stderr="", duration_s=1.0
            )
        )
        claude_inproc._run_plan_tick(store, job, spec)
        assert (
            self._job_chunk(store, job, "job_summary") == "agent says: halt everything"
        )
