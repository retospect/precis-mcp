"""Declared halts preserve recovery context without silently losing work."""

from __future__ import annotations

import json
from typing import Any

import pytest

from precis.alerts import alert_history
from precis.store import Store
from precis.store.types import Tag
from precis.utils.prompt import AssemblyContext
from precis.utils.tick_conclusion import parse
from precis.workers import planner_prompt
from precis.workers.executors import claude_inproc as ci
from precis.workers.halt_policy import UNCERTAIN_HALT_TAG
from precis.workers.halt_recovery import advance_halt_recovery
from precis.workers.job_types.plan_tick import PlanTickOutcome
from tests.test_plan_tick_resume import _FakeSpec, _mk_job, _mk_parent


def _conclusion(halt_class: str | None = None, *, blocked_on: str | None = None) -> str:
    lines = ["=== TICK CONCLUSION ===", "verdict: halt"]
    if halt_class is not None:
        lines.append(f"halt-class: {halt_class}")
    if blocked_on is not None:
        lines.append(f"blocked-on: {blocked_on}")
    lines.extend(["summary: The database is unavailable.", "=== END ==="])
    return "\n".join(lines)


def _audit(conclusion: str) -> str:
    return "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init"},
            {
                "type": "result",
                "subtype": "success",
                "is_error": False,
                "result": conclusion,
            },
        )
    )


def _run_halt(
    store: Store,
    parent_id: int,
    halt_class: str | None = None,
    *,
    blocked_on: str | None = None,
    audited: bool = True,
    exit_code: int = 0,
) -> int:
    job_id = _mk_job(store, parent_id)
    text = _conclusion(halt_class, blocked_on=blocked_on)
    outcome = PlanTickOutcome(
        exit_code=exit_code,
        stdout=_audit(text) if audited else text,
        stderr="",
        duration_s=1.0,
    )
    ci._run_plan_tick(store, job_id, _FakeSpec(outcome))
    return job_id


@pytest.mark.parametrize(
    "claim_history", [{"attempts": 1}, {"reclaims": [{"why": "epoch"}]}]
)
def test_worker_restart_cannot_repeat_an_unaudited_recovery_attempt(
    store: Store, claim_history: dict[str, Any]
) -> None:
    parent = _mk_parent(store)
    _run_halt(store, parent, "misconceived")
    advance_halt_recovery(store)
    job = _mk_job(store, parent)
    store.update_ref(job, meta_patch=claim_history)

    class MustNotRun:
        name = "plan_tick"

        def run(self, **kwargs: Any) -> None:
            pytest.fail(
                "reclaimed recovery repeated before its prior outcome was reviewed"
            )

    ci._run_plan_tick(store, job, MustNotRun())
    assert UNCERTAIN_HALT_TAG in {str(tag) for tag in store.tags_for(parent)}
    assert "STATUS:succeeded" in {str(tag) for tag in store.tags_for(job)}
    with store.pool.connection() as conn:
        row = conn.execute("SELECT meta FROM refs WHERE ref_id = %s", (job,)).fetchone()
    assert row is not None and row[0].get("recovery_retry_blocked")


def _halt_meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    halt = row[0].get("halt")
    assert isinstance(halt, dict)
    return halt


@pytest.mark.parametrize("resume_reason", ["timeout", "max_turns", "paused"])
def test_interrupted_recovery_cannot_use_the_ordinary_resume_path(
    store: Store, resume_reason: str
) -> None:
    parent = _mk_parent(store)
    _run_halt(store, parent, "misconceived")
    advance_halt_recovery(store)
    assert _halt_meta(store, parent)["retry_released"] is True
    job = _mk_job(store, parent)
    outcome = PlanTickOutcome(
        exit_code=124,
        stdout="interrupted after a write with unknown outcome",
        stderr="timeout",
        duration_s=60.0,
        resume_reason=resume_reason,
    )
    ci._run_plan_tick(store, job, _FakeSpec(outcome))
    tags = {str(tag) for tag in store.tags_for(parent)}
    assert UNCERTAIN_HALT_TAG in tags
    assert not any(tag.startswith("child-failed:") for tag in tags)
    assert "STATUS:waiting_time" not in {str(tag) for tag in store.tags_for(job)}
    with store.pool.connection() as conn:
        meta = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (job,)
        ).fetchone()
    assert meta is not None and meta[0].get("recovery_retry_blocked")


