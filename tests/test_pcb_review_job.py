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


def test_not_checkable_writes_a_note_not_a_verdict_and_is_skipped(
    store: Store, board: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    model = _install(
        monkeypatch,
        {"verdict": "not_checkable", "reason": "no datasheet facts for C1"},
    )
    _run(store, board, item="current-budget-per-rail")
    assert model.calls == 1
    q = review.queue(store, board, item="current-budget-per-rail")
    assert [st.status for st in q.todo] == ["not checked"]
    assert q.checklist is not None
    notes = store.checklist_notes_list(
        target_ref_id=board,
        checklist_id=q.checklist["id"],
        item_name="current-budget-per-rail",
    )
    assert [n["kind"] for n in notes] == ["question"]
    assert notes[0]["body"] == "not checkable (no datasheet facts for C1)"
    assert notes[0]["origin"] == review.REVIEWER

    summary, _ = _run(store, board, item="current-budget-per-rail")
    assert model.calls == 1
    assert "1 not checkable" in summary[0]

    # Any input revision moving re-opens it.
    monkeypatch.setattr(
        precis.pcb, "PASSIVE_MARGIN_REVISION", precis.pcb.PASSIVE_MARGIN_REVISION + 1
    )
    _run(store, board, item="current-budget-per-rail")
    assert model.calls == 2


def test_stale_item_answered_not_checkable_is_skipped_next_run(
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
    assert review.queue(store, board, item=item).todo[0].status.startswith("stale")
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
    # not-checkable note, never a ledger pass.
    item = "current-budget-per-rail"
    _install(monkeypatch, {**_PASS, "anchors": anchors})
    summary, _ = _run(store, board, item=item)
    assert "pass refused" in summary[0]
    q = review.queue(store, board, item=item)
    assert [st.status for st in q.todo] == ["not checked"]
    assert q.checklist is not None
    notes = store.checklist_notes_list(
        target_ref_id=board, checklist_id=q.checklist["id"], item_name=item
    )
    assert [n["body"] for n in notes] == [
        "not checkable (pass refused: no datasheet facts for U1 (no part number))"
    ]


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


def test_part_input_resolves_unknown_part_to_none(store: Store, board: int) -> None:
    assert PcbBridge().input_revision(store, board, "part:C999999999") is None


# ── op='review' ───────────────────────────────────────────────────────


def test_op_review_runs_check_and_enqueues_once(store: Store, board: int) -> None:
    pcb = PcbHandler(hub=Hub(store=store))
    resp = pcb.put(id="rv-board", args={"op": "review", "limit": 5})
    assert "enqueued" in resp.body
    assert "check run" in resp.body
    run_id, _ = store.pcb_drc_findings_latest(board)
    assert run_id is not None
    # Same queue state → the same idempotency key (no second job).
    again = pcb.put(id="rv-board", args={"op": "review", "limit": 5})
    assert "created job id=" in resp.body
    assert "existing job id=" in again.body


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
