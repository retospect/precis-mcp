"""``put(kind='job', mode='extend')``: the audited operator door that
bumps a live job's stored ``params.resources.wall_seconds`` and an
already-stamped ``meta.deadline`` together — per job (``id=``) or per
group (``args={'under'}``), never on a completed job (gr625303)."""

from __future__ import annotations

import time
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import job as job_mod
from precis.handlers.job import JobHandler
from precis.store import Store
from precis.store.types import Tag


@pytest.fixture
def jobs(hub: Hub) -> JobHandler:
    return JobHandler(hub=hub)


def _insert_job(
    store: Store,
    *,
    status: str = "queued",
    wall_seconds: int | None = 5400,
    deadline: float | None = None,
    job_type: str = "fake",
    parent_id: int | None = None,
) -> int:
    params: dict[str, Any] = {}
    if wall_seconds is not None:
        params["resources"] = {"wall_seconds": wall_seconds}
    meta: dict[str, Any] = {"job_type": job_type, "executor": "ssh_node"}
    if params:
        meta["params"] = params
    if deadline is not None:
        meta["deadline"] = deadline
    ref = store.insert_ref(
        kind="job", slug=None, title="t", meta=meta, parent_id=parent_id
    )
    store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="system")
    return ref.id


def _meta(store: Store, job_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="job", id=job_id)
    assert ref is not None
    return dict(ref.meta or {})


def _extend(jobs: JobHandler, job_id: int, **kw: Any) -> str:
    kw.setdefault("seconds", 3600)
    kw.setdefault("reason", "needs another hour")
    return jobs.put(id=job_id, mode="extend", **kw).body


# ── single job ────────────────────────────────────────────────────────


def test_extend_bumps_wall_and_deadline_and_audits(
    jobs: JobHandler, store: Store
) -> None:
    deadline = time.time() + 5400
    job_id = _insert_job(store, status="running", deadline=deadline)

    body = _extend(jobs, job_id)

    meta = _meta(store, job_id)
    assert meta["params"]["resources"]["wall_seconds"] == 5400 + 3600
    # The deadline keeps its original submit origin: old + delta, never
    # now + budget — elapsed time is not reset (the gripe's ask).
    assert meta["deadline"] == pytest.approx(deadline + 3600)
    [ev] = store.events_for(job_id, source="job-extend")
    assert ev.payload["wall_seconds"] == {"old": 5400, "new": 9000}
    assert ev.payload["deadline"]["new"] == pytest.approx(deadline + 3600)
    assert ev.payload["reason"] == "needs another hour"
    assert ev.payload["by"]
    assert f"job #{job_id}" in body
    assert "wall_seconds 5400 -> 9000" in body
    assert "deadline 20" in body  # old/new ISO stamps rendered
    assert f"get(kind='job', id={job_id})" in body


