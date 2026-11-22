"""The land-conflict path of ``plan_dispatch`` (gr741695, Reto's ruling
gr735593): a land red on a rebase or merge conflict stamps the task's
``meta.conflict``; the tick mints a conflict-only attempt (``params.
conflict``), reviews only its resolution (``params.resolution``), lands it
ahead of every other land, and asks after three failed tries."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.plans import lanes
from precis.store import Store
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.utils import dispatch_score as _dscore
from precis.workers.job_types import land_slice, run_slice
from precis.workers.job_types import plan_dispatch as pd
from tests._lane_rows import SEED_LANES, use_lanes
from tests.test_plan_dispatch import (
    _answer_ask,
    _backdate,
    _children,
    _comment_texts,
    _dark_advertise,
    _factory_on,  # noqa: F401  (autouse fixture of the sibling module)
    _failed_job,
    _job,
    _jobs_under,
    _mark_done,
    _meta,
    _one_task_plan,
    _open_tags,
    _review_plan,
    _reviewed_pending,
    _set_job_status,
    _slice_job,
    _status,
    _todo_comment,
)
from tests.test_plan_dispatch_park import _ask_blocking

_HOST = "build-host-1"
_H0 = "0" * 40  # the commit that failed to land
_REF = "refs/factory/build/{}/7"
_FILES = ["src/a.py", "src/b.py"]
_CONFLICT_LINE = "scripts/ship: rebase conflict onto origin/main"


@pytest.fixture(autouse=True)
def _lanes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The seed lane table: a four-rung code ladder. The params schema is
    the real ``run_slice.PARAMS_SCHEMA``."""
    use_lanes(monkeypatch)


def _ts(minutes: float) -> str:
    at = datetime.now(UTC) + timedelta(minutes=minutes)
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _conflict(
    task: int, *, head: str = _H0, at: str | None = None, job: int | None = None
) -> dict[str, Any]:
    """The lander's ``meta.conflict`` stamp; ``job`` the land job that wrote
    it (a stamp without it is the clock-comparing fallback)."""
    stamp: dict[str, Any] = {
        "head": head,
        "ref": _REF.format(task),
        "base_tip": "b" * 40,
        "files": list(_FILES),
        "at": at or _ts(-1),
    }
    if job is not None:
        stamp["job"] = job
    return stamp


def _build_ref(task: int) -> dict[str, Any]:
    return {
        "state": "pushed",
        "ref": _REF.format(task),
        "sha": _H0,
        "head": _H0,
        "wip": False,
        "by": "builder",
        "note": "",
        "host": _HOST,
        "at": _ts(-30),
    }


def _event(store: Store, job: int, text: str) -> None:
    store.chunks.insert_chunks(
        job, [ChunkInsert(ord=1, text=text, meta={"chunk_kind": "job_event"})]
    )


def _conflicted(store: Store, task: int) -> int:
    """``task``'s state right after a land red on a conflict: a build that
    ran on the ladder's foot, its failed land, the lander's stamp, and
    ``meta.build_ref`` standing. Returns the failed land's job id."""
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task)})
    _slice_job(
        store,
        task,
        "run_slice",
        "succeeded",
        meta={"lane": "devin", "model": "swe-2-high"},
    )
    land = _slice_job(store, task, "land_slice", "failed", params={"todo_id": task})
    _event(store, land, f"✖ {_CONFLICT_LINE}")
    store.stamp_ref_meta(task, {"conflict": _conflict(task, job=land)})
    return land


def _conflict_jobs(store: Store, task: int) -> list[tuple[int, dict[str, Any]]]:
    return [
        (jid, m)
        for jid, m in _jobs_under(store, task)
        if "conflict" in (m.get("params") or {})
    ]


def _end_try(
    store: Store,
    job: int,
    *,
    pushed: bool,
    lane: str = "devin",
    model: str = "swe-2-high",
) -> None:
    """The conflict attempt ended: it ran on ``lane``/``model`` and either
    pushed its work (``meta.push.state``) or left none."""
    store.stamp_ref_meta(
        job,
        {
            "lane": lane,
            "model": model,
            "push": {
                "state": "pushed" if pushed else "empty",
                "host": _HOST,
                "at": _ts(0),
            },
        },
    )
    _set_job_status(store, job, "succeeded" if pushed else "failed")
    if not pushed:
        _event(store, job, "run_slice: left no commit")


def _land_conflicts_again(store: Store, task: int) -> None:
    """The resolution's land ran, conflicted again and stamped the task."""
    land = _slice_job(store, task, "land_slice", "failed", params={"todo_id": task})
    _event(store, land, f"✖ {_CONFLICT_LINE}")
    _backdate(store, land, 2)  # the lander stamps DURING the land's run
    store.stamp_ref_meta(task, {"conflict": _conflict(task, head="1" * 40, at=_ts(-1))})


def _asks(store: Store, root: int) -> list[int]:
    return [c for c in _children(store, root) if "ask" in _meta(store, c)]


# ── constants ───────────────────────────────────────────────────────


def test_the_restated_keys_match_the_lander() -> None:
    assert pd.CONFLICT_KEY == land_slice.CONFLICT_KEY == "conflict"


# ── 1. the conflict-only attempt ────────────────────────────────────


