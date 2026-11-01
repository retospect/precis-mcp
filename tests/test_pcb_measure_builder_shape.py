"""Builder-shaped measure rows land in the stored ``pcb_measures`` shape.

``precis_geom.pcb``'s ``Board.near``/``apart``/``align``/``measure`` emit
``{metric: 'near', operands: ['C1', 'U1'], hard, weight, bound}``. Stored
verbatim, that row read ``near | gauge | pending``: ``resolve_measures``
skipped it and ``evaluate_measures`` never judged it, so a builder decap
measure never steered placement and never failed the ``measure`` check.
:func:`precis.pcb.rows.measure_row` converts it on every put path
(``_pcb_insert_measure``) and on the offline ``pcb.local`` place paths. A
measure is also a revision input: adding one stales a recorded check run.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest

import precis_geom.pcb as pcb
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import eyes, local
from precis.pcb.optimize import resolve_measures
from precis.pcb.rows import measure_row
from tests._pcb_outline import closed_outline
from tests.test_pcb_measure_check import _P2, _graph, _part

_INSTANCES = [{"instance": "C1"}, {"instance": "U1"}]


def _builder() -> tuple[pcb.Board, pcb.Part, pcb.Part]:
    b = pcb.Board("measure-rows")
    return b, b.part("C1"), b.part("U1")


def _near(*, hard: bool = False, bound: float = 2.0) -> dict[str, Any]:
    """C1 within ``bound`` mm of U1, as the builder emits it — ``hard=True``
    through ``b.measure``, the builder's only door to a hard measure."""
    b, c1, u1 = _builder()
    if hard:
        return b.measure("near", [c1, u1], bound=bound, hard=True)
    return b.near(c1, u1, within_mm=bound)


# ── unit: the conversion ───────────────────────────────────────────────


def test_builder_near_is_the_unconverted_shape() -> None:
    """The row ``put`` receives from a board script."""
    assert _near() == {
        "metric": "near",
        "operands": ["C1", "U1"],
        "hard": False,
        "weight": 1.0,
        "bound": 2.0,
    }


def test_near_converts_to_a_soft_proximity_bounded_by_its_goal() -> None:
    assert measure_row(_near()) == {
        "metric": "proximity",
        "operands": _INSTANCES,
        "goal": 2.0,
        "strength": "soft",
        "weight": 1.0,
        "direction": None,
        "reason": None,
        "meta": {},
    }
    # No direction needed: proximity's natural sense is "within" (≤ goal).
    assert eyes.measure_bound(None, "proximity") == "upper"


def test_hard_true_stores_strength_hard() -> None:
    assert measure_row(_near(hard=True))["strength"] == "hard"


def test_apart_converts_to_a_separation_floor() -> None:
    b, c1, u1 = _builder()
    row = measure_row(b.apart(c1, u1, at_least_mm=10.0))
    assert (row["metric"], row["goal"], row["strength"]) == ("separation", 10.0, "soft")
    assert row["operands"] == _INSTANCES
    assert eyes.measure_bound(None, "separation") == "lower"


def test_align_moves_its_axis_into_meta() -> None:
    b, c1, u1 = _builder()
    row = measure_row(b.align(c1, u1, axis="x"))
    assert (row["metric"], row["goal"], row["meta"]) == ("align", None, {"axis": "x"})
    assert "axis" not in row


def test_generic_measure_keeps_a_stored_metric_and_its_weight() -> None:
    b, c1, u1 = _builder()
    height = measure_row(b.measure("height", [c1], bound=3.0, hard=True, weight=2.0))
    assert (height["metric"], height["goal"], height["strength"], height["weight"]) == (
        "height",
        3.0,
        "hard",
        2.0,
    )
    # A goal alone is the goal (the board-script emitter's round trip).
    goal = measure_row(b.measure("proximity", [c1, u1], goal=8.0, hard=True))
    assert (goal["metric"], goal["goal"], goal["strength"]) == (
        "proximity",
        8.0,
        "hard",
    )


