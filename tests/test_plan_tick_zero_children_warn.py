"""gr333433: a tick that declares ``verdict: done`` while minting zero
child todos/jobs this tick must WARN, not silently pass. The prod
incident: a tick claimed to have "verified the runtime is a fresh docker
container ... wrote /data/hello.txt" on a todo it had no capability to
execute, then self-tagged ``STATUS:done`` with zero children spawned — a
soft, cheap signal for anyone auditing a suspicious "done", not a hard
gate (a planner legitimately closes a leaf that needed no execution)."""

from __future__ import annotations

import logging

import pytest

from precis.store import Store
from precis.utils.tick_conclusion import TickConclusion
from precis.workers.executors.claude_inproc import _build_job_result_text

pytestmark = pytest.mark.db


def _mk_parent_and_job(store: Store) -> tuple[int, int]:
    parent = store.insert_ref(kind="todo", slug=None, title="parent todo", meta={})
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick",
        meta={"executor": "claude_inproc", "job_type": "plan_tick"},
        parent_id=parent.id,
    )
    return int(parent.id), int(job.id)


def test_verdict_done_with_zero_children_warns(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    parent_id, job_id = _mk_parent_and_job(store)
    conclusion = TickConclusion(verdict="done", summary="closed with no work", files=[])

    with caplog.at_level(logging.WARNING, logger="precis.workers.executors.claude_inproc"):
        _build_job_result_text(
            store=store,
            job_ref_id=job_id,
            parent_ref_id=parent_id,
            model="sonnet",
            exit_code=0,
            duration_s=1.0,
            conclusion=conclusion,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("verdict=done" in r.getMessage() for r in warnings), caplog.text
    assert any(str(parent_id) in r.getMessage() for r in warnings), caplog.text


def test_verdict_done_with_children_minted_does_not_warn(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    parent_id, job_id = _mk_parent_and_job(store)
    store.insert_ref(
        kind="todo", slug=None, title="a minted subtask", meta={}, parent_id=parent_id
    )
    conclusion = TickConclusion(verdict="done", summary="closed after children", files=[])

    with caplog.at_level(logging.WARNING, logger="precis.workers.executors.claude_inproc"):
        _build_job_result_text(
            store=store,
            job_ref_id=job_id,
            parent_ref_id=parent_id,
            model="sonnet",
            exit_code=0,
            duration_s=1.0,
            conclusion=conclusion,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("verdict=done" in r.getMessage() for r in warnings), caplog.text


def test_verdict_continue_with_zero_children_does_not_warn(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """The warning is specific to ``done`` — a ``continue``/``yield``/
    ``halt`` verdict with no children minted this tick is a normal
    "still working" shape, not a suspicious close."""
    parent_id, job_id = _mk_parent_and_job(store)
    conclusion = TickConclusion(verdict="continue", summary="still going", files=[])

    with caplog.at_level(logging.WARNING, logger="precis.workers.executors.claude_inproc"):
        _build_job_result_text(
            store=store,
            job_ref_id=job_id,
            parent_ref_id=parent_id,
            model="sonnet",
            exit_code=0,
            duration_s=1.0,
            conclusion=conclusion,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("verdict=done" in r.getMessage() for r in warnings), caplog.text


def test_no_conclusion_block_does_not_warn(
    store: Store, caplog: pytest.LogCaptureFixture
) -> None:
    """No parsed tick-conclusion block at all (LLM omitted it) must not
    trip the check — there's no ``verdict`` to compare against."""
    parent_id, job_id = _mk_parent_and_job(store)

    with caplog.at_level(logging.WARNING, logger="precis.workers.executors.claude_inproc"):
        _build_job_result_text(
            store=store,
            job_ref_id=job_id,
            parent_ref_id=parent_id,
            model="sonnet",
            exit_code=0,
            duration_s=1.0,
            conclusion=None,
        )

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert not any("verdict=done" in r.getMessage() for r in warnings), caplog.text