def test_a_conflicted_task_mints_a_conflict_only_attempt(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    res = pd.run_pass(store)
    assert res.dispatched == 1
    ((job, meta),) = _conflict_jobs(store, task)
    assert meta["job_type"] == "run_slice"
    assert meta["params"]["conflict"] == {
        "head": _H0,
        "ref": _REF.format(task),
        "base_tip": "b" * 40,
        "files": _FILES,
        "try": 1,
    }
    # Tries 1 and 2 run where the build ran; the hub pin holds too.
    assert (meta["params"]["lane"], meta["params"]["model"]) == (
        "devin",
        "swe-2-high",
    )
    assert meta["params"]["target_node"] == _HOST
    assert meta["conflict_try"] == 1
    tries = _meta(store, task)["conflict_tries"]
    assert (tries["n"], tries["job"], tries["sent_back"]) == (1, job, None)


def test_the_mechanical_conflict_signature_does_not_fire_for_it(
    store: Store,
) -> None:
    """The conflict path owns the red: no ``brief:`` comment, no
    ``mech_retries`` accounting."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    pd.run_pass(store)
    assert not [t for t in _comment_texts(store, task) if t.startswith("brief:")]
    assert "mech_retries" not in _meta(store, task)


def test_without_the_lander_stamp_the_old_mechanical_path_stands(
    store: Store,
) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    store.stamp_ref_meta(task, {"conflict": None})
    pd.run_pass(store)
    assert _conflict_jobs(store, task) == []
    assert any(t.startswith("brief:") for t in _comment_texts(store, task))
    assert _meta(store, task)["mech_retries"]["sig"] == "conflict"


def test_a_free_failure_or_cancel_mints_the_same_try_again(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    pd.run_pass(store)
    ((job, _),) = _conflict_jobs(store, task)
    store.stamp_ref_meta(job, {"attempt_free": "lane-wait"})
    _set_job_status(store, job, "failed")
    pd.run_pass(store, now=datetime.now(UTC) + timedelta(hours=1))
    tries = [m["params"]["conflict"]["try"] for _j, m in _conflict_jobs(store, task)]
    assert tries == [1, 1]


# ── 2. its own count, its own bound ─────────────────────────────────


def test_conflict_tries_burn_no_ordinary_attempt_and_no_climb(store: Store) -> None:
    """A conflict attempt that failed, and the land red that sent for it,
    are not attempts: the ordinary count stays where the build left it,
    ``attempt:<n>`` tags (which count every red) are not read, and the
    ladder is never climbed by the conflict path."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    store.add_tag(task, Tag.open("attempt:5"), set_by="system")
    pd.run_pass(store)
    ((job, _),) = _conflict_jobs(store, task)
    _end_try(store, job, pushed=False)
    plan = pd._load(store)
    assert pd._attempts(plan, plan.nodes[task]) == 0
    pd.run_pass(store)
    # Try 2 is still on the build's rung — not the climb's next one.
    (_, m1), (_, m2) = _conflict_jobs(store, task)
    assert m2["params"]["conflict"]["try"] == 2
    assert (m2["params"]["lane"], m2["params"]["model"]) == ("devin", "swe-2-high")
    assert m1["params"]["lane"] == m2["params"]["lane"]


def test_the_conflict_sequence_to_the_strongest_model_and_the_ask(
    store: Store,
) -> None:
    """conflict -> attempt 1 -> its land conflicts again -> attempt 2 ->
    its resolution review says FIX -> attempt 3 on the top rung -> fails ->
    one coordinator ask."""
    mint = _review_plan(store, preland=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="devin")
    _conflicted(store, build)

    # attempt 1: pushes; its land conflicts again.
    pd.run_pass(store)
    ((j1, m1),) = _conflict_jobs(store, build)
    assert m1["params"]["conflict"]["try"] == 1
    _end_try(store, j1, pushed=True)
    _backdate(store, j1, 3)  # minted before the lander's second stamp
    _land_conflicts_again(store, build)

    # attempt 2: on the same rung; it pushes, the work is held for review.
    pd.run_pass(store)
    (_, _), (j2, m2) = _conflict_jobs(store, build)
    assert m2["params"]["conflict"]["try"] == 2
    assert (m2["params"]["lane"], m2["params"]["model"]) == ("devin", "swe-2-high")
    # The second conflict's head is what the new attempt must resolve.
    assert m2["params"]["conflict"]["head"] == "1" * 40
    _end_try(store, j2, pushed=True)
    store.stamp_ref_meta(
        build,
        {
            "pending_land": {
                "todo_id": build,
                "branch": f"slice/{build}",
                "ref": _REF.format(build),
                "head": "2" * 40,
                "lane": "devin",
                "target_node": _HOST,
            }
        },
    )
    _mark_done(store, build)

    # its resolution review runs and says FIX.
    pd.run_pass(store)
    rjobs = _jobs_under(store, review)
    assert len(rjobs) == 1
    assert rjobs[0][1]["params"]["resolution"] == {
        "of": "1" * 40,
        "ref": _REF.format(build),
    }
    _set_job_status(store, rjobs[0][0], "succeeded")
    _todo_comment(store, review, "review: FIX — the merge dropped b.py's guard")
    res = pd.run_pass(store)
    assert any("conflict try 2 failed" in n for n in res.notes)
    bmeta = _meta(store, build)
    assert bmeta.get("preland_fix_rounds") is None
    assert bmeta.get("pending_land") is None

    # attempt 3 mints in the same pass, pinned to the top rung.
    conflict_jobs = _conflict_jobs(store, build)
    assert len(conflict_jobs) == 3
    j3, m3 = conflict_jobs[-1]
    assert m3["params"]["conflict"]["try"] == 3
    assert (m3["params"]["lane"], m3["params"]["model"]) == (
        "claude-sub",
        "claude-opus-5-5",
    )
    _end_try(store, j3, pushed=False, lane="claude-sub", model="claude-opus-5-5")
    res = pd.run_pass(store)
    assert res.asks == 1
    (ask,) = _asks(store, mint.root_id)
    ref = store.get_ref(kind="todo", id=ask)
    assert ref is not None and "land conflict not cleared by 3" in str(ref.title)
    body = next(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(ask)
        if c.chunk_kind == TODO_BODY_KIND
    )
    assert "the last on the strongest model" in body
    assert "`src/a.py`" in body and "`src/b.py`" in body
    # Parked: nothing mints while the ask is open; the answer re-arms a
    # fresh set of tries.
    assert pd.run_pass(store).dispatched == 0
    _answer_ask(store, ask, "acknowledged")
    pd.run_pass(store)
    assert _meta(store, build).get("conflict_tries", {}).get("n") == 1
    assert len(_conflict_jobs(store, build)) == 4


def test_try_three_names_the_strongest_model_in_a_note(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    job = _slice_job(
        store,
        task,
        "run_slice",
        "failed",
        params={"todo_id": task, "conflict": {"try": 2}},
        meta={"push": {"state": "empty", "host": _HOST, "at": _ts(0)}},
    )
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 2, "job": job}})
    res = pd.run_pass(store)
    assert any("pinned to the strongest model" in n for n in res.notes)
    ((_jid, meta),) = _conflict_jobs(store, task)[-1:]
    assert meta["params"]["conflict"]["try"] == 3


