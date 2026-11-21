"""The cause-based breaker (``precis.plans.lane_breaker``) wired into the
gripe_dispatch intake: K builds on one lane/host dying of one engine cause
stop new builds there; every other lane keeps minting; closing the ask
resets it. Replaces the parked-chain cap as the brake (Reto 2026-11-16)."""

from __future__ import annotations

from typing import Any

import pytest

from precis import settings as psettings
from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.plans import lane_breaker as lb
from precis.plans import lanes
from precis.plans.lanes import Lane
from precis.store import Store
from precis.store.types import Tag
from precis.workers import gripe_dispatch as gd
from precis.workers._mint import mint_background_job
from tests._lane_rows import use_lanes
from tests.workers.test_gripe_dispatch import (  # noqa: F401  (autouse fixtures)
    _fresh_parked_cache,
    _fresh_trust_cache,
    _gripe,
    _light,
    _open_tags,
    _roots_for,
)

HTTP_401 = (
    "run_slice: precis MCP preflight: HTTP 401 from http://mcp/x — the server "
    "rejected the session's bearer"
)
RED_TEST = "run_slice: session for td1 failed (exit 1); tests/test_x.py::test_y FAILED"


@pytest.fixture(autouse=True)
def _fresh_breaker_cache() -> Any:
    psettings.invalidate(lb.BREAKER_PARKS_KEY)
    yield
    psettings.invalidate(lb.BREAKER_PARKS_KEY)


@pytest.fixture
def three_lanes(monkeypatch: pytest.MonkeyPatch) -> tuple[Lane, ...]:
    table = (
        Lane("devin", "devin_tree", "d", 2, frozenset({"code"})),
        Lane("codex", "codex_exec", "c", 1, frozenset({"code"})),
        Lane("kimi", "kimi_tree", "k", 1, frozenset({"code"})),
    )
    use_lanes(monkeypatch, *table)
    return table


def _todo(store: Store, title: str = "build") -> int:
    return int(store.insert_ref(kind="todo", slug=None, title=title).id)


def _job(
    store: Store,
    todo: int,
    *,
    lane: str,
    host: str = "host-1",
    status: str = "failed",
    text: str = HTTP_401,
    meta: dict[str, Any] | None = None,
) -> int:
    from precis.store.types import ChunkInsert

    with store.tx() as conn:
        ref = mint_background_job(
            store,
            conn=conn,
            job_type="run_slice",
            executor="ssh_node",
            title="run_slice",
            params={"todo_id": todo, "target_node": host},
            parent_id=todo,
            status=status,
        )
    store.stamp_ref_meta(ref.id, {"lane": lane, **(meta or {})})
    store.chunks.insert_chunks(
        ref.id, [ChunkInsert(ord=1, text=text, meta={"chunk_kind": "job_event"})]
    )
    return int(ref.id)


def _fail_streak(
    store: Store, lane: str, n: int, *, host: str = "host-1", text: str = HTTP_401
) -> list[int]:
    """``n`` failed builds on ``lane`` over two build nodes."""
    nodes = [_todo(store, f"{lane}-a"), _todo(store, f"{lane}-b")]
    return [
        _job(store, nodes[i % 2], lane=lane, host=host, text=text) for i in range(n)
    ]


def _open_asks(store: Store) -> list[int]:
    return [a for a, _b in lb._open_breaker_asks(store)]


def _close(store: Store, ask: int) -> None:
    store.add_tag(
        ask, Tag.closed("STATUS", "done"), set_by="system", replace_prefix=True
    )


def test_k_defaults_to_five_and_is_a_setting(store: Store) -> None:
    assert lb.breaker_parks(store) == 5
    psettings.set_setting(lb.BREAKER_PARKS_KEY, "3", store=store)
    assert lb.breaker_parks(store) == 3


