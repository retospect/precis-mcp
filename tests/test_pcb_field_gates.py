"""The field polish's accept gates (gr504558).

The polish's descent is platform-sensitive (L-BFGS-B on x86 and arm64
lands different basins), so the gates are pinned here with the legality
verdict or the measures bill injected: what is asserted is the gate's
response to an illegal or worse candidate, never which candidate a
platform descends to.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from precis.pcb import DEFAULT_STACKUP
from precis.pcb import field as F
from precis.pcb.ir import from_graph
from precis.pcb.optimize import OptimizeConfig, OptimizeEngine


def _plane_graph(n: int = 9, gnd_members: int | None = None) -> dict[str, Any]:
    """``n`` parts in a signal ring, every part's ``g`` pin on GND."""
    inst = [{"refdes": f"U{i}"} for i in range(n)]
    gnd = range(n if gnd_members is None else gnd_members)
    nets = [
        {
            "name": "GND",
            "net_class": "ground",
            "domain": "electrical",
            "members": [{"refdes": f"U{i}", "pin": "g"} for i in gnd],
        }
    ]
    for i in range(n):
        nets.append(
            {
                "name": f"S{i}",
                "net_class": "signal",
                "domain": "electrical",
                "members": [
                    {"refdes": f"U{i}", "pin": "s"},
                    {"refdes": f"U{(i + 1) % n}", "pin": "t"},
                ],
            }
        )
    return {"instances": inst, "nets": nets}


def _plane_ir(pitch: float, origin: float = 3.0, n: int = 9) -> Any:
    """The ring board with GND declared a plane on the bottom layer, the
    parts on a 3-column grid at ``pitch``."""
    ir = from_graph(_plane_graph(n), stackup=DEFAULT_STACKUP)
    gnd = next(k for k in range(ir.n_nets) if str(ir.net_name[k]) == "GND")
    ir.promote_plane(gnd, len(ir.stackup) - 1)
    for i in range(n):
        ir.inst_x[i] = origin + (i % 3) * pitch
        ir.inst_y[i] = origin + (i // 3) * pitch
        ir.inst_rot[i] = 0.0
    return ir


def _jittered_ir() -> Any:
    """The spread board, nudged off the exact grid: aligned courtyards tie
    their SAT witnesses, a kink the polish's gradient probe refuses on."""
    ir = _plane_ir(6.0)
    for i in range(ir.n_instances):
        ir.inst_x[i] += 0.37 * i % 1.1
        ir.inst_y[i] += 0.23 * (i % 4)
    return ir


def _gate_engine() -> tuple[OptimizeEngine, Any]:
    ir = _jittered_ir()
    return OptimizeEngine(ir, OptimizeConfig(seed=1)), ir


def _revertible_part() -> int:
    """A part the polish moves whose SEED pose stays legal against every
    other part's polished pose — reverting it alone leaves a legal board.
    Measured on a throwaway run, so the choice follows whatever basin this
    platform's descent lands in."""
    engine, ir = _gate_engine()
    seed = (ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy())
    res = F.polish_placement(engine)
    assert res.applied, res
    for i in res.moved:
        if engine._placement_is_legal(
            [(i, float(seed[0][i]), float(seed[1][i]))],
            rotations={i: float(seed[2][i])},
        ):
            return int(i)
    raise AssertionError(f"no moved part can go back to its seed legally: {res}")


# ── gr504558 #1: the partial path measures the board it writes ─────────────


def test_partial_accept_gates_measure_the_written_board(monkeypatch) -> None:
    """A part the legality gate refuses goes back to its SEED pose; the
    E gate and the reported ``e_after`` must be the energy of exactly that
    written board — not of the descended pose the reverted part never
    takes (gr504558 #1)."""
    k = _revertible_part()
    engine, ir = _gate_engine()
    ref = str(ir.instance_refdes[k])
    seed_k = (float(ir.inst_x[k]), float(ir.inst_y[k]))
    real_legal = engine._placement_is_legal

    def legal(proposals, rotations=None, in_outline=False):
        if any(i == k for i, _x, _y in proposals):
            return False
        return real_legal(proposals, rotations=rotations, in_outline=in_outline)

    monkeypatch.setattr(engine, "_placement_is_legal", legal)
    monkeypatch.setattr(
        F,
        "_legality_violations",
        lambda engine_, proposals, rotations, limit=8: [f"courtyard: {ref}"],
    )
    calls: list[tuple[np.ndarray, float]] = []
    real_vg = F.Energy.value_grad

    def spy(self, z):
        v, g = real_vg(self, z)
        calls.append((np.array(z, copy=True), float(v)))
        return v, g

    monkeypatch.setattr(F.Energy, "value_grad", spy)
    built = F.board_from_engine(engine)
    assert built is not None
    lay = F.Layout(built[0])
    res = F.polish_placement(engine)
    assert res.applied, res
    assert "partial: reverted" in res.detail
    assert (float(ir.inst_x[k]), float(ir.inst_y[k])) == seed_k
    rot = np.array([0.0 if math.isnan(r) else float(r) for r in ir.inst_rot])
    written = lay.pack_from(
        np.asarray(ir.inst_x, dtype=float),
        np.asarray(ir.inst_y, dtype=float),
        -np.radians(rot),
    )
    matches = [v for z, v in calls if np.allclose(z, written, atol=1e-9)]
    assert matches, "the E gate never evaluated the written board"
    assert res.e_after == pytest.approx(matches[-1], rel=1e-12)


# ── gr504558 #3: an empty soft-pin box refuses instead of crashing ─────────


def test_empty_bounds_box_refuses_instead_of_crashing(monkeypatch) -> None:
    engine, ir = _gate_engine()
    real = engine.bounds_for
    monkeypatch.setattr(
        engine,
        "bounds_for",
        lambda i: (9.0, 9.0, 8.0, 8.0) if i == 2 else real(i),
    )
    before = ir.inst_x.copy()
    res = F.polish_placement(engine)
    assert not res.applied
    assert res.reason == "empty_bounds"
    assert "U2" in res.detail
    assert np.array_equal(ir.inst_x, before)


# ── gr504558 #4: nets too large to pull are reported ───────────────────────


def test_nets_above_the_pull_cap_are_reported() -> None:
    ir = from_graph(_plane_graph(14), stackup=DEFAULT_STACKUP)
    for i in range(14):
        ir.inst_x[i], ir.inst_y[i], ir.inst_rot[i] = 2.0 + 1.2 * i, 5.0, 0.0
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1))
    built = F.board_from_engine(engine)
    assert built is not None
    assert built[0].unpulled_nets == ("GND",)
    res = F.PolishResult(
        False, "no_gain", 1.0, 1.0, (), unpulled_nets=built[0].unpulled_nets
    )
    assert f"unpulled >{F.MAX_PULLED_NET_PINS}-pin nets: GND" in res.summary()


