"""Lane choice under a hub pin (gr778684): the tick resolves the pin
before it picks a lane, so a review and a retry climb choose from the
lanes the pinned host advertises a binary for. Before, the pick ran
first — codex for most reviews — and the pin then sent the job to a host
without ``codex_bin``: the claim's placement gate refused it there, the
pin kept every other host off it, and the build's ``pending_land`` held
the file-overlap gate for everything sharing a file."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis.workers.executors._common import claim_executor_jobs
from precis.workers.job_types import plan_dispatch as pd
from tests._lane_rows import seed_lane, use_lanes
from tests.test_plan_dispatch import (
    _advertise,
    _answer_ask,
    _build_ref,
    _comment_texts,
    _factory_on,  # noqa: F401  (autouse fixture of the sibling module)
    _failed_job,
    _job,
    _jobs_under,
    _mark_done,
    _meta,
    _one_task_plan,
    _open_tags,
    _review_plan,
    _slice_job,
    _status,
)

#: The hub host: runs devin and claude, not codex.
_HOST = "host-a"
_HOST_SLOTS: dict[str, int | None] = {
    "repo_checkout": 1,
    "git": 1,
    "devin_bin": 1,
    "claude_bin": 1,
}


def _claimed_on(store: Store, host: str) -> list[int]:
    """The job ids ``host``'s ssh_node claim takes (rolled back)."""
    with store.pool.connection() as conn:
        claimed = claim_executor_jobs(
            conn,
            executor="ssh_node",
            limit=5,
            node=host,
            reserve_host_id=host,
            parent_not_paused=True,
        )
        conn.rollback()
    return [int(row[0]) for row in claimed]


def _events(store: Store, ref_id: int, event: str) -> list[dict[str, Any]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT payload FROM ref_events WHERE ref_id = %s AND event = %s "
            "ORDER BY event_id",
            (ref_id, event),
        ).fetchall()
    return [dict(r[0] or {}) for r in rows]


def _codex_first_review_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """codex ahead of devin in table order: with the claude builder's
    vendor excluded both sit at 0 live, so the tie goes to codex."""
    use_lanes(
        monkeypatch, seed_lane("codex"), seed_lane("devin"), seed_lane("claude-sub")
    )


def _pinned_review(store: Store, *, pinned: bool = True) -> tuple[int, int]:
    """A pre-land review whose build (built on claude-sub) holds its work
    in ``_HOST``'s hub — no ``build_ref`` (so no pin) when not ``pinned``;
    returns ``(build, review)``."""
    mint = _review_plan(store, preland=True)
    build, review = mint.ids["build"], mint.ids["review"]
    _job(store, build, lane="claude-sub")
    store.stamp_ref_meta(
        build,
        {
            **({"build_ref": _build_ref(build, host=_HOST)} if pinned else {}),
            "pending_land": {
                "todo_id": build,
                "branch": f"slice/{build}",
                "ref": f"refs/factory/build/{build}/7",
                "head": "a" * 40,
                "lane": "claude-sub",
            },
        },
    )
    _mark_done(store, build)
    return build, review


