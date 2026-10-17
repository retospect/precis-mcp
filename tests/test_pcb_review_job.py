"""``pcb_review`` job — the re-check pass over pcb-tapeout's judgment items
(docs/backlog/pcb-datasheet-review.md slice 7).

The model is faked at ``precis.utils.llm.router.route`` (the seam
:func:`precis.pcb.review.ask` reaches); checklist sync, the status
derivation, the verdict ledger and the note thread are real.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

import precis.pcb
from precis.checklist.pcb import PcbBridge
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.jobs.checklist_sync import bundled_checklists_dir, sync_all
from precis.pcb import review
from precis.store import Store
from precis.utils.llm.router import LlmResult, Tier
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.registry import SERVICES_BY_NAME, ServiceKind


def _board(store: Store, slug: str = "rv-board") -> int:
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[
            {
                "refdes": "U1",
                "label": "MCU",
                "pins": [{"name": "1"}, {"name": "2"}],
            },
            # A generic resistor: value + package given, so a pass on it
            # needs no datasheet facts (generic_passive).
            {
                "refdes": "R1",
                "label": "10k",
                "footprint": "0402",
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        nets=[{"name": "VCC"}, {"name": "GND"}],
        connections=[
            {"net": "VCC", "refdes": "U1", "pin": "1"},
            {"net": "VCC", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "U1", "pin": "2"},
            {"net": "GND", "refdes": "R1", "pin": "2"},
        ],
    )
    return int(ref.id)


@pytest.fixture
def board(store: Store) -> int:
    assert sync_all(store, src_dir=bundled_checklists_dir())["status"] == "ok"
    return _board(store)


class _Model:
    """A fake router: answers every item with ``answer`` (a dict, or a
    callable of the prompt) and counts calls."""

    def __init__(self, answer: Any) -> None:
        self.answer = answer
        self.calls = 0

    def __call__(self, req: Any) -> LlmResult:
        self.calls += 1
        a = self.answer(req.prompt) if callable(self.answer) else self.answer
        return LlmResult(
            text=a if isinstance(a, str) else json.dumps(a),
            cost_usd=0.0,
            turns_used=1,
            model="fake",
            tier=Tier.MEDIUM,
            error=None,
        )


def _install(monkeypatch: pytest.MonkeyPatch, answer: Any) -> _Model:
    model = _Model(answer)
    monkeypatch.setattr("precis.utils.llm.router.route", model)
    return model


def _run(store: Store, ref_id: int, **params: Any) -> tuple[list[str], list[str]]:
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    spec = get_job_type("pcb_review")
    assert spec is not None and spec.dispatch is not None
    ctx = DispatchContext(
        store=store,
        ref_id=0,
        title="pcb_review test",
        meta={"params": {"pcb_ref_id": ref_id, **params}},
        set_status=lambda s: None,
        append_chunk=lambda k, t: chunks.append((k, t)),
        set_meta=lambda **_k: None,
        record_failure=lambda msg, **_k: failures.append(msg),
        is_cancel_requested=lambda: False,
    )
    spec.dispatch(ctx, spec)
    return [t for k, t in chunks if k == "job_summary"], failures


def _judgment_count(store: Store, ref_id: int) -> int:
    q = review.queue(store, ref_id)
    return sum(1 for st in q.statuses if st.item["decidability"] == "judgment")


_PASS = {
    "verdict": "pass",
    "reason": "decoupled",
    "quote": "VCC: U1.1 R1.1",
    "anchors": ["R1", "NOPE"],
    "inputs": ["margin:fixed-derating", "bogus:input"],
}


# ── registration ──────────────────────────────────────────────────────


def test_registered_as_inproc_job_type() -> None:
    spec = get_job_type("pcb_review")
    assert spec is not None
    assert spec.compatible_executors == frozenset({"job_inproc"})
    assert "pcb_review" in known_job_types()
    svc = SERVICES_BY_NAME["pcb_review"]
    assert svc.kind is ServiceKind.JOB
    assert svc.uses_model
    assert svc.doc_skill == "precis-pcb-review-help"


# ── parse ─────────────────────────────────────────────────────────────


def test_parse_accepts_fenced_json_and_normalises() -> None:
    a = review.parse('```json\n{"verdict": "Not Checkable", "reason": "x"}\n```')
    assert a.verdict == "not_checkable"
    assert a.anchors == ()


@pytest.mark.parametrize(
    "text", ["no json here", '{"verdict": "maybe"}', "[1, 2]", "{bad json}"]
)
def test_parse_refuses_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        review.parse(text)


# ── the pass ──────────────────────────────────────────────────────────


def test_rerun_on_unchanged_design_makes_no_model_call(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install(monkeypatch, _PASS)
    summary, failures = _run(store, board, limit=60)
    assert not failures
    n = _judgment_count(store, board)
    assert model.calls == n
    assert f"reviewed {n} item(s)" in summary[0]

    q = review.queue(store, board)
    st = next(s for s in q.statuses if s.item["decidability"] == "judgment")
    assert st.status == "pass"
    v = st.verdict
    assert v is not None
    assert v["origin"] == "machine"
    assert v["checked_by"] == review.REVIEWER
    # Unknown anchors are dropped; only offered inputs are recorded.
    assert list(v["anchors"]) == ["R1"]
    assert v["inputs"] == {"margin:fixed-derating": precis.pcb.PASSIVE_MARGIN_REVISION}
    assert v["evidence"]["quote"] == "VCC: U1.1 R1.1"

    summary, failures = _run(store, board, limit=60)
    assert model.calls == n
    assert "nothing to review" in summary[0]


def test_margin_bump_reopens_exactly_the_consumers(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first two items consume the margin policy; the rest do not.
    seen: list[str] = []

    def answer(prompt: str) -> dict[str, Any]:
        seen.append(prompt)
        return {**_PASS, "inputs": ["margin:fixed-derating"] if len(seen) <= 2 else []}

    model = _install(monkeypatch, answer)
    _run(store, board, limit=60)
    first = model.calls
    monkeypatch.setattr(
        precis.pcb, "PASSIVE_MARGIN_REVISION", precis.pcb.PASSIVE_MARGIN_REVISION + 1
    )
    stale = [st for st in review.queue(store, board).todo]
    assert len(stale) == 2
    assert all(st.status.startswith("stale (input changed") for st in stale)
    _run(store, board, limit=60)
    assert model.calls == first + 2


def test_not_checkable_writes_a_verdict_and_is_skipped_until_stale(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = "current-budget-per-rail"
    model = _install(
        monkeypatch,
        {
            "verdict": "not_checkable",
            "reason": "no datasheet facts for C1",
            "anchors": ["R1"],
            "inputs": ["margin:fixed-derating"],
        },
    )
    _run(store, board, item=item)
    assert model.calls == 1
    q = review.queue(store, board, item=item)
    # A current verdict, not a queued item.
    assert q.todo == []
    st = next(s for s in q.statuses if s.name == item)
    assert st.status == "not checkable"
    assert not st.settled
    v = st.verdict
    assert v is not None
    assert v["verdict"] == "not checkable"
    assert v["origin"] == "machine"
    assert v["checked_by"] == review.REVIEWER
    assert v["evidence"]["reason"] == "no datasheet facts for C1"
    assert "job" in v["evidence"]
    assert list(v["anchors"]) == ["R1"]
    assert v["inputs"] == {"margin:fixed-derating": precis.pcb.PASSIVE_MARGIN_REVISION}
    assert q.checklist is not None
    # No question note any more.
    assert (
        store.checklist_notes_list(
            target_ref_id=board, checklist_id=q.checklist["id"], item_name=item
        )
        == []
    )

    summary, _ = _run(store, board, item=item)
    assert model.calls == 1
    assert "1 not checkable" in summary[0]

    # An anchored input revision moving makes it stale and re-opens it.
    monkeypatch.setattr(
        precis.pcb, "PASSIVE_MARGIN_REVISION", precis.pcb.PASSIVE_MARGIN_REVISION + 1
    )
    _run(store, board, item=item)
    assert model.calls == 2


def test_stale_item_answered_not_checkable_is_not_requeued_next_run(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = "current-budget-per-rail"
    model = _install(monkeypatch, _PASS)
    _run(store, board, item=item)
    monkeypatch.setattr(
        precis.pcb, "PASSIVE_MARGIN_REVISION", precis.pcb.PASSIVE_MARGIN_REVISION + 1
    )
    model.answer = {"verdict": "not_checkable", "reason": "no facts"}
    _run(store, board, item=item)
    assert model.calls == 2
    q = review.queue(store, board, item=item)
    assert q.todo == []
    assert next(s for s in q.statuses if s.name == item).status == "not checkable"
    summary, _ = _run(store, board, item=item)
    assert model.calls == 2
    assert "nothing to review" in summary[0]


def test_net_anchor_consumes_the_parts_on_that_net() -> None:
    graph = {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C1"},
            {"refdes": "R1", "part_lcsc": "C2"},
            {"refdes": "R2", "part_lcsc": "C3"},
        ],
        "nets": [
            {"name": "VCC", "members": [{"refdes": "U1", "pin": "1"}]},
            {"name": "GND", "members": [{"refdes": "R2", "pin": "2"}]},
        ],
    }
    revisions = {"part:C1": 4, "part:C2": 0, "part:C3": 1, review.MARGIN_INPUT: 1}
    answer = review.parse('{"verdict": "pass"}')
    got = review._consumed(answer, ["VCC", "R1"], revisions, graph)
    assert got == {"part:C1": 4, "part:C2": 0}
    assert set(review._consumed(answer, [], revisions, graph)) == {
        "part:C1",
        "part:C2",
        "part:C3",
    }


@pytest.mark.parametrize("anchors", [["U1"], ["VCC"], []])
def test_factless_ic_cannot_pass(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch, anchors: list[str]
) -> None:
    # U1 has no part number, so no datasheet facts: a pass naming it, a
    # net it sits on, or the whole board is refused and becomes a
    # not-checkable verdict, never a ledger pass.
    item = "current-budget-per-rail"
    _install(monkeypatch, {**_PASS, "anchors": anchors})
    summary, _ = _run(store, board, item=item)
    assert "pass refused" in summary[0]
    q = review.queue(store, board, item=item)
    assert q.todo == []
    st = next(x for x in q.statuses if x.name == item)
    assert st.status == "not checkable"
    v = st.verdict
    assert v is not None
    assert v["verdict"] == "not checkable"
    assert v["origin"] == "machine"
    assert v["evidence"]["reason"] == (
        "pass refused: no datasheet facts for U1 (no part number)"
    )


def test_generic_resistor_with_value_and_package_can_pass(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    item = "current-budget-per-rail"
    _install(monkeypatch, {**_PASS, "anchors": ["R1"]})
    _run(store, board, item=item)
    st = review.queue(store, board, item=item).statuses
    assert next(s for s in st if s.name == item).status == "pass"


@pytest.mark.parametrize(
    ("inst", "generic"),
    [
        ({"refdes": "R1", "label": "10k", "footprint": "0402"}, True),
        ({"refdes": "L2", "label": "4.7uH", "footprint": "0805"}, True),
        (
            {
                "refdes": "C3",
                "label": "C",
                "part_description": "100nF 16V X7R 0402 MLCC Capacitor",
            },
            True,
        ),
        ({"refdes": "R1", "label": "R", "footprint": "0402"}, False),  # no value
        ({"refdes": "R1", "label": "10k"}, False),  # no package
        ({"refdes": "LED1", "label": "10", "footprint": "0603"}, False),
        ({"refdes": "U1", "label": "10k", "footprint": "SOT-23"}, False),
    ],
)
def test_generic_passive(inst: dict[str, Any], generic: bool) -> None:
    assert review.generic_passive(inst) is generic


def test_factless_parts_reads_facts_revisions() -> None:
    graph = {
        "instances": [
            {"refdes": "U1", "part_lcsc": "C1"},
            {"refdes": "U2", "part_lcsc": "C2"},
            {"refdes": "R1", "label": "10k", "footprint": "0402"},
        ],
        "nets": [{"name": "VCC", "members": [{"refdes": "U1", "pin": "1"}]}],
    }
    revisions = {"part:C1": 3, "part:C2": 0}
    assert review.factless_parts(["VCC", "R1"], graph, revisions) == []
    assert review.factless_parts([], graph, revisions) == ["U2 (C2)"]


def test_failed_answers_stay_queued_and_fail_the_run(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, "I think it is fine")
    _summary, failures = _run(store, board, limit=3)
    assert failures and "every item failed" in failures[0]
    assert len(review.queue(store, board).todo) == _judgment_count(store, board)


def test_limit_bounds_one_run(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install(monkeypatch, _PASS)
    summary, _ = _run(store, board, limit=3)
    assert model.calls == 3
    assert "still queued (limit 3)" in summary[0]


def test_part_input_resolves_unknown_part_to_zero(store: Store, board: int) -> None:
    # No part ref reads as rev 0 (never extracted), so a verdict that found
    # the part fact-less goes stale once it is pulled and extracted.
    assert PcbBridge().input_revision(store, board, "part:C999999999") == 0


# ── op='review' ───────────────────────────────────────────────────────


def test_op_review_enqueues_once_and_runs_no_check(store: Store, board: int) -> None:
    pcb = PcbHandler(hub=Hub(store=store))
    resp = pcb.put(id="rv-board", args={"op": "review", "limit": 5})
    assert "enqueued" in resp.body
    # The job runs the check, not the put.
    assert store.pcb_drc_findings_latest(board)[0] is None
    # Same queue state → the same idempotency key (no second job).
    again = pcb.put(id="rv-board", args={"op": "review", "limit": 5})
    assert "created job id=" in resp.body
    assert "existing job id=" in again.body


def test_job_runs_check_surface_itself(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _PASS)
    assert store.pcb_drc_findings_latest(board)[0] is None
    summaries, failures = _run(store, board, limit=1)
    assert not failures
    assert store.pcb_drc_findings_latest(board)[0] is not None
    assert "check run" in "\n".join(summaries)


def test_op_review_settled_item_queues_nothing(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _PASS)
    _run(store, board, item="current-budget-per-rail")
    pcb = PcbHandler(hub=Hub(store=store))
    resp = pcb.put(
        id="rv-board", args={"op": "review", "item": "current-budget-per-rail"}
    )
    assert "nothing to review" in resp.body
    with pytest.raises(BadInput, match="not an item"):
        pcb.put(id="rv-board", args={"op": "review", "item": "no-such-item"})


# ── whole-board fingerprint vs. recorded check runs ───────────────────


def test_whole_board_fingerprint_ignores_recorded_check_run(
    store: Store, board: int
) -> None:
    """A recorded check run stamps ``board.meta['drc_runs_by_phase']``;
    that is bookkeeping, so the whole-board digest holds. A real design
    edit (moving a placed instance) still moves it."""
    from precis.pcb import check_run

    bridge = PcbBridge()
    before = bridge.fingerprint(store, board, []).digest

    design = store.pcb_load(board)
    check_run.run(store, board, design, record=True)

    # Non-vacuous: the run really was recorded.
    recorded = store.pcb_load(board)["board"].get("meta") or {}
    assert recorded.get("drc_runs_by_phase"), recorded
    assert bridge.fingerprint(store, board, []).digest == before

    assert store.pcb_move_instance(board, "R1", x=12.5, y=7.25)
    assert bridge.fingerprint(store, board, []).digest != before


# ── a part with no ref: verdict records part:<C#> = 0 ─────────────────


def test_not_checkable_on_refless_part_goes_stale_once_pulled_and_extracted(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.pcb import datasheet_facts as df

    lcsc = "C990011"
    ref, _created, _counts = store.pcb_apply(
        slug="rv-refless",
        title="rv-refless",
        components=[
            {
                "refdes": "U2",
                "label": "LDO",
                "part_lcsc": lcsc,
                "pins": [{"name": "1"}, {"name": "2"}],
            }
        ],
        nets=[{"name": "VCC"}, {"name": "GND"}],
        connections=[
            {"net": "VCC", "refdes": "U2", "pin": "1"},
            {"net": "GND", "refdes": "U2", "pin": "2"},
        ],
    )
    board = int(ref.id)
    assert store.part_ref_id(lcsc) is None
    item = "current-budget-per-rail"
    key = f"part:{lcsc}"
    _install(monkeypatch, {**_PASS, "anchors": ["U2"], "inputs": []})
    summary, _ = _run(store, board, item=item)
    assert "pass refused" in summary[0]

    q = review.queue(store, board, item=item)
    assert q.todo == []
    st = next(s for s in q.statuses if s.name == item)
    assert st.status == "not checkable"
    assert st.verdict is not None
    assert st.verdict["inputs"][key] == 0

    # Pulled but not yet extracted: still reads 0, still current.
    part_id = store.ensure_part_ref(lcsc, uncatalogued_ok=True)
    q = review.queue(store, board, item=item)
    assert q.todo == []
    assert next(s for s in q.statuses if s.name == item).status == "not checkable"

    # Facts extracted: facts_revision > 0 -> stale, queued again.
    assert df.write_facts(store, part_id, {"packages": {}}, allow_empty=True) > 0
    q = review.queue(store, board, item=item)
    st = next(s for s in q.statuses if s.name == item)
    assert st.status == f"stale (input changed: {key})"
    assert [t.name for t in q.todo] == [item]
