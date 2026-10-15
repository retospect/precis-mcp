"""gr488206 — the quest hub's board panel for quests served by pcb refs,
and the ``/pcb/{slug}/op`` + ``/pcb/{slug}/view/{name}`` endpoints its
buttons and links use.

Runs against the fake store/runtime (``tests/precis_web/conftest.py``):
the pcb/checklist store reads are monkeypatched per board, and every
queued op lands in ``runtime.calls`` instead of the prod queue.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from precis.handlers.pcb import PcbHandler
from precis.store import Tag
from precis_web import quest_board
from precis_web.routes import pcb as pcb_routes

_BOARDS = {
    482130: SimpleNamespace(
        id=482130, kind="pcb", slug="taz15", title="Taz V15", retired_at=None
    ),
    482131: SimpleNamespace(
        id=482131,
        kind="pcb",
        slug="taz15-blank",
        title="TAZ blank (routing target)",
        retired_at=None,
    ),
}
_TODOS = [
    SimpleNamespace(id=486828, kind="todo", title="Solar MPPT\nbody", retired_at=None),
    SimpleNamespace(id=485252, kind="todo", title="Leaf wetness node", retired_at=None),
]
_TODO_STATUS = {486828: "done", 485252: "open"}

_DESIGNS: dict[int, dict[str, Any]] = {
    482130: {
        "board": {
            "meta": {
                "phase": "routed",
                "jlc_order": {
                    "state": "cart",
                    "qty": 5,
                    "batch_num": "B123",
                    "added_at": "2026-10-14T10:00Z",
                },
            }
        },
        "route_status": {"realized": 140, "dangling": 11},
        "instances": [
            {"refdes": "U1", "part_lcsc": "C111", "part": {"mpn": "BQ24650"}},
            {"refdes": "R1", "part_lcsc": "C222"},
            {"refdes": "C1"},
        ],
    },
    482131: {
        "board": {"meta": {}},
        "route_status": {"realized": 29, "failed": 26, "unrouted": 3},
        "instances": [{"refdes": "U1"}],
    },
}
_NETS = {
    482130: [],
    482131: [
        {"name": "SCL", "status": "failed", "note": "failed: congestion, crossings"},
        {"name": "SDA", "status": "failed", "note": "failed: congestion"},
        {"name": "GND", "status": "unrouted", "note": None},
    ],
}
_FINDINGS = {
    482130: (
        "run1",
        [
            {"rule": "clearance", "severity": "error"},
            {"rule": "clearance", "severity": "error"},
            {"rule": "silk_missing", "severity": "warn"},
            {"rule": "no_decoupling", "severity": "error"},
            {"rule": "over_voltage", "severity": "warn", "waived_by": "reto"},
        ],
    ),
    482131: (None, []),
}
_BOM = {
    482130: [
        {
            "part_lcsc": "C111",
            "qty": 1,
            "mfr_part": "BQ24650",
            "stock": 100,
            "price": [{"qty": 1, "price": 1.5}, {"qty": 10, "price": 1.2}],
        },
        {
            "part_lcsc": "C222",
            "qty": 4,
            "mfr_part": "RC0402",
            "stock": 2,
            "price": [{"qty": 1, "price": 0.01}],
        },
        {"part_lcsc": None, "qty": 1},
    ],
    482131: [],
}
_ITEMS = [
    {"name": "drc-clean", "rev": 1, "severity": "blocking", "decidability": "judgment"},
    {
        "name": "decoupling-adequate",
        "rev": 1,
        "severity": "blocking",
        "decidability": "judgment",
    },
    {
        "name": "pin-levels",
        "rev": 2,
        "severity": "advisory",
        "decidability": "judgment",
    },
    {"name": "addr-ok", "rev": 1, "severity": "advisory", "decidability": "judgment"},
]
_VERDICTS = {
    482130: {
        "drc-clean": {"verdict": "pass", "item_rev": 1, "origin": "machine"},
        "decoupling-adequate": {"verdict": "fail", "item_rev": 1, "origin": "human"},
        "pin-levels": {"verdict": "pass", "item_rev": 1, "origin": "machine"},
    },
    482131: {},
}


def _job(pcb_id: int, job_type: str) -> dict[str, Any] | None:
    if pcb_id == 482131 and job_type == "pcb_route":
        return {
            "id": 484766,
            "status": "failed",
            "failure_class": "input",
            "error": "pcb_route: refusing to route",
            "created": "2026-10-14 09:50",
        }
    return None


@pytest.fixture
def board_quest(runtime, monkeypatch):
    """Quest 97 served by ``boards`` (set by the test) plus two todos."""
    store = runtime.store
    served: list[Any] = []

    monkeypatch.setattr("precis.quest.gaps._live_servers", lambda s, qid: list(served))
    monkeypatch.setattr(store, "pcb_load", lambda rid: _DESIGNS[rid], raising=False)
    monkeypatch.setattr(
        store, "pcb_route_status", lambda rid: _NETS[rid], raising=False
    )
    monkeypatch.setattr(
        store, "pcb_drc_findings_latest", lambda rid: _FINDINGS[rid], raising=False
    )
    monkeypatch.setattr(
        store, "pcb_bom_availability", lambda rid: _BOM[rid], raising=False
    )
    monkeypatch.setattr(
        store, "checklist_get", lambda name: {"id": 7, "name": name}, raising=False
    )
    monkeypatch.setattr(
        store,
        "checklist_items_current",
        lambda cid, target_ref_id=None: list(_ITEMS),
        raising=False,
    )
    monkeypatch.setattr(
        store,
        "checklist_verdicts_latest_for_target",
        lambda *, target_ref_id, checklist_id: _VERDICTS[target_ref_id],
        raising=False,
    )
    original_tags = store.tags_for

    def tags_for(ref_id: int, **kw: Any) -> list[Any]:
        if ref_id in _TODO_STATUS:
            return [Tag.parse_strict(f"STATUS:{_TODO_STATUS[ref_id]}", kind="todo")]
        return original_tags(ref_id, **kw)

    monkeypatch.setattr(store, "tags_for", tags_for)
    monkeypatch.setattr(quest_board, "last_job", lambda s, rid, jt: _job(rid, jt))
    return served


def test_one_board_replaces_frontier(client, runtime, board_quest) -> None:
    board_quest[:] = [_BOARDS[482130], *_TODOS]
    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="board"' in html
    assert 'id="frontier"' not in html
    assert "Pareto" not in html
    # No frontier read at all when it is hidden.
    views = [a.get("view") for v, a in runtime.calls if a.get("kind") == "quest"]
    assert "frontier" not in views
    assert html.count('class="board-col') == 1
    # Route: dangling nets are not routable.
    assert "140/140 nets routed" in html
    # DRC vs ERC split by rule name; the waived finding is not counted.
    assert "DRC <span" in html and "2 errors</span>, 1 warn" in html
    assert "ERC <span" in html and "1 error</span>, 0 warn" in html
    # Review ledger counts: pin-levels was revised (rev 2) after its
    # verdict (rev 1), so it reads stale, not pass.
    assert "1 pass ·" in html and "1 fail</span>" in html
    assert "1 not checked" in html and "1 stale" in html
    assert "1 machine / 1 human" in html
    # BOM: LCSC on 2 of 3 parts; MPN from part meta (U1) or catalog (R1).
    assert "2/3 parts with LCSC" in html and "2/3 with MPN" in html
    assert "1 short or discontinued</span> of 2 with stock" in html
    assert "$1.24/board" in html
    # Fab readiness: one of two blocking items clear; the order record.
    assert "1/2 blocking items clear" in html
    assert "failing: decoupling-adequate" in html
    assert "cart · qty 5" in html and "B123" in html
    # Links to the pcb views and the board page.
    for v in quest_board.LINKED_VIEWS:
        assert f'href="/pcb/taz15/view/{v}"' in html
    assert 'href="/pcb/taz15"' in html
    # Feature todos with their status.
    assert (
        "td486828" in html
        and "Solar MPPT" in html
        and "body" not in html.split("Features")[1].split("</section>")[0]
    )
    assert "1 done" in html and "1 open" in html
    # Every action form confirms before posting.
    assert html.count('action="/pcb/taz15/op"') == 3
    assert html.count("return confirm(") == 3


def test_two_boards_side_by_side(client, board_quest) -> None:
    board_quest[:] = [_BOARDS[482130], _BOARDS[482131], *_TODOS]
    html = client.get("/refs/quest/97").text
    assert html.count('class="board-col') == 2
    assert "repeat(2, minmax(0, 1fr))" in html
    assert html.index('data-slug="taz15"') < html.index('data-slug="taz15-blank"')
    # The blank board: failures, their classes and the failed route job.
    assert "29/58 nets routed" in html
    assert "26 failed" in html and "3 unrouted" in html
    assert "congestion ×2, crossings ×1" in html
    assert "jo484766" in html and "(input)" in html
    assert "no check run yet" in html
    assert "never run" in html  # no place job on either board


def test_section_failure_degrades_not_500(
    client, runtime, board_quest, monkeypatch
) -> None:
    board_quest[:] = [_BOARDS[482130]]

    def boom(rid: int) -> Any:
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(runtime.store, "pcb_drc_findings_latest", boom)
    resp = client.get("/refs/quest/97")
    assert resp.status_code == 200
    assert "could not read: drc" in resp.text
    assert "2/3 parts with LCSC" in resp.text


def test_board_quest_with_objectives_keeps_frontier(
    client, runtime, board_quest, monkeypatch
) -> None:
    board_quest[:] = [_BOARDS[482130]]
    monkeypatch.setattr(
        runtime.store.quests[0],
        "meta",
        {"rubric_objectives": ["cost:min", "area:min"]},
        raising=False,
    )
    html = client.get("/refs/quest/97").text
    assert 'id="board"' in html
    assert 'id="frontier"' in html


def test_no_pcb_quest_layout_unchanged(client, runtime, board_quest) -> None:
    board_quest[:] = list(_TODOS)
    html = client.get("/refs/quest/97").text
    assert 'id="board"' not in html
    assert 'id="frontier"' in html
    assert 'href="#frontier"' in html
    views = [a.get("view") for v, a in runtime.calls if a.get("kind") == "quest"]
    assert "frontier" in views


# ── actions ──────────────────────────────────────────────────────────


def test_route_action_queues_put_with_opt_ins(client, runtime) -> None:
    resp = client.post(
        "/pcb/taz15/op",
        data={
            "op": "route",
            "corridor": "1.5",
            "escape": "plaza",
            "back": "/refs/quest/97#board",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/refs/quest/97#board"
    assert runtime.calls[-1] == (
        "put",
        {
            "kind": "pcb",
            "id": "taz15",
            "__extras__": {"op": "route", "corridor": 1.5, "escape": "plaza"},
        },
    )


def test_place_action_with_groups(client, runtime) -> None:
    resp = client.post(
        "/pcb/taz15/op", data={"op": "place", "groups": "1"}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/pcb/taz15"
    assert runtime.calls[-1][1]["__extras__"] == {"op": "place", "groups": True}


def test_review_action_queues_review_job(client, runtime) -> None:
    resp = client.post("/pcb/taz15/op", data={"op": "review"}, follow_redirects=False)
    assert resp.status_code == 303
    assert runtime.calls[-1] == (
        "put",
        {"kind": "pcb", "id": "taz15", "__extras__": {"op": "review"}},
    )


def test_action_refusals_queue_nothing(client, runtime) -> None:
    for data in (
        {"op": "rip"},
        {"op": "route", "corridor": "wide"},
    ):
        resp = client.post("/pcb/taz15/op", data=data)
        assert resp.status_code == 400
    assert runtime.calls == []
    runtime.error_verbs.add("put")
    resp = client.post("/pcb/taz15/op", data={"op": "route"})
    assert resp.status_code == 400
    assert "rejected by handler" in resp.text


def test_back_is_never_an_open_redirect(client, runtime) -> None:
    for back in (
        "https://evil.example/",
        "//evil.example/refs/quest/1",
        "/refs/quest//x",
    ):
        resp = client.post(
            "/pcb/taz15/op", data={"op": "review", "back": back}, follow_redirects=False
        )
        assert resp.headers["location"] == "/pcb/taz15"


def test_action_extras_pass_the_pcb_args_gate() -> None:
    """The panel's put args must survive dispatch's ``ARGS_KEYS`` check —
    the same gate an agent's ``put(args=…)`` meets."""
    for op, kw in (
        ("route", {"corridor": "2", "escape": "plaza", "groups": None}),
        ("place", {"corridor": None, "escape": None, "groups": "1"}),
    ):
        verb, args = pcb_routes._op_call("s", op, **kw)
        assert verb == "put"
        assert set(args["__extras__"]) <= PcbHandler.ARGS_KEYS["put"]


# ── view links ───────────────────────────────────────────────────────


def test_view_route_renders_dispatched_view(client, runtime) -> None:
    resp = client.get("/pcb/taz15/view/congestion")
    assert resp.status_code == 200
    assert runtime.calls[-1] == (
        "get",
        {"kind": "pcb", "id": "taz15", "view": "congestion"},
    )
    resp = client.get("/pcb/taz15/view/tapeout")
    assert resp.status_code == 200
    assert runtime.calls[-1] == (
        "get",
        {"kind": "checklist", "id": "pcb-tapeout", "target": "pcb:taz15"},
    )
    assert client.get("/pcb/taz15/view/gerber").status_code == 404
