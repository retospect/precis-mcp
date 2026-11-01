"""Measures between pins (topo-route-review A7).

``proximity``/``separation`` read part origins, so a 2 mm decap bound
beside a 5 mm IC can never hold: taz-leaf-t's 13 ``measure`` errors. A pin
operand (``{'instance': 'U1', 'pin': 'VDD'}``) is measured from that pin's
pad centres — by name, else by pad number — and the three readers (the
eyes verdict behind the ``measure`` finding, the legacy anneal, the topo
proxy) share one resolver, :func:`precis.pcb.ir.pin_pad_offsets`.

The board: a 5 mm IC ``U1`` (VDD on pads 1 and 3, 2.5 mm either side of
its origin) and a 0402-ish ``C1`` 4 mm right of it, pin 1 one millimetre
from U1's pad 3.
"""

from __future__ import annotations

import itertools
from typing import Any

import numpy as np
import pytest

import precis_geom.pcb as pcb
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import check_run, eyes, local
from precis.pcb import ir as pcb_ir
from precis.pcb.board_script import _emit_measure
from precis.pcb.measure_check import measure_findings
from precis.pcb.optimize import (
    _measure_pair_usd,
    resolve_measure_pins,
    resolve_measures,
)
from precis.pcb.rows import measure_row
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.proxy import _measure_violation, _pose_points, local_to_world
from tests._pcb_outline import closed_outline

_Q4: dict[str, Any] = {
    "name": "Q4",
    "pads": [
        {"number": "1", "shape": "rect", "x": -2.5, "y": 0.0, "w": 0.8, "h": 0.3},
        {"number": "2", "shape": "rect", "x": 0.0, "y": 2.5, "w": 0.3, "h": 0.8},
        {"number": "3", "shape": "rect", "x": 2.5, "y": 0.0, "w": 0.8, "h": 0.3},
        {"number": "4", "shape": "rect", "x": 0.0, "y": -2.5, "w": 0.3, "h": 0.8},
    ],
    "pin_map": {"1": "VDD", "2": "DEC", "3": "VDD", "4": "GND"},
}
_P2: dict[str, Any] = {
    "name": "P2",
    "pads": [
        {"number": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.5, "h": 0.5},
        {"number": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5},
    ],
    "pin_map": {"1": "1", "2": "2"},
}


def _near(a: Any, b: Any, goal: float = 2.0, strength: str = "hard") -> dict[str, Any]:
    return {
        "metric": "proximity",
        "operands": [a, b],
        "goal": goal,
        "strength": strength,
    }


#: C1 pin 1 ↔ U1's VDD pins (nearest: pad 3, 1.0 mm) — ok.
_PIN = _near({"instance": "C1", "pin": "1"}, {"instance": "U1", "pin": "VDD"})
#: The same intent between origins (4.0 mm) — VIOLATED.
_ORIGIN = _near({"instance": "C1"}, {"instance": "U1"})
#: U1 pad NUMBER 1, the far VDD pad (6.0 mm from C1 pin 1) — VIOLATED.
_PAD1 = _near({"instance": "C1", "pin": "1"}, {"instance": "U1", "pin": "1"})
#: A pin no pad answers — pending, never steers.
_NOPE = _near({"instance": "C1", "pin": "1"}, {"instance": "U1", "pin": "NOPE"})


def _batch(*measures: dict[str, Any], c1_x: float = 19.0) -> dict[str, Any]:
    return {
        "footprints": [_Q4, _P2],
        "components": [
            {
                "refdes": "U1",
                "label": "ic",
                "footprint": "Q4",
                "x": 15.0,
                "y": 15.0,
                "pins": [{"name": "VDD"}, {"name": "DEC"}, {"name": "GND"}],
            },
            {
                "refdes": "C1",
                "label": "cap",
                "footprint": "P2",
                "x": c1_x,
                "y": 15.0,
                "pins": [{"name": "1"}, {"name": "2"}],
            },
        ],
        "nets": [{"name": "VDD"}, {"name": "GND"}],
        "connections": [
            {"net": "VDD", "refdes": "U1", "pin": "VDD"},
            {"net": "VDD", "refdes": "C1", "pin": "1"},
            {"net": "GND", "refdes": "U1", "pin": "GND"},
            {"net": "GND", "refdes": "C1", "pin": "2"},
        ],
        "features": [closed_outline(0.0, 0.0, 30.0, 30.0)],
        "measures": list(measures),
    }


# ── the eyes over a stub resolver (no IR) ─────────────────────────────


def _graph() -> dict[str, Any]:
    return {
        "instances": [
            {"refdes": "U1", "x": 15.0, "y": 15.0},
            {"refdes": "C1", "x": 19.0, "y": 15.0},
        ],
        "nets": [],
        "unconnected": [],
    }


