"""The post-route legality gate — :func:`precis.pcb.session.
routed_drc_findings` + :func:`precis.pcb.session.strip_drc_violating_nets`
(docs/backlog/pcb-always-valid-board-invariant.md: "``route`` on a board it
cannot fully realize does not leave stored copper with unreported DRC
errors"). Pure: a hand-built IR and RealizeResult, no DB, no router.

Layout: three 2-pin nets, A (U1-U2), B (U3-U4), C (U5-U6), on one top-side
row each. B's track is drawn straight across U1's pad (net A's land) — the
violation; A's and C's tracks are clean (the negative control: without it
"always strip" would pass).
"""

from __future__ import annotations

from typing import Any

from precis.pcb import DEFAULT_STACKUP
from precis.pcb import session as pcb_session
from precis.pcb.capabilities import capability_for
from precis.pcb.drc import process_for_stackup
from precis.pcb.ir import PcbIR, from_graph
from precis.pcb.realize import RealizedTrack, RealizeResult


def _ir() -> PcbIR:
    xy = {
        "U1": (0.0, 0.0),
        "U2": (10.0, 0.0),
        "U3": (0.0, 10.0),
        "U4": (10.0, 10.0),
        "U5": (0.0, 20.0),
        "U6": (10.0, 20.0),
    }
    graph: dict[str, Any] = {
        "instances": [{"refdes": r, "x": x, "y": y} for r, (x, y) in xy.items()],
        "nets": [
            {
                "name": name,
                "members": [{"refdes": a, "pin": "1"}, {"refdes": b, "pin": "1"}],
            }
            for name, a, b in (("A", "U1", "U2"), ("B", "U3", "U4"), ("C", "U5", "U6"))
        ],
    }
    return from_graph(graph, stackup=DEFAULT_STACKUP)


def _track(
    ir: PcbIR, net: str, a: tuple[float, float], b: tuple[float, float]
) -> RealizedTrack:
    net_id = [str(n) for n in ir.net_name].index(net)
    return RealizedTrack(
        seg_id=net_id,
        net_id=net_id,
        layer=0,
        segments=({"shape": "line", "start": list(a), "end": list(b)},),
        length_mm=abs(b[0] - a[0]) + abs(b[1] - a[1]),
        blocked_by=None,
        width_mm=0.2,
    )


def _result(*tracks: RealizedTrack) -> RealizeResult:
    return RealizeResult(tracks=tracks, vias=(), warnings=())


def _findings(
    ir: PcbIR, rres: RealizeResult, fixed: list[dict[str, Any]] | None = None
) -> list[Any]:
    return pcb_session.routed_drc_findings(
        ir,
        rres,
        capability=capability_for(process_for_stackup(ir.stackup)),
        fixed_copper=fixed,
        outline=[[-30.0, -30.0], [40.0, -30.0], [40.0, 40.0], [-30.0, 40.0]],
    )


def _nets(rres: RealizeResult, ir: PcbIR) -> set[str]:
    return {str(ir.net_name[t.net_id]) for t in rres.tracks}


def test_violating_net_is_stripped_and_clean_nets_keep_their_copper() -> None:
    ir = _ir()
    rres = _result(
        _track(ir, "A", (3.0, 0.0), (10.0, 0.0)),
        _track(ir, "B", (-2.0, 0.0), (2.0, 0.0)),  # across U1's pad (net A)
        _track(ir, "C", (0.0, 20.0), (10.0, 20.0)),
    )
    findings = _findings(ir, rres)
    assert any(f.rule == "clearance" and f.severity == "error" for f in findings)

    stripped, problems = pcb_session.strip_drc_violating_nets(ir, rres, findings)

    assert set(problems) == {"B"}  # A is the pad's owner, not router copper in conflict
    assert {p["reason"] for p in problems["B"]} == {"drc:clearance"}
    assert all(p["kind"] == "drc" and p["message"] for p in problems["B"])
    assert _nets(stripped, ir) == {"A", "C"}
    assert len(rres.tracks) == 3  # the input is not mutated


