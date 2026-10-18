"""gr495291: pad numbers given as connection pins, and the way out for a
pin declared under the wrong name.

A connection ``{refdes:'U1', pin:'1'}`` on a part whose cached footprint
numbers its pads used to mint a ghost pin ``'1'`` at a synthesized bound,
and nothing could remove it short of deleting the design. A pad number is
now wired to that pad's pin name; ``op='pin_rename'`` and
``op='pin_remove'`` fix a pin already declared wrong.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler

from .test_pcb_declared_pins import _mini_design


@pytest.fixture
def pcb(store: Any) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _pins(store: Any, slug: str, refdes: str) -> dict[str, str | None]:
    """``{live pin name: net name or None}`` for one instance."""
    ref = store.get_ref(kind="pcb", id=slug)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT p.name, n.name FROM pcb_instances i "
            "JOIN pcb_pins p ON p.component_id = i.component_id "
            "LEFT JOIN pcb_netconns nc "
            "  ON nc.pin_id = p.pin_id AND nc.instance_id = i.instance_id "
            "LEFT JOIN pcb_nets n ON n.net_id = nc.net_id "
            "WHERE i.ref_id = %s AND i.refdes = %s "
            "AND i.retired_at IS NULL AND p.retired_at IS NULL",
            (ref.id, refdes),
        ).fetchall()
    return {str(name): net for name, net in rows}


def _design_with(connections: list[dict[str, Any]]) -> dict[str, Any]:
    args = _mini_design()
    args["nets"].append({"name": "AUXNET"})
    args["connections"] = [
        c for c in args["connections"] if c["net"] != "SIG"
    ] + connections
    return args


def test_pad_number_pin_wires_the_pads_pin_name(pcb: PcbHandler, store: Any) -> None:
    """MINI8 pad 1 is SIG: ``U1.1`` lands on pin SIG, no ghost pin '1'."""
    out = pcb.put(
        id="pin-padnum",
        args=_design_with(
            [
                {"net": "SIG", "refdes": "U1", "pin": "1"},
                {"net": "SIG", "refdes": "U2", "pin": "SIG"},
            ]
        ),
    ).body
    assert "U1.1 -> SIG" in out, out
    pins = _pins(store, "pin-padnum", "U1")
    assert "1" not in pins, pins
    assert pins["SIG"] == "SIG"


def test_pin_name_is_never_remapped(pcb: PcbHandler, store: Any) -> None:
    """A pin that is a footprint pin name wires by name, as before."""
    out = pcb.put(id="pin-byname", args=_mini_design()).body
    assert "pad numbers" not in out
    assert _pins(store, "pin-byname", "U1")["SIG"] == "SIG"


def test_pin_remove_drops_a_ghost_pin_and_its_connection(
    pcb: PcbHandler, store: Any
) -> None:
    pcb.put(
        id="pin-ghost",
        args=_design_with([{"net": "AUXNET", "refdes": "U1", "pin": "BOGUS"}]),
    )
    assert _pins(store, "pin-ghost", "U1")["BOGUS"] == "AUXNET"
    out = pcb.put(
        id="pin-ghost", args={"op": "pin_remove", "refdes": "U1", "pin": "BOGUS"}
    ).body
    assert "pin BOGUS removed, its connection to AUXNET dropped" in out, out
    assert "BOGUS" not in _pins(store, "pin-ghost", "U1")
    again = pcb.put(
        id="pin-ghost", args={"op": "pin_remove", "refdes": "U1", "pin": "BOGUS"}
    ).body
    assert "no change" in again


def test_pin_remove_refuses_a_real_footprint_pin(pcb: PcbHandler) -> None:
    pcb.put(id="pin-real", args=_mini_design())
    with pytest.raises(BadInput, match="op='pins'"):
        pcb.put(
            id="pin-real", args={"op": "pin_remove", "refdes": "U1", "pin": "SPARE"}
        )


def test_pin_rename_keeps_the_connection(pcb: PcbHandler, store: Any) -> None:
    pcb.put(
        id="pin-rename",
        args=_design_with([{"net": "AUXNET", "refdes": "U1", "pin": "BOGUS"}]),
    )
    args = {"op": "pin_rename", "refdes": "U1", "pin": "BOGUS", "to": "BOGUS2"}
    out = pcb.put(id="pin-rename", args=args).body
    assert "renamed to BOGUS2 (net AUXNET)" in out, out
    pins = _pins(store, "pin-rename", "U1")
    assert "BOGUS" not in pins and pins["BOGUS2"] == "AUXNET"
    assert "no change" in pcb.put(id="pin-rename", args=args).body


def test_pin_rename_onto_a_live_pin_refuses_with_next(pcb: PcbHandler) -> None:
    """Renaming a ghost onto a pin that exists would merge two pins; the
    refusal names the remove-and-re-put path instead."""
    pcb.put(
        id="pin-collide",
        args=_design_with([{"net": "AUXNET", "refdes": "U1", "pin": "BOGUS"}]),
    )
    with pytest.raises(BadInput, match="pin_remove"):
        pcb.put(
            id="pin-collide",
            args={"op": "pin_rename", "refdes": "U1", "pin": "BOGUS", "to": "SIG"},
        )


@pytest.mark.parametrize("op", ["pin_rename", "pin_remove"])
def test_pin_edit_ops_need_their_args(pcb: PcbHandler, op: str) -> None:
    pcb.put(id=f"pin-args-{op}", args=_mini_design())
    with pytest.raises(BadInput) as exc:
        pcb.put(id=f"pin-args-{op}", args={"op": op, "refdes": "U1"})
    assert exc.value.next
