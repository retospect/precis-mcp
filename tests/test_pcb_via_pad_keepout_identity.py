"""``check_via_pad_keepout`` naming (gr451052): a finding must name the pad
by its PART (refdes/pin), not only by its net.

Why this needed its own test file rather than folding into
``test_pcb_drc.py``: the whole point is a net-name collision between two
DIFFERENT physical pads, and the assertion that matters is "which pad did
the finding actually name" — a case ``test_pcb_drc.py``'s existing
single-pad-per-net fixtures never exercise. On the EWOD board every escape
net has TWO members at two different physical locations: an electrode pad
on F.Cu and a driver-IC channel pin on B.Cu. Before this fix, a finding
read ``pad[ARR1_R3C4]`` — a bare net name — and was misread as "the R3C4
electrode, 10mm away", concluded to be geometrically impossible. It was in
fact the driver IC's own land, correctly flagged. A shared net NAME is a
NET match, not proof two features are the same physical part; see the
headline test below.

All geometry here is computed from first principles inside each test (via
radius, pad half-width, ``capability.jlc_min["trace_spacing_mm"]``), never
a margin figure copied from a prior run — this module has a documented
history of tests that pinned the defect they were meant to catch.
"""

from __future__ import annotations

from typing import Any

from precis.pcb import drc
from precis.pcb.capabilities import capability_for

_CAP4 = capability_for("4layer")


def test_check_via_pad_keepout_names_the_close_pad_not_the_far_one_sharing_its_net() -> (
    None
):
    """THE HEADLINE. Two pads share one net at two very different physical
    locations: ``ARR1_R3C4`` (the electrode's own land) sits 10mm away on
    F.Cu; ``U7``'s pin ``5`` (the driver IC's land, a different refdes and
    pin) sits right next to the via, on B.Cu. Only the close pad is a real
    keep-out violation. The finding must identify it as ``U7/5`` — NOT as
    "the ARR1_R3C4 electrode" — even though both pads answer to the same
    net. A shared net name is a NET match, not proof the two pads are the
    same physical feature; conflating the two is exactly the misreading
    that cost a full misdiagnosis on the real board.
    """
    net = "ARR1_R3C4"
    far_electrode = {
        "layer": "F.Cu",
        "net": net,
        "shape": "rect",
        "x": 10.0,
        "y": 0.0,
        "w": 1.0,
        "h": 1.0,
        "refdes": "ARR1_R3C4",
        "pin": "1",
    }
    close_driver_pin = {
        "layer": "B.Cu",
        "net": net,
        "shape": "rect",
        "x": 0.0,
        "y": 0.0,
        "w": 1.0,
        "h": 1.0,
        "refdes": "U7",
        "pin": "5",
    }
    via: dict[str, Any] = {
        "ctype": "via",
        "net": "GND",  # a foreign net -- keeps `via_net` from masking which
        # net the "shared" label below actually belongs to.
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        # No "layers"/"span" key -> `_via_layer_names` defaults to every
        # layer the model has: an ordinary through-hole via, reaching both
        # F.Cu and B.Cu, same as a real plated via would.
    }
    model: dict[str, Any] = {
        "layers": ["F.Cu", "B.Cu"],
        "copper": [via],
        "pads": [far_electrode, close_driver_pin],
    }

    # far_electrode is 10mm away: edge-to-edge gap = (10.0 - 0.5) - 0.3 =
    # 9.2mm, far past any jlc_min trace_spacing_mm figure this table
    # carries -- it must not fire.
    # close_driver_pin sits centred exactly on the via: the via's centre is
    # inside the pad's polygon, so shapely's `distance` is 0 and the edge
    # gap is `0 - via_r` = -0.3mm -- a hard violation.
    findings = drc.check_via_pad_keepout(model, _CAP4)

    assert len(findings) == 1
    f = findings[0]
    assert f.rule == "via_pad_keepout" and f.severity == "error"
    obj = f.objects[0]
    assert obj["pad_net"] == net  # confirms the net-sharing setup is real
    assert obj["pad_refdes"] == "U7"
    assert obj["pad_pin"] == "5"
    assert obj["pad_refdes"] != far_electrode["refdes"]
    assert obj["pad_pin"] != far_electrode["pin"]


