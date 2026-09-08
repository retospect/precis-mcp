"""se slice 4, round 1 — the interrogation ledger (`se_notes`,
view='interview') and the design-freedom vocabulary (interval measures,
origin, relation scale, the closed unit registry, view='freedom').
se-kind.md "Ship order" step 4; migration 0005_se_notes_freedom.sql.
"""

from __future__ import annotations

from pathlib import Path

import pytest

import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis_se.freedom import freedom
from precis_se.handler import SeHandler
from precis_se.measures import MeasureSpec, stackup, validate_relation
from precis_se.notes import NoteSpec, open_questions
from precis_se.ops import OpError, SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    # the shared test DB template carries only core migrations —
    # test_se_plugin.py's fixture shape, transferred.
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _tree(ops: list[dict]) -> SeTree:
    return apply_ops(SeTree(), ops)


# ── relation scale ──────────────────────────────────────────────────────


def test_relation_scale_default_not_stored() -> None:
    rel = validate_relation({"source": "hub.od", "offset": 1e-3, "tol": 1e-4})
    assert "scale" not in rel


def test_relation_scale_zero_rejected() -> None:
    with pytest.raises(Exception, match="severs the relation"):
        validate_relation({"source": "hub.od", "scale": 0})


def test_stackup_scale_chain_multiplies() -> None:
    # pinion.teeth = 20; gear.teeth = 10 × pinion.teeth (1:10);
    # shaft.turns_ratio = 0.5 × gear.teeth ± 1 → 100 ± 1, offsets scaled.
    ms = [
        MeasureSpec(block="pinion", name="teeth", value=20.0, unit="count"),
        MeasureSpec(
            block="gear",
            name="teeth",
            relation={"source": "pinion.teeth", "scale": 10.0},
            unit="count",
        ),
        MeasureSpec(
            block="shaft",
            name="idler",
            relation={"source": "gear.teeth", "scale": 0.5, "tol": 1.0},
            unit="count",
        ),
    ]
    by_key = {r.measure: r for r in stackup(ms)}
    assert by_key["gear.teeth"].derived == pytest.approx(200.0)
    assert by_key["shaft.idler"].derived == pytest.approx(100.0)
    assert by_key["shaft.idler"].tol_accum == pytest.approx(1.0)


def test_stackup_scale_applies_to_downstream_offsets_and_tols() -> None:
    # b = 2·a; c = b + 0.1 ± 0.05 walking THROUGH b (b unvalued):
    # c = 2·a + 0.1, tol from b's own relation scaled by c's multiplier (1).
    ms = [
        MeasureSpec(block="a", name="x", value=1.0),
        MeasureSpec(
            block="b", name="x", relation={"source": "a.x", "scale": 2.0, "tol": 0.01}
        ),
        MeasureSpec(
            block="c", name="x", relation={"source": "b.x", "offset": 0.1, "tol": 0.05}
        ),
    ]
    by_key = {r.measure: r for r in stackup(ms)}
    assert by_key["c.x"].derived == pytest.approx(2.1)
    # c's own tol (0.05) + b's tol (0.01) scaled by the multiplier at that
    # hop (1.0 — scale applies past the hop, worst-case linear).
    assert by_key["c.x"].tol_accum == pytest.approx(0.06)


# ── units ───────────────────────────────────────────────────────────────


def test_unit_mismatch_is_a_stackup_problem() -> None:
    ms = [
        MeasureSpec(block="hub", name="od", value=0.02, unit="m"),
        MeasureSpec(
            block="gear",
            name="teeth",
            relation={"source": "hub.od"},
            unit="count",
        ),
    ]
    (res,) = stackup(ms)
    assert res.problem_kind == "unit_mismatch"
    assert "unit agreement" in (res.problem or "")


def test_bad_unit_vocab_rejected_at_write() -> None:
    with pytest.raises(OpError, match="unit"):
        _tree(
            [
                {"op": "add_block", "name": "hub"},
                {
                    "op": "add_measure",
                    "block": "hub",
                    "name": "od",
                    "unit": "furlong",
                },
            ]
        )


# ── interval measures ───────────────────────────────────────────────────


def test_interval_written_and_value_in_band_enforced() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "wheel"},
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "min": 4e-3,
                "max": 8e-3,
            },
        ]
    )
    (m,) = tree.measures
    assert (m.min_value, m.max_value) == (4e-3, 8e-3)
    with pytest.raises(OpError, match="band"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_measure",
                    "block": "wheel",
                    "name": "bore_d",
                    "value": 9e-3,
                }
            ],
        )
    apply_ops(
        tree,
        [{"op": "set_measure", "block": "wheel", "name": "bore_d", "value": 5e-3}],
    )
    assert tree.measures[0].value == 5e-3