def test_unpinned_review_picks_codex_on_this_table(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control: with no hub pin the same table sends the review to
    codex — the pick the pin used to strand."""
    _codex_first_review_table(monkeypatch)
    _advertise(store, _HOST, _HOST_SLOTS)
    _build, review = _pinned_review(store, pinned=False)
    pd.run_pass(store)
    _job_id, meta = _jobs_under(store, review)[-1]
    assert meta["params"]["lane"] == "codex"
    assert "target_node" not in meta["params"]


def test_pinned_review_picks_a_lane_its_host_runs_and_is_claimed_there(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _codex_first_review_table(monkeypatch)
    _advertise(store, _HOST, _HOST_SLOTS)
    build, review = _pinned_review(store)

    pd.run_pass(store)

    jobs = _jobs_under(store, review)
    assert jobs, "the reviewer was not dispatched"
    job_id, meta = jobs[-1]
    assert meta["params"]["target_node"] == _HOST
    assert meta["params"]["lane"] == "devin"
    assert meta[pd.HUB_PIN_KEY] == {"host": _HOST, "todo": build}
    assert pd.HUB_PIN_DROPPED_KEY not in meta
    # The fallback is one the host runs too (claude-sub shares the
    # builder's vendor, so none is left).
    assert meta.get("review_fallback") in (None, "devin", "claude-sub")
    assert _claimed_on(store, _HOST) == [job_id]


def _age_hold(store: Store, todo: int, by: timedelta = timedelta(minutes=31)) -> None:
    """Back-date ``todo``'s dispatch hold's first sight by ``by``."""
    held = dict(_meta(store, todo)[pd.DISPATCH_HOLD_KEY])
    since = datetime.now(UTC) - by
    store.stamp_ref_meta(
        todo,
        {
            pd.DISPATCH_HOLD_KEY: {
                **held,
                "since": since.strftime("%Y-%m-%dT%H:%M:%SZ"),
            }
        },
    )


def _host_gap_asks(store: Store) -> list[tuple[int, dict[str, Any], str]]:
    """``(id, meta.host_gap, STATUS)`` of every host-gap ask."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.meta->'host_gap', t.value FROM refs r "
            "JOIN ref_tags rt ON rt.ref_id = r.ref_id "
            "JOIN tags t ON t.tag_id = rt.tag_id AND t.namespace = 'STATUS' "
            "WHERE r.kind = 'todo' AND jsonb_typeof(r.meta->'host_gap') = 'object' "
            "ORDER BY r.ref_id"
        ).fetchall()
    return [(int(r[0]), dict(r[1]), str(r[2])) for r in rows]


def test_host_gap_wait_is_one_queued_stall_window() -> None:
    from precis.workers import factory_rescue as fr

    assert fr.STAGE_DEADLINES["queued"] == pd.HOST_GAP_WAIT


def test_pinned_reviews_on_a_host_running_no_review_lane_wait_and_ask_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host runs only kimi, which takes no review. An off-hub review
    could not read the work (its clone fetches from its own host's hub),
    so the reviews stay pinned and held. After the wait, ONE ask names the
    host and the missing token for both; it closes when the host
    advertises the token, and the reviews then dispatch there."""
    _codex_first_review_table(monkeypatch)
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "kimi_bin": 1})
    _b1, r1 = _pinned_review(store)
    _b2, r2 = _pinned_review(store)

    res = pd.run_pass(store)

    assert _jobs_under(store, r1) == [] and _jobs_under(store, r2) == []
    reason = res.dispatch_holds[r1]
    assert reason.startswith(pd.HOST_GAP_HOLD)
    assert f"host {_HOST} lacks codex_bin" in reason
    assert _host_gap_asks(store) == []  # inside the wait: no ask yet

    _age_hold(store, r1)
    _age_hold(store, r2)
    res = pd.run_pass(store)

    assert _jobs_under(store, r1) == [] and _jobs_under(store, r2) == []
    ((ask, rec, status),) = _host_gap_asks(store)
    assert (rec["host"], rec["token"], rec["reviews"], status) == (
        _HOST,
        "codex_bin",
        sorted([r1, r2]),
        "open",
    )
    assert "ask-user" in _open_tags(store, ask)

    pd.run_pass(store)
    assert len(_host_gap_asks(store)) == 1  # once per host and token

    _advertise(
        store, _HOST, {"repo_checkout": 1, "git": 1, "kimi_bin": 1, "codex_bin": 1}
    )
    pd.run_pass(store)

    ((_ask, _rec, status),) = _host_gap_asks(store)
    assert status == "done"
    for review in (r1, r2):
        _job_id, meta = _jobs_under(store, review)[-1]
        assert meta["params"]["lane"] == "codex"
        assert meta["params"]["target_node"] == _HOST


_KIMI_ONLY: dict[str, int | None] = {"repo_checkout": 1, "git": 1, "kimi_bin": 1}


def _asked_review(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> tuple[int, int, int]:
    """A pinned review held on host-a's missing codex_bin long enough for
    the host-gap ask; returns ``(build, review, ask)``."""
    _codex_first_review_table(monkeypatch)
    _advertise(store, _HOST, _KIMI_ONLY)
    build, review = _pinned_review(store)
    pd.run_pass(store)
    _age_hold(store, review)
    pd.run_pass(store)
    ((ask, _rec, _status),) = _host_gap_asks(store)
    return build, review, ask


def _backdate_close(store: Store, ask: int, by: timedelta) -> None:
    """Move the ask's terminal STATUS stamp ``by`` into the past."""
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE ref_tags SET created_at = now() - %s::interval "
            "WHERE ref_id = %s AND tag_id IN "
            "(SELECT tag_id FROM tags WHERE namespace = 'STATUS')",
            (f"{int(by.total_seconds())} seconds", ask),
        )
        conn.commit()


