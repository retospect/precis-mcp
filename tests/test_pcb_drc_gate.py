"""``netlist_drc_clean`` auto_check evaluator — pure logic over a stubbed
store (no DB): reads the latest persisted DRC run, never recomputes it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

from precis.workers.auto_check_evaluators import netlist_drc_clean

if TYPE_CHECKING:
    from precis.store import Store


class _StubStore:
    """Duck-types just the one method the evaluator calls."""

    def __init__(self, latest: tuple[str | None, list[dict[str, Any]]]) -> None:
        self._latest = latest

    def pcb_drc_findings_latest(
        self, ref_id: int
    ) -> tuple[str | None, list[dict[str, Any]]]:
        return self._latest


def _store(latest: tuple[str | None, list[dict[str, Any]]]) -> Store:
    return cast("Store", _StubStore(latest))


def test_none_when_no_run_recorded_yet():
    store = _store((None, []))
    result = netlist_drc_clean.evaluate(store, {"type": "netlist_drc_clean", "pcb": 42})
    assert result is None  # "not yet checked", not "clean"


def test_false_when_latest_run_has_an_error_finding():
    findings = [
        {"rule": "trace_width", "severity": "warn", "objects": [], "detail": "…"},
        {"rule": "clearance", "severity": "error", "objects": [], "detail": "…"},
    ]
    store = _store(("run1", findings))
    assert netlist_drc_clean.evaluate(store, {"pcb": 42}) is False


def test_true_when_latest_run_has_only_warnings():
    findings = [
        {"rule": "trace_width", "severity": "warn", "objects": [], "detail": "…"}
    ]
    store = _store(("run1", findings))
    assert netlist_drc_clean.evaluate(store, {"pcb": 42}) is True


def test_true_when_latest_run_is_clean():
    store = _store(("run1", []))
    assert netlist_drc_clean.evaluate(store, {"pcb": 42}) is True


def test_missing_pcb_raises_bad_input():
    from precis.errors import BadInput

    store = _store((None, []))
    try:
        netlist_drc_clean.evaluate(store, {"type": "netlist_drc_clean"})
    except BadInput as exc:
        assert "netlist_drc_clean" in str(exc)
    else:
        raise AssertionError("expected BadInput for a missing pcb= arg")