# ── 3. review only the resolution ───────────────────────────────────


def test_a_review_without_a_conflict_gets_no_resolution_param(
    store: Store,
) -> None:
    mint = _review_plan(store, preland=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="devin")
    store.stamp_ref_meta(
        build, {"pending_land": {"todo_id": build, "branch": f"slice/{build}"}}
    )
    _mark_done(store, build)
    pd.run_pass(store)
    ((_jid, meta),) = _jobs_under(store, review)
    assert "resolution" not in meta["params"]


def test_a_resolution_review_fix_is_not_a_preland_round(store: Store) -> None:
    """The hand-back still reaches the builder (a ``handoff:`` comment) but
    ``meta.preland_fix_rounds`` is not fed: the three tries are the bound,
    not :data:`pd.PRELAND_FIX_CAP`."""
    mint = _reviewed_pending(store, "review: FIX — b.py lost its guard")
    build = mint.ids["build"]
    _conflicted(store, build)
    job = _slice_job(
        store,
        build,
        "run_slice",
        "succeeded",
        params={"todo_id": build, "conflict": {"try": 1}},
        meta={"push": {"state": "pushed", "host": _HOST, "at": _ts(0)}},
    )
    store.stamp_ref_meta(build, {"conflict_tries": {"n": 1, "job": job}})
    res = pd.run_pass(store)
    assert any("conflict try 1 failed" in n for n in res.notes)
    meta = _meta(store, build)
    # The sent-back attempt is a failed try: try 2 mints in the same pass.
    assert meta["conflict_tries"]["n"] == 2
    assert [
        m["params"]["conflict"]["try"] for _j, m in _conflict_jobs(store, build)
    ] == [
        1,
        2,
    ]
    assert meta.get("preland_fix_rounds") is None
    assert any(
        t.startswith("handoff: review: FIX") for t in _comment_texts(store, build)
    )


# ── 4. land it next ─────────────────────────────────────────────────


def _resolved(store: Store, mint: Any, *, pushed_min: float = -5) -> int:
    """``mint``'s build after a pushed conflict resolution held for its
    pre-land review; returns the attempt job."""
    build = mint.ids["build"]
    store.stamp_ref_meta(
        build,
        {"conflict": _conflict(build, at=_ts(-20)), "build_ref": _build_ref(build)},
    )
    job = _slice_job(
        store,
        build,
        "run_slice",
        "succeeded",
        params={"todo_id": build, "conflict": {"try": 1}},
        meta={"push": {"state": "pushed", "host": _HOST, "at": _ts(pushed_min)}},
    )
    store.stamp_ref_meta(build, {"conflict_tries": {"n": 1, "job": job}})
    return job


def _land_jobs(store: Store, task: int) -> list[tuple[int, dict[str, Any]]]:
    return [
        (jid, m)
        for jid, m in _jobs_under(store, task)
        if m.get("job_type") == "land_slice"
    ]


def test_the_resolutions_land_takes_the_top_score_and_holds_the_others(
    store: Store,
) -> None:
    a = _reviewed_pending(store, "review: LAND")
    b = _reviewed_pending(store, "review: LAND")
    _resolved(store, a)
    res = pd.run_pass(store)
    a_land = _land_jobs(store, a.ids["build"])
    assert len(a_land) == 1
    assert a_land[0][1][_dscore.SCORE_META_KEY] == pd.CONFLICT_FIRST_SCORE
    # The other task's land waits, with a note naming the task.
    assert _land_jobs(store, b.ids["build"]) == []
    assert any(
        f"td{b.ids['build']}: land held — waits for td{a.ids['build']}" in n
        for n in res.notes
    )
    assert _status(store, b.ids["review"]) != "done"
    # The land of the resolution succeeds and the lander clears the
    # conflict with the build ref: the held land flows on the next pass.
    _set_job_status(store, a_land[0][0], "succeeded")
    store.stamp_ref_meta(
        a.ids["build"], {"conflict": None, "build_ref": None, "pending_land": None}
    )
    _mark_done(store, a.ids["build"])
    res = pd.run_pass(store)
    assert len(_land_jobs(store, b.ids["build"])) == 1
    assert "conflict_tries" not in _meta(store, a.ids["build"]) or (
        _meta(store, a.ids["build"])["conflict_tries"] is None
    )