def test_extend_keeps_other_params_and_resources(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _insert_job(store)
    store.stamp_ref_meta(
        job_id,
        {"params": {"seed": 7, "resources": {"wall_seconds": 5400, "cpuset": "0-4"}}},
    )

    _extend(jobs, job_id)

    assert _meta(store, job_id)["params"] == {
        "seed": 7,
        "resources": {"wall_seconds": 9000, "cpuset": "0-4"},
    }


def test_extend_queued_job_without_a_deadline_bumps_wall_only(
    jobs: JobHandler, store: Store
) -> None:
    """A queued job's deadline is stamped at submit — only the stored
    budget exists to extend; the response says so."""
    job_id = _insert_job(store, status="queued", deadline=None)

    body = _extend(jobs, job_id)

    assert _meta(store, job_id)["params"]["resources"]["wall_seconds"] == 9000
    assert "deadline" not in _meta(store, job_id)
    assert "deadline (none stamped)" in body


def test_extend_deadline_only_job_bumps_deadline(
    jobs: JobHandler, store: Store
) -> None:
    deadline = time.time() + 5400
    job_id = _insert_job(store, status="running", wall_seconds=None, deadline=deadline)

    body = _extend(jobs, job_id)

    assert _meta(store, job_id)["deadline"] == pytest.approx(deadline + 3600)
    assert "wall_seconds (none stored)" in body


@pytest.mark.parametrize("status", ["succeeded", "failed", "cancelled"])
def test_extend_refuses_a_completed_job(
    jobs: JobHandler, store: Store, status: str
) -> None:
    job_id = _insert_job(store, status=status, deadline=time.time() + 100)

    with pytest.raises(BadInput, match=f"STATUS:{status}"):
        _extend(jobs, job_id)

    assert _meta(store, job_id)["params"]["resources"]["wall_seconds"] == 5400
    assert store.events_for(job_id, source="job-extend") == []


def test_extend_refuses_when_nothing_to_extend(jobs: JobHandler, store: Store) -> None:
    job_id = _insert_job(store, wall_seconds=None, deadline=None)

    with pytest.raises(BadInput, match="nothing to extend"):
        _extend(jobs, job_id)


@pytest.mark.parametrize("seconds", [None, 0, -5, 604801, "abc", 1.5, True])
def test_extend_seconds_bound(jobs: JobHandler, store: Store, seconds: Any) -> None:
    job_id = _insert_job(store)
    kw: dict[str, Any] = {"reason": "r"}
    if seconds is not None:
        kw["seconds"] = seconds
    with pytest.raises(BadInput, match="seconds"):
        jobs.put(id=job_id, mode="extend", **kw)
    assert _meta(store, job_id)["params"]["resources"]["wall_seconds"] == 5400


def test_extend_seconds_accepts_string_and_float_forms(
    jobs: JobHandler, store: Store
) -> None:
    a = _insert_job(store)
    b = _insert_job(store)
    _extend(jobs, a, seconds="3600")
    _extend(jobs, b, seconds=3600.0)
    assert _meta(store, a)["params"]["resources"]["wall_seconds"] == 9000
    assert _meta(store, b)["params"]["resources"]["wall_seconds"] == 9000


def test_extend_requires_a_reason(jobs: JobHandler, store: Store) -> None:
    job_id = _insert_job(store)
    with pytest.raises(BadInput, match="reason"):
        jobs.put(id=job_id, mode="extend", seconds=60)
    with pytest.raises(BadInput, match="reason"):
        jobs.put(id=job_id, mode="extend", seconds=60, reason="  ")


def test_extend_requires_a_target(jobs: JobHandler) -> None:
    with pytest.raises(BadInput, match="requires id="):
        jobs.put(mode="extend", seconds=60, reason="r")


def test_extend_id_and_under_are_mutually_exclusive(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _insert_job(store)
    with pytest.raises(BadInput, match="not both"):
        jobs.put(id=job_id, mode="extend", under="qu1", seconds=60, reason="r")


def test_extend_refuses_mint_kwargs(jobs: JobHandler, store: Store) -> None:
    job_id = _insert_job(store)
    cases: list[dict[str, Any]] = [
        {"text": "why"},
        {"params": {}},
        {"executor": "claude_inproc"},
        {"job_type": "fake"},
        {"parent_id": 1},
    ]
    for kw in cases:
        with pytest.raises(BadInput, match="mints no job row|under"):
            jobs.put(id=job_id, mode="extend", seconds=60, reason="r", **kw)
    assert _meta(store, job_id)["params"]["resources"]["wall_seconds"] == 5400


@pytest.mark.parametrize("mode", [None, "retry", "retarget", "setting", "wake"])
def test_extend_args_refused_on_other_modes(jobs: JobHandler, mode: Any) -> None:
    with pytest.raises(BadInput, match=r"only apply to put\(mode='extend'\)"):
        jobs.put(id=42, mode=mode, seconds=60)
    with pytest.raises(BadInput, match=r"only apply to put\(mode='extend'\)"):
        jobs.put(id=42, mode=mode, under="qu1")


# ── group (under=) ────────────────────────────────────────────────────


def _quest_tree(store: Store) -> tuple[int, int]:
    """A quest root and a todo under it — the member chain the under=
    walk resolves jobs beneath."""
    quest = store.insert_ref(kind="quest", slug=None, title="q", meta={})
    todo = store.insert_ref(
        kind="todo", slug=None, title="t", meta={}, parent_id=quest.id
    )
    return int(quest.id), int(todo.id)


def test_extend_group_under_extends_live_jobs_skips_terminal(
    jobs: JobHandler, store: Store
) -> None:
    quest_id, todo_id = _quest_tree(store)
    a = _insert_job(
        store, status="running", parent_id=todo_id, deadline=time.time() + 100
    )
    b = _insert_job(store, status="queued", parent_id=todo_id)
    done = _insert_job(store, status="succeeded", parent_id=todo_id)
    other = _insert_job(store, status="running")  # outside the tree

    body = jobs.put(
        mode="extend", under=f"qu{quest_id}", seconds=7200, reason="more time"
    ).body

    for job_id in (a, b):
        assert _meta(store, job_id)["params"]["resources"]["wall_seconds"] == 12600
    assert _meta(store, done)["params"]["resources"]["wall_seconds"] == 5400
    assert _meta(store, other)["params"]["resources"]["wall_seconds"] == 5400
    assert f"extended 2 job(s) under qu{quest_id}" in body
    assert f"jo{a}" in body and f"jo{b}" in body
    assert "skipped terminal: 1 completed job(s)" in body
    # Every extended job carries its own audit event.
    assert len(store.events_for(a, source="job-extend")) == 1
    assert len(store.events_for(b, source="job-extend")) == 1
    assert store.events_for(done, source="job-extend") == []


def test_extend_group_under_reaches_jobs_linked_not_parented(
    jobs: JobHandler, store: Store
) -> None:
    """The subject-link attachment counts too — same resolution as
    ``search(kind='job', under=…)``."""
    quest_id, _todo_id = _quest_tree(store)
    linked = _insert_job(store, status="queued")
    store.add_link(src_ref_id=linked, dst_ref_id=quest_id, relation="fixes")

    body = jobs.put(mode="extend", under=quest_id, seconds=60, reason="r").body

    assert _meta(store, linked)["params"]["resources"]["wall_seconds"] == 5460
    assert f"jo{linked}" in body


def test_extend_group_job_type_filter(jobs: JobHandler, store: Store) -> None:
    quest_id, todo_id = _quest_tree(store)
    seed = _insert_job(
        store, status="running", job_type="autocatpath_seed", parent_id=todo_id
    )
    tick = _insert_job(
        store, status="running", job_type="quest_tick", parent_id=todo_id
    )

    jobs.put(
        mode="extend",
        under=quest_id,
        job_type="autocatpath_seed",
        seconds=60,
        reason="seeds only",
    )

    assert _meta(store, seed)["params"]["resources"]["wall_seconds"] == 5460
    assert _meta(store, tick)["params"]["resources"]["wall_seconds"] == 5400


def test_extend_group_cap(
    jobs: JobHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(job_mod, "_EXTEND_GROUP_CAP", 2)
    quest_id, todo_id = _quest_tree(store)
    for _ in range(3):
        _insert_job(store, status="queued", parent_id=todo_id)

    with pytest.raises(BadInput, match="exceeds the per-call bound"):
        jobs.put(mode="extend", under=quest_id, seconds=60, reason="r")


def test_extend_group_refuses_a_truncated_member_walk(
    jobs: JobHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subtree bigger than the member walk cap resolves an INCOMPLETE
    job set — extend refuses before writing rather than silently
    extending a partial group (td627156)."""
    monkeypatch.setattr(job_mod, "_UNDER_MEMBER_CAP", 1)
    quest_id, todo_id = _quest_tree(store)  # root + todo = 2 rows > cap
    job_id = _insert_job(store, status="queued", parent_id=todo_id)

    with pytest.raises(BadInput, match="member walk cap"):
        jobs.put(mode="extend", under=quest_id, seconds=60, reason="r")

    assert _meta(store, job_id)["params"]["resources"]["wall_seconds"] == 5400
    assert store.events_for(job_id, source="job-extend") == []


def test_extend_group_refuses_a_truncated_job_resolution(
    jobs: JobHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Same refusal when the job-resolution LIMIT itself fires: the
    resolved set may be missing live jobs past the cut."""
    monkeypatch.setattr(job_mod, "_UNDER_JOB_CAP", 2)
    quest_id, todo_id = _quest_tree(store)
    ids = [_insert_job(store, status="queued", parent_id=todo_id) for _ in range(3)]

    with pytest.raises(BadInput, match="row cap"):
        jobs.put(mode="extend", under=quest_id, seconds=60, reason="r")

    for job_id in ids:
        assert _meta(store, job_id)["params"]["resources"]["wall_seconds"] == 5400
        assert store.events_for(job_id, source="job-extend") == []


def test_extend_group_with_nothing_live_reports_zero(
    jobs: JobHandler, store: Store
) -> None:
    quest_id, todo_id = _quest_tree(store)
    _insert_job(store, status="succeeded", parent_id=todo_id)

    body = jobs.put(mode="extend", under=quest_id, seconds=60, reason="r").body

    assert "extended 0 job(s)" in body
    assert "skipped terminal: 1 completed job(s)" in body


def test_extend_group_job_type_without_under_refused(
    jobs: JobHandler, store: Store
) -> None:
    job_id = _insert_job(store)
    with pytest.raises(BadInput, match="only with under|only an args"):
        jobs.put(
            id=job_id,
            mode="extend",
            job_type="fake",
            seconds=60,
            reason="r",
        )


# ── the card surfaces both fields (gripe: raw reads bury them) ────────


def test_job_card_shows_resources_wall_and_deadline(
    jobs: JobHandler, store: Store
) -> None:
    deadline = time.time() + 5400
    job_id = _insert_job(store, status="running", deadline=deadline)

    body = jobs.get(id=job_id).body

    assert "resources.wall_seconds: 5400" in body
    assert "deadline: 20" in body
