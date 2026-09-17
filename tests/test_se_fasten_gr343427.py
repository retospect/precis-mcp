"""Regression for gr343427: a tapped/thread-forming terminal feature must
be sized from **engagement**, never from the whole terminal member
(rung 3c's far-end feature builder, :func:`precis_se.fasten._far_end_feature`
/ :func:`precis_se.fasten._stamp`).

Found dogfooding unicycle-mk2 (``seat_bolt.thread``—``seatpost.top``,
ISO 4762 M6×35): after the walk-termination fix the stack came out
right (a 25 mm saddle clamped, the seatpost 10 mm engaged), but the
stamped ``#seatpost.tapped`` feature carried the seatpost's whole 150 mm
thickness as its depth — a tapped hole the full length of a 150 mm
tube is not what anyone drills for 10 mm of engaged thread.

Same posture and M6 fixture shape as ``test_se_fasten.py`` (a hand-built
:class:`SeTree`, real poses in metres, no store), scaled to a thick
terminal member so the bug and the fix are both checkable by hand.
"""

from __future__ import annotations

import pytest

from precis_se import catalog, fasten
from precis_se.ops import ConnectSpec, SeBlock, SeTree

#: ISO 4762 M6 socket cap, metres — same M6 coarse (1 mm) pitch as
#: `test_se_fasten.py`'s M6X30 fixture; ``length`` is set per fixture.
M6 = {
    "thread_size": "M6",
    "thread_pitch": 0.001,
    "outer_diameter": 0.006,
    "head_diameter": 0.010,
    "head_height": 0.006,
    "drive_size": 0.005,
}

CLAMPED_6MM = "box:w0.05d0.05h0.006"

#: The house blind-hole rule (`thread_forming.json` ``blind_hole``) × M6's
#: 1 mm pitch: 2 pitches of tip clearance for every blind far end, plus 3
#: pitches of tap chamfer for a cut thread only.
_TAPPED_ALLOWANCE_M = 0.005
_CORE_ALLOWANCE_M = 0.002


def _bought(name: str, pose: list[float], specs: dict) -> SeBlock:
    node = SeBlock(name=name, pose=list(pose))
    node.bound_kind = "component"
    node.bound = "the-screw"
    node.derived = catalog.derive("fastener", specs)
    return node


def _seatpost_like(
    *,
    terminal_thickness_m: float,
    length_m: float,
    mode: str = "cnc-2.5ax/aluminium",
    params: dict | None = None,
) -> SeTree:
    """A 6 mm clamped member (the saddle) and a terminal member
    ``terminal_thickness_m`` deep (the seatpost) in ``mode`` — the same
    shape as the dogfood stack, at whatever terminal thickness/material a
    test wants to check the blind-hole cap against."""
    tree = SeTree()
    tree.blocks["bolt"] = _bought("bolt", [0, 0, 0], dict(M6, length=length_m))
    tree.blocks["saddle"] = SeBlock(
        name="saddle", pose=[0, 0, 0.006], envelope=CLAMPED_6MM
    )
    terminal = SeBlock(
        name="seatpost",
        pose=[0, 0, 0.012],
        envelope=f"box:w0.05d0.05h{terminal_thickness_m}",
    )
    terminal.mode = mode
    tree.blocks["seatpost"] = terminal
    tree.connects.append(
        ConnectSpec(
            a_block="bolt",
            a_port="thread",
            b_block="seatpost",
            b_port="top",
            joint={
                "class": "rigid",
                "mechanism": "screw",
                "params": params or {},
            },
        )
    )
    return tree


def _only(tree: SeTree) -> fasten.FastenResult:
    results = fasten.fasten(tree)
    assert len(results) == 1
    return results[0]


class TestBlindTapDepthFromEngagement:
    def test_a_thick_terminal_member_gets_a_blind_hole_not_full_thickness(
        self,
    ) -> None:
        """The dogfood case: a 150 mm seatpost with 10 mm of engaged
        thread gets a blind hole a little deeper than the engagement,
        nowhere near the member's full 150 mm."""
        # grip (6 mm under the saddle) + 10 mm of engagement.
        res = _only(_seatpost_like(terminal_thickness_m=0.150, length_m=0.006 + 0.010))
        assert res.termination == "tapped"
        assert res.thread_strategy == "tapped"
        assert res.thread is not None
        assert res.thread.engagement_m == pytest.approx(0.010)
        tapped = next(h for h in res.holes if h.kind == "tapped")
        assert tapped.depth_m == pytest.approx(0.010 + _TAPPED_ALLOWANCE_M)
        assert tapped.thread_depth_m == pytest.approx(0.012)
        assert tapped.depth_m < 0.150
        assert not tapped.through

    def test_the_blind_depth_is_capped_at_the_member_thickness(self) -> None:
        """A member thinner than engagement + allowance can't take a
        blind hole deeper than itself — it comes out a through hole
        instead, and the feature says so rather than implying a bottom
        that isn't there."""
        # grip (6 mm) + 6 mm of engagement, into an 8 mm member: engagement
        # (6 mm) + the 3 mm allowance is 9 mm, more than the 8 mm the
        # member actually is.
        res = _only(_seatpost_like(terminal_thickness_m=0.008, length_m=0.006 + 0.006))
        assert res.thread is not None
        assert res.thread.engagement_m == pytest.approx(0.006)
        tapped = next(h for h in res.holes if h.kind == "tapped")
        assert tapped.depth_m == pytest.approx(0.008)
        assert tapped.through

    def test_clearance_holes_through_intermediate_members_are_unaffected(
        self,
    ) -> None:
        """Only the far-end feature changes — the saddle's clearance
        hole still runs the whole way through the saddle, and is marked
        as the through hole it is."""
        res = _only(_seatpost_like(terminal_thickness_m=0.150, length_m=0.006 + 0.010))
        clearance = next(h for h in res.holes if h.kind == "clearance")
        assert clearance.block == "saddle"
        assert clearance.depth_m == pytest.approx(0.006)
        assert clearance.through


class TestBlindThreadFormingDepth:
    """Same defect, same fix, the other strategy the gripe names: a
    thread-forming core hole is a cross-section (``core_hole`` never
    returns a depth), so it hits the exact same ``far.depth_m is None``
    path as a tapped hole."""

    def test_a_thick_printed_terminal_member_gets_a_blind_core_hole(
        self,
    ) -> None:
        res = _only(
            _seatpost_like(
                terminal_thickness_m=0.150,
                length_m=0.006 + 0.010,
                mode="fdm/asa",
                params={"thread_strategy": "thread-forming"},
            )
        )
        assert res.thread_strategy == "thread-forming"
        assert res.thread is not None
        assert res.thread.engagement_m == pytest.approx(0.010)
        core = next(h for h in res.holes if h.kind == "core")
        assert core.depth_m == pytest.approx(0.010 + _CORE_ALLOWANCE_M)
        assert core.depth_m < 0.150
        assert not core.through
