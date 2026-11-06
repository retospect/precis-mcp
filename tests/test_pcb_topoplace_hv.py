"""pcb-topo-hv-preference (td569574) — the measured In1-horizontal /
In2-vertical inner-layer direction preference: the pure helper
(``_inner_axis``/``_direction_pen``) plus the ``hv_weight`` wiring
through ``build_wires``. Default ``hv_weight=0.0`` must take the exact
pre-existing code path — bit-for-bit identical wires to the no-arg call.
"""

from __future__ import annotations

from precis.pcb.topoplace.mesh import build_mesh
from precis.pcb.topoplace.wires import (
    HV_WEIGHT_LOW,
    HV_WEIGHT_MED,
    PlantedVia,
    WirePath,
    _direction_pen,
    _inner_axis,
    build_wires,
)
from tests.test_pcb_topoplace_layer_search import _CLEAR, _TRACE, _row_board

_N_LAYERS_4L: int = 4
_N_LAYERS_2L: int = 2
_IN1: int = 1  # horizontal-preferring inner layer (index 1)
_IN2: int = 2  # vertical-preferring inner layer (index 2)
_OUTER_TOP: int = 0
_P0: tuple[float, float] = (0.0, 0.0)
_HORIZONTAL_Q: tuple[float, float] = (10.0, 0.0)
_VERTICAL_Q: tuple[float, float] = (0.0, 10.0)
_VIA_DIA_MM: float = 0.6


def test_inner_axis_in1_horizontal_in2_vertical() -> None:
    assert _inner_axis(_IN1, _N_LAYERS_4L) == 0
    assert _inner_axis(_IN2, _N_LAYERS_4L) == 1


def test_inner_axis_outer_layers_have_no_axis() -> None:
    assert _inner_axis(_OUTER_TOP, _N_LAYERS_4L) == -1
    assert _inner_axis(_N_LAYERS_4L - 1, _N_LAYERS_4L) == -1


def test_inner_axis_two_layer_board_has_no_inner_layer() -> None:
    assert _inner_axis(0, _N_LAYERS_2L) == -1
    assert _inner_axis(1, _N_LAYERS_2L) == -1


def test_direction_pen_horizontal_run_cheaper_on_in1_than_in2() -> None:
    on_in1 = _direction_pen(_P0, _HORIZONTAL_Q, _IN1, _N_LAYERS_4L, HV_WEIGHT_MED)
    on_in2 = _direction_pen(_P0, _HORIZONTAL_Q, _IN2, _N_LAYERS_4L, HV_WEIGHT_MED)
    assert on_in1 < on_in2
    assert on_in1 == 0.0  # travel is purely along In1's own axis


def test_direction_pen_vertical_run_cheaper_on_in2_than_in1() -> None:
    on_in1 = _direction_pen(_P0, _VERTICAL_Q, _IN1, _N_LAYERS_4L, HV_WEIGHT_MED)
    on_in2 = _direction_pen(_P0, _VERTICAL_Q, _IN2, _N_LAYERS_4L, HV_WEIGHT_MED)
    assert on_in2 < on_in1
    assert on_in2 == 0.0  # travel is purely along In2's own axis


def test_direction_pen_zero_weight_is_zero() -> None:
    assert _direction_pen(_P0, _HORIZONTAL_Q, _IN2, _N_LAYERS_4L, 0.0) == 0.0


def test_direction_pen_outer_layer_is_zero() -> None:
    assert (
        _direction_pen(_P0, _VERTICAL_Q, _OUTER_TOP, _N_LAYERS_4L, HV_WEIGHT_MED) == 0.0
    )
    assert (
        _direction_pen(_P0, _VERTICAL_Q, _N_LAYERS_4L - 1, _N_LAYERS_4L, HV_WEIGHT_MED)
        == 0.0
    )


def test_direction_pen_two_layer_board_is_zero() -> None:
    assert _direction_pen(_P0, _VERTICAL_Q, 0, _N_LAYERS_2L, HV_WEIGHT_MED) == 0.0
    assert _direction_pen(_P0, _VERTICAL_Q, 1, _N_LAYERS_2L, HV_WEIGHT_MED) == 0.0


def _branch_signature(
    b: WirePath,
) -> tuple[tuple[tuple[float, float], ...], tuple[int, ...], tuple[PlantedVia, ...]]:
    return b.points, b.layers, b.vias


def _row_wires(hv_weight: float | None) -> tuple[WirePath, ...]:
    """``build_wires`` over the row board (a pin pair with a foreign
    pad row between them — the same fixture
    ``test_pcb_topoplace_layer_search`` uses — at the via diameter that
    sends the crossing under the row through an inner layer), either at
    a given ``hv_weight`` or (``None``) the no-kwarg default."""
    d = _row_board()
    mesh = build_mesh(d, d.pose0)
    rule = (_TRACE, _CLEAR, _VIA_DIA_MM)
    if hv_weight is None:
        w = build_wires(mesh, d, _N_LAYERS_4L, (rule, rule), pose=d.pose0)
    else:
        w = build_wires(
            mesh, d, _N_LAYERS_4L, (rule, rule), pose=d.pose0, hv_weight=hv_weight
        )
    return w.branches


def test_build_wires_hv_weight_zero_matches_the_no_kwarg_default() -> None:
    default = _row_wires(None)
    explicit_zero = _row_wires(0.0)
    assert [_branch_signature(b) for b in default] == [
        _branch_signature(b) for b in explicit_zero
    ]


def test_build_wires_nonzero_hv_weight_is_deterministic() -> None:
    first = _row_wires(HV_WEIGHT_LOW)
    second = _row_wires(HV_WEIGHT_LOW)
    assert [_branch_signature(b) for b in first] == [
        _branch_signature(b) for b in second
    ]


def test_build_wires_nonzero_hv_weight_still_routes_the_net() -> None:
    branches = [b for b in _row_wires(HV_WEIGHT_MED) if b.net == 0]
    assert len(branches) == 1
