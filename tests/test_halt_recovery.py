"""Audited halt parks resume; shared infrastructure outages admit one probe."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.alerts import resolve_alert
from precis.store import Store
from precis.store.types import Tag
from precis.workers import halt_recovery as recovery


def _tags(store: Store, ref_id: int) -> set[str]:
    return {str(tag) for tag in store.tags_for(ref_id)}


def _meta(store: Store, ref_id: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT meta FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
    assert row is not None
    return dict(row[0])


def _parent(store: Store) -> int:
    ref = store.insert_ref(kind="todo", slug=None, title="Recover infrastructure halt")
    store.add_tag(ref.id, Tag.closed("STATUS", "open"), set_by="system")
    return ref.id


def _halted_job(
    store: Store,
    parent_id: int,
    *,
    due: bool = True,
    halt_class: str = "infra",
    retry_safe: bool = True,
) -> int:
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="Halted tick",
        parent_id=parent_id,
        meta={"executor": "claude_inproc", "job_type": "plan_tick"},
    )
    now = datetime.now(UTC)
    halt = {
        "class": halt_class,
        "job_ref": job.id,
        "at": now.isoformat(),
        "retry_safe": retry_safe,
        "retry_at": (now + timedelta(hours=-1 if due else 1)).isoformat(),
    }
    store.update_ref(parent_id, meta_patch={"halt": halt})
    store.update_ref(job.id, meta_patch={"halt": halt})
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")
    park = (
        "waiting-for:plan-repair"
        if halt_class == "misconceived"
        else "waiting-for:infra"
    )
    store.add_tag(parent_id, Tag.open(park), set_by="system")
    return job.id


def _alerts(store: Store) -> list[tuple[int, dict[str, Any], Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta, resolved_at FROM refs WHERE kind = 'alert' "
            "AND alert_source = %s AND fingerprint = %s ORDER BY ref_id",
            (recovery.SOURCE, recovery.FINGERPRINT),
        ).fetchall()
    return [(int(row[0]), dict(row[1]), row[2]) for row in rows]


def _trip(store: Store) -> tuple[list[int], int]:
    parents = [_parent(store), _parent(store)]
    _halted_job(store, parents[0])
    _halted_job(store, parents[0])
    _halted_job(store, parents[1])
    recovery.advance_halt_recovery(store)
    alerts = _alerts(store)
    assert len(alerts) == 1
    assert alerts[0][1]["breaker_state"] == "open"
    return parents, alerts[0][0]


def _probe(store: Store, alert_id: int) -> int:
    store.update_ref(
        alert_id,
        meta_patch={
            "next_probe_at": (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
        },
    )
    recovery.advance_halt_recovery(store)
    state = _meta(store, alert_id)
    assert state["breaker_state"] == "half-open"
    return int(state["probe_parent"])


def test_due_infra_park_releases_with_matching_audited_job(store: Store) -> None:
    parent = _parent(store)
    _halted_job(store, parent)

    recovery.advance_halt_recovery(store)

    assert "waiting-for:infra" not in _tags(store, parent)
    assert not any(tag.startswith("halt:") for tag in _tags(store, parent))
    halt = _meta(store, parent)["halt"]
    assert halt["retry_released"] is True
    assert "retry_at" not in halt
    assert _alerts(store) == []


def test_infra_park_is_not_released_before_retry_time(store: Store) -> None:
    parent = _parent(store)
    _halted_job(store, parent, due=False)

    recovery.advance_halt_recovery(store)

    assert "waiting-for:infra" in _tags(store, parent)
    assert not _meta(store, parent)["halt"].get("retry_released")


@pytest.mark.parametrize("receipt", ["missing", "unsafe", "wrong-job", "other-parent"])
def test_unverifiable_job_receipt_requires_human_review(
    store: Store, receipt: str
) -> None:
    parent = _parent(store)
    job = _halted_job(store, parent)
    if receipt == "missing":
        store.update_ref(job, meta_patch={"halt": None})
    elif receipt == "unsafe":
        store.update_ref(
            job, meta_patch={"halt": {**_meta(store, job)["halt"], "retry_safe": False}}
        )
    elif receipt == "wrong-job":
        store.update_ref(
            job, meta_patch={"halt": {**_meta(store, job)["halt"], "job_ref": job + 1}}
        )
    else:
        alien = _parent(store)
        alien_job = _halted_job(store, alien, due=False)
        store.update_ref(
            parent,
            meta_patch={"halt": {**_meta(store, parent)["halt"], "job_ref": alien_job}},
        )

    recovery.advance_halt_recovery(store)

    assert "ask-user:uncertain-halt-outcome" in _tags(store, parent)
    assert "waiting-for:infra" not in _tags(store, parent)
    assert not _meta(store, parent)["halt"].get("retry_released")


def test_recovery_preserves_independent_human_holds(store: Store) -> None:
    parent = _parent(store)
    _halted_job(store, parent)
    holds = {
        "halt",
        "halt:owner-review",
        "ask-user:approve-the-result",
        "waiting-for:external",
    }
    for hold in holds:
        store.add_tag(parent, Tag.open(hold), set_by="user")

    recovery.advance_halt_recovery(store)

    assert holds <= _tags(store, parent)
    assert "waiting-for:infra" not in _tags(store, parent)


def test_three_halts_on_one_todo_do_not_trip_shared_outage(store: Store) -> None:
    parent = _parent(store)
    for _ in range(3):
        _halted_job(store, parent)

    recovery.advance_halt_recovery(store)

    assert _alerts(store) == []
    assert "waiting-for:infra" not in _tags(store, parent)


def test_breaker_trips_once_and_enrolls_existing_and_new_victims(store: Store) -> None:
    parents, alert_id = _trip(store)
    cohort = f"waiting-for:outage-{alert_id}"
    newcomer = _parent(store)
    _halted_job(store, newcomer)

    recovery.advance_halt_recovery(store)
    recovery.advance_halt_recovery(store)

    assert len(_alerts(store)) == 1
    assert "severity:critical" in _tags(store, alert_id)
    for parent in [*parents, newcomer]:
        assert cohort in _tags(store, parent)
        assert "waiting-for:infra" not in _tags(store, parent)
        with store.tx() as conn:
            assert recovery.infra_dispatch_allowed(conn, parent) is False


def test_breaker_releases_one_probe_and_admits_only_its_parent(store: Store) -> None:
    parents, alert_id = _trip(store)

    probe = _probe(store, alert_id)
    recovery.advance_halt_recovery(store)

    assert probe in parents
    cohort = f"waiting-for:outage-{alert_id}"
    assert cohort not in _tags(store, probe)
    assert sum(cohort in _tags(store, parent) for parent in parents) == 1
    for parent in parents:
        with store.tx() as conn:
            assert recovery.infra_dispatch_allowed(conn, parent) is (parent == probe)


def test_clean_probe_closes_breaker_and_releases_cohort_without_retrip(
    store: Store,
) -> None:
    parents, alert_id = _trip(store)
    probe = _probe(store, alert_id)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="Successful probe",
        parent_id=probe,
        meta={"job_type": "plan_tick", "executor": "claude_inproc"},
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")

    recovery.advance_halt_recovery(store)
    recovery.advance_halt_recovery(store)

    alerts = _alerts(store)
    assert len(alerts) == 1
    assert alerts[0][2] is not None
    assert alerts[0][1]["breaker_state"] == "closed"
    for parent in parents:
        assert not any(
            tag.startswith("waiting-for:outage-") for tag in _tags(store, parent)
        )
        with store.tx() as conn:
            assert recovery.infra_dispatch_allowed(conn, parent) is True


def test_failed_infra_probe_rejoins_cohort_and_doubles_cooldown(store: Store) -> None:
    parents, alert_id = _trip(store)
    probe = _probe(store, alert_id)
    _halted_job(store, probe)

    recovery.advance_halt_recovery(store)

    state = _meta(store, alert_id)
    assert state["breaker_state"] == "open"
    assert state["cooldown_s"] == 2 * recovery.COOLDOWN_S
    assert state["probe_parent"] is None
    assert datetime.fromisoformat(state["next_probe_at"]) > datetime.now(UTC)
    for parent in parents:
        assert f"waiting-for:outage-{alert_id}" in _tags(store, parent)
    assert len(_alerts(store)) == 1


@pytest.mark.parametrize(
    "result_meta", [{"resumed_reason": "timeout"}, {"halt": {"class": "blocked"}}]
)
def test_success_status_without_clean_probe_result_does_not_close(
    store: Store, result_meta: dict[str, Any]
) -> None:
    _, alert_id = _trip(store)
    probe = _probe(store, alert_id)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="Incomplete probe",
        parent_id=probe,
        meta={"job_type": "plan_tick", **result_meta},
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")

    recovery.advance_halt_recovery(store)

    assert _alerts(store)[0][2] is None
    assert _meta(store, alert_id)["breaker_state"] == "open"


def test_manual_resolution_releases_audited_cohort_without_retrip(store: Store) -> None:
    parents, alert_id = _trip(store)
    store.add_tag(parents[0], Tag.open("halt:human-pause"), set_by="user")

    assert resolve_alert(store, alert_id, resolved_by="operator") is True
    recovery.advance_halt_recovery(store)
    recovery.advance_halt_recovery(store)

    assert len(_alerts(store)) == 1
    for parent in parents:
        assert not any(
            tag.startswith("waiting-for:outage-") for tag in _tags(store, parent)
        )
    assert "halt:human-pause" in _tags(store, parents[0])


def test_plan_repair_release_is_independent_of_infra_outage(store: Store) -> None:
    _, alert_id = _trip(store)
    repair = _parent(store)
    _halted_job(store, repair, halt_class="misconceived")

    recovery.advance_halt_recovery(store)

    assert "waiting-for:plan-repair" not in _tags(store, repair)
    assert _meta(store, repair)["halt"]["retry_released"] is True
    assert _meta(store, alert_id)["breaker_state"] == "open"


def test_completed_probe_cannot_be_minted_again_before_maintenance(
    store: Store,
) -> None:
    _, alert_id = _trip(store)
    probe = _probe(store, alert_id)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="Finished probe",
        parent_id=probe,
        meta={"job_type": "plan_tick"},
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")
    with store.tx() as conn:
        assert recovery.infra_dispatch_allowed(conn, probe) is False


def test_probe_exhaustion_cannot_reuse_its_preprobe_safety_receipt(
    store: Store,
) -> None:
    _, alert_id = _trip(store)
    probe = _probe(store, alert_id)
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="Probe exhausted after uncertain write",
        parent_id=probe,
        meta={"job_type": "plan_tick", "resumed_reason": "timeout"},
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")
    recovery.advance_halt_recovery(store)
    assert "ask-user:uncertain-halt-outcome" in _tags(store, probe)
    assert f"waiting-for:outage-{alert_id}" not in _tags(store, probe)


def test_runner_blocked_uncertain_probe_cannot_close_the_breaker(store: Store) -> None:
    from precis.workers.executors import claude_inproc as executor
    from precis.workers.job_types.plan_tick import PlanTickOutcome
    from tests.test_plan_tick_resume import _FakeSpec, _mk_job

    parents, alert_id = _trip(store)
    probe = _probe(store, alert_id)
    job = _mk_job(store, probe)
    executor._run_plan_tick(
        store,
        job,
        _FakeSpec(
            PlanTickOutcome(
                exit_code=124,
                stdout="uncertain interrupted probe",
                stderr="timeout",
                duration_s=60.0,
            )
        ),
    )
    assert _meta(store, job).get("recovery_retry_blocked")
    recovery.advance_halt_recovery(store)
    assert _alerts(store)[0][2] is None
    assert _meta(store, alert_id)["breaker_state"] == "open"
    assert "ask-user:uncertain-halt-outcome" in _tags(store, probe)
    other = next(parent for parent in parents if parent != probe)
    assert f"waiting-for:outage-{alert_id}" in _tags(store, other)


def test_safely_paused_probe_remains_the_only_live_probe(store: Store) -> None:
    import json

    from precis.workers.executors import claude_inproc as executor
    from precis.workers.job_types.plan_tick import PlanTickOutcome
    from tests.test_plan_tick_resume import _FakeSpec, _mk_job

    parents, alert_id = _trip(store)
    probe = _probe(store, alert_id)
    job = _mk_job(store, probe)
    audit = "\n".join(
        json.dumps(event)
        for event in (
            {"type": "system", "subtype": "init"},
            {"type": "result", "is_error": False, "result": "window pause"},
        )
    )
    executor._run_plan_tick(
        store,
        job,
        _FakeSpec(
            PlanTickOutcome(
                exit_code=1,
                stdout=audit,
                stderr="window pause",
                duration_s=1.0,
                resume_reason="paused",
                retry_at=datetime.now(UTC) + timedelta(hours=1),
            )
        ),
    )
    assert "STATUS:waiting_time" in _tags(store, job)
    recovery.advance_halt_recovery(store)
    assert _meta(store, alert_id)["breaker_state"] == "half-open"
    assert _meta(store, alert_id)["probe_parent"] == probe
    assert "ask-user:uncertain-halt-outcome" not in _tags(store, probe)
    with store.tx() as conn:
        assert all(
            not recovery.infra_dispatch_allowed(conn, parent) for parent in parents
        )


def test_touching_historical_halt_jobs_does_not_trip_an_outage(store: Store) -> None:
    parents = [_parent(store), _parent(store)]
    for parent in [parents[0], parents[0], parents[1]]:
        job = _halted_job(store, parent)
        halt = _meta(store, job)["halt"]
        halt["at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
        store.update_ref(job, meta_patch={"halt": halt, "note": "new annotation"})
    recovery.advance_halt_recovery(store)
    assert _alerts(store) == []


def test_probe_skips_a_member_with_an_independent_owner_hold(store: Store) -> None:
    parents, alert_id = _trip(store)
    store.add_tag(parents[0], Tag.open("halt:owner-hold"), set_by="user")
    assert _probe(store, alert_id) == parents[1]
    assert "halt:owner-hold" in _tags(store, parents[0])


def test_named_dependency_parks_cannot_starve_managed_recovery(store: Store) -> None:
    # "infra" is also a legitimate external dependency name. These
    # blocked tasks have no timed recovery and must not fill its batch.
    for _ in range(200):
        blocked = _parent(store)
        store.update_ref(blocked, meta_patch={"halt": {"class": "blocked"}})
        store.add_tag(blocked, Tag.open("waiting-for:infra"), set_by="system")
    parent = _parent(store)
    _halted_job(store, parent)
    recovery.advance_halt_recovery(store)
    assert "waiting-for:infra" not in _tags(store, parent)


def test_open_breaker_stops_agent_mints_but_allows_compute_dispatch(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.workers.dispatch import run_dispatch_pass

    monkeypatch.setenv("PRECIS_DAILY_COST_CEILING", "1000000")
    _, alert_id = _trip(store)
    agent = store.insert_ref(
        kind="todo",
        slug=None,
        title="Agent work waits for outage recovery",
        meta={"executor": "claude_inproc", "job_type": "fix_gripe", "params": {}},
    )
    compute = store.insert_ref(
        kind="todo",
        slug=None,
        title="Compute work continues through outage",
        meta={"executor": "ssh_node", "job_type": "struct_relax", "params": {}},
    )

    run_dispatch_pass(store)

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT parent_id FROM refs WHERE kind = 'job' AND parent_id = ANY(%s)",
            ([agent.id, compute.id],),
        ).fetchall()
    assert [int(row[0]) for row in rows] == [compute.id]
    assert _meta(store, alert_id)["breaker_state"] == "open"
