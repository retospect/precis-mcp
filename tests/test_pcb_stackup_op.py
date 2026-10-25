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
from precis.pcb import DEFAULT_STACKUP, STACKUP_PRESETS
from precis.pcb import ir as pcb_ir
from tests.fixtures.pcb.stackups import LEGACY_V1_STACKUP

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
    # A board born before gr495395 switched the default: inner planes only.
    board_id = store.pcb_graph(ref.id)["board"]["board_id"]
    store.pcb_set_stackup(board_id, LEGACY_V1_STACKUP)
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


def test_op_stackup_pour_nets_assigns_like_plane_net(pcb, store):
    """``pour_nets`` (gr486958) is the signal-layer spelling of the same
    declaration ``plane_net`` makes — a fill the source carries the net
    by. It must reach the same ``pcb_planes`` write, else the key is the
    decorative kind this op was written to kill."""
    pcb.put(id="stk-pour", args=_DESIGN)
    resp = pcb.put(
        id="stk-pour",
        args={
            "op": "stackup",
            "layers": [
                {"name": "F.Cu", "role": "signal"},
                {"name": "In1.Cu", "role": "plane", "plane_net": "N1"},
                {"name": "In2.Cu", "role": "signal"},
                {"name": "B.Cu", "role": "signal", "pour_nets": ["N2"]},
            ],
        },
    )
    assert "pours=N2" in resp.body
    ref = store.get_ref(kind="pcb", id="stk-pour")
    assert ref is not None
    assert {(r["layer"], r["net"]) for r in store.pcb_planes_list(ref.id)} == {
        ("In1.Cu", "N1"),
        ("B.Cu", "N2"),
    }


def test_op_stackup_several_pour_nets_record_but_assign_none(pcb, store):
    """The mixed case: several fill-carried nets on one signal layer are
    all recorded, but a layer is one sheet of copper — none is assigned
    (guessing which pours would silently connect copper)."""
    pcb.put(id="stk-pour-mix", args=_DESIGN)
    pcb.put(
        id="stk-pour-mix",
        args={
            "op": "stackup",
            "layers": [
                {"name": "F.Cu", "role": "signal"},
                {"name": "In1.Cu", "role": "plane"},
                {"name": "In2.Cu", "role": "signal"},
                {"name": "B.Cu", "role": "signal", "pour_nets": ["N1", "N2"]},
            ],
        },
    )
    ref = store.get_ref(kind="pcb", id="stk-pour-mix")
    assert ref is not None
    stackup = store.pcb_graph(ref.id)["board"]["stackup"]
    assert next(e for e in stackup if e["name"] == "B.Cu")["pour_nets"] == [
        "N1",
        "N2",
    ]
    assert store.pcb_planes_list(ref.id) == []


def test_op_stackup_pour_nets_rejects(pcb):
    """``plane_net`` and ``pour_nets`` name two different shapes of the
    same fact; both on one entry is refused, and an unknown pour net is
    an error — never a silent skip."""
    pcb.put(id="stk-pour-bad", args=_DESIGN)
    with pytest.raises(BadInput, match="plane_net and pour_nets cannot both"):
        pcb.put(
            id="stk-pour-bad",
            args={
                "op": "stackup",
                "layers": [
                    {"name": "F.Cu", "role": "signal"},
                    {
                        "name": "In1.Cu",
                        "role": "plane",
                        "plane_net": "N1",
                        "pour_nets": ["N2"],
                    },
                    {"name": "In2.Cu", "role": "signal"},
                    {"name": "B.Cu", "role": "signal"},
                ],
            },
        )
    with pytest.raises(BadInput, match="no net 'NOPE'"):
        pcb.put(
            id="stk-pour-bad",
            args={
                "op": "stackup",
                "layers": [
                    {"name": "F.Cu", "role": "signal"},
                    {"name": "In1.Cu", "role": "plane"},
                    {"name": "In2.Cu", "role": "signal"},
                    {"name": "B.Cu", "role": "signal", "pour_nets": ["NOPE"]},
                ],
            },
        )


_GND_DESIGN: dict[str, Any] = {
    **_DESIGN,
    "nets": [{"name": "GND"}, {"name": "N2"}],
    "connections": [
        {"net": "GND", "refdes": "U1", "pin": "A"},
        {"net": "GND", "refdes": "R1", "pin": "1"},
        {"net": "N2", "refdes": "U1", "pin": "B"},
        {"net": "N2", "refdes": "R1", "pin": "2"},
    ],
}