def test_trips_after_k_same_cause_failures_on_one_lane_and_only_that_lane(
    store: Store, three_lanes: Any
) -> None:
    _light(store)
    gripes = [_gripe(store, f"g{i}", prio=i + 1) for i in range(6)]
    assert gd.lane_capacity(store) == 4  # devin 2 + codex 1 + kimi 1
    jobs = _fail_streak(store, "codex", 4)
    assert lb.detect(store) == []  # K-1: no trip
    jobs.append(_job(store, _todo(store), lane="codex"))
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    [trip] = res.extra["breaker"]
    assert (trip["scope"], trip["name"], trip["cause"]) == ("lane", "codex", "http-401")
    assert sorted(trip["jobs"]) == sorted(jobs)
    # ONE ask naming lane, cause and job ids
    [ask] = _open_asks(store)
    assert trip["ask"] == ask
    ref = store.get_ref(kind="todo", id=ask)
    assert ref is not None and "lane codex" in ref.title and "http-401" in ref.title
    assert {"ask-user"} <= _open_tags(store, ask)
    body = next(
        c.text
        for c in store.chunks.list_chunks_for_ref(ask)
        if c.chunk_kind == "todo_body"
    )
    assert all(f"jo{j}" in body for j in jobs) and "http-401" in body
    # The breaker removes codex from lane supply. Intake still fills the
    # queue against the four free executor slots advertised by the fixture.
    assert gd.lane_capacity(store) == 3  # devin 2 + kimi 1
    assert res.extra["free_executor_slots"] == 4
    assert res.ok == 4 and sum(1 for g in gripes if _roots_for(store, g)) == 4
    # and out of every lane pick, while the others stay pickable
    assert "codex" in lanes.lane_backoffs(store)
    assert set(lanes.lane_backoffs(store)) == {"codex"}
    lane = lanes.pick_lane(store, "code", lanes=three_lanes)
    assert lane is not None and lane.name != "codex"
    # a second pass raises no second ask
    again = gd.run_gripe_dispatch_pass(store, host="h1")
    assert again.extra["breaker"] == [] and _open_asks(store) == [ask]


def test_closing_the_ask_resets_the_breaker(store: Store, three_lanes: Any) -> None:
    _light(store)
    _fail_streak(store, "codex", 5)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    [ask] = [t["ask"] for t in res.extra["breaker"]]
    assert gd.lane_capacity(store) == 3 and lb.tripped_lanes(store) == {"codex"}
    _close(store, ask)
    # reset: capacity is back and the same old jobs do not re-trip it
    assert lb.tripped_lanes(store) == set() and lanes.lane_backoffs(store) == {}
    assert gd.lane_capacity(store) == 4
    assert gd.run_gripe_dispatch_pass(store, host="h1").extra["breaker"] == []
    # K fresh failures after the reset trip it again
    _fail_streak(store, "codex", 5)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert [t["name"] for t in res.extra["breaker"]] == ["codex"]
    assert gd.lane_capacity(store) == 3


@pytest.mark.parametrize(
    "case",
    ["mixed-causes", "success-in-window", "one-node", "task-cause", "other-lane"],
)
def test_no_trip_without_one_shared_engine_cause(
    store: Store, three_lanes: Any, case: str
) -> None:
    a, b = _todo(store), _todo(store)
    if case == "mixed-causes":
        for i in range(4):
            _job(store, a if i % 2 else b, lane="codex")
        _job(store, a, lane="codex", text="run_slice: HTTP Error 402: payment required")
    elif case == "success-in-window":
        for i in range(4):
            _job(store, a if i % 2 else b, lane="codex")
        _job(store, a, lane="codex", status="succeeded", text="ok")
    elif case == "one-node":
        for _ in range(5):
            _job(store, a, lane="codex")  # one node burning its own ladder
    elif case == "task-cause":
        for i in range(5):
            _job(store, a if i % 2 else b, lane="codex", text=RED_TEST)
    else:  # five failures, but spread over three lanes: no lane has K
        for i, lane in enumerate(("codex", "kimi", "devin", "codex", "kimi")):
            _job(store, a if i % 2 else b, lane=lane, host=f"h{i}")
    assert lb.detect(store) == []


def test_causes_use_the_existing_classifiers(store: Store) -> None:
    c = lb.job_cause
    assert c({"host_refusal": {"lane": "x"}}, "") == "host-refusal"
    assert c({}, "Please run /login — not logged in") == "auth:not-logged-in"
    assert c({}, "precis MCP preflight: HTTP 403 from u") == "http-403"
    assert c({}, "rate limit exceeded") == "rate"
    assert c({}, "You have hit your usage limit; quota exceeded") == "quota"
    assert c({"failure_class": "infra"}, "orphan swept") == "infra"
    assert c({"exit_code": 1}, "tests/test_a.py::test_b FAILED") is None


