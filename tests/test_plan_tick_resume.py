"""Fix B — a plan_tick that exhausts its --max-turns budget is *resumable*,
not a hard failure.

A coroutine tick cut off at the turn ceiling left productive work on the
table; the next tick continues with a fresh budget. So instead of
bubbling it (which parks the parent out of the rotation), the executor
marks it succeeded-but-non-blocking up to a per-parent streak cap — and
only bubbles when a tick runs out *repeatedly* (the task genuinely needs
splitting). Covers the stream detector, the streak helpers, the audit
text, and the executor's resume-vs-bubble decision end to end.
"""

from __future__ import annotations

from precis.store.store import Store
from precis.store.types import Tag
from precis.utils.claude_agent import stream_terminal_reason
from precis.workers.executors import claude_inproc as ci
from precis.workers.job_types.plan_tick import PlanTickOutcome

# A trailing stream-json result event for each terminal condition.
_MAX_TURNS = (
    '{"type":"result","subtype":"error_max_turns","is_error":true,'
    '"terminal_reason":"max_turns","num_turns":30,"result":"partial work"}'
)
_CLEAN = '{"type":"result","subtype":"success","is_error":false,"result":"done"}'
_OTHER_ERR = (
    '{"type":"result","subtype":"error_during_execution","is_error":true,'
    '"result":"boom"}'
)


# ── stream detector ────────────────────────────────────────────────


def test_stream_terminal_reason_detects_max_turns() -> None:
    assert stream_terminal_reason(_MAX_TURNS) == "max_turns"


def test_stream_terminal_reason_clean_is_none() -> None:
    assert stream_terminal_reason(_CLEAN) is None
    assert stream_terminal_reason("") is None
    assert stream_terminal_reason("plain stub text, no result event") is None


def test_stream_terminal_reason_other_error_passthrough() -> None:
    assert stream_terminal_reason(_OTHER_ERR) == "error_during_execution"


# ── streak helpers ─────────────────────────────────────────────────


def test_streak_bump_and_reset(store: Store) -> None:
    parent = store.insert_ref(kind="todo", slug=None, title="P")
    with store.pool.connection() as conn:
        assert ci._bump_max_turns_streak(conn, parent.id) == 1
        assert ci._bump_max_turns_streak(conn, parent.id) == 2
        conn.commit()
    with store.pool.connection() as conn:
        ci._reset_max_turns_streak(conn, parent.id)
        conn.commit()
    with store.pool.connection() as conn:
        assert ci._bump_max_turns_streak(conn, parent.id) == 1  # reset to 0, +1
        conn.commit()


# ── executor resume-vs-bubble ──────────────────────────────────────


class _FakeSpec:
    """Stands in for the plan_tick JobTypeSpec — returns a canned outcome
    instead of shelling out to ``claude -p``."""

    name = "plan_tick"

    def __init__(self, outcome: PlanTickOutcome) -> None:
        self._outcome = outcome

    def run(self, **_kw: object) -> PlanTickOutcome:
        return self._outcome


def _mk_parent(store: Store) -> int:
    parent = store.insert_ref(kind="todo", slug=None, title="enrich a thing")
    store.add_tag(
        parent.id, Tag.closed("STATUS", "open"), set_by="agent", replace_prefix=True
    )
    store.add_tag(parent.id, Tag.closed("LLM", "sonnet"), set_by="agent")
    return parent.id


def _mk_job(store: Store, parent_id: int) -> int:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick",
        parent_id=parent_id,
        meta={
            "executor": "claude_inproc",
            "job_type": "plan_tick",
            "params": {"model": "sonnet"},
        },
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "running"), set_by="system", replace_prefix=True
    )
    return job.id


def _run(store: Store, job_id: int, stream: str, exit_code: int) -> None:
    spec = _FakeSpec(
        PlanTickOutcome(
            exit_code=exit_code, stdout=stream, stderr="", duration_s=3.0
        )
    )
    ci._run_plan_tick(store, job_id, spec)


def test_max_turns_resumes_without_bubbling(store: Store) -> None:
    """A max-turns tick under the cap → STATUS:succeeded, no bubble, the
    parent's streak ticks up, and the audit reads 'resumed'."""
    parent_id = _mk_parent(store)
    job_id = _mk_job(store, parent_id)

    _run(store, job_id, _MAX_TURNS, exit_code=1)

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:succeeded" in job_tags
    parent_tags = {str(t) for t in store.tags_for(parent_id)}
    assert not any(t.startswith("child-failed:") for t in parent_tags)
    with store.pool.connection() as conn:
        streak = conn.execute(
            "SELECT (meta->>'plan_tick_max_turns_streak')::int FROM refs "
            "WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()[0]
    assert streak == 1
    # job_result audit chunk reflects the resume
    with store.pool.connection() as conn:
        results = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_result'",
            (job_id,),
        ).fetchall()
    assert any("resumed" in r[0] for r in results)


def test_repeated_max_turns_past_cap_bubbles(store: Store, monkeypatch) -> None:
    """Past the cap, a max-turns tick bubbles as a real failure so the
    owner can split the task."""
    monkeypatch.setenv("PRECIS_PLAN_TICK_MAX_TURNS_RESUMES", "1")
    parent_id = _mk_parent(store)

    # tick 1: streak 1 <= cap 1 → resume
    _run(store, _mk_job(store, parent_id), _MAX_TURNS, exit_code=1)
    parent_tags = {str(t) for t in store.tags_for(parent_id)}
    assert not any(t.startswith("child-failed:") for t in parent_tags)

    # tick 2: streak 2 > cap 1 → bubble
    job2 = _mk_job(store, parent_id)
    _run(store, job2, _MAX_TURNS, exit_code=1)
    job_tags = {str(t) for t in store.tags_for(job2)}
    assert "STATUS:failed" in job_tags
    parent_tags = {str(t) for t in store.tags_for(parent_id)}
    assert f"child-failed:{job2}" in parent_tags


def test_real_failure_still_bubbles_and_resets_streak(store: Store) -> None:
    """A non-max-turns failure bubbles as before and clears any streak."""
    parent_id = _mk_parent(store)
    # prime a streak
    _run(store, _mk_job(store, parent_id), _MAX_TURNS, exit_code=1)
    # now a genuine error
    job2 = _mk_job(store, parent_id)
    _run(store, job2, _OTHER_ERR, exit_code=1)
    job_tags = {str(t) for t in store.tags_for(job2)}
    assert "STATUS:failed" in job_tags
    parent_tags = {str(t) for t in store.tags_for(parent_id)}
    assert f"child-failed:{job2}" in parent_tags
    with store.pool.connection() as conn:
        present = conn.execute(
            "SELECT meta ? 'plan_tick_max_turns_streak' FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()[0]
    assert present is False  # reset


def test_clean_tick_succeeds_and_resets_streak(store: Store) -> None:
    parent_id = _mk_parent(store)
    _run(store, _mk_job(store, parent_id), _MAX_TURNS, exit_code=1)  # prime streak
    job2 = _mk_job(store, parent_id)
    _run(store, job2, _CLEAN, exit_code=0)
    job_tags = {str(t) for t in store.tags_for(job2)}
    assert "STATUS:succeeded" in job_tags
    with store.pool.connection() as conn:
        present = conn.execute(
            "SELECT meta ? 'plan_tick_max_turns_streak' FROM refs WHERE ref_id = %s",
            (parent_id,),
        ).fetchone()[0]
    assert present is False
