"""§1d net naming rule: a net of only supply pins (free ends) is refused."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _free_end_args(conns: list[tuple[str, str, str]]) -> dict[str, Any]:
    return {
        "components": [
            {
                "refdes": "U1",
                "label": "drv",
                "footprint": "X",
                "pins": [{"name": "A"}, {"name": "B"}],
                "supplies": {"role": "pad_driver", "blocks": [["A"], ["B"]]},
            },
            {
                "refdes": "P1",
                "label": "pad",
                "footprint": "Y",
                "pins": [{"name": "1", "client_of": "pad_driver"}],
            },
        ],
        "connections": [{"net": n, "refdes": r, "pin": p} for n, r, p in conns],
    }


def test_all_supply_net_refused(pcb):
    with pytest.raises(BadInput, match="only supply pins") as e:
        pcb.put(
            id="free-ends",
            args=_free_end_args([("N1", "U1", "A"), ("N1", "U1", "B")]),
        )
    assert "'N1'" in str(e.value) and "U1.A" in str(e.value)
    assert "client_of" in str(e.value)


def test_supply_plus_client_net_accepted(pcb):
    pcb.put(id="free-ok", args=_free_end_args([("N1", "U1", "A"), ("N1", "P1", "1")]))


def test_supply_only_net_gains_constrained_member_same_apply(pcb):
    pcb.put(
        id="free-late",
        args=_free_end_args([("N1", "U1", "A"), ("N1", "U1", "B"), ("N1", "P1", "1")]),
    )


def test_moving_the_constrained_pin_off_a_net_is_refused(pcb):
    pcb.put(
        id="free-move",
        args=_free_end_args([("N1", "U1", "A"), ("N1", "U1", "B"), ("N1", "P1", "1")]),
    )
    with pytest.raises(BadInput, match="only supply pins"):
        pcb.put(id="free-move", args=_free_end_args([("N2", "P1", "1")]))


def test_unchanged_legacy_all_supply_net_not_refused(pcb):
    legacy = _free_end_args([("N1", "U1", "A"), ("N1", "U1", "B")])
    del legacy["components"][0]["supplies"]
    pcb.put(id="free-legacy", args=legacy)
    # Declaring supplies later leaves N1 all-supply; an apply that does not
    # change N1's membership must still land.
    pcb.put(id="free-legacy", args=_free_end_args([("N2", "P1", "1")]))