def test_a_host_streak_across_lanes_holds_those_lanes_out(
    store: Store, three_lanes: Any
) -> None:
    """Neither lane alone has K failures, but one host does: the host trips
    and holds the lanes that ran there; a lane on another host keeps
    dispatching."""
    _light(store)
    nodes = [_todo(store), _todo(store)]
    for i in range(3):
        _job(store, nodes[i % 2], lane="codex", host="sick-host")
    for i in range(2):
        _job(store, nodes[i % 2], lane="devin", host="sick-host")
    _job(store, nodes[0], lane="kimi", host="fine-host", text=RED_TEST)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    [trip] = res.extra["breaker"]
    assert (trip["scope"], trip["name"]) == ("host", "sick-host")
    assert trip["lanes"] == ["codex", "devin"]
    assert lb.tripped_lanes(store) == {"codex", "devin"}
    assert gd.lane_capacity(store) == 1  # kimi only
    ref = store.get_ref(kind="todo", id=trip["ask"])
    assert ref is not None and "host sick-host" in ref.title
    # closing the host's ask resets every lane it held: no re-trip on the
    # same old jobs, under the host or under either lane
    _close(store, trip["ask"])
    assert lb.tripped_lanes(store) == set() and lb.detect(store) == []
    assert gd.lane_capacity(store) == 4


def test_a_lane_trip_is_not_reported_again_as_its_host(
    store: Store, three_lanes: Any
) -> None:
    _light(store)
    _fail_streak(store, "codex", 5, host="only-host")
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    assert [(t["scope"], t["name"]) for t in res.extra["breaker"]] == [
        ("lane", "codex")
    ]


def test_breaker_ask_card_names_holds_and_release(
    store: Store, hub: Hub, three_lanes: Any
) -> None:
    """gr735227: the ask card says what it holds and what closing it
    releases — no SQL read of ``meta.breaker``."""
    _light(store)
    _fail_streak(store, "codex", 5)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    [trip] = res.extra["breaker"]
    body = TodoHandler(hub=hub).get(id=trip["ask"]).body
    assert "breaker: holds lane codex · since" in body
    last = max(trip["jobs"])
    assert (
        "closing it releases: dispatch resumes on lane codex; "
        f"only builds newer than jo{last} count toward a new trip"
    ) in body


def test_breaker_ask_search_row_carries_holds_cell(
    store: Store, hub: Hub, three_lanes: Any
) -> None:
    """gr735227: the ``search(tags=['ask-user'])`` row names the held
    lane(s)/host and since when; a page without a breaker ask drops the
    column."""
    _light(store)
    _fail_streak(store, "codex", 5)
    gd.run_gripe_dispatch_pass(store, host="h1")
    out = TodoHandler(hub=hub).search(tags=["ask-user"]).body
    assert "lane codex · since" in out

    # and the host scope names the lanes under the host
    h = TodoHandler(hub=hub)
    h.put(
        text="host sick-host: builds died",
        tags=["ask-user"],
        meta={
            "ask": {"kind": "ruling", "blocks": "nothing"},
            "breaker": {
                "scope": "host",
                "name": "sick-host",
                "cause": "http-401",
                "jobs": [1, 2, 3],
                "lanes": ["codex", "devin"],
                "last_job": 3,
                "k": 5,
            },
        },
    )
    out = h.search(tags=["ask-user"]).body
    assert "host sick-host (lanes codex, devin) · since" in out


def test_plain_ask_page_drops_the_holds_column(store: Store, hub: Hub) -> None:
    h = TodoHandler(hub=hub)
    h.put(
        text="plain ruling?",
        tags=["ask-user"],
        meta={"ask": {"kind": "ruling", "blocks": "nothing"}},
    )
    assert "holds" not in h.search(tags=["ask-user"]).body


@pytest.mark.parametrize("status", ["done", "won't-do", "auto-timeout"])
def test_terminal_breaker_does_not_display_active_hold(
    store: Store, hub: Hub, three_lanes: Any, status: str
) -> None:
    _light(store)
    _fail_streak(store, "codex", 5)
    res = gd.run_gripe_dispatch_pass(store, host="h1")
    [trip] = res.extra["breaker"]
    store.add_tag(
        trip["ask"],
        Tag.closed("STATUS", status),
        set_by="system",
        replace_prefix=True,
    )
    h = TodoHandler(hub=hub)
    body = h.get(id=trip["ask"]).body
    assert "breaker: holds" not in body
    assert "closing it releases" not in body
    out = h.search(tags=["ask-user", f"STATUS:{status}"]).body
    assert "lane codex · since" not in out
    assert "holds" not in out