@pytest.mark.parametrize(
    ("preset", "sides", "layers"),
    [
        ("4l-two-sided", "both", 4),
        ("4l-top-only", "top", 4),
        ("2l", "both", 2),
    ],
)
def test_op_stackup_preset_round_trips_through_the_view(
    pcb, store, preset, sides, layers
):
    """pcb-stackup-regions slice 3: each preset writes its stackup, the
    view names it with its sides, and the fills show as what the router
    skips (GND pours on every filled layer)."""
    slug = f"stk-preset-{preset}"
    pcb.put(id=slug, args=_GND_DESIGN)
    resp = pcb.put(id=slug, args={"op": "stackup", "preset": preset})
    assert f"(preset {preset})" in resp.body
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    stored = store.pcb_graph(ref.id)["board"]["stackup"]
    assert stored == pcb_ir.validate_stackup(stored)
    assert len(stored) == layers
    body = pcb.get(id=slug, view="stackup").body
    assert f"preset {preset} (sides: {sides})" in body
    assert "GND (F.Cu, fill)" in body and "GND (B.Cu, fill)" in body
    planes = pcb.get(id=slug, view="planes").body
    assert "fill" in planes and "area_mm2" in planes
    # Writing layers by hand drops the preset label.
    pcb.put(id=slug, args={"op": "stackup", "layers": stored})
    assert "preset" not in pcb.get(id=slug, view="stackup").body.splitlines()[0]


def test_op_stackup_preset_refusals(pcb):
    pcb.put(id="stk-preset-bad", args=_DESIGN)
    with pytest.raises(BadInput, match="unknown preset"):
        pcb.put(id="stk-preset-bad", args={"op": "stackup", "preset": "6l"})
    with pytest.raises(BadInput, match="not both"):
        pcb.put(
            id="stk-preset-bad",
            args={"op": "stackup", "preset": "2l", "layers": _TWO_SIGNAL},
        )
    # _DESIGN has no GND: the preset's plane/fill name no net.
    with pytest.raises(BadInput, match="ground="):
        pcb.put(id="stk-preset-bad", args={"op": "stackup", "preset": "2l"})
    resp = pcb.put(
        id="stk-preset-bad", args={"op": "stackup", "preset": "2l", "ground": "N1"}
    )
    assert "fill=N1" in resp.body


def test_op_stackup_is_listed_in_the_unknown_op_error(pcb):
    """The op surface is discoverable — an LLM that guesses a wrong op
    name is told the real list, including this one."""
    pcb.put(id="stk-list", args=_DESIGN)
    with pytest.raises(BadInput) as exc:
        pcb.put(id="stk-list", args={"op": "layers"})
    assert "stackup" in str(exc.value.options or [])


# ── batch-meta stackup authoring (gr515320) ─────────────────────────────
#
# ``precis_geom.pcb.Board(stackup=…)`` writes ``meta.stackup_preset`` /
# ``meta.stackup_layers`` into the put batch. The store's put path applies
# them to the board row; neither key present leaves the board on
# DEFAULT_STACKUP.


def test_put_meta_stackup_preset_applies_to_the_board(pcb, store):
    """A ``Board(stackup='2l')`` upload gets the 2-layer preset, not the
    silent 4-layer default."""
    pcb.put(id="stk-meta-2l", args={**_GND_DESIGN, "meta": {"stackup_preset": "2l"}})
    ref = store.get_ref(kind="pcb", id="stk-meta-2l")
    assert ref is not None
    stored = store.pcb_graph(ref.id)["board"]["stackup"]
    assert [e["name"] for e in stored] == ["F.Cu", "B.Cu"]
    assert all(e.get("fill") == "GND" for e in stored)
    body = pcb.get(id="stk-meta-2l", view="stackup").body
    assert "preset 2l (sides: both)" in body


