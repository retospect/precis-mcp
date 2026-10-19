"""Per-(class pair, layer) clearance through the put surface
(``docs/backlog/pcb-vector-router.md`` slice 1): the EWOD HV case the
slice exists for, and the P7 refusals."""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler

_SLUG = "ewod-hv-layers"
_HV = {"working_voltage_v": 250}


@pytest.fixture
def pcb(store):
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(
        id=_SLUG,
        args={
            "generators": [
                {
                    "name": "ARR1",
                    "generator": "ewod_pad_array",
                    "params": {"grid": [3, 3]},
                }
            ]
        },
    )
    return handler


_LAYERS = {
    "clearance_by_layer": {
        "F.Cu": {"min_clearance_mm": 0.152},
        "B.Cu": {"min_clearance_mm": 0.4},
    }
}


def _design(pcb: PcbHandler) -> dict:
    ref = pcb.store.get_ref(kind="pcb", id=_SLUG)
    assert ref is not None
    return pcb.store.pcb_load(ref.id)


def _class_rules(pcb: PcbHandler, extra: dict) -> str:
    """Set the escape class to the generator's own rules plus ``extra``."""
    rules = {**_design(pcb)["net_classes"]["ewod_ARR1_escape"], **extra}
    args = {"op": "class_rules", "name": "ewod_ARR1_escape", "rules": rules}
    return pcb.put(id=_SLUG, args=args).body


def test_class_floor_on_every_layer_refuses_the_array_stubs(pcb) -> None:
    """The refusal lists its first findings only; which F.Cu pairs remain
    under the per-layer rule is pinned in full by
    ``tests/test_pcb_drc.py::test_ewod_hv_per_layer_floor_*``."""
    with pytest.raises(
        BadInput,
        match=r"clearance: track\[ARR1_\w+\] <-> pad\[ARR1/\w+ net \w+\] on F\.Cu",
    ):
        _class_rules(pcb, _HV)


def test_per_layer_floor_with_a_pair_rule_puts(pcb) -> None:
    body = _class_rules(
        pcb,
        {
            **_HV,
            **_LAYERS,
            # The array's own plaza vias, at the fixture's default pitch
            # (not the HV builder's sizing): a same-class pair rule.
            "pair_rules": [{"with": "ewod_ARR1_escape", "min_clearance_mm": 0.09}],
        },
    )
    assert "rules set" in body
    rules = _design(pcb)["net_classes"]["ewod_ARR1_escape"]
    assert rules["clearance_by_layer"]["B.Cu"] == {"min_clearance_mm": 0.4}


def test_unknown_layer_and_class_are_refused(pcb) -> None:
    with pytest.raises(BadInput, match="no such copper layer") as exc:
        _class_rules(pcb, {"clearance_by_layer": {"In9.Cu": {"min_clearance_mm": 0.4}}})
    assert exc.value.options
    with pytest.raises(BadInput, match="no such net class 'pixels'"):
        _class_rules(pcb, {"pair_rules": [{"with": "pixels", "min_clearance_mm": 0.2}]})


def test_design_put_refuses_an_unknown_layer_before_writing(store) -> None:
    handler = PcbHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="no such copper layer"):
        handler.put(
            id="hv-design",
            args={
                "net_classes": {
                    "hv": {"clearance_by_layer": {"X.Cu": {"min_clearance_mm": 0.4}}}
                }
            },
        )
    assert store.get_ref(kind="pcb", id="hv-design") is None