def test_the_hold_lapses_after_the_bound_with_a_note(store: Store) -> None:
    a = _reviewed_pending(store, "review: LAND")
    b = _reviewed_pending(store, "review: LAND")
    _resolved(store, a, pushed_min=-(pd.CONFLICT_LAND_HOLD.total_seconds() / 60 + 5))
    # A's own land still mints (it is the held one); B's flows too.
    res = pd.run_pass(store)
    assert len(_land_jobs(store, b.ids["build"])) == 1
    assert any("lapsed" in n and f"td{a.ids['build']}" in n for n in res.notes)


def test_another_repository_or_base_is_not_held(store: Store) -> None:
    a = _reviewed_pending(store, "review: LAND")
    b = _reviewed_pending(store, "review: LAND")
    _resolved(store, a)
    store.stamp_ref_meta(b.ids["build"], {"land_target": "release/r9"})
    pd.run_pass(store)
    assert len(_land_jobs(store, b.ids["build"])) == 1


def test_a_queued_land_of_the_resolution_is_raised_to_the_top_score(
    store: Store,
) -> None:
    """A land minted by ``run_slice`` (a flow without a pre-land review)
    carries only the finish-first floor; the tick raises it."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(
        task,
        {"conflict": _conflict(task, at=_ts(-20)), "build_ref": _build_ref(task)},
    )
    job = _slice_job(
        store,
        task,
        "run_slice",
        "succeeded",
        params={"todo_id": task, "conflict": {"try": 1}},
        meta={"push": {"state": "pushed", "host": _HOST, "at": _ts(-1)}},
    )
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 1, "job": job}})
    land = _slice_job(
        store,
        task,
        "land_slice",
        "queued",
        meta={_dscore.SCORE_META_KEY: _dscore.FINISH_FIRST_SCORE},
    )
    pd.run_pass(store)
    meta = dict(next(m for jid, m in _jobs_under(store, task) if jid == land))
    assert meta[_dscore.SCORE_META_KEY] == pd.CONFLICT_FIRST_SCORE


def test_a_pending_resolutions_reviewer_gets_the_top_score(store: Store) -> None:
    mint = _review_plan(store, preland=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="devin")
    _resolved(store, mint)
    store.stamp_ref_meta(
        build,
        {
            "pending_land": {
                "todo_id": build,
                "branch": f"slice/{build}",
                "ref": _REF.format(build),
                "head": "2" * 40,
                "target_node": _HOST,
            }
        },
    )
    _mark_done(store, build)
    pd.run_pass(store)
    ((_jid, meta),) = _jobs_under(store, review)
    assert meta[_dscore.SCORE_META_KEY] == pd.CONFLICT_FIRST_SCORE
    assert meta["params"]["resolution"] == {"of": _H0, "ref": _REF.format(build)}


# ── 5. clearing ─────────────────────────────────────────────────────


def test_start_over_clears_the_conflict_and_the_count(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 3, "job": 0}})
    from tests.test_plan_dispatch_park import _ask_blocking

    ask = _ask_blocking(store, task, mint.root_id)
    _answer_ask(store, ask, "start over")
    pd.run_pass(store)
    meta = _meta(store, task)
    assert meta["start_over"] == ask
    assert meta.get("conflict") is None
    assert meta.get("conflict_tries") is None
    assert meta["build_ref"]["host"] == _HOST  # the launch needs the host
    assert _conflict_jobs(store, task) == []


def test_a_land_clears_the_count_on_the_next_tick(store: Store) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 2, "job": 0}})
    # The lander cleared ``meta.conflict`` with ``meta.build_ref``.
    pd.run_pass(store)
    assert _meta(store, task).get("conflict_tries") is None


# ── round 3: the reviewer's findings ────────────────────────────────


def test_the_minted_params_validate_against_the_real_run_slice_schema(
    store: Store,
) -> None:
    """Item 1: a conflict attempt and a resolution review carry params the
    real ``run_slice.PARAMS_SCHEMA`` accepts (no patched schema)."""
    mint = _review_plan(store, preland=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="devin")
    _conflicted(store, build)
    assert pd.run_pass(store).dispatched == 1
    ((j1, m1),) = _conflict_jobs(store, build)
    assert pd._params_problem(run_slice.PARAMS_SCHEMA, m1["params"]) is None
    _end_try(store, j1, pushed=True)
    store.stamp_ref_meta(
        build,
        {
            "pending_land": {
                "todo_id": build,
                "branch": f"slice/{build}",
                "head": "2" * 40,
            }
        },
    )
    _mark_done(store, build)
    pd.run_pass(store)
    ((_jid, meta),) = _jobs_under(store, review)
    assert "resolution" in meta["params"]
    assert pd._params_problem(run_slice.PARAMS_SCHEMA, meta["params"]) is None


def test_an_ordinary_red_land_after_a_resolution_ends_the_episode(
    store: Store,
) -> None:
    """Item 2: the resolution's land failed for a non-conflict reason (the
    stamp is not newer than the land's start). The tick clears the conflict
    keys with a note; the rebuild is an ordinary build, its review is no
    resolution review and a FIX on it is an ordinary pre-land round."""
    mint = _review_plan(store, preland=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="devin")
    _conflicted(store, build)
    pd.run_pass(store)
    ((j1, _),) = _conflict_jobs(store, build)
    _end_try(store, j1, pushed=True)
    land = _slice_job(store, build, "land_slice", "failed", params={"todo_id": build})
    _event(store, land, "✖ tests red")
    res = pd.run_pass(store)
    assert any("conflict episode over" in n for n in res.notes)
    meta = _meta(store, build)
    assert meta.get("conflict") is None and meta.get("conflict_tries") is None
    # The ordinary rebuild mints (no conflict param) ...
    rebuild_id, rebuild = _jobs_under(store, build)[-1]
    assert "conflict" not in rebuild["params"]
    # ... and pushes; its review is no resolution review.
    _set_job_status(store, rebuild_id, "succeeded")
    store.stamp_ref_meta(
        build,
        {
            "pending_land": {
                "todo_id": build,
                "branch": f"slice/{build}",
                "head": "3" * 40,
            }
        },
    )
    _mark_done(store, build)
    pd.run_pass(store)
    ((rjob, rmeta),) = _jobs_under(store, review)
    assert "resolution" not in rmeta["params"]
    _set_job_status(store, rjob, "succeeded")
    _todo_comment(store, review, "review: FIX — an ordinary finding")
    pd.run_pass(store)
    meta = _meta(store, build)
    assert meta["preland_fix_rounds"]["count"] == 1
    assert meta.get("conflict_tries") is None
    assert all(
        "conflict" not in m["params"] for _j, m in _jobs_under(store, build)[-1:]
    )


def test_a_cancelled_or_timed_out_resolution_land_releases_the_hold(
    store: Store,
) -> None:
    """A land that ended without conflicting again no longer holds the
    other lands."""
    for status in ("cancelled", "timed-out"):
        a = _reviewed_pending(store, "review: LAND")
        b = _reviewed_pending(store, "review: LAND")
        _resolved(store, a)
        pd.run_pass(store)
        (a_land,) = _land_jobs(store, a.ids["build"])
        assert _land_jobs(store, b.ids["build"]) == []
        _set_job_status(store, a_land[0], status)
        pd.run_pass(store)
        assert len(_land_jobs(store, b.ids["build"])) == 1, status
        assert _meta(store, a.ids["build"]).get("conflict") is None


def test_try_three_with_the_strongest_rungs_cap_spent_asks_and_says_why(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Item 3: the top rung (claude-sub's opus) has a ``rung_caps`` cap its
    recorded spend meets while lower rungs are uncapped: waiting would not
    clear it, so the tries are spent and the ask names the cap."""
    use_lanes(
        monkeypatch,
        *SEED_LANES,
        entitlements={"claude-sub": {"rung_caps": {"claude-opus-5-5": 25.0}}},
    )
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    job = _slice_job(
        store,
        task,
        "run_slice",
        "failed",
        params={"todo_id": task, "conflict": {"try": 2}},
        meta={
            "push": {"state": "empty", "host": _HOST, "at": _ts(0)},
            "usage": {"on_demand_usd": 30.0},
        },
    )
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 2, "job": job}})
    res = pd.run_pass(store)
    assert res.asks == 1 and res.dispatched == 0
    (ask,) = _asks(store, mint.root_id)
    body = next(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(ask)
        if c.chunk_kind == TODO_BODY_KIND
    )
    assert "could not run on the strongest model" in body and "$25.00 cap" in body
    # No try 3 was minted.
    tries = [m["params"]["conflict"]["try"] for _j, m in _conflict_jobs(store, task)]
    assert tries == [2]


