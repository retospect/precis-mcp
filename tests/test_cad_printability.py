"""cad/printability.py — orientation search + process DRC (se-print-
implementer.md rung 3). Fixtures are built through the cad DSL
(``parse_source`` + ``build_design``) and tessellated the same way
``handlers/cad.py``'s ``view='printability'`` does — the folded
``manifold3d`` solid, native (metre) units.
"""

from __future__ import annotations

import math
from typing import Any

import numpy as np
import pytest

from precis.cad.export import _solid_mesh
from precis.cad.printability import (
    candidates,
    orient,
    process_findings,
    rotate_to_frame,
    rules_mm_to_m,
    score,
)
from precis.cad.scene import parse_source
from precis.cad.vec import vec3

pytest.importorskip("manifold3d")

# A generous, complete policy — flat weights, matching the cad view's own
# convention (se would pass its own family-level weights instead).
_FLAT_POLICY: dict[str, Any] = {
    "weights": {
        "overhang": 1.0,
        "bed_contact": 1.0,
        "height": 1.0,
        "bridges": 1.0,
        "load_vs_layer": 1.0,
    },
    "sweep_deg": 30,
}

_FULL_RULES = {
    "max_overhang": 45.0,
    "max_bridge": 0.008,  # 8 mm
    "layer_height": 0.0002,  # 0.2 mm
    "min_bed_contact": 0.15,
}


def _mesh(text: str):
    spec = parse_source(text)
    return _solid_mesh(spec)


# ---------------------------------------------------------------------------
# candidates()
# ---------------------------------------------------------------------------


def test_candidates_axis_only_with_no_sweep() -> None:
    cands = candidates(0.0)
    assert len(cands) == 6
    for v in cands:
        assert math.isclose(float(np.linalg.norm(v)), 1.0, abs_tol=1e-9)


def test_candidates_deterministic_and_deduplicated() -> None:
    a = candidates(30.0)
    b = candidates(30.0)
    assert len(a) == len(b)
    for va, vb in zip(a, b, strict=True):
        assert np.allclose(va, vb, atol=1e-9)
    # no duplicate pair within the dedup tolerance
    for i in range(len(a)):
        for j in range(i + 1, len(a)):
            assert float(np.linalg.norm(a[i] - a[j])) > 1e-6
    # every candidate is unit length
    for v in a:
        assert math.isclose(float(np.linalg.norm(v)), 1.0, abs_tol=1e-6)
    # sweeping strictly widens the axis-only set
    assert len(a) > len(candidates(0.0))


# ---------------------------------------------------------------------------
# T-shape — stem-up beats stem-down (overhang)
# ---------------------------------------------------------------------------

_T_SHAPE = """
component t
top   add box:w40mmd40mmh10mm  @0mm,0mm,10mm
stem  add box:w10mmd10mmh10mm  @0mm,0mm,0mm
"""


def test_t_shape_prefers_stem_up() -> None:
    mesh = _mesh(_T_SHAPE)
    cands = orient(mesh, _FULL_RULES, _FLAT_POLICY, [])
    best = cands[0]
    # "stem-up": the wide top ends up on the bed, so the mesh's own +z axis
    # (where the wide top sits) must map to -z — i.e. down=(0,0,1).
    assert np.allclose(best.down, vec3(0.0, 0.0, 1.0), atol=1e-6)
    # the opposite (stem-down, wide top overhangs) scores worse.
    stem_down = next(
        c for c in cands if np.allclose(c.down, vec3(0.0, 0.0, -1.0), atol=1e-6)
    )
    assert stem_down.score > best.score
    assert stem_down.terms.get("overhang_area", 0.0) > best.terms.get(
        "overhang_area", 0.0
    )


def test_t_shape_stem_down_reports_overhang_finding() -> None:
    mesh = _mesh(_T_SHAPE)
    findings = process_findings(mesh, vec3(0.0, 0.0, -1.0), _FULL_RULES)
    assert any(f.rule == "overhang" for f in findings)


# ---------------------------------------------------------------------------
# tall thin plate — lying flat beats standing up
# ---------------------------------------------------------------------------

_TALL_PLATE = "post add box:w5mmd5mmh100mm"


