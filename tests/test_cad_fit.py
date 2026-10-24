"""``precis.cad.fit`` — the mesh fit engine (se-fit-check-and-probe slice 1).

Engine-level tests: solids come straight from
``tessellate_component``/``place`` so no manifest or run dir is involved.
"""

from __future__ import annotations

import time

import pytest

from precis.cad.fit import (
    CHORD_MAX_M,
    Connect,
    FitReport,
    component_chord,
    default_rules,
    fit,
    place,
    tessellate_component,
    unmeshable_reason,
)
from precis.cad.scene import parse_source
from precis.cad.tessellate import _FN, _circular_seg
from precis.cad.vec import identity, rotation, translation

_CHORD = 1e-5  # 0.01 mm — the small test parts hit the CHORD_MAX cap


def _part(src: str, comp: str, name: str, xf, **kw):
    spec = parse_source(src)
    solid = tessellate_component(spec, comp, _CHORD)
    assert solid is not None, src
    return place({comp: solid}, xf, name=name, chord=_CHORD, **kw)


def _box(name: str, x_m: float, **kw):
    return _part(
        "component p\nbase add box:w10mmd10mmh10mm\n",
        "p",
        name,
        translation(x_m, 0, 0),
        **kw,
    )


def _errors(rep: FitReport) -> list[str]:
    return [r.subject for r in rep.errors]


def test_tessellate_component_folds_boolean() -> None:
    spec = parse_source(
        "component p\nbase add cyl:r10mmh20mm\nhole cut cyl:r4mmh22mm @0mm,0mm,-1mm\n"
    )
    solid = tessellate_component(spec, "p", _CHORD)
    assert solid is not None
    import math

    expect = math.pi * (10e-3**2 - 4e-3**2) * 20e-3
    assert solid.volume() == pytest.approx(expect, rel=0.02)


def test_seated_pair_touching_is_ok() -> None:
    """A rigid connect between touching parts passes — and rides the
    referee (gap 0 sits inside the 3×chord band)."""
    a = _part("component p\nbase add box:w10mmd10mmh10mm\n", "p", "a", identity())
    b = _part(
        "component p\nbase add box:w10mmd10mmh10mm\n",
        "p",
        "b",
        translation(0.01, 0, 0),
    )

    def refiner(spec_src: str):
        spec = parse_source(spec_src)

        def go(finer: float):
            s = tessellate_component(spec, "p", finer)
            return {"p": s.transform([[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0]])}

        return go

    a.refine = refiner("component p\nbase add box:w10mmd10mmh10mm\n")
    b.refine = None
    rep = fit([a, b], [Connect("a", "b", joint={"class": "rigid"})], default_rules())
    assert rep.errors == []


def test_overlap_is_error() -> None:
    rep = fit([_box("a", 0.0), _box("b", 0.007)], [], default_rules())
    assert _errors(rep) == ["a / b"]
    row = rep.errors[0]
    assert "overlap" in row.measure and "mm³" in row.measure


def test_connected_gap_5mm_is_error() -> None:
    """A connect across a +5 mm gap: 'expected 0'."""
    rep = fit(
        [_box("a", 0.0), _box("b", 0.015)],
        [Connect("a", "b", joint={"class": "rigid"})],
        default_rules(),
    )
    assert _errors(rep) == ["a / b"]
    assert "gap +5.000 mm" in rep.errors[0].measure


def test_press_overlap_passes() -> None:
    """A press pair overlapping 0.05 mm is in spec — press expects
    contact-or-overlap."""
    rep = fit(
        [_box("a", 0.0), _box("b", 0.00995)],
        [Connect("a", "b", joint={"class": "rigid", "mechanism": "press"})],
        default_rules(),
    )
    assert rep.errors == []
    assert any("press" in r.rule for r in rep.rows if r.severity == "ok")


def test_rotated_pair_overlap() -> None:
    """A 45°-rotated bar crossing a wall — a pose the AABB-only checks
    wave through."""
    wall = _part("component p\nbase add box:w2mmd40mmh40mm\n", "p", "wall", identity())
    bar = _part(
        "component p\nbase add box:w2mmd40mmh40mm\n",
        "p",
        "bar",
        rotation(0, 0, 45),
    )
    rep = fit([wall, bar], [], default_rules())
    assert _errors(rep) == ["bar / wall"]