def test_empty_band_rejected() -> None:
    with pytest.raises(OpError, match="empty band"):
        _tree(
            [
                {"op": "add_block", "name": "w"},
                {
                    "op": "add_measure",
                    "block": "w",
                    "name": "d",
                    "min": 2.0,
                    "max": 1.0,
                },
            ]
        )


def test_interval_anchor_derives_a_band() -> None:
    ms = [
        MeasureSpec(block="hub", name="od", min_value=0.01, max_value=0.02),
        MeasureSpec(
            block="wheel",
            name="bore",
            relation={"source": "hub.od", "offset": 1e-3, "tol": 5e-4},
        ),
    ]
    (res,) = stackup(ms)
    assert res.derived is None
    assert res.derived_min == pytest.approx(0.011)
    assert res.derived_max == pytest.approx(0.021)
    assert res.problem is None


def test_relationless_value_outside_own_band_is_flagged_at_read() -> None:
    # write time gates this via _check_band; a hand-edited stored row must
    # still surface at read time (reviewer finding: without a relation the
    # measure previously got no stack-up row at all).
    m = MeasureSpec(block="hub", name="od", value=0.03, min_value=0.01, max_value=0.02)
    (res,) = stackup([m])
    assert res.problem_kind == "mismatch"
    assert "own" in (res.problem or "")
    # a CLEAN anchor-only measure still produces no row (view unchanged).
    ok = MeasureSpec(
        block="hub", name="id", value=0.015, min_value=0.01, max_value=0.02
    )
    assert stackup([ok]) == []


def test_derived_point_outside_declared_band_is_mismatch() -> None:
    ms = [
        MeasureSpec(block="hub", name="od", value=0.03),
        MeasureSpec(
            block="wheel",
            name="bore",
            min_value=0.004,
            max_value=0.008,
            relation={"source": "hub.od", "tol": 1e-4},
        ),
    ]
    (res,) = stackup(ms)
    assert res.problem_kind == "mismatch"
    assert "band" in (res.problem or "")


# ── origin ──────────────────────────────────────────────────────────────


def test_measure_origin_vocab_and_default() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "hub"},
            {"op": "add_measure", "block": "hub", "name": "od"},
            {
                "op": "add_measure",
                "block": "hub",
                "name": "id",
                "origin": "proposed",
            },
        ]
    )
    assert tree.measures[0].origin == "user"
    assert tree.measures[1].origin == "proposed"
    with pytest.raises(OpError, match="origin"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_measure",
                    "block": "hub",
                    "name": "x",
                    "origin": "robot",
                }
            ],
        )


def test_facet_origin_stamped_on_envelope_and_pose() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "hub"},
            {
                "op": "set_envelope",
                "block": "hub",
                "envelope": "cyl:r0.02h0.01",
                "origin": "proposed",
            },
            {
                "op": "set_pose",
                "block": "hub",
                "pose": [0, 0, 0.1],
                "origin": "proposed",
            },
        ]
    )
    assert tree.blocks["hub"].origins == {"envelope": "proposed", "pose": "proposed"}
    # re-authoring as user clears the stamp; omitting origin keeps it.
    apply_ops(tree, [{"op": "set_pose", "block": "hub", "pose": [0, 0, 0.2]}])
    assert tree.blocks["hub"].origins["pose"] == "proposed"
    apply_ops(
        tree,
        [{"op": "set_pose", "block": "hub", "pose": [0, 0, 0.3], "origin": "user"}],
    )
    assert "pose" not in tree.blocks["hub"].origins


# ── notes ledger ────────────────────────────────────────────────────────


def test_note_ledger_question_answer_settles() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "wheel"},
            {
                "op": "add_note",
                "name": "q-bore",
                "kind": "question",
                "text": "what bearing bore?",
                "about": ["wheel"],
            },
        ]
    )
    assert [n.name for n in open_questions(tree.notes)] == ["q-bore"]
    apply_ops(
        tree,
        [
            {
                "op": "add_note",
                "name": "a-bore",
                "kind": "answer",
                "text": "608 bearing: 8 mm",
                "re": "q-bore",
                "origin": "proposed",
            }
        ],
    )
    assert open_questions(tree.notes) == []