_PADS = {
    ("U1", "VDD"): [(12.5, 15.0), (17.5, 15.0)],
    ("U1", "1"): [(12.5, 15.0)],
    ("C1", "1"): [(18.5, 15.0)],
}


def _stub(refdes: str, pin: str) -> list[tuple[float, float]]:
    return _PADS.get((refdes, pin), [])


def test_a_pin_pair_is_measured_between_pads() -> None:
    pin, origin, pad1, nope = eyes.evaluate_measures(
        _graph(), [_PIN, _ORIGIN, _PAD1, _NOPE], pin_points=_stub
    )
    assert (pin["verdict"], pin["value"], pin["pins"]) == ("ok", 1.0, True)
    assert pin["pair"] == ("C1.1", "U1.VDD")
    assert pin["parts"] == ("C1", "U1")
    assert (origin["verdict"], origin["value"]) == ("VIOLATED", 4.0)
    assert "pins" not in origin
    assert (pad1["verdict"], pad1["value"]) == ("VIOLATED", 6.0)
    assert nope["verdict"] == "pending"


def test_without_a_resolver_a_pin_measure_is_pending() -> None:
    (row,) = eyes.evaluate_measures(_graph(), [_PIN])
    assert row["verdict"] == "pending"
    assert eyes.measures_name_pins([_PIN]) and not eyes.measures_name_pins([_ORIGIN])


def test_the_finding_names_the_pins_and_the_pad_gap() -> None:
    (f,) = measure_findings(
        _graph(), [_PIN, {**_PAD1, "reason": "far"}], pin_points=_stub
    )
    assert (f.severity, f.where) == ("error", "measure C1.1–U1.1")
    assert f.objects == ({"refdes": "C1"}, {"refdes": "U1"})
    assert "pad-centre gap 6 mm, limit ≤ 2 mm, residual 4 mm — far" in f.detail
    assert measure_findings(_graph(), [_NOPE], pin_points=_stub) == []


# ── the proxy's scalar transform ───────────────────────────────────────


@pytest.mark.parametrize(("rot", "side"), itertools.product(range(4), (0.0, 1.0)))
def test_pose_points_match_local_to_world(rot: int, side: float) -> None:
    pts = ((0.7, -0.2), (-2.5, 1.25))
    row = np.array([3.0, -4.0, float(rot), side])
    np.testing.assert_allclose(
        np.array(_pose_points(pts, row)), local_to_world(np.array(pts), row)
    )


# ── the builder, the stored row and the script writer ─────────────────


def test_builder_pin_operands_round_trip_to_the_stored_row() -> None:
    b = pcb.Board("pins")
    c1, u1 = b.part("C1"), b.part("U1")
    raw = b.near(c1["1"], u1[30], 2.0, hard=True)
    assert raw["operands"] == [
        {"instance": "C1", "pin": "1"},
        {"instance": "U1", "pin": "30"},
    ]
    row = measure_row(raw)
    assert (row["metric"], row["strength"], row["goal"]) == ("proximity", "hard", 2.0)
    assert row["operands"] == raw["operands"]
    (spec,) = resolve_measures([row])
    assert (spec.pin_a, spec.pin_b) == ("1", "30")
    line = _emit_measure(row, {"C1": "parts['C1']", "U1": "parts['U1']"})
    assert line.startswith(
        "b.measure('proximity', [parts['C1']['1'], parts['U1']['30']]"
    )


def _unknown_pins(tie_by_number: bool) -> list[tuple[str | None, str | None]]:
    b = pcb.Board("pin-check")
    q4 = pcb.Footprint(
        "Q4",
        pads=[pcb.pad(p["number"], "rect", p["x"], p["y"]) for p in _Q4["pads"]],
        pin_map=dict(_Q4["pin_map"]),
    )
    u1 = b.part("U1", footprint=q4)
    c1 = b.part("C1", footprint=pcb.Footprint("P2", pads=[pcb.pad("1", "rect", 0, 0)]))
    b.net("VDD", current=0.001).tie(
        u1["VDD"].etype("input", fmax_hz=0.0, assume="supply"),
        c1["1"].etype("passive", fmax_hz=0.0, assume="cap"),
    )
    b.near(c1["1"], u1[3], 2.0, hard=True)
    if tie_by_number:
        b.net("GND", current=0.001).tie(u1[4].etype("input", fmax_hz=0.0, assume="x"))
    return [(f.refdes, f.pin) for f in b.check().findings if f.code == "pin.unknown"]


def test_a_measure_pad_number_is_not_an_unknown_pin() -> None:
    """``u1[3]`` in a measure names pad 3; the builder's offline
    ``pin.unknown`` must not call it a typo. Wired by number it still is
    one: the server joins netlist pins by name."""
    assert _unknown_pins(tie_by_number=False) == []
    assert _unknown_pins(tie_by_number=True) == [("U1", "4")]