# ── the missing tests named in gr504558 ────────────────────────────────────


def _capture_bounds(monkeypatch: pytest.MonkeyPatch) -> list[list[Any]]:
    seen: list[list[Any]] = []
    real = F.minimize

    def spy(fg, z, **kw):
        seen.append(list(kw["bounds"]))
        return real(fg, z, **kw)

    monkeypatch.setattr(F, "minimize", spy)
    return seen


def test_descent_bounds_vector_with_fewer_rotating_than_translating_parts(
    monkeypatch,
) -> None:
    """z is [x(free), y(free), θ(free_rot)]: with n_rot < n_xy, slot k's y
    bound sits at n_free + k (not n_xy + k, a θ slot), and every θ slot is
    unbounded."""
    ir = _jittered_ir()
    ir.inst_fixed_rot[:5] = True
    engine = OptimizeEngine(ir, OptimizeConfig(seed=1))
    built = F.board_from_engine(engine)
    assert built is not None
    lay = F.Layout(built[0])
    n_free = int(lay.free_xy.sum())
    n_rot = int(lay.free_rot.sum())
    assert 0 < n_rot < n_free
    seen = _capture_bounds(monkeypatch)
    F.polish_placement(engine)
    assert seen
    first = seen[0]
    assert len(first) == lay.size
    for slot, pi in enumerate(np.flatnonzero(lay.free_xy)):
        x0, y0, x1, y1 = engine.bounds_for(int(pi))
        assert first[slot] == (x0, x1)
        assert first[n_free + slot] == (y0, y1)
    for k in range(2 * n_free, 2 * n_free + n_rot):
        assert first[k] == (None, None)