def test_a_backed_off_top_lane_only_waits(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    job = _slice_job(
        store,
        task,
        "run_slice",
        "failed",
        params={"todo_id": task, "conflict": {"try": 2}},
        meta={"push": {"state": "empty", "host": _HOST, "at": _ts(0)}},
    )
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 2, "job": job}})
    monkeypatch.setattr(lanes, "lane_backoffs", lambda store: {"claude-sub": 1})
    res = pd.run_pass(store)
    assert res.asks == 0 and res.dispatched == 0
    assert any("is backed off" in n for n in res.notes)


def test_an_unplaced_ask_is_raised_again_for_a_later_job(store: Store) -> None:
    """Item 4: the mark names the job it was raised for; a later pinned
    job whose first ask is closed gets its own."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    pin = {pd.HUB_PIN_KEY: {"host": _HOST, "todo": task}}
    params = {"todo_id": task, "target_node": _HOST}

    def queued() -> int:
        job = _slice_job(
            store,
            task,
            "run_slice",
            "queued",
            params=params,
            meta={"adapter": "claude_p", **pin},
        )
        _backdate(store, job, 35)
        return job

    _dark_advertise(store, _HOST, {"repo_checkout": 1, "claude_bin": 1, "git": 1})
    first = queued()
    assert pd.run_pass(store).asks == 1
    ask = int(_meta(store, task)[pd._UNPLACED_ASK_KEY]["ask"])
    _set_job_status(store, first, "cancelled")
    _answer_ask(store, ask, "wait")
    second = queued()
    assert pd.run_pass(store).asks == 1
    assert _meta(store, task)[pd._UNPLACED_ASK_KEY]["job"] == second
    assert pd.run_pass(store).asks == 0  # not again for the same job


@pytest.mark.parametrize("bad", ["a string", ["a", "list"], 7])
@pytest.mark.parametrize("key", ["conflict", "conflict_tries", "build_ref", "parked"])
def test_a_non_dict_task_meta_never_crashes_a_pass(
    store: Store, key: str, bad: Any
) -> None:
    """Item 5: every reader checks the shape of the conflict path's and the
    park stamp's metas."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    _ask_blocking(store, task, mint.root_id)
    store.stamp_ref_meta(task, {key: bad})
    pd.run_pass(store)
    pd.run_pass(store)


