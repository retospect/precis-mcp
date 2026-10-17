"""The one route-state rule (precis.pcb.route_state): every consumer reads
these four states, so the edges live here once."""

from __future__ import annotations

from precis.pcb import DANGLING_NET_NOTE
from precis.pcb.route_state import RouteState, classify, summary_line


def _net(name: str, status: str = "unrouted", note: str | None = None) -> dict:
    return {
        "name": name,
        "net_class": None,
        "domain": None,
        "status": status,
        "note": note,
    }


def _err(rule: str = "clearance", waived_by: str | None = None) -> dict:
    return {"rule": rule, "severity": "error", "detail": "x", "waived_by": waived_by}


def test_no_nets_is_not_started_and_routed_is_none():
    rs = classify([])
    assert rs.state is RouteState.NOT_STARTED
    assert rs.routed is None


def test_all_unrouted_is_not_started():
    rs = classify([_net("A"), _net("B")])
    assert rs.state is RouteState.NOT_STARTED
    assert rs.routed is None
    assert [r["name"] for r in rs.pending] == ["A", "B"]


def test_only_dangling_net_after_a_run_is_done():
    rs = classify([_net("TP", "realized", DANGLING_NET_NOTE)])
    assert rs.state is RouteState.DONE
    assert rs.counts == {"dangling": 1}
    assert rs.routed is True


def test_some_net_failed_is_partial_with_issue():
    rs = classify([_net("A", "realized"), _net("B", "failed")])
    assert rs.state is RouteState.PARTIAL
    assert rs.routed is False
    assert [r["name"] for r in rs.pending] == ["B"]


def test_partial_wins_over_drc_errors():
    rs = classify(
        [_net("A", "realized"), _net("B")], drc_run_id="r1", drc_findings=[_err()]
    )
    assert rs.state is RouteState.PARTIAL


def test_all_realized_with_fresh_drc_error_is_drc_fail():
    rs = classify([_net("A", "realized")], drc_run_id="r1", drc_findings=[_err()])
    assert rs.state is RouteState.DRC_FAIL
    assert rs.routed is True
    assert rs.drc_clean is False
    assert [f["rule"] for f in rs.drc_errors] == ["clearance"]


def test_waived_error_and_warn_do_not_fail():
    findings = [_err(waived_by="reto"), {**_err(), "severity": "warn"}]
    rs = classify([_net("A", "realized")], drc_run_id="r1", drc_findings=findings)
    assert rs.state is RouteState.DONE
    assert rs.drc_clean is True


def test_stale_or_missing_drc_is_done_with_unknown_verdict():
    nets = [_net("A", "realized")]
    stale = classify(nets, drc_run_id="r1", drc_findings=[_err()], drc_stale=True)
    assert stale.state is RouteState.DONE
    assert stale.drc_clean is None
    assert classify(nets).drc_clean is None


def test_summary_line_boardless_reads_unrouted():
    assert summary_line(3, {}) == "3 net(s): 3 unrouted"
    assert summary_line(2, {"realized": 1, "failed": 1}) == (
        "2 net(s): 1 routed, 1 failed"
    )