def test_tall_plate_prefers_lying_flat() -> None:
    mesh = _mesh(_TALL_PLATE)
    cands = orient(mesh, _FULL_RULES, _FLAT_POLICY, [])
    best = cands[0]
    # lying flat: down is one of the four horizontal axes, never the
    # standing z-axis.
    assert abs(float(best.down[2])) < 1e-6
    standing = next(
        c for c in cands if np.allclose(c.down, vec3(0.0, 0.0, -1.0), atol=1e-6)
    )
    assert standing.score > best.score
    assert best.terms["height"] < standing.terms["height"]


# ---------------------------------------------------------------------------
# bridge plate — an unsupported span over the limit
# ---------------------------------------------------------------------------

_BRIDGE_PLATE = """
component bridge
leg_a add box:w5mmd20mmh10mm  @-20mm,0mm,0mm
leg_b add box:w5mmd20mmh10mm  @20mm,0mm,0mm
span  add box:w45mmd20mmh5mm  @0mm,0mm,10mm
"""


def test_bridge_plate_finds_bridge_over_the_limit() -> None:
    mesh = _mesh(_BRIDGE_PLATE)
    # down=(0,0,-1) (identity — no rotation): legs on the bed, span across
    # the top, the underside between the legs is an unsupported span well
    # past max_bridge (8 mm).
    findings = process_findings(mesh, vec3(0.0, 0.0, -1.0), _FULL_RULES)
    bridge = next((f for f in findings if f.rule == "bridge"), None)
    assert bridge is not None
    assert "span" in bridge.measured


def test_bridge_plate_short_span_is_clean() -> None:
    mesh = _mesh(_BRIDGE_PLATE)
    lax_rules = dict(_FULL_RULES, max_bridge=0.5)  # 500 mm — nothing qualifies
    findings = process_findings(mesh, vec3(0.0, 0.0, -1.0), lax_rules)
    assert not any(f.rule == "bridge" for f in findings)


# ---------------------------------------------------------------------------
# load penalises a candidate's own build axis
# ---------------------------------------------------------------------------

_RECT_PLATE = "post add box:w5mmd20mmh100mm"


def test_load_along_a_candidates_down_penalises_it() -> None:
    mesh = _mesh(_RECT_PLATE)
    baseline = orient(mesh, _FULL_RULES, _FLAT_POLICY, [])
    best = baseline[0]
    assert np.allclose(best.down, vec3(1.0, 0.0, 0.0), atol=1e-6)  # widest face down

    heavy_load_policy = dict(_FLAT_POLICY)
    heavy_load_policy["weights"] = dict(_FLAT_POLICY["weights"], load_vs_layer=50.0)
    loaded = orient(mesh, _FULL_RULES, heavy_load_policy, [vec3(1.0, 0.0, 0.0)])
    was_best = next(c for c in loaded if np.allclose(c.down, best.down, atol=1e-6))
    assert was_best.terms["load_vs_layer"] > 0.99
    assert loaded[0].down.tolist() != was_best.down.tolist()
    assert loaded[0].score < was_best.score  # no longer the winner


# ---------------------------------------------------------------------------
# honesty: missing policy weight raises, missing rule field is skipped
# ---------------------------------------------------------------------------


def test_missing_policy_weight_raises() -> None:
    mesh = _mesh(_TALL_PLATE)
    bad_policy = {
        "weights": {k: 1.0 for k in ("overhang", "bed_contact", "height", "bridges")},
        "sweep_deg": 30,
    }  # load_vs_layer missing
    with pytest.raises(ValueError, match="load_vs_layer"):
        orient(mesh, _FULL_RULES, bad_policy, [])


def test_missing_sweep_deg_raises() -> None:
    mesh = _mesh(_TALL_PLATE)
    bad_policy = {"weights": _FLAT_POLICY["weights"]}
    with pytest.raises(ValueError, match="sweep_deg"):
        orient(mesh, _FULL_RULES, bad_policy, [])


def test_missing_rule_field_is_skipped_never_defaulted() -> None:
    mesh = _mesh(_TALL_PLATE)
    s = score(mesh, vec3(0.0, 0.0, -1.0), {}, _FLAT_POLICY, [])
    assert set(s.skipped) == {"overhang", "bed_contact", "bridges"}
    assert s.overhang_area is None
    assert s.bed_contact_ratio is None
    assert s.bridge_count is None
    # height/load_vs_layer never need a rules field
    assert s.height > 0.0
    assert s.load_vs_layer == 0.0


