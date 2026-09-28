"""``put(args={'op':'stackup'})`` — authoring the board's copper stackup.

Until this op existed every board was stuck on
:data:`precis.pcb.DEFAULT_STACKUP`, stamped at board birth by
``store.pcb_ensure_board`` and never writable after. Which layers may
carry a trace was therefore engine policy rather than design data — and
since that default makes In1.Cu/In2.Cu planes, B.Cu was the ONLY layer a
4-layer board could route on.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import ir as pcb_ir

_DESIGN: dict[str, Any] = {
    "components": [
        {
            "refdes": "U1",
            "label": "MCU",
            "x": 10.0,
            "y": 10.0,
            "pins": [{"name": "A"}, {"name": "B"}],
        },
        {
            "refdes": "R1",
            "label": "10k 0402",
            "x": 13.0,
            "y": 10.0,
            "pins": [{"name": "1"}, {"name": "2"}],
        },
    ],
    "nets": [{"name": "N1"}, {"name": "N2"}],
    "connections": [
        {"net": "N1", "refdes": "U1", "pin": "A"},
        {"net": "N1", "refdes": "R1", "pin": "1"},
        {"net": "N2", "refdes": "U1", "pin": "B"},
        {"net": "N2", "refdes": "R1", "pin": "2"},
    ],
}

#: The arrangement the escape/driver backlog's "Blocked on" section says
#: the board actually wants: keep In1 as the ground plane, spend In2 on
#: signal, and you have TWO routing layers instead of one.
_TWO_SIGNAL = [
    {"name": "F.Cu", "role": "signal"},
    {"name": "In1.Cu", "role": "plane", "plane_net": "N1"},
    {"name": "In2.Cu", "role": "signal"},
    {"name": "B.Cu", "role": "signal"},
]


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def test_op_stackup_makes_an_inner_layer_routable(pcb, store):
    """Asserted through :func:`precis.pcb.ir.layer_is_routable` rather
    than the stored JSON, because that predicate is what the router
    reads — storing the author's intent somewhere nothing consults is the
    failure this op exists to avoid."""
    pcb.put(id="stk-routable", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="stk-routable")
    assert ref is not None
    before = store.pcb_graph(ref.id)
    assert [
        e["name"] for e in before["board"]["stackup"] if pcb_ir.layer_is_routable(e)
    ] == ["F.Cu", "B.Cu"]

    resp = pcb.put(id="stk-routable", args={"op": "stackup", "layers": _TWO_SIGNAL})
    assert "Routable: ['F.Cu', 'In2.Cu', 'B.Cu']" in resp.body

    after = store.pcb_graph(ref.id)
    assert [
        e["name"] for e in after["board"]["stackup"] if pcb_ir.layer_is_routable(e)
    ] == ["F.Cu", "In2.Cu", "B.Cu"]
    # A stackup `plane_net` was a DEAD key before this op — nothing in the
    # engine read it, so even DEFAULT_STACKUP's own In1.Cu/GND declaration
    # poured nothing. It is applied now, through the same `pcb_planes`
    # rows op='plane_net' writes, so the declaration is true rather than
    # decorative.
    assert [
        (r["layer"], r["net"], r["source"]) for r in store.pcb_planes_list(ref.id)
    ] == [("In1.Cu", "N1", "authored")]


@pytest.mark.parametrize(
    ("slug", "layers", "match"),
    [
        ("one", [{"name": "F.Cu"}], "at least the two outer"),
        (
            "named",
            [{"name": "Top.Cu"}, {"name": "Bot.Cu"}],
            "first and last layers must be named",
        ),
        (
            "typo",
            [{"name": "F.Cu", "routeable": True}, {"name": "B.Cu"}],
            r"unknown key\(s\) \['routeable'\]",
        ),
        ("dup", [{"name": "F.Cu"}, {"name": "F.Cu"}], "duplicate layer name"),
        (
            "role",
            [{"name": "F.Cu", "role": "power"}, {"name": "B.Cu"}],
            "role must be 'signal' or 'plane'",
        ),
        (
            "flag",
            [{"name": "F.Cu", "routable": "yes"}, {"name": "B.Cu"}],
            "routable must be true or false",
        ),
        (
            "allplane",
            [{"name": "F.Cu", "role": "plane"}, {"name": "B.Cu", "role": "plane"}],
            "no routable layer",
        ),
        (
            "three",
            [
                {"name": "F.Cu", "role": "signal"},
                {"name": "In1.Cu", "role": "plane"},
                {"name": "B.Cu", "role": "signal"},
            ],
            "no DRC capability row for a 3-layer stackup",
        ),
    ],
)
def test_op_stackup_rejects(pcb, slug, layers, match):
    """Every rejection names the offending entry. The ``routeable`` typo
    is the load-bearing case: an unknown key silently ignored leaves
    :func:`~precis.pcb.ir.layer_is_routable` on its ``role`` fallback, so
    the author's instruction is accepted, stored, and never honoured."""
    pcb.put(id=f"stk-bad-{slug}", args=_DESIGN)
    with pytest.raises(BadInput, match=match):
        pcb.put(id=f"stk-bad-{slug}", args={"op": "stackup", "layers": layers})