def test_bound_and_goal_must_agree() -> None:
    b, c1, u1 = _builder()
    assert (
        measure_row(b.measure("proximity", [c1, u1], bound=5, goal=5.0))["goal"] == 5.0
    )
    with pytest.raises(BadInput, match="disagree"):
        measure_row(b.measure("proximity", [c1, u1], bound=10.0, goal=5.0))


def test_string_operands_alone_mark_a_row_for_conversion() -> None:
    row = measure_row(
        {
            "metric": "proximity",
            "operands": ["C1", "U1"],
            "goal": 2,
            "strength": "hard",
            "reason": "bypass at the pin",
        }
    )
    assert row["operands"] == _INSTANCES
    assert (row["goal"], row["strength"], row["reason"]) == (
        2.0,
        "hard",
        "bypass at the pin",
    )


def test_an_explicit_strength_that_agrees_with_hard_is_kept() -> None:
    assert measure_row({**_near(), "strength": "gauge"})["strength"] == "gauge"


@pytest.mark.parametrize(
    "row",
    [
        {
            "metric": "separation",
            "operands": [{"role": "sensitive"}, {"role": "noisy"}],
            "goal": 10.0,
            "strength": "soft",
            "reason": "keep opamp off the FET",
        },
        {
            "metric": "align",
            "operands": [{"instance": "D3"}, {"point": [10.0, 5.0]}],
            "meta": {"axis": "x", "offset": [0.0, 1.5]},
            "goal": 0.05,
            "strength": "hard",
        },
        {
            "metric": "via_per_electrode",
            "strength": "invariant",
            "operands": [{"generator": "g1"}],
            "goal": 0,
            "direction": "max",
            "meta": {"generator": "g1"},
        },
        {"metric": "proximity"},
    ],
    ids=["role-separation", "align-datum", "generator-invariant", "bare"],
)
def test_a_stored_shape_row_returns_unchanged(row: dict[str, Any]) -> None:
    before = copy.deepcopy(row)
    assert measure_row(row) is row
    assert row == before


def test_an_unknown_builder_metric_refuses_naming_the_vocabulary() -> None:
    b, c1, u1 = _builder()
    with pytest.raises(BadInput) as exc:
        measure_row(b.measure("nearby", [c1, u1], bound=2.0))
    assert "'nearby'" in exc.value.cause
    assert {"near", "apart", "proximity", "separation", "align", "height"} <= set(
        exc.value.options or []
    )
    assert exc.value.next


@pytest.mark.parametrize(
    ("patch", "match"),
    [
        ({"hard": "yes"}, "hard must be true or false"),
        ({"hard": True, "strength": "soft"}, "contradicts strength"),
        ({"strength": "firm"}, "unknown strength"),
        ({"bound": -1.0}, "number of mm >= 0"),
        ({"bound": "2mm"}, "number of mm >= 0"),
        ({"operands": ["C1", 7]}, "neither a refdes"),
        ({"axis": "y", "meta": {"axis": "x"}}, "contradicts meta"),
    ],
)
def test_a_malformed_builder_row_refuses(patch: dict[str, Any], match: str) -> None:
    with pytest.raises(BadInput, match=match) as exc:
        measure_row({**_near(), **patch})
    assert exc.value.next


def test_the_converted_row_steers_and_is_judged_where_the_raw_one_was_not() -> None:
    raw = _near()
    row = measure_row(raw)
    far = _graph(C1=(15.0, 10.0), U1=(10.0, 10.0))
    # The bug: the builder row resolves to no placer spec and reads pending.
    assert resolve_measures([raw]) == ()
    assert eyes.evaluate_measures(far, [raw])[0]["verdict"] == "pending"

    (spec,) = resolve_measures([row])
    assert (spec.refdes_a, spec.refdes_b, spec.bound, spec.goal_mm) == (
        "C1",
        "U1",
        "upper",
        2.0,
    )
    assert (spec.hard, spec.weight) == (False, 1.0)
    judged = eyes.evaluate_measures(far, [row])[0]
    assert (judged["verdict"], judged["value"], judged["excess"]) == (
        "VIOLATED",
        5.0,
        3.0,
    )
    close = _graph(C1=(10.0, 11.8), U1=(10.0, 10.0))
    assert eyes.evaluate_measures(close, [row])[0]["verdict"] == "ok"