def test_theta_is_never_clamped_to_a_box(monkeypatch) -> None:
    """θ is a free variable in every descent stage: unbounded, except the
    ABNORMAL retry, which pins θ at the stall point (lo == hi) — never at
    a y-box edge."""
    engine, _ir = _gate_engine()
    built = F.board_from_engine(engine)
    assert built is not None
    lay = F.Layout(built[0])
    n_xy = 2 * int(lay.free_xy.sum())
    n_rot = int(lay.free_rot.sum())
    seen = _capture_bounds(monkeypatch)
    F.polish_placement(engine)
    for bounds in seen:
        for lo, hi in bounds[n_xy : n_xy + n_rot]:
            assert (lo, hi) == (None, None) or lo == hi


def test_measures_worse_is_refused_and_undone(monkeypatch) -> None:
    """The field energy does not model authored measures: a pose whose
    move raises the measures bill is undone (``measures_worse``) and the
    board is left as it was."""
    engine, ir = _gate_engine()
    spec = SimpleNamespace(hard=False, weight=1.0, metric="proximity", datum=None)
    monkeypatch.setattr(engine, "_measure_pairs", [(0, 1, spec)])
    engine._money_measures = 0.0
    real_apply, real_undo = engine.apply_move, engine.undo_move

    def apply(move):
        real_apply(move)
        engine._money_measures = 5.0

    def undo(move):
        real_undo(move)
        engine._money_measures = 0.0

    monkeypatch.setattr(engine, "apply_move", apply)
    monkeypatch.setattr(engine, "undo_move", undo)
    before = (ir.inst_x.copy(), ir.inst_y.copy(), ir.inst_rot.copy())
    res = F.polish_placement(engine)
    assert not res.applied
    assert res.reason == "measures_worse", res
    assert "USD" in res.detail
    for a, b in zip(before, (ir.inst_x, ir.inst_y, ir.inst_rot), strict=True):
        assert np.array_equal(a, b)


# ── gr504558 #2: oblique pads, bottom mirror and one rotation sense ───────


@pytest.mark.parametrize(
    ("layer", "inst_rot", "pad_rot"),
    [
        ("top", 0.0, 30.0),
        ("top", 20.0, 25.0),
        ("bottom", 0.0, 30.0),
        ("bottom", 20.0, 25.0),
    ],
)
def test_oblique_pad_outline_matches_the_pose_transform(
    layer: str, inst_rot: float, pad_rot: float
) -> None:
    """A rect pad's true outline is its corners rotated by the pad's own
    ``rot``, then mirrored (bottom) and rotated by the instance
    (:mod:`precis.pcb.pose`). The DRC shape function, the clearance body
    and the DRC ring edges must all draw exactly that outline: on a bottom
    part the land sits at instance - pad, and the ring edges used to turn
    the opposite way from the shape function on either side."""
    from shapely.geometry import Polygon

    from precis.pcb import clearance, drc, padplace, pose

    raw = [
        {
            "number": "1",
            "shape": "RECT",
            "x": 1.0,
            "y": 0.5,
            "w": 2.0,
            "h": 0.8,
            "rot": pad_rot,
            "layer": "F.Cu",
        }
    ]
    inst = {"x": 10.0, "y": 20.0, "rot": inst_rot, "refdes": "U1", "layer": layer}
    placed, _ = padplace.place_footprint_pads(raw, inst, layers=["F.Cu"])
    pad = placed[0]
    assert "oblique_rot" in pad
    corners = [
        pose.point_to_world(
            1.0 + cx, 0.5 + cy, 10.0, 20.0, inst_rot, mirrored=layer == "bottom"
        )
        for cx, cy in (
            pose.rotate_cw(px, py, pad_rot)
            for px, py in ((-1.0, -0.4), (1.0, -0.4), (1.0, 0.4), (-1.0, 0.4))
        )
    ]
    truth = Polygon(corners)
    shape = drc._copper_item_polygon({"ctype": "pad", **pad})
    assert shape is not None
    assert shape.symmetric_difference(truth).area == pytest.approx(0.0, abs=1e-3)
    body = clearance.body_of({"ctype": "pad", **pad})
    assert body is not None and body.core is not None
    assert body.core.symmetric_difference(truth).area == pytest.approx(0.0, abs=1e-3)
    ring = Polygon([(e[0], e[1]) for e in drc._pad_boundary_edges(pad)])
    assert ring.symmetric_difference(truth).area == pytest.approx(0.0, abs=1e-3)