def test_put_meta_stackup_layers_applies_to_the_board(pcb, store):
    """A ``Board(stackup=4)``-style bare layer list is normalised like
    ``local._stackup`` — signal rows, inner layers routable."""
    pcb.put(
        id="stk-meta-layers",
        args={
            **_DESIGN,
            "meta": {"stackup_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]},
        },
    )
    ref = store.get_ref(kind="pcb", id="stk-meta-layers")
    assert ref is not None
    stored = store.pcb_graph(ref.id)["board"]["stackup"]
    assert stored == [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In1.Cu", "role": "signal", "routable": True},
        {"name": "In2.Cu", "role": "signal", "routable": True},
        {"name": "B.Cu", "role": "signal"},
    ]


def test_put_meta_stackup_refusals(pcb):
    """An unknown preset, or a preset whose layer count disagrees with
    ``stackup_layers``, is refused — the error names the valid presets."""
    with pytest.raises(BadInput, match="not a stackup preset") as exc:
        pcb.put(
            id="stk-meta-bad",
            args={**_DESIGN, "meta": {"stackup_preset": "6l"}},
        )
    for name in sorted(STACKUP_PRESETS):
        assert name in str(exc.value.next)

    with pytest.raises(BadInput, match="disagree") as exc:
        pcb.put(
            id="stk-meta-bad",
            args={
                **_DESIGN,
                "meta": {
                    "stackup_preset": "2l",
                    "stackup_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
                },
            },
        )
    for name in sorted(STACKUP_PRESETS):
        assert name in str(exc.value.next)


def test_put_without_meta_stackup_keeps_the_default(pcb, store):
    """The default applies only when the batch authors no stackup at
    all."""
    pcb.put(id="stk-meta-none", args=_DESIGN)
    ref = store.get_ref(kind="pcb", id="stk-meta-none")
    assert ref is not None
    assert store.pcb_graph(ref.id)["board"]["stackup"] == DEFAULT_STACKUP


# ── batch-meta parity with op='stackup' (gr515869) ──────────────────────
#
# A batch-meta preset must reach the SAME end state as op='stackup' with
# that preset: the board row (stackup + meta) and the pcb_planes rows the
# preset's plane_net/pour_nets declarations produce. Until this ran the
# batch path stored the stackup rows and stopped — placement_sides only
# rode along when a preset named it, and the plane declarations were
# never applied.


def test_put_meta_preset_matches_op_stackup_preset_end_state(pcb, store):
    """``Board(stackup='4l-two-sided')`` and ``op='stackup', preset=
    '4l-two-sided'`` yield identical pcb_boards and pcb_planes rows —
    including the In1.Cu/GND plane the preset declares (the batch put
    carries its own GND net, which the apply resolves)."""
    pcb.put(
        id="stk-meta-eq",
        args={**_GND_DESIGN, "meta": {"stackup_preset": "4l-two-sided"}},
    )
    pcb.put(id="stk-op-eq", args=_GND_DESIGN)
    pcb.put(id="stk-op-eq", args={"op": "stackup", "preset": "4l-two-sided"})
    meta_ref = store.get_ref(kind="pcb", id="stk-meta-eq")
    op_ref = store.get_ref(kind="pcb", id="stk-op-eq")
    assert meta_ref is not None and op_ref is not None
    meta_board = store.pcb_graph(meta_ref.id)["board"]
    op_board = store.pcb_graph(op_ref.id)["board"]
    assert meta_board["stackup"] == op_board["stackup"]
    assert meta_board["meta"] == op_board["meta"]
    assert store.pcb_planes_list(meta_ref.id) == store.pcb_planes_list(op_ref.id)
    # The regression this guards: the preset's declared plane landed.
    assert [
        (r["layer"], r["net"], r["source"]) for r in store.pcb_planes_list(meta_ref.id)
    ] == [("In1.Cu", "GND", "authored")]


def test_put_meta_layers_pour_nets_assign_like_the_op(pcb, store):
    """``pour_nets`` in batch-meta ``stackup_layers`` reaches the same
    ``pcb_planes`` write ``op='stackup'`` gives it — one named net, one
    authored row."""
    layers = [
        {"name": "F.Cu", "role": "signal"},
        {"name": "In1.Cu", "role": "plane", "plane_net": "N1"},
        {"name": "In2.Cu", "role": "signal"},
        {"name": "B.Cu", "role": "signal", "pour_nets": ["N2"]},
    ]
    pcb.put(id="stk-meta-pour", args={**_DESIGN, "meta": {"stackup_layers": layers}})
    ref = store.get_ref(kind="pcb", id="stk-meta-pour")
    assert ref is not None
    assert {(r["layer"], r["net"]) for r in store.pcb_planes_list(ref.id)} == {
        ("In1.Cu", "N1"),
        ("B.Cu", "N2"),
    }


def test_put_meta_preset_missing_ground_net_is_an_error(pcb):
    """Same refusal the op raises: a declared plane/fill naming no live
    net is an author error, never a silent skip — and the whole put rolls
    back with it."""
    with pytest.raises(BadInput, match="no net 'GND'"):
        pcb.put(id="stk-meta-nognd", args={**_DESIGN, "meta": {"stackup_preset": "2l"}})


def test_put_meta_placement_sides_lands_without_stackup_keys(pcb, store):
    """A ``Board(sides='top')`` upload authors ``meta.placement_sides``
    with no stackup key at all — it still reaches ``pcb_boards.meta`` and
    the stackup keeps the default."""
    pcb.put(id="stk-sides", args={**_DESIGN, "meta": {"placement_sides": "top"}})
    ref = store.get_ref(kind="pcb", id="stk-sides")
    assert ref is not None
    board = store.pcb_graph(ref.id)["board"]
    assert board["meta"]["placement_sides"] == "top"
    assert board["stackup"] == DEFAULT_STACKUP


def test_put_meta_placement_sides_lands_beside_stackup_layers(pcb, store):
    """The half-applied case from the gripe: ``stackup_layers`` alone
    carried no preset to stamp sides, so ``placement_sides`` stranded on
    ``refs.meta``. It lands on the board row now."""
    pcb.put(
        id="stk-sides-layers",
        args={
            **_DESIGN,
            "meta": {
                "placement_sides": "top",
                "stackup_layers": ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
            },
        },
    )
    ref = store.get_ref(kind="pcb", id="stk-sides-layers")
    assert ref is not None
    board = store.pcb_graph(ref.id)["board"]
    assert board["meta"]["placement_sides"] == "top"
    assert "stackup_preset" not in board["meta"]
    assert [e["name"] for e in board["stackup"]] == [
        "F.Cu",
        "In1.Cu",
        "In2.Cu",
        "B.Cu",
    ]