# ── store: every put path converts ─────────────────────────────────────


@pytest.fixture
def handler(store: Any) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _board(c1_xy: tuple[float, float], *measures: dict[str, Any]) -> dict[str, Any]:
    """U1 at (10, 10) and its bypass C1 at ``c1_xy`` — the
    ``test_pcb_measure_check`` board — with ``measures`` as given."""
    board: dict[str, Any] = {
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
    }
    if measures:
        board["measures"] = list(measures)
    return board


def _ref_id(store: Any, slug: str) -> int:
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_put_stores_a_builder_near_as_a_soft_proximity(
    handler: PcbHandler, store: Any
) -> None:
    handler.put(id="mb-near", args=_board((10.0, 11.8), _near()))
    (row,) = store.pcb_measures_list(_ref_id(store, "mb-near"))
    assert (row["metric"], row["goal"], row["strength"], row["weight"]) == (
        "proximity",
        2.0,
        "soft",
        1.0,
    )
    assert row["operands"] == _INSTANCES
    assert (row["direction"], row["meta"]) == (None, {})
    body = handler.get(id="mb-near", view="measures").body
    assert "proximity\tsoft\t2\t1.8\tok" in body, body


def test_put_hard_builder_measure_fails_the_check(
    handler: PcbHandler, store: Any
) -> None:
    handler.put(id="mb-hard", args=_board((15.0, 10.0), _near(hard=True)))
    (row,) = store.pcb_measures_list(_ref_id(store, "mb-hard"))
    assert (row["metric"], row["strength"]) == ("proximity", "hard")
    body = handler.get(id="mb-hard", view="check").body
    assert "\terror\tmeasure\tmeasure C1–U1\t-3.000\t" in body, body


def test_put_builder_apart_stores_a_separation(handler: PcbHandler, store: Any) -> None:
    b, c1, u1 = _builder()
    handler.put(
        id="mb-apart", args=_board((15.0, 10.0), b.apart(c1, u1, at_least_mm=10.0))
    )
    (row,) = store.pcb_measures_list(_ref_id(store, "mb-apart"))
    assert (row["metric"], row["goal"], row["strength"]) == ("separation", 10.0, "soft")
    assert row["operands"] == _INSTANCES
    body = handler.get(id="mb-apart", view="measures").body
    assert "separation\tsoft\t10\t5\tVIOLATED" in body, body


def test_put_keeps_a_stored_shape_row(handler: PcbHandler, store: Any) -> None:
    authored = {
        "metric": "proximity",
        "operands": _INSTANCES,
        "goal": 2.5,
        "strength": "gauge",
        "direction": "max",
        "weight": 0.5,
        "reason": "bypass at the pin",
        "meta": {"note": "authored"},
    }
    handler.put(id="mb-stored", args=_board((10.0, 11.8), dict(authored)))
    (row,) = store.pcb_measures_list(_ref_id(store, "mb-stored"))
    assert {k: row[k] for k in authored} == authored


def test_put_refuses_an_unknown_builder_metric_and_stores_nothing(
    handler: PcbHandler, store: Any
) -> None:
    b, c1, u1 = _builder()
    with pytest.raises(BadInput) as exc:
        handler.put(
            id="mb-unknown",
            args=_board((10.0, 11.8), b.measure("nearby", [c1, u1], bound=2.0)),
        )
    assert "'nearby'" in exc.value.cause
    assert "proximity" in (exc.value.options or [])
    # The refusal rolled the whole put back: no design was created.
    assert store.get_ref(kind="pcb", id="mb-unknown") is None