def test_stripped_result_rechecks_clean() -> None:
    ir = _ir()
    rres = _result(
        _track(ir, "A", (3.0, 0.0), (10.0, 0.0)),
        _track(ir, "B", (-2.0, 0.0), (2.0, 0.0)),
        _track(ir, "C", (0.0, 20.0), (10.0, 20.0)),
    )
    stripped, _ = pcb_session.strip_drc_violating_nets(ir, rres, _findings(ir, rres))

    again = _findings(ir, stripped)
    router_errors = [
        f
        for f in again
        if f.severity == "error" and any(o.get("derived") for o in f.objects)
    ]
    assert router_errors == []
    _, problems = pcb_session.strip_drc_violating_nets(ir, stripped, again)
    assert problems == {}


def test_two_conflicting_router_nets_are_both_stripped() -> None:
    ir = _ir()
    rres = _result(
        _track(ir, "A", (0.0, 5.0), (10.0, 5.0)),
        _track(ir, "B", (0.0, 5.05), (10.0, 5.05)),  # 0.05 mm centre apart: overlap
        _track(ir, "C", (0.0, 20.0), (10.0, 20.0)),
    )
    stripped, problems = pcb_session.strip_drc_violating_nets(
        ir, rres, _findings(ir, rres)
    )
    assert set(problems) == {"A", "B"}
    assert _nets(stripped, ir) == {"C"}


def test_finding_between_pad_and_fixed_copper_only_strips_nothing() -> None:
    ir = _ir()
    fixed = [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "B",
            "fixed": True,
            "width_mm": 0.2,
            "segments": [{"shape": "line", "start": [-2.0, 0.0], "end": [2.0, 0.0]}],
        }
    ]
    rres = _result(_track(ir, "C", (0.0, 20.0), (10.0, 20.0)))
    findings = _findings(ir, rres, fixed)
    # The fixed track on U1's pad is a real error — just not the router's.
    assert any(f.rule == "clearance" and f.severity == "error" for f in findings)

    stripped, problems = pcb_session.strip_drc_violating_nets(ir, rres, findings)

    assert problems == {}
    assert stripped is rres


def test_router_net_sharing_a_name_with_fixed_copper_is_only_stripped_for_its_own() -> (
    None
):
    """A fixed net-B track on a pad must not get the ROUTER's net-B copper
    stripped when that copper is itself clean: the ``derived`` tag, not the
    net name, decides who yields."""
    ir = _ir()
    fixed = [
        {
            "ctype": "track",
            "layer": "F.Cu",
            "net": "B",
            "fixed": True,
            "width_mm": 0.2,
            "segments": [{"shape": "line", "start": [-2.0, 0.0], "end": [2.0, 0.0]}],
        }
    ]
    rres = _result(_track(ir, "B", (0.0, 10.0), (10.0, 10.0)))
    stripped, problems = pcb_session.strip_drc_violating_nets(
        ir, rres, _findings(ir, rres, fixed)
    )
    assert problems == {}
    assert _nets(stripped, ir) == {"B"}


def test_a_router_via_on_a_foreign_pad_is_stripped_with_its_rule() -> None:
    from precis.pcb.realize import RealizedVia

    ir = _ir()
    net_b = [str(n) for n in ir.net_name].index("B")
    via = RealizedVia(
        seg_id=net_b,
        net_id=net_b,
        x=0.0,
        y=0.0,  # on U1's pad
        dia_mm=0.6,
        drill_mm=0.3,
        layer_lo=0,
        layer_hi=1,
        endpoint="a",
    )
    rres = RealizeResult(
        tracks=(_track(ir, "C", (0.0, 20.0), (10.0, 20.0)),),
        vias=(via,),
        warnings=(),
    )
    stripped, problems = pcb_session.strip_drc_violating_nets(
        ir, rres, _findings(ir, rres)
    )
    assert set(problems) == {"B"}
    assert "drc:via_pad_keepout" in {p["reason"] for p in problems["B"]}
    assert stripped.vias == ()
    assert _nets(stripped, ir) == {"C"}