def test_a_closed_host_gap_ask_is_the_ruling_for_a_day(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A person closes the ask while the review still waits: no new ask
    for host-a / codex_bin for HOST_GAP_ASK_QUIET from the close, then
    exactly one."""
    _build, review, ask = _asked_review(store, monkeypatch)
    _answer_ask(store, ask, "keep waiting")

    res = pd.run_pass(store)
    assert [a for a, _r, s in _host_gap_asks(store) if s == "open"] == []
    assert any("the closed ask is the ruling" in note for note in res.notes)

    _backdate_close(store, ask, pd.HOST_GAP_ASK_QUIET - timedelta(minutes=5))
    pd.run_pass(store)
    assert [a for a, _r, s in _host_gap_asks(store) if s == "open"] == []

    _backdate_close(store, ask, pd.HOST_GAP_ASK_QUIET + timedelta(minutes=5))
    pd.run_pass(store)
    pd.run_pass(store)
    reopened = [(a, r) for a, r, s in _host_gap_asks(store) if s == "open"]
    assert len(reopened) == 1 and reopened[0][1]["reviews"] == [review]


def test_a_fixed_answer_that_did_not_fix_it_asks_again_after_the_wait(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The answer says the token was fixed, the host still lacks it: a new
    ask after one HOST_GAP_WAIT from the close, not a day."""
    _build, _review, ask = _asked_review(store, monkeypatch)
    _answer_ask(store, ask, f"codex_bin fixed on {_HOST}")

    pd.run_pass(store)
    assert [a for a, _r, s in _host_gap_asks(store) if s == "open"] == []

    _backdate_close(store, ask, pd.HOST_GAP_WAIT + timedelta(minutes=1))
    pd.run_pass(store)
    assert len([a for a, _r, s in _host_gap_asks(store) if s == "open"]) == 1


def test_an_open_host_gap_ask_closes_wont_do_when_its_reviews_are_gone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _build, review, ask = _asked_review(store, monkeypatch)
    store.add_tag(
        review, Tag.closed("STATUS", "won't-do"), set_by="system", replace_prefix=True
    )

    pd.run_pass(store)

    assert _status(store, ask) == "won't-do"
    assert _meta(store, ask)["host_gap"]["closed_by"] == "tick"


def test_an_open_host_gap_ask_closes_done_when_a_lane_now_fits(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host gains devin_bin, not codex_bin: the review dispatches on
    devin there, nothing waits on codex_bin, and the ask closes done."""
    _build, review, ask = _asked_review(store, monkeypatch)
    _advertise(store, _HOST, {**_KIMI_ONLY, "devin_bin": 1})

    pd.run_pass(store)

    _job_id, meta = _jobs_under(store, review)[-1]
    assert (meta["params"]["lane"], meta["params"]["target_node"]) == (
        "devin",
        _HOST,
    )
    assert _status(store, ask) == "done"
    assert "a lane now fits" in " ".join(_comment_texts(store, ask))


def test_an_open_host_gap_ask_refreshes_its_reviews_and_links_them(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    _b1, r1, ask = _asked_review(store, monkeypatch)
    _b2, r2 = _pinned_review(store)
    pd.run_pass(store)
    _age_hold(store, r2)

    pd.run_pass(store)

    ((same, rec, status),) = _host_gap_asks(store)
    assert (same, status, rec["reviews"]) == (ask, "open", sorted([r1, r2]))
    with store.pool.connection() as conn:
        linked = {
            int(r[0])
            for r in conn.execute(
                "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
                "AND relation = 'related-to'",
                (ask,),
            ).fetchall()
        }
    assert {r1, r2} <= linked


def test_enqueue_roots_never_reconciles_host_gap_asks(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A partial pass sees no held review for the ask and must not read
    that as "nothing waits any more"."""
    _build, _review, ask = _asked_review(store, monkeypatch)
    other = _one_task_plan(store)

    pd.enqueue_roots(store, [other.root_id])

    assert _status(store, ask) == "open"


def test_an_unknown_lane_is_no_host_gap(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``params.lane`` the lane table does not hold has no token: no
    host-gap hold, no pin drop, and a note names the lane."""
    use_lanes(monkeypatch)
    _advertise(store, _HOST, _KIMI_ONLY)
    task = _one_task_plan(store).ids["task"]
    store.stamp_ref_meta(
        task,
        {
            "build_ref": _build_ref(task, host=_HOST),
            "op": {"type": "run_slice", "params": {"lane": "ghost-lane"}},
        },
    )

    res = pd.run_pass(store)

    assert not str(res.dispatch_holds.get(task) or "").startswith(pd.HOST_GAP_HOLD)
    assert any("lane 'ghost-lane' is not in the lane table" in n for n in res.notes)
    assert all(pd.HUB_PIN_DROPPED_KEY not in m for _j, m in _jobs_under(store, task))


def test_rescue_reads_a_host_gap_hold_as_a_wait(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The held review has no job and its idle clock runs out: the stall
    watchdog reads the host-gap hold as a wait, so no per-node ask."""
    from precis.workers import factory_rescue as fr

    _codex_first_review_table(monkeypatch)
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "kimi_bin": 1})
    _build, review = _pinned_review(store)
    pd.run_pass(store)

    res = fr.run_stall_pass(store, datetime.now(UTC) + timedelta(hours=2))

    assert all(rid != review for rid, _c, _s in res.rescued)
    assert any(f"td{review}: waiting ({pd.HOST_GAP_HOLD}" in note for note in res.notes)


def test_pinned_review_takes_the_fallback_its_host_runs(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """devin, the one other-vendor lane the host runs, is at cap, so no
    lane on the host is open; the unconstrained pick (codex) has devin as
    its fallback, and the host runs that — the review takes it, pinned."""
    use_lanes(
        monkeypatch,
        seed_lane("codex"),
        replace(seed_lane("devin"), cap=1),
        seed_lane("claude-sub"),
    )
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "devin_bin": 1})
    build, review = _pinned_review(store)
    other = _one_task_plan(store).ids["task"]
    _job(store, other, status="running", lane="devin")

    res = pd.run_pass(store)

    _job_id, meta = _jobs_under(store, review)[-1]
    assert meta["params"]["lane"] == "devin"
    assert meta["params"]["target_node"] == _HOST
    assert meta[pd.HUB_PIN_KEY] == {"host": _HOST, "todo": build}
    assert any(
        "review takes its fallback lane devin" in note
        and "does not run lane codex" in note
        for note in res.notes
    )