@pytest.mark.parametrize("halt_class", ["infra", "blocked", "misconceived", "cost-cap"])
def test_parser_recognizes_declared_halt_classes(halt_class: str) -> None:
    parsed = parse(_conclusion(halt_class.upper(), blocked_on="database-admin"))
    assert parsed is not None
    assert parsed.halt_class == halt_class
    assert parsed.blocked_on == "database-admin"
    assert parsed.summary == "The database is unavailable."


@pytest.mark.parametrize("halt_class", [None, "unknown"])
def test_parser_defaults_unclassified_halts_to_infrastructure(
    halt_class: str | None,
) -> None:
    parsed = parse(_conclusion(halt_class))
    assert parsed is not None
    assert parsed.halt_class == "infra"


@pytest.mark.parametrize("halt_class", ["infra", None, "unknown"])
def test_infrastructure_halt_parks_with_job_bound_recovery_receipt(
    store: Store, halt_class: str | None
) -> None:
    parent_id = _mk_parent(store)
    job_id = _run_halt(store, parent_id, halt_class)

    tags = {str(tag) for tag in store.tags_for(parent_id)}
    assert "waiting-for:infra" in tags
    assert not any(tag == "halt" or tag.startswith("halt:") for tag in tags)
    halt = _halt_meta(store, parent_id)
    assert halt["class"] == "infra"
    assert halt["job_ref"] == job_id
    assert halt["retry_safe"] is True
    assert halt["retry_at"] > halt["at"]
    assert halt["summary_first_line"] == "The database is unavailable."
    assert _halt_meta(store, job_id) == halt
    assert "STATUS:succeeded" in {str(tag) for tag in store.tags_for(job_id)}


def test_unavailable_audit_requires_human_review_instead_of_blind_retry(
    store: Store,
) -> None:
    parent_id = _mk_parent(store)
    _run_halt(store, parent_id, audited=False)

    tags = {str(tag) for tag in store.tags_for(parent_id)}
    assert UNCERTAIN_HALT_TAG in tags
    assert "waiting-for:infra" not in tags
    halt = _halt_meta(store, parent_id)
    assert halt["retry_safe"] is False
    assert "retry_at" not in halt
    assert halt["audit_reason"]


@pytest.mark.parametrize(
    "target,expected_tag",
    [(None, "waiting-for:external"), ("database-admin", "waiting-for:database-admin")],
)
def test_blocked_halt_names_external_dependency_without_automatic_retry(
    store: Store, target: str | None, expected_tag: str
) -> None:
    parent_id = _mk_parent(store)
    _run_halt(store, parent_id, "blocked", blocked_on=target)

    assert expected_tag in {str(tag) for tag in store.tags_for(parent_id)}
    halt = _halt_meta(store, parent_id)
    assert halt["class"] == "blocked"
    assert "retry_at" not in halt


def test_cost_cap_remains_terminal_without_automatic_retry(store: Store) -> None:
    parent_id = _mk_parent(store)
    _run_halt(store, parent_id, "cost-cap")

    assert "halt:cost-cap" in {str(tag) for tag in store.tags_for(parent_id)}
    assert "retry_at" not in _halt_meta(store, parent_id)


