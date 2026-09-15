"""Rung 3b: can a driver actually reach the screw
(``se-off-the-shelf-fabrication.md`` engine 2, tool access).

The geometric cases are built so the *answer* is obvious by hand: a screw
in open air can be turned by anything, and a screw at the bottom of a
narrow slot can only be turned by something narrow. What the test pins is
which tool the pass picks and that it names the blocker — not the exact
envelope of a screwdriver, which is bench data anyone may edit.
"""

from __future__ import annotations

from precis_se import catalog, fasten, toolaccess
from precis_se.ops import ConnectSpec, SeBlock, SeTree

M4_CAP = {
    "thread_size": "M4",
    "thread_pitch": 0.0007,
    "outer_diameter": 0.004,
    "head_diameter": 0.007,
    "head_height": 0.004,
    "length": 0.020,
    "drive_size": 0.003,
    "drive_type": "socket",
    "head_form": "cap",
    "point_type": "machine",
}


class TestToolTable:
    def test_a_hex_socket_offers_keys_and_bits_in_preference_order(self) -> None:
        tools = toolaccess.tools_for("socket", 3.0)
        ids = [t.tool_id for t in tools]
        assert ids[0] == "hex-key-short"  # the torque position first
        assert "bit-driver" in ids

    def test_the_torque_position_sweeps_wider_than_it_is_tall(self) -> None:
        short = next(
            t
            for t in toolaccess.tools_for("socket", 3.0)
            if t.tool_id == "hex-key-short"
        )
        assert short.swing_radius_m > short.axial_m
        long_arm = next(
            t
            for t in toolaccess.tools_for("socket", 3.0)
            if t.tool_id == "hex-key-long"
        )
        assert long_arm.axial_m > long_arm.swing_radius_m

    def test_torx_is_driven_with_bits_not_l_keys(self) -> None:
        ids = [t.tool_id for t in toolaccess.tools_for("torx", 3.95)]
        assert ids and not any(i.startswith("hex-key") for i in ids)

    def test_a_key_size_nobody_makes_gets_no_key(self) -> None:
        """Rounding 4.5 to 4 would put a tool that does not exist into a
        finding."""
        ids = [t.tool_id for t in toolaccess.tools_for("socket", 4.5)]
        assert not any(i.startswith("hex-key") for i in ids)

    def test_an_unknown_drive_offers_nothing_rather_than_guessing(self) -> None:
        assert toolaccess.tools_for("mystery", 3.0) == []
        assert toolaccess.tools_for(None, None) == []


def _tree(*, walls: bool) -> SeTree:
    """An M4 cap screw at the origin driving +z into a plate. With
    ``walls``, two tall blocks stand either side of the head, 6 mm from
    the axis — closer than any hex key's sweep."""
    tree = SeTree()
    screw = SeBlock(name="screw", pose=[0, 0, 0])
    screw.bound_kind = "component"
    screw.bound = "iso-4762-m4x20"
    screw.derived = catalog.derive("fastener", M4_CAP)
    tree.blocks["screw"] = screw
    plate = SeBlock(name="plate", pose=[0, 0, 0.006], envelope="box:w0.05d0.05h0.010")
    plate.mode = "cnc-2.5ax/aluminium"
    tree.blocks["plate"] = plate
    if walls:
        for i, x in enumerate((-0.008, 0.008)):
            tree.blocks[f"wall_{i}"] = SeBlock(
                name=f"wall_{i}",
                pose=[x, 0, -0.030],
                envelope="box:w0.004d0.05h0.030",
            )
    tree.connects.append(
        ConnectSpec(
            a_block="screw",
            a_port="thread",
            b_block="plate",
            b_port="hole",
            joint={"class": "rigid", "mechanism": "screw"},
        )
    )
    return tree


class TestGeometry:
    def test_a_screw_in_open_air_is_driven_with_the_preferred_tool(self) -> None:
        (res,) = fasten.fasten(_tree(walls=False))
        assert res.tool is not None
        assert "hex key" in res.tool
        assert "no_tool_access" not in {f.rule for f in res.findings}

    def test_walls_closer_than_the_sweep_rule_out_the_key(self) -> None:
        """The whole reason the check exists: the screw fits, the *tool*
        does not."""
        (res,) = fasten.fasten(_tree(walls=True))
        rules = {f.rule for f in res.findings}
        if res.tool is None:
            assert "no_tool_access" in rules
            detail = next(f.detail for f in res.findings if f.rule == "no_tool_access")
            assert "wall_" in detail  # it names what is in the way
        else:
            # A narrower tool got in, which is the useful answer — and it
            # must not be the wide-sweep key.
            assert "hex key, short arm" not in res.tool