def test_note_re_must_name_a_question() -> None:
    tree = _tree(
        [
            {"op": "add_note", "name": "d1", "kind": "decision", "text": "FDM only"},
        ]
    )
    with pytest.raises(OpError, match="must name a question"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_note",
                    "name": "a1",
                    "kind": "answer",
                    "text": "yes",
                    "re": "d1",
                }
            ],
        )
    with pytest.raises(OpError, match="no live note"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_note",
                    "name": "a2",
                    "kind": "answer",
                    "text": "yes",
                    "re": "nope",
                }
            ],
        )


def test_note_duplicate_and_question_with_re_rejected() -> None:
    tree = _tree([{"op": "add_note", "name": "q1", "kind": "question", "text": "?"}])
    with pytest.raises(OpError, match="duplicate note"):
        apply_ops(
            tree,
            [{"op": "add_note", "name": "q1", "kind": "question", "text": "again"}],
        )
    with pytest.raises(OpError, match="takes no 're'"):
        apply_ops(
            tree,
            [
                {
                    "op": "add_note",
                    "name": "q2",
                    "kind": "question",
                    "text": "?",
                    "re": "q1",
                }
            ],
        )


def test_remove_note_leaves_orphaned_answer() -> None:
    tree = _tree(
        [
            {"op": "add_note", "name": "q1", "kind": "question", "text": "?"},
            {"op": "add_note", "name": "a1", "kind": "answer", "text": "!", "re": "q1"},
            {"op": "remove_note", "name": "q1"},
        ]
    )
    assert [n.name for n in tree.notes] == ["a1"]
    # the orphan is kept (read-time honesty), not cascaded.
    assert tree.notes[0].re == "q1"


# ── freedom report ──────────────────────────────────────────────────────


def _wheel_tree() -> SeTree:
    return _tree(
        [
            {"op": "add_block", "name": "hub", "envelope": "cyl:r0.01h0.02"},
            {"op": "add_block", "name": "wheel"},
            {"op": "add_block", "name": "loose"},
            {"op": "add_port", "block": "hub", "name": "axle"},
            {"op": "add_port", "block": "wheel", "name": "bore"},
            {
                "op": "connect",
                "a": "hub.axle",
                "b": "wheel.bore",
                "joint": {"class": "revolute", "axis": [0, 0, 1]},
            },
            {
                "op": "add_measure",
                "block": "wheel",
                "name": "bore_d",
                "min": 4e-3,
                "max": 8e-3,
                "origin": "proposed",
            },
            {"op": "add_measure", "block": "hub", "name": "od"},
        ]
    )


def test_freedom_report_sections() -> None:
    report = freedom(_wheel_tree())
    assert [m.klass for m in report.motions] == ["revolute"]
    assert report.unconnected == ["loose"]
    states = {u.measure: u.state for u in report.undecided}
    assert states == {"wheel.bore_d": "band", "hub.od": "handle"}
    banded = next(u for u in report.undecided if u.state == "band")
    assert banded.band == (4e-3, 8e-3)
    assert banded.origin == "proposed"
    # wheel + loose have no envelope; hub does.
    assert report.unenveloped == ["loose", "wheel"]
    assert report.measure_origins == {"proposed": 1, "user": 1}


def test_freedom_decided_measures_excluded() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_measure", "block": "a", "name": "x", "value": 1.0},
            {
                "op": "add_measure",
                "block": "a",
                "name": "y",
                "relation": {"source": "a.x", "offset": 0.1},
            },
        ]
    )
    report = freedom(tree)
    assert report.undecided == []


def test_freedom_underived_relation_reported() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {
                "op": "add_measure",
                "block": "a",
                "name": "y",
                "relation": {"source": "gone.x"},
            },
        ]
    )
    (u,) = freedom(tree).undecided
    assert u.state == "underived"


def test_freedom_skips_pure_templates() -> None:
    tree = _tree(
        [
            {"op": "add_block", "name": "spoke_t"},
            {"op": "instance_block", "name": "s1", "template": "spoke_t"},
        ]
    )
    report = freedom(tree)
    # the template is not a floating part; the instance is.
    assert report.unconnected == ["s1"]
    assert report.unenveloped == []


# ── drc: minimum-constraint advisory + unit-aware magnitude ─────────────


