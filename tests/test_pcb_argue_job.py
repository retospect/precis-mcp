"""``pcb_argue`` job — the LLM acts on a design's open argument notes
(docs/backlog/pcb-argue-with-design.md slice 2).

The model is faked at ``precis.utils.llm.router.route`` (the seam
:func:`precis.pcb.argue.act` reaches); everything else — note ledger,
open-question derivation, evidence build, the job row — is real.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.job import JobHandler
from precis.handlers.pcb import PcbHandler
from precis.pcb import argue
from precis.utils.llm.router import LlmResult, Tier
from precis.utils.notes import open_questions
from precis.workers.executors._context import DispatchContext
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import pcb_argue as pa
from precis.workers.registry import SERVICES_BY_NAME, ServiceKind


def _design(*, outline: bool = False) -> dict[str, Any]:
    args: dict[str, Any] = {
        "components": [
            {
                "refdes": r,
                "label": "resistor",
                "footprint": "local-R",
                "pins": [{"name": "1", "pad": "1"}, {"name": "2", "pad": "2"}],
            }
            for r in ("R1", "R2")
        ],
        "nets": [{"name": "SIG"}],
        "connections": [{"net": "SIG", "refdes": "R1", "pin": "1"}],
        "footprints": [
            {
                "name": "local-R",
                "pads": [
                    {
                        "number": "1",
                        "x": -0.5,
                        "y": 0,
                        "w": 0.6,
                        "h": 0.6,
                        "shape": "rect",
                    },
                    {
                        "number": "2",
                        "x": 0.5,
                        "y": 0,
                        "w": 0.6,
                        "h": 0.6,
                        "shape": "rect",
                    },
                ],
            }
        ],
    }
    if outline:
        args["features"] = [
            {
                "ftype": "outline",
                "geom": {"path": [[0, 0], [40, 0], [40, 20], [0, 20]]},
            }
        ]
    return args


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _seed(pcb, store, slug="argue-board", *, outline: bool = False):
    pcb.put(id=slug, args=_design(outline=outline))
    store.pcb_move_instance(_ref(store, slug).id, "R1", x=10.0, y=10.0, rot=0)
    store.pcb_move_instance(_ref(store, slug).id, "R2", x=20.0, y=10.0, rot=0)
    return _ref(store, slug)


def _ref(store, slug):
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return ref


def _ctx(
    store,
    params: dict[str, Any],
    chunks: list[tuple[str, str]],
    failures: list[str],
) -> DispatchContext:
    return DispatchContext(
        store=store,
        ref_id=0,
        title="pcb_argue test",
        meta={"job_type": "pcb_argue", "params": params},
        set_status=lambda s: None,
        append_chunk=lambda k, t: chunks.append((k, t)),
        set_meta=lambda **_k: None,
        record_failure=lambda msg, **_k: failures.append(msg),
        is_cancel_requested=lambda: False,
    )


def _result(text: str | None, error: str | None = None) -> LlmResult:
    return LlmResult(
        text=text or "",
        cost_usd=0.0,
        turns_used=1,
        model="fake",
        tier=Tier.MEDIUM,
        error=error,
    )


def _fake_route(answer: str = "R1 sits 10.0mm from R2; no clearance finding."):
    def route(req):
        return _result(answer)

    return route


def _failing_route(req):
    return _result(None, error="backend down")


def _specs(notes):
    from precis.utils.notes import NoteSpec

    return [
        NoteSpec(
            name=n["name"],
            kind=n["kind"],
            body=n["body"],
            re=n["re"],
            about=list(n["about"]),
            origin=n["origin"],
            created_at=n["created_at"],
        )
        for n in notes
    ]


# ── registration ──────────────────────────────────────────────────────


def test_registered_as_inproc_bounded_job_type():
    spec = get_job_type("pcb_argue")
    assert spec is not None
    assert spec.compatible_executors == frozenset({"job_inproc"})
    assert "pcb_argue" in known_job_types()
    svc = SERVICES_BY_NAME["pcb_argue"]
    assert svc.kind is ServiceKind.JOB
    assert svc.uses_model


def test_pcb_parent_kind_accepts_the_job(pcb, store):
    ref = _seed(pcb, store)
    resp = JobHandler(hub=pcb.hub).put(
        job_type="pcb_argue",
        executor="job_inproc",
        parent_id=ref.id,
        params={"pcb_ref_id": ref.id},
    )
    assert "job" in resp.body


# ── _finding_touches ──────────────────────────────────────────────────


def _f(**kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = dict(
        rule="clearance",
        severity="error",
        where="w",
        detail="d",
        objects=[],
        margin_mm=None,
        source="placement-drc",
    )
    base.update(kw)
    return base


def test_finding_touches_net_part_pin():
    f = _f(
        objects=(
            {"ctype": "track", "net": "HV_RAIL"},
            {"ctype": "pad", "refdes": "U1", "pin": "3", "net": "SIG"},
        )
    )
    assert pa._finding_touches(f, "net:HV_RAIL")
    assert pa._finding_touches(f, "U1")
    assert pa._finding_touches(f, "U1.3")
    assert not pa._finding_touches(f, "U1.4")
    assert not pa._finding_touches(f, "net:GND")
    assert not pa._finding_touches(f, "U2")


def test_finding_touches_feature_and_object_handles():
    edge = _f(rule="board_edge_clearance", where="board edge", detail="0.1mm short")
    assert pa._finding_touches(edge, "feature:outline")
    assert pa._finding_touches(edge, "outline:0")
    via = _f(objects=({"ctype": "via", "net": "GND", "x": 12.3, "y": 4.5},))
    assert pa._finding_touches(via, "via:12.3:4.5")
    assert not pa._finding_touches(via, "via:12.3:9.9")
    silk = _f(rule="silk_edge_clearance", objects=({"ctype": "silk", "refdes": "U7"},))
    assert pa._finding_touches(silk, "silk:F.SilkS:refdes:U7")


# ── _evidence ─────────────────────────────────────────────────────────


def test_evidence_quotes_findings_distances_and_measures():
    findings = [
        _f(
            rule="clearance",
            detail="copper clearance 0.02mm < JLC min 0.09mm",
            objects=({"ctype": "pad", "refdes": "R1", "pin": "2", "net": "SIG"},),
            margin_mm=-0.07,
        ),
        _f(rule="unrelated", objects=({"ctype": "track", "net": "OTHER"},)),
    ]
    graph = {
        "instances": [
            {"refdes": "R1", "x": 0.0, "y": 0.0},
            {"refdes": "R2", "x": 10.0, "y": 0.0},
        ],
        "nets": [],
    }
    verdicts = [
        {
            "metric": "separation",
            "over": "R1, R2",
            "verdict": "VIOLATED",
            "value": 10.0,
            "goal": 30.0,
            "reason": "keep apart",
        },
    ]
    ev = pa._evidence(
        ["R1", "R2"],
        [argue.Resolved("R1", "part", {}), argue.Resolved("R2", "part", {})],
        findings,
        graph,
        verdicts,
        ["machinery ran pads-only"],
    )
    text = "\n".join(ev)
    assert "pads-only" in text
    assert "clearance 0.02mm" in text
    assert "unrelated" not in text
    assert "R1<->R2: 10.000mm" in text
    assert "VIOLATED" in text


def test_evidence_whole_design_argument_gets_error_findings():
    findings = [
        _f(severity="error", rule="clearance", detail="0.02mm"),
        _f(severity="warn", rule="octilinear", detail="non-45deg"),
    ]
    ev = pa._evidence([], [], findings, {"instances": [], "nets": []}, [], [])
    text = "\n".join(ev)
    assert "clearance" in text
    assert "octilinear" not in text


# ── dispatch ──────────────────────────────────────────────────────────


def test_dispatch_answers_each_open_question_in_place(pcb, store, monkeypatch):
    ref = _seed(pcb, store)
    store.pcb_note_insert(
        ref.id,
        name="q-shorted",
        kind="question",
        body="R1.2 and R2 look shorted — are they?",
        about=["R1.2", "R2"],
    )
    monkeypatch.setattr(
        "precis.utils.llm.router.route",
        _fake_route("R1.2's land is 9.3mm from R2's; not shorted."),
    )
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    pa._dispatch(_ctx(store, {"pcb_ref_id": ref.id}, chunks, failures), pa.SPEC)

    assert failures == []
    notes = store.pcb_notes_list(ref.id)
    answers = [n for n in notes if n["kind"] == "answer"]
    assert len(answers) == 1
    a = answers[0]
    assert a["re"] == "q-shorted"
    assert a["origin"] == "proposed"
    assert a["about"] == ["R1.2", "R2"]
    assert "9.3mm" in a["body"]
    # Settled: the question is no longer open.
    assert [q.name for q in open_questions(_specs(notes))] == []
    summary = [t for k, t in chunks if k == "job_summary"][-1]
    assert "answered 1" in summary


def test_dispatch_no_open_questions_is_a_clean_noop(pcb, store):
    ref = _seed(pcb, store)
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    pa._dispatch(_ctx(store, {"pcb_ref_id": ref.id}, chunks, failures), pa.SPEC)
    assert failures == []
    assert "no open argument questions" in chunks[-1][1]


def test_dispatch_model_failure_leaves_the_question_open(pcb, store, monkeypatch):
    ref = _seed(pcb, store)
    store.pcb_note_insert(
        ref.id, name="q-why", kind="question", body="why is R1 here?", about=["R1"]
    )
    monkeypatch.setattr("precis.utils.llm.router.route", _failing_route)
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    pa._dispatch(_ctx(store, {"pcb_ref_id": ref.id}, chunks, failures), pa.SPEC)
    # Every call failed -> the job itself fails, the question stays open
    # ("recorded, unanswered", never a fabricated answer).
    assert failures
    notes = store.pcb_notes_list(ref.id)
    assert [n["kind"] for n in notes] == ["question"]
    assert [q.name for q in open_questions(_specs(notes))] == ["q-why"]


def test_dispatch_limit_bounds_the_run(pcb, store, monkeypatch):
    ref = _seed(pcb, store)
    store.pcb_note_insert(
        ref.id, name="q-one", kind="question", body="R1?", about=["R1"]
    )
    store.pcb_note_insert(
        ref.id, name="q-two", kind="question", body="R2?", about=["R2"]
    )
    monkeypatch.setattr("precis.utils.llm.router.route", _fake_route())
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    pa._dispatch(
        _ctx(store, {"pcb_ref_id": ref.id, "limit": 1}, chunks, failures), pa.SPEC
    )
    notes = store.pcb_notes_list(ref.id)
    assert sum(1 for n in notes if n["kind"] == "answer") == 1
    assert [q.name for q in open_questions(_specs(notes))] == ["q-two"]


def test_dispatch_question_param_targets_one_note(pcb, store, monkeypatch):
    ref = _seed(pcb, store)
    store.pcb_note_insert(
        ref.id, name="q-one", kind="question", body="R1?", about=["R1"]
    )
    store.pcb_note_insert(
        ref.id, name="q-two", kind="question", body="R2?", about=["R2"]
    )
    monkeypatch.setattr("precis.utils.llm.router.route", _fake_route())
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    pa._dispatch(
        _ctx(store, {"pcb_ref_id": ref.id, "question": "q-two"}, chunks, failures),
        pa.SPEC,
    )
    notes = store.pcb_notes_list(ref.id)
    assert [n["re"] for n in notes if n["kind"] == "answer"] == ["q-two"]


def test_dispatch_handles_typed_in_body_are_resolved(pcb, store, monkeypatch):
    ref = _seed(pcb, store)
    store.pcb_note_insert(
        ref.id,
        name="q-net",
        kind="question",
        body="is net:SIG too close to R2?",
        about=[],
    )
    monkeypatch.setattr("precis.utils.llm.router.route", _fake_route())
    chunks: list[tuple[str, str]] = []
    failures: list[str] = []
    pa._dispatch(_ctx(store, {"pcb_ref_id": ref.id}, chunks, failures), pa.SPEC)
    notes = store.pcb_notes_list(ref.id)
    (answer,) = [n for n in notes if n["kind"] == "answer"]
    # Typed-but-unclicked handles resolve too (the grammar's own rule).
    assert "net:SIG" in answer["about"]
    assert "R2" in answer["about"]


# ── feature:outline on the Edge_Cuts group ────────────────────────────


def test_fab_svg_stamps_feature_outline_only_when_a_features_row(pcb, store):
    ref = _seed(pcb, store, outline=True)
    body = pcb._render_fab_svg(ref.id, ref.slug).body
    assert 'id="layer-Edge_Cuts"' in body
    assert (
        'id="layer-Edge_Cuts" class="layer" fill="#f0d000" data-handle="feature:outline"'
        in body
    )

    bare = _seed(pcb, store, slug="argue-board-bare", outline=False)
    body = pcb._render_fab_svg(bare.id, bare.slug).body
    assert 'data-handle="feature:outline"' not in body