@pytest.mark.parametrize("bad", ["a string", ["a", "list"], 7])
def test_a_non_dict_job_push_never_crashes_a_pass(store: Store, bad: Any) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    _slice_job(
        store,
        task,
        "run_slice",
        "failed",
        params={"todo_id": task, "conflict": ["not", "a", "dict"]},
        meta={"push": bad},
    )
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 1, "job": "x"}})
    _ask_blocking(store, task, mint.root_id)
    pd.run_pass(store)
    pd.run_pass(store)


@pytest.mark.parametrize(
    "stamp", [(pd.HUMAN_ONLY_KEY, ["scripts/ship"]), (pd.MODEL_RULE_KEY, "same model")]
)
def test_a_deterministic_refusal_on_a_conflict_attempt_wins(
    store: Store, stamp: tuple[str, Any]
) -> None:
    """Item 6: the refusal's own ask is filed, not three tries and then the
    conflict ask."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    pd.run_pass(store)
    ((job, _),) = _conflict_jobs(store, task)
    _end_try(store, job, pushed=False)
    store.stamp_ref_meta(job, {stamp[0]: stamp[1]})
    res = pd.run_pass(store)
    assert res.asks == 1 and res.dispatched == 0
    (ask,) = _asks(store, mint.root_id)
    ref = store.get_ref(kind="todo", id=ask)
    assert ref is not None and "land conflict" not in str(ref.title)
    assert len(_conflict_jobs(store, task)) == 1


def test_a_voided_conflict_attempt_is_not_a_failed_try(store: Store) -> None:
    """Item 7: a ``void:`` open tag on the attempt means the same try
    mints again."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    pd.run_pass(store)
    ((job, _),) = _conflict_jobs(store, task)
    _end_try(store, job, pushed=False)
    store.add_tag(job, Tag.open("void:seat"), set_by="system")
    pd.run_pass(store)
    tries = [m["params"]["conflict"]["try"] for _j, m in _conflict_jobs(store, task)]
    assert tries == [1, 1]


def test_no_build_ref_voids_the_conflict(store: Store) -> None:
    """Item 9: with ``build_ref`` cleared by hand no attempt can continue
    from a tip nobody holds: both keys clear, the ordinary path runs."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    store.stamp_ref_meta(
        task, {"build_ref": None, "conflict_tries": {"n": 1, "job": 0}}
    )
    res = pd.run_pass(store)
    assert any("holds no build_ref any more" in n for n in res.notes)
    meta = _meta(store, task)
    assert meta.get("conflict") is None and meta.get("conflict_tries") is None
    assert _conflict_jobs(store, task) == []
    assert res.dispatched == 1  # the ordinary rebuild


def test_a_held_reviewer_keeps_its_claim_and_status(store: Store) -> None:
    """Item 10: the reviewer of a land held behind another task's
    resolution is not touched — claimed and ``doing`` as its run left it —
    and is not re-dispatched."""
    a = _reviewed_pending(store, "review: LAND")
    b = _reviewed_pending(store, "review: LAND")
    _resolved(store, a)
    review_b = b.ids["review"]
    before = _jobs_under(store, review_b)
    assert any(t.startswith("claimed-by:") for t in _open_tags(store, review_b))
    res = pd.run_pass(store)
    assert _land_jobs(store, b.ids["build"]) == []
    assert _status(store, review_b) == "doing"
    assert any(t.startswith("claimed-by:") for t in _open_tags(store, review_b))
    assert _jobs_under(store, review_b) == before
    assert "review" not in _meta(store, review_b)


def test_two_pending_resolutions_land_in_push_order(store: Store) -> None:
    a = _reviewed_pending(store, "review: LAND")
    b = _reviewed_pending(store, "review: LAND")
    _resolved(store, a, pushed_min=-10)
    _resolved(store, b, pushed_min=-5)
    res = pd.run_pass(store)
    assert len(_land_jobs(store, a.ids["build"])) == 1
    assert _land_jobs(store, b.ids["build"]) == []
    assert any(
        f"td{b.ids['build']}: land held — waits for td{a.ids['build']}" in n
        for n in res.notes
    )
    (a_land,) = _land_jobs(store, a.ids["build"])
    _set_job_status(store, a_land[0], "succeeded")
    store.stamp_ref_meta(
        a.ids["build"], {"conflict": None, "build_ref": None, "pending_land": None}
    )
    pd.run_pass(store)
    assert len(_land_jobs(store, b.ids["build"])) == 1


def test_a_lapsed_hold_is_noted_once_per_holder(store: Store) -> None:
    a = _reviewed_pending(store, "review: LAND")
    b = _reviewed_pending(store, "review: LAND")
    c = _reviewed_pending(store, "review: LAND")
    _resolved(store, a, pushed_min=-(pd.CONFLICT_LAND_HOLD.total_seconds() / 60 + 5))
    res = pd.run_pass(store)
    assert len(_land_jobs(store, b.ids["build"])) == 1
    assert len(_land_jobs(store, c.ids["build"])) == 1
    assert len([n for n in res.notes if "lapsed" in n]) == 1


def test_resolution_param_and_score_read_the_same_targets(store: Store) -> None:
    """Item 11: a reviewer's held targets decide both ``params.resolution``
    and the top score."""
    mint = _review_plan(store, preland=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="devin")
    _resolved(store, mint)
    # No pending_land: nothing held, so neither applies.
    plan = pd._load(store)
    node = plan.nodes[review]
    assert pd._resolution_param(plan, node) is None
    assert pd._conflict_first(plan, node, "run_slice") is False


# ── round 4 ─────────────────────────────────────────────────────────


def test_conflict_era_reds_do_not_cap_the_ordinary_rebuild(store: Store) -> None:
    """Item 1: build red (1), build ok, land conflicts, try 1 pushes and its
    land conflicts, try 2 pushes and its land goes CI red (2): the rows say
    2 while ``attempt:<n>`` (every red) says 5. After the episode ends the
    rows count — one ordinary rebuild on the rung the count gives, no cap
    ask."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _failed_job(store, task, "boom", lane="devin", model="swe-2-high")
    _conflicted(store, task)
    store.add_tag(task, Tag.open("attempt:5"), set_by="system")
    pd.run_pass(store)
    ((j1, _),) = _conflict_jobs(store, task)
    _end_try(store, j1, pushed=True)
    land = _slice_job(store, task, "land_slice", "failed", params={"todo_id": task})
    _event(store, land, f"✖ {_CONFLICT_LINE}")
    store.stamp_ref_meta(
        task, {"conflict": _conflict(task, head="1" * 40, at=_ts(-1), job=land)}
    )
    pd.run_pass(store)
    (_, _), (j2, _) = _conflict_jobs(store, task)
    _end_try(store, j2, pushed=True)
    red = _slice_job(store, task, "land_slice", "failed", params={"todo_id": task})
    _event(store, red, "✖ ci red")
    res = pd.run_pass(store)
    assert any("conflict episode over" in n for n in res.notes)
    plan = pd._load(store)
    assert pd._attempts(plan, plan.nodes[task]) == 2
    assert (res.dispatched, res.asks) == (1, 0)
    _jid, meta = _jobs_under(store, task)[-1]
    assert "conflict" not in meta["params"]
    # Rung 3 of the seed ladder: the count (2) says where the climb starts.
    assert (meta["params"]["lane"], meta["params"]["model"]) == (
        "devin",
        "swe-2-max",
    )


