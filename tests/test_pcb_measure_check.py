"""The ``measure`` check rule (Reto 2026-11-01: "decap and crystal, within
some limit, make it fail drc").

A VIOLATED ``hard`` placement measure is an ``error`` on ``view='check'``
and ``view='drc'``, a ``soft`` one a ``warn``; ``gauge``, ``invariant`` and
``pending`` rows add nothing. The limit is the measure's own goal. The
unit half drives :func:`precis.pcb.measure_check.measure_findings` over a
plain graph dict; the board half puts a two-part design through the
handler and reads both check views.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb import check_run
from precis.pcb.drc import DrcFinding, finding_key
from precis.pcb.measure_check import measure_findings
from tests._pcb_outline import closed_outline

# ── unit: the rule over a graph dict ───────────────────────────────────


def _graph(**xy: tuple[float, float] | None) -> dict[str, Any]:
    roles = {"U1": ["sensitive"], "U3": ["sensitive"], "Q1": ["noisy"]}
    return {
        "instances": [
            {
                "refdes": r,
                "x": None if p is None else p[0],
                "y": None if p is None else p[1],
                "roles": roles.get(r, []),
            }
            for r, p in xy.items()
        ],
        "nets": [],
        "unconnected": [],
    }


def _near(
    strength: str | None = "hard", goal: float = 2.0, **extra: Any
) -> dict[str, Any]:
    """The decap idiom: C1 within ``goal`` mm of U1."""
    return {
        "metric": "proximity",
        "operands": [{"instance": "C1"}, {"instance": "U1"}],
        "goal": goal,
        "strength": strength,
        "reason": "bypass at the pin",
        **extra,
    }


def test_hard_proximity_out_of_limit_is_one_error_naming_both_parts() -> None:
    (f,) = measure_findings(
        _graph(C1=(15.0, 10.0), U1=(10.0, 10.0)), [_near(measure_id=7)]
    )
    assert (f.rule, f.severity, f.where) == ("measure", "error", "measure C1–U1")
    assert f.objects == ({"refdes": "C1"}, {"refdes": "U1"})
    assert f.margin_mm == pytest.approx(-3.0)
    assert f.detail == (
        "hard proximity (measure #7): centre gap 5 mm, limit ≤ 2 mm, "
        "residual 3 mm — bypass at the pin"
    )


def test_within_the_limit_adds_nothing() -> None:
    assert measure_findings(_graph(C1=(10.0, 11.8), U1=(10.0, 10.0)), [_near()]) == []
    # The limit itself holds: ≤ goal is ok.
    assert measure_findings(_graph(C1=(12.0, 10.0), U1=(10.0, 10.0)), [_near()]) == []


def test_soft_proximity_out_of_limit_is_one_warn() -> None:
    (f,) = measure_findings(_graph(C1=(15.0, 10.0), U1=(10.0, 10.0)), [_near("soft")])
    assert (f.rule, f.severity, f.where) == ("measure", "warn", "measure C1–U1")


@pytest.mark.parametrize("strength", ["gauge", "invariant", None])
def test_gauge_invariant_and_unset_strength_add_nothing(strength: str | None) -> None:
    """A gauge is a ruler and an unset strength stores as one; invariant
    rows belong to the always-valid gate (``invariant:<metric>``)."""
    graph = _graph(C1=(15.0, 10.0), U1=(10.0, 10.0))
    assert measure_findings(graph, [_near(strength)]) == []


def test_pending_rows_add_nothing() -> None:
    """An unresolved operand (unplaced U1) or a goal-less row is not a
    violation."""
    assert measure_findings(_graph(C1=(15.0, 10.0), U1=None), [_near()]) == []
    graph = _graph(C1=(15.0, 10.0), U1=(10.0, 10.0))
    assert measure_findings(graph, [{**_near(), "goal": None}]) == []


def test_height_and_connectivity_metrics_are_out_of_scope() -> None:
    """``height`` is not a placement pair (a ``view='measures'`` verdict
    only); the connectivity metrics evaluate ``pending``."""
    graph = _graph(C1=(15.0, 10.0), U1=(10.0, 10.0))
    graph["instances"][0]["height_mm"] = 5.0
    rows: list[dict[str, Any]] = [
        {
            "metric": "height",
            "operands": [{"instance": "C1"}],
            "goal": 3.0,
            "strength": "hard",
        },
        {"metric": "supply_path", "operands": [], "strength": "hard"},
    ]
    assert measure_findings(graph, rows) == []


def test_role_separation_names_the_binding_pair() -> None:
    """A role measure spans several parts; the finding names the pair the
    evaluator judged binding (the closest one for a separation)."""
    m = {
        "metric": "separation",
        "operands": [{"role": "sensitive"}, {"role": "noisy"}],
        "goal": 10.0,
        "strength": "soft",
    }
    graph = _graph(U1=(0.0, 0.0), U3=(20.0, 0.0), Q1=(24.0, 0.0))
    (f,) = measure_findings(graph, [m])
    assert (f.severity, f.where) == ("warn", "measure U3–Q1")
    assert f.objects == ({"refdes": "U3"}, {"refdes": "Q1"})
    assert "centre gap 4 mm, limit ≥ 10 mm, residual 6 mm" in f.detail


def test_target_direction_reports_the_band() -> None:
    m = {**_near(goal=10.0), "direction": "target"}
    (f,) = measure_findings(_graph(C1=(25.0, 10.0), U1=(10.0, 10.0)), [m])
    assert "centre gap 15 mm, limit 10 ± 1 mm, residual 4 mm" in f.detail


def test_align_to_a_datum_names_the_part_and_the_point() -> None:
    m = {
        "metric": "align",
        "operands": [{"instance": "D3"}, {"point": [10.0, 5.0]}],
        "meta": {"axis": "x"},
        "goal": 0.05,
        "strength": "hard",
    }
    (f,) = measure_findings(_graph(D3=(10.3, 9.0)), [m])
    assert (f.severity, f.where) == ("error", "measure D3–(10, 5)")
    assert f.objects == ({"refdes": "D3"},)
    assert "x offset error 0.3 mm, limit ≤ 0.05 mm, residual 0.25 mm" in f.detail


def test_finding_identity_is_the_pair_not_the_margin() -> None:
    """A nudge that changes the residual is the same finding (the delta
    reads it ``still``, worse or better), never gone + new."""
    keys = {
        finding_key(f)
        for x in (15.0, 16.0)
        for f in measure_findings(_graph(C1=(x, 10.0), U1=(10.0, 10.0)), [_near()])
    }
    assert len(keys) == 1


# ── board: both check views over a stored design ───────────────────────

#: A two-pad land small enough that C1 can sit inside 2 mm of U1 without
#: a courtyard overlap (the judged put refuses one).
_P2 = {
    "name": "P2",
    "pads": [
        {"number": "1", "shape": "rect", "x": -0.5, "y": 0.0, "w": 0.5, "h": 0.5},
        {"number": "2", "shape": "rect", "x": 0.5, "y": 0.0, "w": 0.5, "h": 0.5},
    ],
    "pin_map": {"1": "1", "2": "2"},
}


@pytest.fixture
def pcb(store: Any) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _part(refdes: str, xy: tuple[float, float]) -> dict[str, Any]:
    return {
        "refdes": refdes,
        "label": "p2",
        "footprint": "P2",
        "x": xy[0],
        "y": xy[1],
        "pins": [{"name": "1"}, {"name": "2"}],
    }


def _board(c1_xy: tuple[float, float], strength: str = "hard") -> dict[str, Any]:
    """U1 at (10, 10), its bypass C1 at ``c1_xy``, one 2 mm proximity."""
    return {
        "footprints": [_P2],
        "components": [_part("U1", (10.0, 10.0)), _part("C1", c1_xy)],
        "nets": [{"name": "VDD"}, {"name": "GND"}],
        "connections": [
            {"net": "VDD", "refdes": "U1", "pin": "1"},
            {"net": "VDD", "refdes": "C1", "pin": "1"},
            {"net": "GND", "refdes": "U1", "pin": "2"},
            {"net": "GND", "refdes": "C1", "pin": "2"},
        ],
        "features": [closed_outline(0.0, 0.0, 30.0, 30.0)],
        "measures": [_near(strength)],
    }


def _measure_rows(store: Any, slug: str) -> dict[str, list[DrcFinding]]:
    """The ``measure`` findings of the run each view computes: ``drc`` is
    :func:`check_run.run`'s preset, ``check`` adds ERC at the derived
    phase — what the two get views run on an unrecorded board."""
    ref = store.get_ref(kind="pcb", id=slug)
    design = store.pcb_load(ref.id)
    runs = {
        "drc": check_run.run(store, ref.id, design),
        "check": check_run.run(
            store,
            ref.id,
            design,
            eval_phase=check_run.design_phase(design),
            eval_phase_claimed=False,
            erc=True,
        ),
    }
    out: dict[str, list[DrcFinding]] = {}
    for view, ran in runs.items():
        assert ran is not None, view
        out[view] = [f for f in ran.findings if f.rule == "measure"]
    return out


def test_board_hard_measure_5mm_out_fails_check_and_drc(
    pcb: PcbHandler, store: Any
) -> None:
    pcb.put(id="mc-hard", args=_board((15.0, 10.0)))
    for view, rows in _measure_rows(store, "mc-hard").items():
        assert len(rows) == 1, (view, rows)
        f = rows[0]
        assert (f.severity, f.where) == ("error", "measure C1–U1"), view
        assert {o["refdes"] for o in f.objects} == {"C1", "U1"}
        assert "(measure #" in f.detail
        assert "centre gap 5 mm, limit ≤ 2 mm, residual 3 mm" in f.detail
    for view in ("check", "drc"):
        body = pcb.get(id="mc-hard", view=view).body
        assert "\terror\tmeasure\tmeasure C1–U1\t-3.000\t" in body, body


def test_board_within_the_limit_has_no_measure_finding(
    pcb: PcbHandler, store: Any
) -> None:
    pcb.put(id="mc-near", args=_board((10.0, 11.8)))
    assert _measure_rows(store, "mc-near") == {"drc": [], "check": []}
    for view in ("check", "drc"):
        assert "\tmeasure\t" not in pcb.get(id="mc-near", view=view).body


def test_board_soft_measure_out_of_limit_is_one_warn(
    pcb: PcbHandler, store: Any
) -> None:
    pcb.put(id="mc-soft", args=_board((15.0, 10.0), strength="soft"))
    want = [("warn", "measure C1–U1")]
    for view, rows in _measure_rows(store, "mc-soft").items():
        assert [(f.severity, f.where) for f in rows] == want, view
    body = pcb.get(id="mc-soft", view="check").body
    assert "\twarn\tmeasure\tmeasure C1–U1\t" in body, body


def test_board_gauge_measure_adds_nothing(pcb: PcbHandler, store: Any) -> None:
    pcb.put(id="mc-gauge", args=_board((15.0, 10.0), strength="gauge"))
    assert _measure_rows(store, "mc-gauge") == {"drc": [], "check": []}
    # The measures view still reports the violated ruler.
    assert "VIOLATED" in pcb.get(id="mc-gauge", view="measures").body


def test_claimed_netlist_phase_skips_the_measure_rule(pcb: PcbHandler) -> None:
    """Measures judge placement; a claimed ``netlist`` eval runs only the
    geometry-free rules."""
    pcb.put(id="mc-netlist", args=_board((15.0, 10.0)))
    body = pcb.get(id="mc-netlist", view="check", args={"phase": "netlist"}).body
    assert "\tmeasure\t" not in body


def test_recorded_run_keeps_the_finding_for_reuse(pcb: PcbHandler) -> None:
    """``op='check'`` persists the finding; the unchanged board's
    ``view='drc'`` serves that run back with it (``measure`` is not an
    ERC rule, so the ERC-less preset keeps it)."""
    pcb.put(id="mc-rec", args=_board((15.0, 10.0)))
    pcb.put(id="mc-rec", args={"op": "check"})
    body = pcb.get(id="mc-rec", view="drc").body
    assert "reused run" in body
    assert "\terror\tmeasure\tmeasure C1–U1\t-3.000\t" in body, body