def test_parent_child_unconnected_skipped() -> None:
    rep = fit(
        [_box("child", 0.0, parent="a"), _box("a", 0.005)],
        [],
        default_rules(),
    )
    assert rep.errors == []


def test_field_leaf_is_info_not_error() -> None:
    """A node carrying ``rd`` (or blend/field:) is listed 'not
    fit-checked' and never fails (Decision 12)."""
    spec = parse_source("component p\nbase add box:w10mmd10mmh10mmrd1mm\n")
    assert unmeshable_reason(spec, "p") is not None
    rep = fit(
        [
            place(
                {},
                identity(),
                name="filleted",
                chord=_CHORD,
                note="rd on node 'base'",
            ),
            _box("b", 0.005),
        ],
        [],
        default_rules(),
    )
    assert rep.errors == []
    assert any(
        r.severity == "info" and "not fit-checked" in r.measure for r in rep.rows
    )


def test_min_clearance_pair_rule() -> None:
    rep = fit(
        [_box("a", 0.0), _box("b", 0.011)],
        [],
        default_rules(),
        min_clearance={("a", "b"): 2e-3},
    )
    assert _errors(rep) == ["a / b"]
    assert "min_clearance" in rep.errors[0].rule


def test_multi_connect_strictest_wins() -> None:
    """prismatic+press on one pair: press fires on the +0.24 mm gap and
    both connects are named."""
    rep = fit(
        [_box("crown", 0.0), _box("seatpost", 0.01024)],
        [
            Connect(
                "crown",
                "seatpost",
                joint={"class": "prismatic"},
                a_port="sock",
                b_port="tube",
            ),
            Connect(
                "crown",
                "seatpost",
                joint={"mechanism": "press"},
                a_port="sock",
                b_port="tube",
            ),
        ],
        default_rules(),
    )
    assert _errors(rep) == ["crown / seatpost"]
    row = rep.errors[0]
    assert "press" in row.rule and "crown.sock" in row.rule


def test_chord_scales_with_size() -> None:
    """Decision 2: ``min(0.01 mm, 1e-4 × AABB diagonal)`` — a small part
    drops below the cap so its mesh stays finer than _FN; a large part
    rides the 0.01 mm cap."""
    small = parse_source("component p\nbase add cyl:r1mmh2mm\n")
    big = parse_source("component p\nbase add cyl:r200mmh400mm\n")
    cs, cb = component_chord(small, "p"), component_chord(big, "p")
    assert cs < CHORD_MAX_M < cb or cb == CHORD_MAX_M
    assert _circular_seg(cs, 1e-3) > _FN  # 0.4 µm chord on r=1 mm
    assert _circular_seg(cb, 0.2) > _FN  # 0.01 mm on r=200 mm still refines


def test_determinism_byte_identical() -> None:
    placed = [_box("a", 0.0), _box("b", 0.007), _box("c", 0.012)]
    conns = [Connect("a", "c", joint={"mechanism": "press"})]
    t1 = fit(placed, conns, default_rules()).text()
    t2 = fit(placed, conns, default_rules()).text()
    assert t1 == t2


def test_mass_refusal_and_table() -> None:
    placed = [_box("a", 0.0), _box("b", 0.02)]
    rep = fit(placed, [], default_rules())
    assert any("refused" in line for line in rep.mass_rows)
    a = _part(
        "component p\nbase add box:w10mmd10mmh10mm\n",
        "p",
        "a2",
        identity(),
        material={"p": "pla"},
    )
    rep3 = fit([a], [], default_rules(), materials={"pla": 1240.0})
    assert any("a2 |" in line and "kg" in line for line in rep3.mass_rows)


def test_aabb_broadphase_under_200ms() -> None:
    """Stress: a grid of well-separated parts — the O(n²) AABB pass is
    the cost (no pair measurements run)."""
    placed = []
    for i in range(120):
        x, y = (i % 12) * 0.02, (i // 12) * 0.02
        placed.append(_box(f"p{i}", 0.0))
        placed[-1] = place(
            placed[-1].solids, translation(x, y, 0), name=f"p{i}", chord=_CHORD
        )
    t0 = time.monotonic()
    rep = fit(placed, [], default_rules())
    elapsed_ms = (time.monotonic() - t0) * 1000
    assert rep.errors == []
    assert rep.n_pairs == 0
    assert elapsed_ms < 200.0
