"""``claude_inproc._run_doctor_tick`` — the executor arm's own job-ref
bookkeeping (job_summary/job_result/transcript/status), mirroring
``test_plan_tick_resume.py``'s ``_FakeSpec`` pattern: ``spec.run`` is
stubbed so this only exercises the executor's side, not
``doctor_tick.run`` itself (covered by ``tests/test_doctor_tick.py``).
"""

from __future__ import annotations

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors import claude_inproc as ci
from precis.workers.job_types.doctor_tick import DoctorTickOutcome

pytestmark = pytest.mark.db


class _FakeSpec:
    name = "doctor_tick"

    def __init__(self, outcome: DoctorTickOutcome) -> None:
        self._outcome = outcome

    def run(self, **_kw: object) -> DoctorTickOutcome:
        return self._outcome


def _mk_job(store: Store) -> int:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="doctor_tick",
        meta={"executor": "claude_inproc", "job_type": "doctor_tick", "params": {}},
    )
    store.add_tag(
        job.id, Tag.closed("STATUS", "running"), set_by="system", replace_prefix=True
    )
    return job.id


def _job_result_texts(store: Store, job_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_result'",
            (job_id,),
        ).fetchall()
    return [r[0] for r in rows]


def _job_summary_texts(store: Store, job_id: int) -> list[str]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_summary'",
            (job_id,),
        ).fetchall()
    return [r[0] for r in rows]


def test_success_writes_summary_result_transcript_and_succeeds(store: Store) -> None:
    job_id = _mk_job(store)
    outcome = DoctorTickOutcome(
        exit_code=0,
        text="## Classification\nall green",
        raw_text="<stream-json full transcript>",
        error=None,
        duration_s=7.5,
        cost_usd=0.02,
        report_ref_id=999,
    )

    ci._run_doctor_tick(store, job_id, _FakeSpec(outcome))

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:succeeded" in job_tags
    assert any("Classification" in t for t in _job_summary_texts(store, job_id))
    result_texts = _job_result_texts(store, job_id)
    assert any("exit=0" in t and "report=ref:999" in t for t in result_texts)
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta->>'transcript', meta->>'wall_seconds' FROM refs "
            "WHERE ref_id = %s",
            (job_id,),
        ).fetchone()
    assert row is not None
    assert row[0] == "<stream-json full transcript>"
    assert float(row[1]) == pytest.approx(7.5)


def test_failure_marks_failed_and_records_error(store: Store) -> None:
    job_id = _mk_job(store)
    outcome = DoctorTickOutcome(
        exit_code=1,
        text="",
        raw_text="<partial stream>",
        error="doctor_tick: empty reply — nothing to report",
        duration_s=2.0,
        cost_usd=None,
        report_ref_id=None,
    )

    ci._run_doctor_tick(store, job_id, _FakeSpec(outcome))

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:failed" in job_tags
    result_texts = _job_result_texts(store, job_id)
    assert any("empty reply" in t for t in result_texts)


def test_run_raising_is_recorded_as_a_job_event_and_failed(store: Store) -> None:
    job_id = _mk_job(store)

    class _RaisingSpec:
        name = "doctor_tick"

        def run(self, **_kw: object) -> DoctorTickOutcome:
            raise RuntimeError("boom")

    ci._run_doctor_tick(store, job_id, _RaisingSpec())

    job_tags = {str(t) for t in store.tags_for(job_id)}
    assert "STATUS:failed" in job_tags
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND chunk_kind = 'job_event'",
            (job_id,),
        ).fetchall()
    assert any("boom" in r[0] for r in rows)


def test_registry_dispatches_to_run_doctor_tick(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_run_one`` routes ``job_type='doctor_tick'`` to ``_run_doctor_tick``
    (the hardcoded-run branch, not the plugin ``dispatch`` protocol)."""
    job_id = _mk_job(store)
    called: dict[str, object] = {}

    def fake_run_doctor_tick(s: Store, ref_id: int, spec: object) -> None:
        called["ref_id"] = ref_id

    monkeypatch.setattr(ci, "_run_doctor_tick", fake_run_doctor_tick)
    ctx = ci._build_dispatch_context(
        store,
        job_id,
        title="doctor_tick",
        meta={"job_type": "doctor_tick", "executor": "claude_inproc", "params": {}},
    )

    ci._run_one(ctx)

    assert called["ref_id"] == job_id