def test_rules_mm_to_m_converts_lengths_only() -> None:
    mm_rules = {
        "layer_height": 0.2,
        "max_bridge": 8.0,
        "max_overhang": 45.0,
        "min_bed_contact": 0.15,
        "max_build": None,
    }
    m_rules = rules_mm_to_m(mm_rules)
    assert m_rules["layer_height"] == pytest.approx(0.0002)
    assert m_rules["max_bridge"] == pytest.approx(0.008)
    assert m_rules["max_overhang"] == 45.0  # degrees — untouched
    assert m_rules["min_bed_contact"] == 0.15  # ratio — untouched
    assert m_rules["max_build"] is None  # unpublished figure stays None


# ---------------------------------------------------------------------------
# rotate_to_frame — se's export will reuse this directly
# ---------------------------------------------------------------------------


def test_rotate_to_frame_puts_bed_at_zero_and_down_at_minus_z() -> None:
    mesh = _mesh(_TALL_PLATE)
    verts, _tris = mesh
    rotated = rotate_to_frame(verts, vec3(1.0, 0.0, 0.0))
    assert math.isclose(float(rotated[:, 2].min()), 0.0, abs_tol=1e-9)
    # lying on its side: the 100 mm dimension is now horizontal, so the
    # z-extent is only the original 5 mm cross-section.
    assert rotated[:, 2].max() < 0.01  # < 10 mm, well under the original 100 mm


# ---------------------------------------------------------------------------
# through the cad handler — mirrors the other probe-view handler tests
# ---------------------------------------------------------------------------


def test_view_printability_through_handler(store) -> None:
    from precis.dispatch import Hub as HandlerHub
    from precis.handlers.cad import CadHandler

    cad = CadHandler(hub=HandlerHub(store=store))
    cad.put(id="t-shape", text=_T_SHAPE)
    resp = cad.get(
        id="t-shape",
        view="printability",
        args={
            "max_overhang": "45deg",
            "max_bridge": "8mm",
            "layer_height": "0.2mm",
            "min_bed_contact": 0.15,
        },
    )
    assert "printability" in resp.body
    assert "down" in resp.body and "score" in resp.body
    assert "flat 1.0 weights" in resp.body


def test_view_printability_skips_missing_rule_args(store) -> None:
    from precis.dispatch import Hub as HandlerHub
    from precis.handlers.cad import CadHandler

    cad = CadHandler(hub=HandlerHub(store=store))
    cad.put(id="post", text=_TALL_PLATE)
    resp = cad.get(id="post", view="printability", args={})
    assert "skipped" in resp.body
    for key in ("max_overhang", "max_bridge", "layer_height", "min_bed_contact"):
        assert key in resp.body


def test_printability_rules_angles_in_degrees_lengths_in_metres() -> None:
    """The engine compares face angles in degrees (the bare-degree
    convention se_capabilities.json stores) — parse_quantity's SI radian
    must come back out as degrees (pre-ship review finding)."""
    from precis.handlers.cad import _printability_rules

    rules, skipped = _printability_rules(
        {"max_overhang": "45deg", "max_bridge": "8mm", "min_bed_contact": 0.15}
    )
    assert rules["max_overhang"] == pytest.approx(45.0)
    assert rules["max_bridge"] == pytest.approx(0.008)
    assert rules["min_bed_contact"] == 0.15
    assert skipped == ["layer_height"]


def test_view_printability_pinned_down(store) -> None:
    from precis.dispatch import Hub as HandlerHub
    from precis.handlers.cad import CadHandler

    cad = CadHandler(hub=HandlerHub(store=store))
    cad.put(id="t-shape2", text=_T_SHAPE)
    resp = cad.get(
        id="t-shape2",
        view="printability",
        args={
            "down": [0, 0, -1],
            "max_overhang": "45deg",
            "max_bridge": "8mm",
            "layer_height": "0.2mm",
            "min_bed_contact": 0.15,
        },
    )
    assert "pinned" in resp.body
    assert "overhang" in resp.body  # stem-down should surface the finding