def test_op_stackup_refuses_to_strand_a_plane_assignment(pcb):
    """Dropping a layer out from under live copper would leave rows no
    exporter, DRC pass or router could resolve — silently absent from the
    fab output rather than loudly rejected here."""
    pcb.put(id="stk-strand", args=_DESIGN)
    pcb.put(id="stk-strand", args={"op": "plane_net", "layer": "In1.Cu", "net": "N1"})
    with pytest.raises(BadInput, match=r"\['In1.Cu'\] still carries copper"):
        pcb.put(
            id="stk-strand",
            args={
                "op": "stackup",
                "layers": [
                    {"name": "F.Cu", "role": "signal"},
                    {"name": "B.Cu", "role": "signal"},
                ],
            },
        )


def test_op_stackup_plane_net_for_an_unknown_net_is_an_error(pcb):
    """Never a silent skip — a declaration the engine quietly drops is
    exactly the defect this op exists to stop producing."""
    pcb.put(id="stk-nonet", args=_DESIGN)
    with pytest.raises(BadInput, match="no net 'GND'"):
        pcb.put(
            id="stk-nonet",
            args={
                "op": "stackup",
                "layers": [
                    {"name": "F.Cu", "role": "signal"},
                    {"name": "In1.Cu", "role": "plane", "plane_net": "GND"},
                    {"name": "In2.Cu", "role": "plane"},
                    {"name": "B.Cu", "role": "signal"},
                ],
            },
        )


def test_op_stackup_writes_nothing_when_one_plane_net_is_unknown(pcb, store):
    """A rejected call must leave the board exactly as it found it.
    ``pcb_assign_plane`` returns 0 for a net that does not resolve, so
    assigning-and-checking in one pass would write the FIRST declaration,
    reject the second, and store no stackup at all — the board keeping
    half of an instruction the caller was told had failed."""
    pcb.put(id="stk-partial", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="stk-partial")
    assert ref is not None
    before = store.pcb_graph(ref.id)["board"]["stackup"]
    with pytest.raises(BadInput, match="no net 'NOPE'"):
        pcb.put(
            id="stk-partial",
            args={
                "op": "stackup",
                "layers": [
                    {"name": "F.Cu", "role": "signal"},
                    # N1 exists; NOPE does not. N1's assignment must not
                    # survive the rejection.
                    {"name": "In1.Cu", "role": "plane", "plane_net": "N1"},
                    {"name": "In2.Cu", "role": "plane", "plane_net": "NOPE"},
                    {"name": "B.Cu", "role": "signal"},
                ],
            },
        )
    assert store.pcb_planes_list(ref.id) == []
    assert store.pcb_graph(ref.id)["board"]["stackup"] == before


def test_op_stackup_is_listed_in_the_unknown_op_error(pcb):
    """The op surface is discoverable — an LLM that guesses a wrong op
    name is told the real list, including this one."""
    pcb.put(id="stk-list", args=_DESIGN)
    with pytest.raises(BadInput) as exc:
        pcb.put(id="stk-list", args={"op": "layers"})
    assert "stackup" in str(exc.value.options or [])