def test_hard_measure_with_no_consumer_advised() -> None:
    from precis_se.drc import drc

    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {
                "op": "add_measure",
                "block": "a",
                "name": "x",
                "value": 0.01,
                "strength": "hard",
            },
        ]
    )
    report = drc(tree)
    assert any(f.rule == "minimum_constraint" for f in report.findings)


def test_hard_measure_with_consumer_not_advised() -> None:
    from precis_se.drc import drc

    tree = _tree(
        [
            {"op": "add_block", "name": "a"},
            {"op": "add_block", "name": "b"},
            {
                "op": "add_measure",
                "block": "a",
                "name": "x",
                "value": 0.01,
                "strength": "hard",
            },
            {
                "op": "add_measure",
                "block": "b",
                "name": "y",
                "relation": {"source": "a.x", "offset": 1e-3},
            },
        ]
    )
    report = drc(tree)
    assert not any(f.rule == "minimum_constraint" for f in report.findings)


def test_magnitude_advisory_skips_non_metre_units() -> None:
    from precis_se.drc import drc

    tree = _tree(
        [
            {"op": "add_block", "name": "gear", "envelope": "cyl:r0.02h0.01"},
            {
                "op": "add_measure",
                "block": "gear",
                "name": "teeth",
                "value": 200.0,
                "unit": "count",
            },
        ]
    )
    report = drc(tree)
    assert not any(f.rule == "implausible_magnitude" for f in report.findings)


# ── persistence round-trip ──────────────────────────────────────────────


def test_notes_and_freedom_fields_roundtrip(handler: SeHandler, store: Store) -> None:
    from precis_se import persist

    handler.put(
        id="freedom-rt",
        args={
            "ops": [
                {"op": "add_block", "name": "hub"},
                {
                    "op": "set_envelope",
                    "block": "hub",
                    "envelope": "cyl:r0.01h0.02",
                    "origin": "proposed",
                },
                {
                    "op": "add_measure",
                    "block": "hub",
                    "name": "od",
                    "min": 0.01,
                    "max": 0.02,
                    "unit": "m",
                    "origin": "proposed",
                },
                {
                    "op": "add_note",
                    "name": "q1",
                    "kind": "question",
                    "text": "how big?",
                    "about": ["hub.od"],
                },
            ]
        },
    )
    ref = store.get_ref(kind="se", id="freedom-rt")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    (m,) = tree.measures
    assert (m.min_value, m.max_value, m.origin, m.unit) == (0.01, 0.02, "proposed", "m")
    assert tree.blocks["hub"].origins == {"envelope": "proposed"}
    (note,) = tree.notes
    assert (note.name, note.kind, note.re) == ("q1", "question", None)
    assert note.about == ["hub.od"]
    first_created = note.created_at
    assert first_created is not None

    # created_at survives the retire/reinsert save cycle (the timeline
    # must not restamp on every edit).
    handler.edit(
        id="freedom-rt",
        args={
            "ops": [
                {
                    "op": "add_note",
                    "name": "a1",
                    "kind": "answer",
                    "text": "1-2 cm band",
                    "re": "q1",
                }
            ]
        },
    )
    tree2 = persist.load_tree(store, ref.id)
    q1 = next(n for n in tree2.notes if n.name == "q1")
    assert q1.created_at == first_created
    assert [n.name for n in tree2.notes] == ["q1", "a1"]


def test_interview_and_freedom_views_render(handler: SeHandler) -> None:
    handler.put(
        id="freedom-view",
        args={
            "ops": [
                {"op": "add_block", "name": "hub"},
                {
                    "op": "add_measure",
                    "block": "hub",
                    "name": "od",
                    "min": 0.01,
                    "max": 0.02,
                },
                {
                    "op": "add_note",
                    "name": "q1",
                    "kind": "question",
                    "text": "how big?",
                    "about": ["hub.od", "gone.x"],
                },
            ]
        },
    )
    interview = handler.get(id="freedom-view", view="interview").body
    assert "1 open question(s)" in interview
    assert "gone.x (dangling)" in interview
    free = handler.get(id="freedom-view", view="freedom").body
    assert "hub.od" in free
    assert "band" in free
    # empty design honesty
    handler.put(id="freedom-empty", text=None, args={"ops": []})
    free_empty = handler.get(id="freedom-empty", view="freedom").body
    assert "EMPTY design also reads as fully decided" in free_empty


def test_note_spec_dataclass_defaults() -> None:
    n = NoteSpec(name="q", kind="question", body="?")
    assert n.origin == "user"
    assert n.about == []
    assert n.created_at is None
