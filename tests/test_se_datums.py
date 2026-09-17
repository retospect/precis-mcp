"""precis_se datums + measurement-from-geometry evaluator —
docs/backlog/se-datum-measure-eval.md, one test per acceptance
criterion (criterion 9 — ``precis migrate --dry-run`` on a DB without
the column — is a manual check, reported at ship).

Criteria 1–7 run on in-memory ``SeTree``s; criterion 8 goes through the
handler + store like ``test_se_plugin.py`` (the plugin migration
fixture applies ``0012_se_measure_datum.sql``).
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
import pytest

import precis_se
from precis.dispatch import Hub
from precis.store import Store
from precis_se import persist
from precis_se.datums import (
    Selector,
    _hull_area_2d,
    d_measure,
    evaluate_measure,
    parse_selector,
    rank_datums,
    resolve,
)
from precis_se.handler import SeHandler
from precis_se.measures import MeasureError, MeasureSpec, stackup, validate_relation
from precis_se.ops import OpError, SeTree, apply_ops

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)
    return SeHandler(hub=hub)


def _tree(*blocks: dict) -> SeTree:
    tree = SeTree()
    apply_ops(tree, [{"op": "add_block", **b} for b in blocks])
    return tree


def _measure(block: str, name: str, **kw: object) -> MeasureSpec:
    return MeasureSpec(block=block, name=name, **kw)  # type: ignore[arg-type]


# ── c1: frame default — prismatic names the 3-2-1 faces; rotational
#         reports axis + base face ────────────────────────────────────────


def test_c1_frame_default_prismatic_and_rotational() -> None:
    tree = _tree(
        {"name": "plate", "envelope": "box:w0.1d0.05h0.01"},
        {"name": "pin", "envelope": "cyl:r0.004h0.02"},
    )
    box = resolve(tree, tree.blocks["plate"], None)
    assert box.resolved == "frame"
    # The 3-2-1: three face planes through/at the frame origin, named
    # as instance.tag — bottom (z=0), the −y and −x sides.
    assert sorted(box.members) == ["plate.bottom", "plate.side0", "plate.side3"]
    cyl = resolve(tree, tree.blocks["pin"], None)
    assert cyl.resolved == "frame"
    assert sorted(cyl.members) == ["axis:pin", "pin.bottom"]


# ── c2: port faces are free datums — ranked above larger non-port flats ──


def test_c2_port_outranks_larger_plain_face() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.1d0.05h0.01"})
    apply_ops(tree, [{"op": "add_port", "block": "b", "name": "seat"}])
    ranked = rank_datums(tree, tree.blocks["b"])
    assert ranked[0].datum == "port:seat"
    assert ranked[0].score == float("inf")
    assert "free" in ranked[0].reason
    # Every planar face — including the larger bottom/top — ranks below.
    assert all(r.datum.startswith("face:") for r in ranked[1:-1])
    assert ranked[-1].datum == "frame"


# ── c3: evaluate_measure — geometric number, loud on band breach ─────────


def test_c3_evaluate_derived_and_mismatch_note() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    # Wall thickness across x: datum = the −x face (side3), measured
    # feature = the +x face (side1).
    spec = _measure(
        "b",
        "wall_x",
        value=0.04,
        datum="face:b.side3",
        relation={"feature": "face:b.side1"},
    )
    out = evaluate_measure(tree, spec)
    assert out.value == pytest.approx(0.04)
    assert out.source == "derived" and out.datum_resolved == "b.side3"
    assert not any("mismatch" in n for n in out.notes)
    bad = evaluate_measure(
        tree,
        _measure(
            "b",
            "wall_x",
            value=0.05,
            min_value=0.049,
            max_value=0.051,
            datum="face:b.side3",
            relation={"feature": "face:b.side1"},
        ),
    )
    assert any("mismatch" in n and "0.05" in n and "0.04" in n for n in bad.notes)


def test_c3_mismatch_precedence_band_then_tol_then_exact() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    rel = {"feature": "face:b.side1"}

    def run(**kw: object) -> tuple[str, ...]:
        kw.setdefault("relation", rel)
        return evaluate_measure(
            tree, _measure("b", "wall_x", datum="face:b.side3", **kw)
        ).notes

    # Band wins: the derived 0.04 sits inside [0.03, 0.06] even though the
    # declared value is far away and the band is wide — no mismatch.
    assert not any(
        "mismatch" in n for n in run(value=0.055, min_value=0.03, max_value=0.06)
    )
    # A one-sided band works too, and is checked against the *derived* value.
    assert any("outside band" in n for n in run(min_value=0.041))
    assert not any("mismatch" in n for n in run(max_value=0.041))
    # No band: tol around the declared value.
    assert not any(
        "mismatch" in n for n in run(value=0.045, relation={**rel, "tol": 0.01})
    )
    assert any(
        "tol 0.001" in n for n in run(value=0.045, relation={**rel, "tol": 0.001})
    )
    # No band, no tol: exact.
    assert any("mismatch" in n for n in run(value=0.0401))
    assert not any("mismatch" in n for n in run(value=0.04))


def test_c3_non_length_unit_derives_nothing() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    out = evaluate_measure(
        tree,
        _measure(
            "b",
            "tilt",
            value=5.0,
            unit="deg",
            min_value=4.0,
            max_value=6.0,
            datum="face:b.side3",
            relation={"feature": "face:b.side1"},
        ),
    )
    assert out.value is None
    assert not any("mismatch" in n for n in out.notes)
    assert any("'deg'" in n and "length in m" in n for n in out.notes)


# ── c4: moving the pose leaves datum-relative measurements unchanged ─────


def test_c4_pose_invariant() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    spec = _measure(
        "b",
        "wall_x",
        datum="face:b.side3",
        relation={"feature": "face:b.side1"},
    )
    a = evaluate_measure(tree, spec)
    apply_ops(
        tree,
        [
            {
                "op": "set_pose",
                "block": "b",
                "pose": [0.5, 0.5, 0.5],
                "rot": [0.0, 0.0, 0.7],
            }
        ],
    )
    b = evaluate_measure(tree, spec)
    assert b.value == pytest.approx(a.value, rel=1e-9)
    assert b.datum_resolved == a.datum_resolved


# ── c5: d_measure — central difference over envelope params only ─────────


def test_c5_d_measure_box_width() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    # Width measured between the two x faces (side3 datum, side1 feature).
    spec = _measure(
        "b",
        "wall_x",
        datum="face:b.side3",
        relation={"feature": "face:b.side1"},
    )
    grad = d_measure(tree, spec)
    assert grad["w"] == pytest.approx(1.0, abs=1e-6)
    assert grad["d"] == pytest.approx(0.0, abs=1e-6)
    assert grad["h"] == pytest.approx(0.0, abs=1e-6)


# ── c6: predicate re-resolution emits the datum-moved note ───────────────


def test_c6_face_largest_moves() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.1d0.05h0.01"})
    spec = _measure(
        "b",
        "m",
        datum="face:largest",
        relation={"feature": "face:b.side1"},
    )
    first = evaluate_measure(tree, spec)
    assert first.datum_resolved == "b.top"
    # Re-parameterise so a different face is largest.
    apply_ops(
        tree, [{"op": "set_envelope", "block": "b", "envelope": "box:w0.01d0.05h0.1"}]
    )
    second = evaluate_measure(tree, spec, prev_resolved=first.datum_resolved)
    moved_to = second.datum_resolved
    assert moved_to is not None and moved_to != first.datum_resolved
    assert any(
        n.startswith("datum moved: b.top → ") and moved_to in n for n in second.notes
    )


# ── c7: strict on shape, lenient on existence ────────────────────────────


def test_c7_selector_strict_shape_lenient_existence() -> None:
    # Shape errors are loud at parse/write.
    with pytest.raises(MeasureError):
        parse_selector("faces:foo")
    with pytest.raises(MeasureError):
        parse_selector("flange:left")
    tree = _tree({"name": "body", "envelope": "box:w0.04d0.02h0.01"})
    apply_ops(
        tree,
        [
            {
                "op": "add_measure",
                "block": "body",
                "name": "m",
                "datum": "face:body.side9",  # parses; the face isn't there
            }
        ],
    )
    out = evaluate_measure(tree, tree.measures[0])
    assert out.value is None and out.datum_resolved is None
    assert any("face:body.side9" in n for n in out.notes)
    with pytest.raises(OpError, match="datum"):
        apply_ops(
            tree,
            [
                {
                    "op": "set_measure",
                    "block": "body",
                    "name": "m",
                    "datum": "faces:foo",
                }
            ],
        )


# ── c8: round-trip — save/load preserves datum; NULL loads as frame ──────


def test_c8_datum_persists_and_null_defaults_frame(
    handler: SeHandler, store: Store
) -> None:
    doc = json.dumps(
        {
            "description": "datum round-trip",
            "ops": [
                {
                    "op": "add_block",
                    "name": "plate",
                    "envelope": "box:w0.1d0.05h0.01",
                },
                {"op": "add_port", "block": "plate", "name": "seat"},
                {
                    "op": "add_measure",
                    "block": "plate",
                    "name": "thick",
                    "value": 0.01,
                    "datum": "port:seat",
                },
                {"op": "add_measure", "block": "plate", "name": "plain", "value": 0.02},
            ],
        }
    )
    handler.put(id="dt1", text=doc)
    ref = store.get_ref(kind="se", id="dt1")
    assert ref is not None
    tree = persist.load_tree(store, ref.id)
    by_name = {m.name: m for m in tree.measures}
    assert by_name["thick"].datum == "port:seat"
    assert by_name["plain"].datum is None
    # A NULL datum resolves as the pose frame.
    out = evaluate_measure(tree, by_name["plain"])
    assert out.datum_resolved == "frame"
    # Views: measures shows the selector + resolved target; datums shows
    # the ranking and which measures hang off each datum.
    measures = handler.get(id="dt1", view="measures")
    assert "port:seat" in measures.body
    datums = handler.get(id="dt1", view="datums")
    assert "port:seat" in datums.body and "free" in datums.body
    assert "thick" in datums.body  # the measure hangs off the port datum


def test_c8_datums_view_hangs_predicate_measures_off_resolved_row(
    handler: SeHandler,
) -> None:
    doc = json.dumps(
        {
            "description": "predicate datum in the datums view",
            "ops": [
                {"op": "add_block", "name": "b", "envelope": "box:w0.1d0.05h0.01"},
                {
                    "op": "add_measure",
                    "block": "b",
                    "name": "wide",
                    "value": 0.1,
                    "datum": "face:largest",
                    "relation": {"feature": "face:b.side1"},
                },
            ],
        }
    )
    handler.put(id="dt2", text=doc)
    body = handler.get(id="dt2", view="datums").body
    # The predicate never string-equals a ranked row; it must still hang
    # off the row its resolution names.
    assert "wide (declared face:largest)" in body


# ── c9: parse_selector — every shape-error branch in the v1 grammar ──────


def test_c9_parse_selector_shape_errors() -> None:
    with pytest.raises(MeasureError, match="non-empty string"):
        parse_selector("")
    with pytest.raises(MeasureError, match="non-empty string"):
        parse_selector(123)
    with pytest.raises(MeasureError, match="unknown datum selector"):
        parse_selector("nope")
    with pytest.raises(MeasureError, match="port selector needs a plain name"):
        parse_selector("port:a.b")
    with pytest.raises(MeasureError, match="axis selector is"):
        parse_selector("axis:a.b")
    with pytest.raises(MeasureError, match="face:normal="):
        parse_selector("face:normal=q")
    with pytest.raises(MeasureError, match="face selector is"):
        parse_selector("face:justname")
    # 'perp=assembly' and a plain 'axis:<instance>' both parse cleanly.
    assert parse_selector("face:perp=assembly").pred == "perp_assembly"
    assert parse_selector("axis:pin") == Selector(kind="axis", instance="pin")


# ── c10: resolve — instance/envelope misses, predicates, non-string input ─


def test_c10_resolve_missing_instance_and_envelope() -> None:
    tree = _tree({"name": "empty"}, {"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    # axis naming an instance that doesn't exist in the tree.
    miss = resolve(tree, tree.blocks["b"], "axis:ghost")
    assert miss.error is not None and "no parseable envelope" in miss.error
    # a face naming a missing instance takes the same _placed miss.
    face_miss = resolve(tree, tree.blocks["b"], "face:ghost.bottom")
    assert face_miss.error is not None and "no parseable envelope" in face_miss.error
    # a block with no envelope at all resolves to the same "no envelope" miss.
    no_env = resolve(tree, tree.blocks["empty"], "frame")
    assert no_env.error is not None and "no parseable envelope" in no_env.error


def test_c10_resolve_malformed_stored_envelope_never_crashes() -> None:
    # a hand-corrupted envelope string (bypassing write-time DSL
    # validation, which lives in precis_se.ops) fails to parse — a
    # finding, never a crash.
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    tree.blocks["b"].envelope = "not a shape"
    out = resolve(tree, tree.blocks["b"], "frame")
    assert out.error is not None and "no parseable envelope" in out.error


def test_c10_resolve_port_unknown_name_and_success_with_direction() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    apply_ops(
        tree,
        [{"op": "add_port", "block": "b", "name": "seat", "direction": [0, 0, 1]}],
    )
    miss = resolve(tree, tree.blocks["b"], "port:ghost")
    assert miss.error is not None and "no port" in miss.error
    ok = resolve(tree, tree.blocks["b"], "port:seat")
    assert ok.resolved == "port:seat"
    assert ok.normal is not None and ok.point is not None


def test_c10_resolve_axis_selector_success_and_not_rotational() -> None:
    tree = _tree(
        {"name": "b", "envelope": "box:w0.04d0.02h0.01"},
        {"name": "pin", "envelope": "cyl:r0.004h0.02"},
    )
    ok = resolve(tree, tree.blocks["b"], "axis:pin")
    assert ok.resolved == "axis:pin"
    bad = resolve(tree, tree.blocks["b"], "axis:b")
    assert bad.error is not None and "has no axis" in bad.error


def test_c10_resolve_face_predicates() -> None:
    tree = _tree(
        {"name": "b", "envelope": "box:w0.1d0.05h0.01"},
        {"name": "ball", "envelope": "sphere:r0.01"},
        {"name": "pin", "envelope": "cyl:r0.004h0.02"},
    )
    # No planar faces at all -> the predicate errs before scoring.
    none = resolve(tree, tree.blocks["ball"], "face:largest")
    assert none.error is not None and "no planar faces" in none.error
    # normal= resolves the aligned face...
    top = resolve(tree, tree.blocks["b"], "face:normal=+z")
    assert top.resolved == "b.top"
    # ...and errs when nothing on the block is aligned with it (a
    # cylinder's only planar faces are its ±z ends).
    no_match = resolve(tree, tree.blocks["pin"], "face:normal=+x")
    assert no_match.error is not None and "normal ≈" in no_match.error
    # perp=assembly picks the face closest to perpendicular with the
    # assembly direction — default +z, or an explicit override.
    perp = resolve(tree, tree.blocks["b"], "face:perp=assembly")
    assert perp.resolved is not None
    perp_x = resolve(
        tree, tree.blocks["b"], "face:perp=assembly", assembly_dir=[1, 0, 0]
    )
    assert perp_x.resolved is not None


def test_c10_resolve_accepts_selector_object_not_just_text() -> None:
    # resolve()'s selector text (echoed on every ResolvedDatum, error or
    # not) is reconstructed from a Selector object the same way a caller's
    # string round-trips — the non-string path through _selector_text.
    tree = _tree({"name": "b", "envelope": "box:w0.1d0.05h0.01"})
    cases = [
        (Selector(kind="port", name="seat"), "port:seat"),
        (Selector(kind="axis", instance="b"), "axis:b"),
        (Selector(kind="face", pred="largest"), "face:largest"),
        (Selector(kind="face", pred="normal", arg="+z"), "face:normal=+z"),
        (Selector(kind="face", pred="perp_assembly"), "face:perp=assembly"),
        (Selector(kind="face", instance="b", tag="top"), "face:b.top"),
    ]
    for sel, text in cases:
        out = resolve(tree, tree.blocks["b"], sel)
        assert out.selector == text


# ── c11: rank_datums — scored reason names the assembly-alignment term ───


def test_c11_rank_datums_with_assembly_dir_shows_alignment() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.1d0.05h0.01"})
    ranked = rank_datums(tree, tree.blocks["b"], assembly_dir=[0, 0, 1])
    faces = [r for r in ranked if r.datum.startswith("face:")]
    assert faces and all("n·assembly" in r.reason for r in faces)


# ── c12: evaluate_measure — the defensive/edge notes ──────────────────────


def test_c12_evaluate_measure_block_not_found() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    out = evaluate_measure(tree, _measure("ghost", "m"))
    assert out.value is None
    assert any("not found" in n for n in out.notes)


def test_c12_evaluate_measure_malformed_datum_and_feature_selectors() -> None:
    # A hand-corrupted stored selector (write-time shape validation lives
    # in precis_se.ops, not here) is a note, never a crash: resolve()
    # itself doesn't catch parse_selector's MeasureError, so
    # evaluate_measure must — for both the datum and the feature selector.
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    bad_datum = evaluate_measure(tree, _measure("b", "m", datum="bogus"))
    assert bad_datum.value is None and bad_datum.datum_resolved is None
    assert any("unresolvable datum selector" in n for n in bad_datum.notes)
    bad_feature = evaluate_measure(
        tree, _measure("b", "m", relation={"feature": "bogus"})
    )
    assert bad_feature.value is None and bad_feature.datum_resolved == "frame"
    assert any("unresolvable feature selector" in n for n in bad_feature.notes)


def test_c12_evaluate_measure_feature_exists_but_unresolvable() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    out = evaluate_measure(
        tree, _measure("b", "m", relation={"feature": "face:b.side9"})
    )
    assert out.value is None and out.datum_resolved == "frame"
    assert any("face:b.side9" in n for n in out.notes)


def test_c12_evaluate_measure_frame_datum_and_frame_feature_has_no_normal() -> None:
    # A 'frame' feature has no normal of its own (it's a point + 3-2-1
    # members) — projected against a frame datum, there is nothing to
    # project onto.
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    out = evaluate_measure(tree, _measure("b", "m", relation={"feature": "frame"}))
    assert out.value is None
    assert any("nothing to project onto" in n for n in out.notes)


# ── c13: d_measure — block/envelope misses and unknown params ────────────


def test_c13_d_measure_block_or_envelope_missing() -> None:
    tree = _tree({"name": "empty"}, {"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    assert d_measure(tree, _measure("ghost", "m")) == {}
    assert d_measure(tree, _measure("empty", "m")) == {}
    spec = _measure(
        "b", "m", datum="face:b.side3", relation={"feature": "face:b.side1"}
    )
    tree.blocks["b"].envelope = "not a shape"
    assert d_measure(tree, spec) == {}


def test_c13_d_measure_unknown_param_is_nan() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    spec = _measure(
        "b", "m", datum="face:b.side3", relation={"feature": "face:b.side1"}
    )
    grad = d_measure(tree, spec, params=["zzz"])
    assert math.isnan(grad["zzz"])


# ── c14: _hull_area_2d — fewer than 3 unique projected points is zero ────


def test_c14_hull_area_2d_degenerate_is_zero() -> None:
    assert _hull_area_2d(np.array([[0.0, 0.0], [1.0, 1.0]])) == 0.0
    assert _hull_area_2d(np.array([[0.0, 0.0]])) == 0.0


# ── c15: measures.validate_relation / stackup — feature-relation shapes ──


def test_c15_validate_relation_feature_shape_errors() -> None:
    with pytest.raises(MeasureError, match="selector string"):
        validate_relation({"feature": ""})
    with pytest.raises(MeasureError, match="selector string"):
        validate_relation({"feature": 123})
    with pytest.raises(MeasureError, match="exactly one source"):
        validate_relation({})
    with pytest.raises(MeasureError, match="takes no scale/offset/tol"):
        validate_relation({"feature": "face:b.side1", "tol": 0.01})


def test_c15_validate_relation_source_and_feature_combine() -> None:
    out = validate_relation(
        {"source": "a.b", "offset": 0.001, "feature": "face:a.side1"}
    )
    assert out["source"] == "a.b" and out["feature"] == "face:a.side1"


def test_c15_stackup_feature_only_relation_is_its_own_anchor() -> None:
    # A feature-only relation is a geometry binding, not a stack-up
    # chain — an anchor by construction; its own band agreement still
    # runs against the declared value.
    ms = [
        MeasureSpec(
            block="b",
            name="m",
            value=0.05,
            min_value=0.0,
            max_value=0.01,
            relation={"feature": "face:b.side1"},
        )
    ]
    (res,) = stackup(ms)
    assert res.problem_kind == "mismatch"
    assert res.chain == ["b.m"]


# ── c16: ops — add_measure 'datum' must be a non-empty selector string ───


def test_c16_add_measure_datum_must_be_a_string() -> None:
    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    with pytest.raises(OpError, match="selector string"):
        apply_ops(
            tree,
            [{"op": "add_measure", "block": "b", "name": "m", "datum": 123}],
        )
    with pytest.raises(OpError, match="selector string"):
        apply_ops(
            tree,
            [{"op": "add_measure", "block": "b", "name": "m", "datum": "   "}],
        )


# ── c17: handler render paths — relation text, empty tree, corrupt datum ─


def test_c17_measures_view_renders_feature_only_and_combined_relations(
    handler: SeHandler,
) -> None:
    doc = json.dumps(
        {
            "description": "relation feature rendering",
            "ops": [
                {"op": "add_block", "name": "a", "envelope": "box:w0.1d0.05h0.01"},
                {"op": "add_block", "name": "b", "envelope": "box:w0.04d0.02h0.01"},
                {"op": "add_measure", "block": "a", "name": "od", "value": 0.1},
                {
                    "op": "add_measure",
                    "block": "b",
                    "name": "feat_only",
                    "value": 0.04,
                    "relation": {"feature": "face:b.side1"},
                },
                {
                    "op": "add_measure",
                    "block": "b",
                    "name": "combo",
                    "value": 0.04,
                    "relation": {
                        "source": "a.od",
                        "offset": 0.0,
                        "tol": 0.001,
                        "feature": "face:b.side1",
                    },
                },
            ],
        }
    )
    handler.put(id="dt3", text=doc)
    body = handler.get(id="dt3", view="measures").body
    # feature-only: no 'source' means rel starts as '—', so the feature
    # note stands alone.
    assert "feature face:b.side1" in body
    # combined source+feature: the feature note is appended to the
    # source-derived relation text.
    assert "· feature face:b.side1" in body


def test_c17_datums_view_empty_tree_and_multi_block_measure_isolation(
    handler: SeHandler,
) -> None:
    handler.put(id="dt4", text=json.dumps({"description": "empty", "ops": []}))
    body = handler.get(id="dt4", view="datums").body
    assert "(no blocks)" in body
    doc = json.dumps(
        {
            "description": "two blocks, one measured",
            "ops": [
                {"op": "add_block", "name": "a", "envelope": "box:w0.1d0.05h0.01"},
                {"op": "add_block", "name": "b", "envelope": "box:w0.04d0.02h0.01"},
                {"op": "add_measure", "block": "a", "name": "m", "value": 0.1},
            ],
        }
    )
    handler.put(id="dt5", text=doc)
    # block 'b' carries no measures of its own — the per-block loop must
    # skip block 'a's measure rather than hanging it off 'b's ranking too.
    body2 = handler.get(id="dt5", view="datums").body
    assert "## a" in body2 and "## b" in body2
    a_section, b_section = body2.split("## a", 1)[1].split("## b", 1)
    assert "m (default)" in a_section
    assert "m (default)" not in b_section


def test_c17_datums_view_survives_hand_corrupted_datum_selector(
    handler: SeHandler, store: Store
) -> None:
    doc = json.dumps(
        {
            "description": "corrupt datum",
            "ops": [
                {"op": "add_block", "name": "b", "envelope": "box:w0.04d0.02h0.01"},
                {"op": "add_measure", "block": "b", "name": "m", "value": 0.04},
            ],
        }
    )
    handler.put(id="dt6", text=doc)
    ref = store.get_ref(kind="se", id="dt6")
    assert ref is not None
    with store.pool.connection() as c:
        c.execute(
            "UPDATE se_measures SET datum = %s "
            "WHERE ref_id = %s AND name = 'm' AND retired_at IS NULL",
            ("not a valid selector", ref.id),
        )
    # neither the datums view nor its per-measure resolve() attempt
    # crashes on the malformed selector; a selector with no colon fails
    # parse_selector itself (MeasureError), so it never matches any
    # ranked row's identity and the measure hangs off none of them.
    body = handler.get(id="dt6", view="datums").body
    assert "## b" in body
    assert "m (default)" not in body and "m (declared)" not in body


def test_c17_measure_row_survives_evaluate_measure_raising(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Belt-and-suspenders: evaluate_measure's own error paths are always
    # caught internally (c12), but _measure_row doesn't lean on that
    # invariant holding forever — simulate a raise directly to prove the
    # render degrades to '—' rather than crashing the view.
    from precis_se import handler as handler_mod

    tree = _tree({"name": "b", "envelope": "box:w0.04d0.02h0.01"})
    spec = _measure("b", "m", value=0.04)

    def boom(*_a: object, **_kw: object) -> None:
        raise MeasureError("simulated")

    monkeypatch.setattr(handler_mod.se_datums, "evaluate_measure", boom)
    row = handler_mod._measure_row(spec, tree)
    assert row["derived"] == "—"