def test_pinned_review_waits_while_its_hosts_lane_is_full(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host runs only claude-sub, which is at cap, and the
    unconstrained pick's fallback (devin) is not on the host: the review
    waits for the host's lane instead of dropping the pin."""
    use_lanes(
        monkeypatch,
        seed_lane("codex"),
        seed_lane("devin"),
        replace(seed_lane("claude-sub"), cap=1),
    )
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "claude_bin": 1})
    _build, review = _pinned_review(store)
    other = _one_task_plan(store).ids["task"]
    _job(store, other, status="running", lane="claude-sub")

    res = pd.run_pass(store)

    assert _jobs_under(store, review) == []
    assert f"every review lane host {_HOST} runs is full" in str(
        res.dispatch_holds.get(review)
    )


def test_pinned_retry_climbs_past_a_lane_its_host_does_not_run(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_escalate``: the climb above devin swe-2-high lands on codex on
    this table, which the hub host cannot run — the retry takes the next
    rung the host runs, stays pinned and is claimed there."""
    codex = replace(
        seed_lane("codex"), task_classes=frozenset({"code", "review", "validate"})
    )
    use_lanes(monkeypatch, seed_lane("devin"), codex, seed_lane("claude-sub"))
    _advertise(store, _HOST, _HOST_SLOTS)
    task = _one_task_plan(store).ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task, host=_HOST)})
    _failed_job(store, task, "build red on the diff", lane="devin", model="swe-2-high")

    pd.run_pass(store)

    job_id, meta = _jobs_under(store, task)[-1]
    assert meta["job_type"] == "run_slice"
    assert meta["params"]["target_node"] == _HOST
    assert meta["params"]["lane"] == "claude-sub"
    assert meta[pd.HUB_PIN_KEY] == {"host": _HOST, "todo": task}
    assert _claimed_on(store, _HOST) == [job_id]


def test_unpinned_retry_climbs_to_codex_on_this_table(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control for the climb above: without a build_ref the next rung
    is codex."""
    codex = replace(
        seed_lane("codex"), task_classes=frozenset({"code", "review", "validate"})
    )
    use_lanes(monkeypatch, seed_lane("devin"), codex, seed_lane("claude-sub"))
    task = _one_task_plan(store).ids["task"]
    _failed_job(store, task, "build red on the diff", lane="devin", model="swe-2-high")

    pd.run_pass(store)

    _job_id, meta = _jobs_under(store, task)[-1]
    assert meta["params"]["lane"] == "codex"


def _code_codex() -> Any:
    """codex fit for code, so it sits on the code ladder (rung 2)."""
    return replace(
        seed_lane("codex"), task_classes=frozenset({"code", "review", "validate"})
    )


def test_pinned_build_waits_out_a_token_flap_then_stays_pinned(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host's probe drops claude_bin for a pass: the build's next rung
    is claude-sub, so the retry is held — not unpinned — and dispatches
    pinned once the token is back."""
    use_lanes(monkeypatch)
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "kimi_bin": 1})
    task = _one_task_plan(store).ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task, host=_HOST)})
    _failed_job(store, task, "build red on the diff", lane="devin", model="swe-2-high")

    res = pd.run_pass(store)

    assert len(_jobs_under(store, task)) == 1  # only the failed attempt
    assert f"host {_HOST} lacks claude_bin" in res.dispatch_holds[task]

    _advertise(store, _HOST, _HOST_SLOTS)
    pd.run_pass(store)

    _job_id, meta = _jobs_under(store, task)[-1]
    assert meta["params"]["lane"] == "claude-sub"
    assert meta["params"]["target_node"] == _HOST
    assert pd.HUB_PIN_DROPPED_KEY not in meta


def test_pinned_build_drops_the_pin_after_the_wait(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host runs no code lane for a whole wait: the retry mints
    unpinned, once, and the job says why and that the run restarts off
    the hub."""
    use_lanes(monkeypatch)
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "kimi_bin": 1})
    task = _one_task_plan(store).ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task, host=_HOST)})
    _failed_job(store, task, "build red on the diff", lane="devin", model="swe-2-high")
    pd.run_pass(store)
    _age_hold(store, task)

    res = pd.run_pass(store)

    job_id, meta = _jobs_under(store, task)[-1]
    assert meta["params"]["lane"] == "claude-sub"
    assert "target_node" not in meta["params"]
    assert pd.HUB_PIN_KEY not in meta
    dropped = meta[pd.HUB_PIN_DROPPED_KEY]
    assert (dropped["host"], dropped["todo"], dropped["token"]) == (
        _HOST,
        task,
        "claude_bin",
    )
    assert dropped["note"].startswith("the run restarts off the hub")
    assert _events(store, job_id, "hub-pin-dropped") == [dropped]
    assert any(f"hub pin to {_HOST} dropped" in note for note in res.notes)