def test_misconceived_task_gets_one_prompted_repair_then_terminal_alert(
    store: Store,
) -> None:
    parent_id = _mk_parent(store)
    _run_halt(store, parent_id, "misconceived")

    tags = {str(tag) for tag in store.tags_for(parent_id)}
    assert "waiting-for:plan-repair" in tags
    assert "halt:misconceived" not in tags
    halt = _halt_meta(store, parent_id)
    assert halt["repair_pending"] is True
    prompt = planner_prompt._m_identity(
        AssemblyContext(store=store, ref_id=parent_id, model="sonnet")
    )
    assert "The database is unavailable." in prompt
    assert "re-plan" in prompt
    assert "halt-class: misconceived" in prompt

    store.remove_tag(parent_id, Tag.open("waiting-for:plan-repair"))
    job_id = _run_halt(store, parent_id, "misconceived")

    assert "halt:misconceived" in {str(tag) for tag in store.tags_for(parent_id)}
    halt = _halt_meta(store, parent_id)
    assert halt["job_ref"] == job_id
    assert halt["repair_pending"] is False
    assert "retry_at" not in halt
    history = alert_history(
        store, source="planner:misconceived", fingerprint=f"misconceived:{parent_id}"
    )
    assert history["state"] == "open"
    assert history["open"]["severity"] == "info"


def test_declared_halt_handles_failed_subprocess_without_failure_bubble(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    parent_id = _mk_parent(store)
    monkeypatch.setenv("PRECIS_PLAN_TICK_RESUME_CAP", "0")
    job_id = _run_halt(store, parent_id, exit_code=1)

    assert "STATUS:succeeded" in {str(tag) for tag in store.tags_for(job_id)}
    tags = {str(tag) for tag in store.tags_for(parent_id)}
    assert "waiting-for:infra" in tags
    assert not any(tag.startswith("child-failed:") for tag in tags)
    with store.pool.connection() as conn:
        jobs = conn.execute(
            "SELECT ref_id FROM refs WHERE kind = 'job' AND parent_id = %s",
            (parent_id,),
        ).fetchall()
    assert [row[0] for row in jobs] == [job_id]


def test_clean_intermediate_tick_does_not_restore_the_repair_allowance(
    store: Store,
) -> None:
    parent_id = _mk_parent(store)
    _run_halt(store, parent_id, "misconceived")
    store.remove_tag(parent_id, Tag.open("waiting-for:plan-repair"))
    job_id = _mk_job(store, parent_id)
    conclusion = _conclusion().replace("verdict: halt", "verdict: continue")
    outcome = PlanTickOutcome(
        exit_code=0, stdout=_audit(conclusion), stderr="", duration_s=1.0
    )
    ci._run_plan_tick(store, job_id, _FakeSpec(outcome))

    halt = _halt_meta(store, parent_id)
    assert halt["repair_pending"] is False
    prompt = planner_prompt._m_identity(
        AssemblyContext(store=store, ref_id=parent_id, model="sonnet")
    )
    assert "Plan repair" not in prompt
    _run_halt(store, parent_id, "misconceived")
    assert "halt:misconceived" in {str(tag) for tag in store.tags_for(parent_id)}


def test_runner_preserves_existing_human_halt(store: Store) -> None:
    parent_id = _mk_parent(store)
    store.add_tag(parent_id, Tag.open("halt:human-pause"), set_by="user")
    _run_halt(store, parent_id)

    assert "halt:human-pause" in {str(tag) for tag in store.tags_for(parent_id)}


def test_runner_clears_only_provable_same_tick_worker_halt(store: Store) -> None:
    parent_id = _mk_parent(store)
    job_id = _mk_job(store, parent_id)

    class SelfHaltingSpec(_FakeSpec):
        def run(self, **_kw: object) -> PlanTickOutcome:
            store.add_tag(parent_id, Tag.open("halt:planner-stuck"), set_by="agent")
            with store.tx() as conn:
                conn.execute(
                    "UPDATE ref_tags SET principal = 'worker:plan_tick' "
                    "WHERE ref_id = %s AND tag_id = "
                    "(SELECT tag_id FROM tags WHERE namespace = 'OPEN' "
                    "AND value = 'halt:planner-stuck')",
                    (parent_id,),
                )
            return self._outcome

    outcome = PlanTickOutcome(
        exit_code=0,
        stdout=_audit(_conclusion()),
        stderr="",
        duration_s=1.0,
    )
    ci._run_plan_tick(store, job_id, SelfHaltingSpec(outcome))

    tags = {str(tag) for tag in store.tags_for(parent_id)}
    assert "halt:planner-stuck" not in tags
    assert "waiting-for:infra" in tags
