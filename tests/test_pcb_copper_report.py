"""The copper report: a source board's own copper measured against the
rules precis resolves (pcb-epro-import slice 1c).

Every number here is chosen against the ``4layer`` capability row: fab
minimum 0.09 mm track/gap and 0.25 mm via / 0.15 mm drill, house default
0.15 mm track/gap. The report must call out anything below the FAB minimum by name
(the spec: "must be called out, not averaged away"), and flag every other
disagreement with the rule a re-route would draw to.
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.pcb import copper_report
from precis.pcb.capabilities import capability_for
from precis.pcb.rules import resolve_net_rules

LAYERS = ["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"]


@pytest.fixture
def cap():
    return capability_for("4layer")


def _track(net: str, y: float, width: float, layer: str = "F.Cu") -> dict[str, Any]:
    return {
        "ctype": "track",
        "layer": layer,
        "net": net,
        "segments": [{"shape": "line", "start": [0.0, y], "end": [10.0, y]}],
        "width_mm": width,
    }


def _via(net: str, x: float, y: float, dia: float, drill: float) -> dict[str, Any]:
    return {
        "ctype": "via",
        "layer": "F.Cu",
        "net": net,
        "x": x,
        "y": y,
        "dia_mm": dia,
        "drill_mm": drill,
        "span": ["F.Cu", "B.Cu"],
    }


def _pad(net: str, x: float, y: float, w: float = 0.5) -> dict[str, Any]:
    return {
        "net": net,
        "layer": "F.Cu",
        "shape": "rect",
        "x": x,
        "y": y,
        "w": w,
        "h": w,
    }


def _rules(cap, *names: str):
    return {n: resolve_net_rules("", layer_is_outer=True, fab_caps=cap) for n in names}


def _findings(rep: copper_report.CopperReport, net: str) -> list[str]:
    return next(c for c in rep.nets if c.measured.net == net).findings


def test_a_track_below_the_fab_minimum_is_called_out_by_name(cap) -> None:
    rep = copper_report.report(
        [_track("A", 0.0, 0.08)], [], LAYERS, _rules(cap, "A"), cap
    )
    assert any(
        f.startswith("BELOW FAB MINIMUM: source track 0.080")
        for f in _findings(rep, "A")
    )


def test_a_track_wider_than_the_rule_hints_at_a_missing_current(cap) -> None:
    """The most useful finding for the annotation step: the author drew
    1 mm where precis would draw 0.15 mm, so the net probably carries a
    current the spec does not know yet."""
    rep = copper_report.report(
        [_track("PWR", 0.0, 1.0), _track("SIG", 5.0, 0.15), _track("SIG", 9.0, 0.15)],
        [],
        LAYERS,
        _rules(cap, "PWR", "SIG"),
        cap,
    )
    (finding,) = _findings(rep, "PWR")
    assert "source widens to 1.000 mm where precis would draw 0.150 mm" in finding


def test_the_authors_default_width_is_said_once_not_per_net(cap) -> None:
    """Measured on the first real board: every net used the author's
    0.254 mm default against precis' 0.150 mm, and flagging that per net
    flagged all 89 and buried the genuinely wide ones. It is one fact about
    the board, so it is one line."""
    tracks = [_track(n, 2.0 * i, 0.254) for i, n in enumerate(("A", "B", "C"))]
    tracks.append(_track("PWR", 20.0, 1.0))
    rep = copper_report.report(
        tracks, [], LAYERS, _rules(cap, "A", "B", "C", "PWR"), cap
    )
    assert [c.measured.net for c in rep.nets if c.findings] == ["PWR"]
    assert rep.board[0] == (
        "author's default track 0.254 mm; precis draws 0.150 mm for an unannotated net"
    )


def test_a_track_at_the_rule_has_no_finding(cap) -> None:
    rep = copper_report.report(
        [_track("A", 0.0, 0.15)], [], LAYERS, _rules(cap, "A"), cap
    )
    assert _findings(rep, "A") == []


def test_the_gap_between_two_nets_is_measured_edge_to_edge(cap) -> None:
    # Centres 0.3 mm apart, 0.15 mm wide -> 0.15 mm of air.
    rep = copper_report.report(
        [_track("A", 0.0, 0.15), _track("B", 0.3, 0.15)],
        [],
        LAYERS,
        _rules(cap, "A", "B"),
        cap,
    )
    a = next(c for c in rep.nets if c.measured.net == "A").measured
    assert a.closest is not None
    assert a.closest.gap_mm == pytest.approx(0.15, abs=1e-3)
    assert a.closest.other_net == "B"
    assert a.closest.layer == "F.Cu"
    assert rep.layers["F.Cu"][1] == pytest.approx(0.15, abs=1e-3)


def test_a_tight_gap_is_flagged_against_the_rule_and_the_floor(cap) -> None:
    # Centres 0.23 mm apart -> 0.08 mm of air: below rule AND fab minimum.
    rep = copper_report.report(
        [_track("A", 0.0, 0.15), _track("B", 0.23, 0.15)],
        [],
        LAYERS,
        _rules(cap, "A", "B"),
        cap,
    )
    f = _findings(rep, "A")
    assert any(x.startswith("BELOW FAB MINIMUM: source gap 0.080 mm to B") for x in f)
    assert any(
        "source runs 0.080 mm from B on F.Cu where precis requires 0.150" in x
        for x in f
    )


def test_same_net_copper_is_never_a_gap(cap) -> None:
    rep = copper_report.report(
        [_track("A", 0.0, 0.15), _track("A", 0.2, 0.15)],
        [],
        LAYERS,
        _rules(cap, "A"),
        cap,
    )
    assert rep.nets[0].measured.closest is None


def test_copper_on_different_layers_is_never_a_gap(cap) -> None:
    rep = copper_report.report(
        [_track("A", 0.0, 0.15), _track("B", 0.0, 0.15, layer="B.Cu")],
        [],
        LAYERS,
        _rules(cap, "A", "B"),
        cap,
    )
    assert all(c.measured.closest is None for c in rep.nets)


def test_a_pad_closes_a_gap_but_a_pad_pair_does_not(cap) -> None:
    """Two pads of a fine-pitch part sit close because the PART says so;
    that is a footprint fact, not a routing decision. A track running past
    a foreign pad is a routing decision and must be measured."""
    pads = [_pad("X", 20.0, 0.0), _pad("Y", 20.6, 0.0)]  # 0.1 mm apart
    rep = copper_report.report(
        [_track("A", 0.4, 0.15)],  # 10 mm from both pads: out of probe range
        pads,
        LAYERS,
        _rules(cap, "A", "X", "Y"),
        cap,
    )
    assert (
        rep.layers.get("F.Cu", (None, None))[1] is None
    )  # the pad pair is not counted

    near = [_pad("X", 10.0, 0.5)]  # pad edge y=0.25; track edge y=0.075
    rep = copper_report.report(
        [_track("A", 0.0, 0.15)], near, LAYERS, _rules(cap, "A", "X"), cap
    )
    a = rep.nets[0].measured
    assert a.closest is not None and a.closest.other_net == "X"
    assert a.closest.gap_mm == pytest.approx(0.175, abs=1e-3)


def test_vias_are_measured_and_compared(cap) -> None:
    """A via below the author's own common via AND precis' rule is a
    per-net finding; the common via against the rule is said once."""
    rep = copper_report.report(
        [
            _via("A", 0.0, 0.0, 0.2, 0.1),
            _via("B", 5.0, 0.0, 0.6, 0.3),
            _via("C", 9.0, 0.0, 0.6, 0.3),
        ],
        [],
        LAYERS,
        _rules(cap, "A", "B", "C"),
        cap,
    )
    assert any(b.startswith("author's via 0.600/0.300 mm") for b in rep.board)
    assert _findings(rep, "B") == [] and _findings(rep, "C") == []
    m = rep.nets[0].measured
    assert (m.n_vias, m.via_dia_min_mm, m.via_drill_min_mm) == (1, 0.2, 0.1)
    f = rep.nets[0].findings
    assert any(x.startswith("BELOW FAB MINIMUM: source via diameter 0.200") for x in f)
    assert any(x.startswith("BELOW FAB MINIMUM: source via drill 0.100") for x in f)
    rule = rep.nets[0].rules
    assert rule is not None and rule.via_dia_mm is not None
    assert any(
        f"source via 0.200 mm is smaller than precis' {rule.via_dia_mm:.3f} mm" in x
        for x in f
    )


def test_a_net_precis_does_not_know_is_reported_not_dropped(cap) -> None:
    rep = copper_report.report([_track("GHOST", 0.0, 0.15)], [], LAYERS, {}, cap)
    assert _findings(rep, "GHOST") == [
        "precis has no such net, so there is no rule to compare"
    ]


def test_no_pads_is_a_written_note_not_a_silent_gap(cap) -> None:
    rep = copper_report.report(
        [_track("A", 0.0, 0.15)], [], LAYERS, _rules(cap, "A"), cap
    )
    assert any("no pads given" in n for n in rep.notes)
    rep = copper_report.report(
        [_track("A", 0.0, 0.15)], [_pad("X", 50.0, 50.0)], LAYERS, _rules(cap, "A"), cap
    )
    assert rep.notes == []


def test_render_lists_the_disagreeing_nets_first(cap) -> None:
    rep = copper_report.report(
        [_track("AAA", 0.0, 0.15), _track("AAA", 2.0, 0.15), _track("ZZZ", 5.0, 1.0)],
        [],
        LAYERS,
        _rules(cap, "AAA", "ZZZ"),
        cap,
    )
    lines = copper_report.render(rep)
    assert "  2 net(s) carry copper; 1 disagree with precis' rules" in lines
    net_lines = [ln for ln in lines if ln.startswith("  AAA") or ln.startswith("  ZZZ")]
    assert net_lines[0].startswith("  ZZZ")
    assert any(ln.startswith("    ! source widens") for ln in lines)
