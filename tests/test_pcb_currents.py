"""gr520545: placement and routing require authored net currents."""

from __future__ import annotations

import pytest

import precis_geom.pcb as pcb
from precis.errors import BadInput
from precis.pcb import local
from precis.pcb.currents import require_net_currents
from precis.pcb.rows import net_row
from precis.pcb.session import require_layout_ready
from precis.pcb.topoplace import design_from_ir
from precis.pcb.topoplace.realise import route_design
from tests._pcb_outline import closed_outline


def _board(current: float | None) -> pcb.Board:
    board = pcb.Board("currents", outline=pcb.rect(20, 10))
    footprint = board.footprint(
        pcb.Footprint("single", [pcb.pad("1", "rect", 0, 0, w=1, h=1)])
    )
    a = board.part("A", footprint=footprint).at(4, 5)
    b = board.part("B", footprint=footprint).at(16, 5)
    board.net("GND", cls="ground", current=current).tie(
        a["1"].etype(
            "power_in", max_current_a=0.5, max_voltage_v=5.0, assume="gnd pin"
        ),
        b["1"].etype(
            "power_in", max_current_a=0.5, max_voltage_v=5.0, assume="gnd pin"
        ),
    )
    return board


@pytest.mark.parametrize("placer", [local.place, local.topo_place])
def test_place_refuses_missing_ground_current(placer) -> None:
    with pytest.raises(BadInput) as exc:
        placer(_board(None))
    assert "GND" in str(exc.value.next)
    assert "1 net(s)" in str(exc.value.next)
    assert "b.net('GND', current=0.01)" in str(exc.value.next)


@pytest.mark.parametrize("current", [0.0, 0.001])
def test_place_proceeds_with_every_current_explicit(current: float) -> None:
    placement = local.place(_board(current), iters=1)
    assert set(placement.poses) == {"A", "B"}


@pytest.mark.parametrize("placer", [local.place, local.topo_place])
@pytest.mark.parametrize("x,y,axis", [(-1, 0, "x"), (0, -1, "y"), (-1, -1, "x and y")])
def test_local_place_refuses_negative_outline_before_solving(
    placer, x: int, y: int, axis: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """gr543693: both offline placers obey the handler's datum gate."""
    batch = _board(0).to_batch()
    batch["features"] = [closed_outline(x, y, 20, 10)]

    def no_solve(*args, **kwargs):
        pytest.fail("readiness must refuse before constructing the solver IR")

    monkeypatch.setattr(local, "_build_ir", no_solve)
    with pytest.raises(BadInput, match=f"{axis} below 0") as exc:
        placer(batch)
    assert "minimum at" in str(exc.value)
    assert "lands on (0,0)" in str(exc.value.next)
    assert "'ftype':'outline'" in str(exc.value.next)


@pytest.mark.parametrize("x,y", [(0, 0), (1, 2)])
def test_layout_readiness_accepts_positive_outline_and_explicit_zero(
    x: int, y: int
) -> None:
    require_layout_ready(
        {"nets": [{"name": "ZERO", "est_current_a": 0}]},
        [closed_outline(x, y, 20, 10)],
    )


def test_route_design_refuses_unset_ir_current() -> None:
    design = local._design(_board(None), None)
    ir = local._build_ir(design)
    topo = design_from_ir(ir, local._footprints(design, ir), route_planes=True)
    with pytest.raises(BadInput) as exc:
        route_design(topo, ir)
    assert "GND" in str(exc.value.next)


def test_missing_current_hint_caps_names_and_reports_total() -> None:
    with pytest.raises(BadInput) as exc:
        require_net_currents((f"NET_{i:02d}", None) for i in range(12))
    hint = str(exc.value.next)
    assert "12 net(s)" in hint and "+2 more" in hint
    assert all(f"NET_{i:02d}" in hint for i in range(10))
    assert "NET_10" not in hint and "NET_11" not in hint


@pytest.mark.parametrize("field", ["current", "est_current_a"])
def test_net_row_preserves_explicit_zero_current(field: str) -> None:
    row = net_row({"name": "ZERO", field: 0})
    assert row["est_current_a"] == 0
    require_net_currents([(row["name"], row["est_current_a"])])


@pytest.mark.parametrize("missing", [None, float("nan")])
def test_check_reports_unset_currents_as_hard_findings(missing: float | None) -> None:
    board = _board(missing)
    board.net("SIGNAL", current=missing)
    board.net("ZERO", current=0)
    findings = [f for f in board.check().findings if f.code == "net.current_unset"]
    assert {f.net for f in findings} == {"GND", "SIGNAL"}
    assert all(not f.not_checked for f in findings)