@pytest.mark.parametrize(
    "op",
    [{"instance": "C1", "pin": ""}, {"instance": "C1", "pin": True}, {"pin": "1"}],
)
def test_a_malformed_pin_operand_refuses(op: dict[str, Any]) -> None:
    with pytest.raises(BadInput, match="pin operand") as exc:
        measure_row({"metric": "near", "operands": [op, "U1"], "bound": 2.0})
    assert exc.value.next


# ── the three readers over one real IR (DB-free, the local path) ───────


@pytest.fixture
def board() -> tuple[Any, pcb_ir.PcbIR, dict[str, dict[str, Any]]]:
    design = local._design(_batch(_PIN, _ORIGIN, _PAD1, _NOPE), None)
    ir, fps, _specs = local._prepare(design)
    return design, ir, fps


def test_the_resolver_reads_names_then_pad_numbers(board) -> None:
    _design, ir, _fps = board
    u1 = [str(r) for r in ir.instance_refdes].index("U1")
    assert sorted(pcb_ir.pin_pad_offsets(ir, u1, "VDD")) == [(-2.5, 0.0), (2.5, 0.0)]
    assert pcb_ir.pin_pad_offsets(ir, u1, "1") == ((-2.5, 0.0),)
    assert pcb_ir.pin_pad_offsets(ir, u1, "NOPE") == ()
    points = pcb_ir.pin_pad_points(ir)
    assert sorted(points("U1", "VDD")) == [(12.5, 15.0), (17.5, 15.0)]
    assert points("C1", "1") == ((18.5, 15.0),)


def test_the_eyes_verdicts_over_the_real_pads(board) -> None:
    design, ir, _fps = board
    rows = eyes.evaluate_measures(
        design.graph, design.measures, pin_points=pcb_ir.pin_pad_points(ir)
    )
    assert [(r["verdict"], r["value"]) for r in rows] == [
        ("ok", 1.0),
        ("VIOLATED", 4.0),
        ("VIOLATED", 6.0),
        ("pending", None),
    ]


def test_the_legacy_anneal_prices_the_pads(board) -> None:
    design, ir, _fps = board
    inst = {str(r): i for i, r in enumerate(ir.instance_refdes)}
    specs = resolve_measure_pins(
        resolve_measures(design.measures),
        lambda r, p: pcb_ir.pin_pad_offsets(ir, inst[r], p),
    )
    # The pin-less NOPE measure drops: pending in the eyes, so it must not steer.
    assert len(specs) == 3
    usd = [_measure_pair_usd(ir, inst[s.refdes_a], inst[s.refdes_b], s) for s in specs]
    assert usd[0] == 0.0 and usd[1] > 0.0 and usd[2] > usd[1]


def test_the_topo_proxy_prices_the_pads(board) -> None:
    design, ir, fps = board
    td = design_from_ir(ir, fps, features=design.features, measures=design.measures)
    assert len(td.measures) == 3
    pin, origin, pad1 = td.measures
    assert pin.pts_a == ((-0.5, 0.0),)
    assert sorted(pin.pts_b) == [(-2.5, 0.0), (2.5, 0.0)]
    assert origin.pts_a == origin.pts_b == ()
    pose = td.pose0
    viol = [_measure_violation(m, pose[m.ia], pose[m.ib]) for m in td.measures]
    assert viol[0] == 0.0
    assert viol[1] == pytest.approx(10.0 * 2.0)  # hard ×10, 4 mm vs 2 mm
    assert viol[2] == pytest.approx(10.0 * 4.0)  # pad 1 is 6 mm away


# ── a stored board: the check and the measures view ────────────────────


@pytest.fixture
def handler(store: Any) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def test_stored_board_check_and_view_read_the_pads(handler: PcbHandler, store) -> None:
    handler.put(id="pin-measures", args=_batch(_PIN, _ORIGIN))
    ref = store.get_ref(kind="pcb", id="pin-measures")
    run = check_run.run(store, ref.id, store.pcb_load(ref.id))
    assert run is not None
    found = [(f.severity, f.where) for f in run.findings if f.rule == "measure"]
    assert found == [("error", "measure C1–U1")]
    body = handler.get(id="pin-measures", view="measures").body
    rows = [ln for ln in body.splitlines() if "proximity" in ln]
    assert len(rows) == 2, body
    # The pin row reads ok at 1 mm; the origin row VIOLATED at 4 mm.
    assert sorted("VIOLATED" in ln for ln in rows) == [False, True], body
    assert any("\t1\tok\t" in ln for ln in rows), body