def test_a_capacity_refused_resolution_land_does_not_end_the_episode(
    store: Store,
) -> None:
    """Item 2: an attempt-free land says nothing about the branch: the
    conflict keys stand, the hold stands, the re-land mints and the count is
    not reset."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    pd.run_pass(store)
    ((j1, _),) = _conflict_jobs(store, task)
    _end_try(store, j1, pushed=True)
    land = _slice_job(
        store,
        task,
        "land_slice",
        "failed",
        params={"todo_id": task, "branch": f"slice/{task}", "target_node": _HOST},
        meta={
            "attempt_free": "infra-land",
            "red_class": land_slice.ADMISSION_RED_CLASS,
        },
    )
    tries = dict(_meta(store, task)["conflict_tries"])
    plan = pd._load(store)
    assert pd._conflict_void(plan, plan.nodes[task]) is None
    assert pd._conflict_pending_since(plan, plan.nodes[task]) is not None
    res = pd.run_pass(store)
    assert not any("conflict episode over" in n for n in res.notes)
    meta = _meta(store, task)
    assert meta["conflict"]["head"] == _H0 and meta["conflict_tries"] == tries
    (_jid, rl), *_ = [x for x in _jobs_under(store, task)][-1:]
    assert rl["job_type"] == "land_slice" and rl["params"]["todo_id"] == task
    assert _jobs_under(store, task)[-1][0] > land


def test_a_stamp_by_a_later_land_is_a_failed_try_whatever_the_clocks_say(
    store: Store,
) -> None:
    """Item 4: ``conflict.job`` decides. The stamp's ``at`` is the land
    host's clock — here an hour BEHIND the database — and the resolution
    still reads as sent back to a new try."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    pd.run_pass(store)
    ((j1, _),) = _conflict_jobs(store, task)
    _end_try(store, j1, pushed=True)
    land = _slice_job(store, task, "land_slice", "failed", params={"todo_id": task})
    _event(store, land, f"✖ {_CONFLICT_LINE}")
    store.stamp_ref_meta(
        task, {"conflict": _conflict(task, head="1" * 40, at=_ts(-60), job=land)}
    )
    plan = pd._load(store)
    assert pd._conflict_view(plan, plan.nodes[task])[:2] == ("try", 2)
    assert pd._conflict_void(plan, plan.nodes[task]) is None
    pd.run_pass(store)
    assert [
        m["params"]["conflict"]["try"] for _j, m in _conflict_jobs(store, task)
    ] == [
        1,
        2,
    ]


def test_a_stamp_by_an_earlier_land_is_not_a_renewal_whatever_the_clocks_say(
    store: Store,
) -> None:
    """Item 4, the other direction: the lander's clock is ahead (``at`` is
    in the future); the stamp names the land BEFORE the attempt, so the
    pushed resolution is not read as a failed try."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    land0 = _conflicted(store, task)
    store.stamp_ref_meta(task, {"conflict": _conflict(task, at=_ts(30), job=land0)})
    pd.run_pass(store)
    ((j1, _),) = _conflict_jobs(store, task)
    _end_try(store, j1, pushed=True)
    plan = pd._load(store)
    assert pd._conflict_view(plan, plan.nodes[task])[0] == "wait"


@pytest.mark.parametrize("minutes", [-90, 90])
def test_the_void_rule_reads_the_land_job_not_the_clocks(
    store: Store, minutes: int
) -> None:
    """Item 4: a land newer than the attempt ended. The stamp names that
    land (a conflict again) -> the episode stands, whatever ``at`` says; it
    names an earlier one -> the land failed for another reason and the
    episode is void, whatever ``at`` says."""
    # The offset is the parameter, not the timestamp: a timestamp in the
    # test id differs between xdist workers that collect a second apart.
    at = _ts(minutes)
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    land0 = _conflicted(store, task)
    pd.run_pass(store)
    ((j1, _),) = _conflict_jobs(store, task)
    _end_try(store, j1, pushed=True)
    land = _slice_job(store, task, "land_slice", "failed", params={"todo_id": task})
    store.stamp_ref_meta(task, {"conflict": _conflict(task, at=at, job=land)})
    plan = pd._load(store)
    assert pd._conflict_void(plan, plan.nodes[task]) is None
    store.stamp_ref_meta(task, {"conflict": _conflict(task, at=at, job=land0)})
    plan = pd._load(store)
    assert pd._conflict_void(plan, plan.nodes[task]) is not None


def test_a_stamp_without_job_falls_back_to_the_clocks(store: Store) -> None:
    """Item 4: a stamp from before the lander wrote ``job``."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    store.stamp_ref_meta(task, {"conflict": _conflict(task, at=_ts(-1))})
    pd.run_pass(store)
    ((j1, _),) = _conflict_jobs(store, task)
    _end_try(store, j1, pushed=True)
    plan = pd._load(store)
    assert pd._conflict_view(plan, plan.nodes[task])[0] == "wait"
    store.stamp_ref_meta(task, {"conflict": _conflict(task, at=_ts(5))})
    plan = pd._load(store)
    assert pd._conflict_view(plan, plan.nodes[task])[:2] == ("try", 2)