def test_adding_a_measure_changes_the_revision_and_stales_the_recorded_run(
    handler: PcbHandler, store: Any
) -> None:
    handler.put(id="mb-rev", args=_board((15.0, 10.0)))
    handler.put(id="mb-rev", args={"op": "check"})
    ref_id = _ref_id(store, "mb-rev")
    rev = store.pcb_design_revision(ref_id)
    assert "reused run" in handler.get(id="mb-rev", view="drc").body

    handler.put(id="mb-rev", args={"measures": [_near(hard=True)]})
    assert store.pcb_design_revision(ref_id) != rev
    body = handler.get(id="mb-rev", view="drc").body
    assert "reused run" not in body
    assert "\terror\tmeasure\tmeasure C1–U1\t-3.000\t" in body, body


def test_the_snapped_stamp_leaves_the_revision_alone(
    handler: PcbHandler, store: Any
) -> None:
    """``meta.snapped`` is the place job's mark on an ``ok`` align — no
    check verdict reads it, so it must not stale a recorded run."""
    align = {
        "metric": "align",
        "operands": _INSTANCES,
        "meta": {"axis": "x"},
        "goal": 0.05,
        "strength": "hard",
    }
    handler.put(id="mb-snap", args=_board((10.0, 11.8), align))
    ref_id = _ref_id(store, "mb-snap")
    rev = store.pcb_design_revision(ref_id)
    (row,) = store.pcb_measures_list(ref_id)
    assert store.pcb_measures_mark_snapped(ref_id, [row["measure_id"]]) == 1
    assert store.pcb_measures_list(ref_id)[0]["meta"]["snapped"] is True
    assert store.pcb_design_revision(ref_id) == rev


# ── offline: pcb.local prices the same rows ────────────────────────────


def _offline_board(metric: str = "near") -> pcb.Board:
    """Two one-pad parts 12 mm apart on a 20 x 10 board (the
    ``test_pcb_currents`` board) and a 2 mm builder measure between them."""
    board = pcb.Board("measure-offline", outline=pcb.rect(20, 10))
    footprint = board.footprint(
        pcb.Footprint("single", [pcb.pad("1", "rect", 0, 0, w=1, h=1)])
    )
    a = board.part("A", footprint=footprint).at(4, 5)
    b = board.part("B", footprint=footprint).at(16, 5)
    board.net("GND", cls="ground", current=0.01).tie(
        a["1"].etype(
            "power_in", max_current_a=0.5, max_voltage_v=5.0, assume="gnd pin"
        ),
        b["1"].etype(
            "power_in", max_current_a=0.5, max_voltage_v=5.0, assume="gnd pin"
        ),
    )
    board.measure(metric, [a, b], bound=2.0)
    return board


def test_offline_place_prices_the_builder_measure() -> None:
    (raw,) = _offline_board().to_batch()["measures"]
    assert local._design(_offline_board(), None).measures == [measure_row(raw)]
    (spec,) = local.place(_offline_board(), iters=1).measures
    assert (spec.refdes_a, spec.refdes_b, spec.bound, spec.goal_mm) == (
        "A",
        "B",
        "upper",
        2.0,
    )


def test_offline_topo_place_prices_the_builder_measure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The topo design carries the measure and the anneal's objective
    prices it — captured at the kernel call, before the anneal runs."""
    from precis.pcb import topoplace as tp

    class _Captured(Exception):
        pass

    seen: dict[str, Any] = {}

    def _spy(d: Any, **kw: Any) -> Any:
        seen["td"], seen["objective"] = d, kw["objective"]
        raise _Captured

    monkeypatch.setattr(tp, "topo_place", _spy)
    with pytest.raises(_Captured):
        local.topo_place(_offline_board(), out=str(tmp_path / "topo"))
    td = seen["td"]
    (m,) = td.measures
    assert (td.parts[m.ia], td.parts[m.ib]) == ("A", "B")
    assert (m.bound, m.goal_mm, m.hard) == ("upper", 2.0, False)
    assert seen["objective"].measures > 0


def test_offline_place_refuses_an_unknown_builder_metric() -> None:
    with pytest.raises(BadInput, match="'nearby'"):
        local.place(_offline_board("nearby"), iters=1)