def test_escalate_counts_a_spent_host_rung_as_not_runnable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The host runs only claude-sub, and both its rungs' caps are spent:
    nothing on the host can run, so the climb picks as if unpinned
    (codex) and the pin drops after the wait — instead of a lane-wait on
    a cap that never clears."""
    use_lanes(
        monkeypatch,
        seed_lane("devin"),
        _code_codex(),
        seed_lane("claude-sub"),
        entitlements={
            "claude-sub": {
                "rung_caps": {"claude-sonnet-5-5": 5.0, "claude-opus-5-5": 5.0}
            }
        },
    )
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "claude_bin": 1})
    task = _one_task_plan(store).ids["task"]
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task, host=_HOST)})
    _failed_job(
        store,
        task,
        "build red on the diff",
        lane="devin",
        model="swe-2-high",
        meta={"usage": {"cost_usd": 6.0}},
    )

    res = pd.run_pass(store)
    assert f"host {_HOST} lacks codex_bin" in res.dispatch_holds[task]
    _age_hold(store, task)
    pd.run_pass(store)

    _job_id, meta = _jobs_under(store, task)[-1]
    assert meta["params"]["lane"] == "codex"
    assert "target_node" not in meta["params"]
    assert meta[pd.HUB_PIN_DROPPED_KEY]["token"] == "codex_bin"


def test_conflict_try_three_counts_a_spent_host_rung_as_not_runnable(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Try 3 on a host that runs only devin, both devin rungs' caps spent:
    the try takes the ladder's top (claude-sub opus) as if unpinned and
    the pin drops after the wait — no spurious "cannot run" ask."""
    from tests._lane_rows import SEED_LANES
    from tests.test_plan_dispatch_conflict import _conflict_jobs, _conflicted

    use_lanes(
        monkeypatch,
        *SEED_LANES,
        entitlements={"devin": {"rung_caps": {"swe-2-high": 25.0, "swe-2-max": 25.0}}},
    )
    _advertise(store, _HOST, {"repo_checkout": 1, "git": 1, "devin_bin": 1})
    task = _one_task_plan(store).ids["task"]
    _conflicted(store, task)
    store.stamp_ref_meta(task, {"build_ref": _build_ref(task, host=_HOST)})
    job = _slice_job(
        store,
        task,
        "run_slice",
        "failed",
        params={"todo_id": task, "conflict": {"try": 2}},
        meta={"usage": {"on_demand_usd": 30.0}},
    )
    store.stamp_ref_meta(task, {"conflict_tries": {"n": 2, "job": job}})

    res = pd.run_pass(store)
    assert res.asks == 0
    assert f"host {_HOST} lacks claude_bin" in res.dispatch_holds[task]
    _age_hold(store, task)
    res = pd.run_pass(store)

    assert res.asks == 0
    _jid, meta = _conflict_jobs(store, task)[-1]
    assert meta["params"]["conflict"]["try"] == 3
    assert (meta["params"]["lane"], meta["params"]["model"]) == (
        "claude-sub",
        "claude-opus-5-5",
    )
    assert "target_node" not in meta["params"]


def test_a_reused_mint_writes_no_job_event(store: Store) -> None:
    """``_mint_job``'s ``job_events`` land only with a new job."""
    task = _one_task_plan(store).ids["task"]
    node = pd._load(store).nodes[task]
    events = (("hub-pin-dropped", {"host": _HOST}),)
    first = pd._mint_job(
        store, node, "run_slice", "ssh_node", {"todo_id": task}, job_events=events
    )
    again = pd._mint_job(
        store, node, "run_slice", "ssh_node", {"todo_id": task}, job_events=events
    )
    assert again == first
    assert _events(store, first, "hub-pin-dropped") == [{"host": _HOST}]