def test_check_via_pad_keepout_detail_names_refdes_and_pin_not_just_net() -> None:
    """The human-readable text (``where``/``detail``), not only the
    structured ``objects`` dict, must carry the refdes/pin -- a reader
    skimming the finding text alone must not be able to repeat the
    net-only misdiagnosis."""
    net = "SIG"
    pad = {
        "layer": "F.Cu",
        "net": net,
        "shape": "rect",
        "x": 0.0,
        "y": 0.0,
        "w": 1.0,
        "h": 1.0,
        "refdes": "U3",
        "pin": "2",
    }
    via = {
        "ctype": "via",
        "net": net,
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],
    }
    model: dict[str, Any] = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}

    findings = drc.check_via_pad_keepout(model, _CAP4)

    assert len(findings) == 1
    f = findings[0]
    assert "U3/2" in f.where
    assert "U3/2" in f.detail
    # The OLD net-only bracket wording must not survive for the pad side
    # of the pairing once refdes/pin identity is available -- that bare
    # form is the whole misdiagnosis this test guards against.
    assert f"pad[{net}]" not in f.where
    assert f"pad[{net}]" not in f.detail


def test_check_via_pad_keepout_falls_back_to_net_only_wording_without_identity() -> (
    None
):
    """Degenerate case: a pad with no refdes/pin (some pads, e.g.
    synthesized ones, may lack that identity per the production code's own
    comment). The identity upgrade must not raise, and must still produce
    a well-formed finding -- falling back to the net-only wording the rule
    always used before this change."""
    net = "SIG"
    pad = {
        "layer": "F.Cu",
        "net": net,
        "shape": "rect",
        "x": 0.0,
        "y": 0.0,
        "w": 1.0,
        "h": 1.0,
        # deliberately no "refdes"/"pin" key at all
    }
    via = {
        "ctype": "via",
        "net": net,
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],
    }
    model: dict[str, Any] = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}

    findings = drc.check_via_pad_keepout(model, _CAP4)

    assert len(findings) == 1
    f = findings[0]
    assert f"pad[{net}]" in f.where
    assert f"pad[{net}]" in f.detail
    assert f.objects[0]["pad_refdes"] is None
    assert f.objects[0]["pad_pin"] is None


def test_check_via_pad_keepout_stays_quiet_when_comfortably_clear_of_every_pad() -> (
    None
):
    """The identity change touched only naming, never the fire condition:
    a via that clears every pad by more than the fab's required copper
    isolation must still report nothing at all."""
    required = _CAP4.jlc_min["trace_spacing_mm"]
    assert required is not None
    pad_half = 0.5  # a 1.0mm square pad, half-width from its centre
    via_r = 0.3  # dia_mm=0.6
    slack = 0.05  # comfortably clear, not pinned to the exact margin
    # Centre-to-centre x offset that leaves an edge-to-edge gap of exactly
    # `required + slack`: the pad's near edge sits at (x - pad_half), the
    # via's own edge at `via_r` out from its centre at x=0, so the gap
    # between them is `(x - pad_half) - via_r`.
    x = pad_half + via_r + required + slack
    pad = {
        "layer": "F.Cu",
        "net": "SIG",
        "shape": "rect",
        "x": x,
        "y": 0.0,
        "w": 1.0,
        "h": 1.0,
        "refdes": "U9",
        "pin": "3",
    }
    via = {
        "ctype": "via",
        "net": "SIG",
        "x": 0.0,
        "y": 0.0,
        "dia_mm": 0.6,
        "drill_mm": 0.3,
        "layers": ["F.Cu"],
    }
    model: dict[str, Any] = {"layers": ["F.Cu"], "copper": [via], "pads": [pad]}

    assert drc.check_via_pad_keepout(model, _CAP4) == []