@pytest.mark.parametrize("bad", ["7", True, [3], 2.5])
def test_a_malformed_stamp_job_falls_back_and_never_crashes(
    store: Store, bad: Any
) -> None:
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    _conflicted(store, task)
    stamp = _conflict(task)
    stamp["job"] = bad
    store.stamp_ref_meta(task, {"conflict": stamp})
    pd.run_pass(store)
    pd.run_pass(store)


@pytest.mark.parametrize("bad", ["7", True, [3], 2.5, 0, -4, None])
def test_a_stamp_job_that_names_no_job_reads_as_absent(bad: Any) -> None:
    """A job id of 0 would compare as older than every job and void the
    episode at once; it must send the comparison to the clocks instead."""
    assert pd._stamp_job({"job": bad}) is None
    assert pd._stamp_job({}) is None
    assert pd._stamp_job({"job": 7}) == 7


def test_the_newest_red_land_is_the_conflict_path_only_when_it_stamped() -> None:
    """The newest job is a failed land. It is the conflict path's red only
    when the task's conflict stamp names that very land; a stamp an earlier
    land left makes it an ordinary red."""
    at = datetime.now(UTC)

    def node(id_: int, kind: str, meta: dict[str, Any], status: str) -> Any:
        return pd._Node(
            id=id_,
            kind=kind,
            parent_id=None,
            title="",
            meta=meta,
            status=status,
            created_at=at,
            prio=None,
            depth=0,
            root=1,
        )

    land = node(40, "job", {"job_type": "land_slice"}, "failed")
    plan: Any = None
    mine = node(1, "todo", {"conflict": {"job": 40}}, "open")
    older = node(1, "todo", {"conflict": {"job": 31}}, "open")
    none = node(1, "todo", {}, "open")
    assert pd._conflict_red_land(plan, mine, 0, [land]) is True
    assert pd._conflict_red_land(plan, older, 0, [land]) is False
    assert pd._conflict_red_land(plan, none, 0, [land]) is False


def test_a_pin_to_a_host_held_at_cap_zero_asks_and_heals(store: Store) -> None:
    """gr752561: a queued job pinned to a live host whose ssh_node lane cap
    is 0 can never claim — it counts as a placement gap, and the ask
    closes once the cap is raised."""
    mint = _one_task_plan(store)
    task = mint.ids["task"]
    store.sync_host_resource_slots(
        _HOST, {"repo_checkout": 1, "claude_bin": 1, "git": 1}
    )
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO host_heartbeat (host, ts) VALUES (%s, now()) "
            "ON CONFLICT (host) DO UPDATE SET ts = EXCLUDED.ts",
            (_HOST,),
        )
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (%s, '0') "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
            (f"claim.executor_cap.ssh_node.{_HOST}",),
        )
        conn.commit()
    job = _slice_job(
        store,
        task,
        "run_slice",
        "queued",
        params={"todo_id": task, "target_node": _HOST},
        meta={
            "adapter": "claude_p",
            pd.HUB_PIN_KEY: {"host": _HOST, "todo": task},
        },
    )
    _backdate(store, job, 35)
    res = pd.run_pass(store)
    assert res.asks == 1
    cap_key = f"claim.executor_cap.ssh_node.{_HOST}"
    assert any("ssh_node cap 0" in note and cap_key in note for note in res.notes)
    assert all("no host advertises" not in note for note in res.notes)
    ask = int(_meta(store, task)[pd._UNPLACED_ASK_KEY]["ask"])
    ask_meta = _meta(store, ask)["ask"]
    assert "ssh_node cap 0" in ask_meta["context"]
    assert cap_key in ask_meta["context"]
    assert ask_meta["options"] == ["raise the cap", "keep the hold", "drop the node"]
    body = next(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(ask)
        if c.chunk_kind == TODO_BODY_KIND
    )
    assert "effective ssh_node cap is 0" in body
    assert f"Set `{cap_key}` above 0" in body
    assert "release the pin" in body
    assert "scheduler/unschedulable" not in body
    assert "provision a host" not in body
    assert pd.run_pass(store).asks == 0
    assert _status(store, ask) == "open"
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE app_state SET value = '2' WHERE key = %s",
            (cap_key,),
        )
        conn.commit()
    pd.run_pass(store)
    assert _status(store, ask) == "done"
