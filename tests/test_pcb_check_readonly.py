"""gr491591 (Reto 2026-10-16, option (a)): the pcb check views are reads.

``get`` ``view='check'``/``'drc'`` compute the run and delta it against
the last RECORDED run, but write no ``pcb_drc_runs``/``pcb_drc_findings``
rows. ``put(args={'op':'check'})`` is the explicit way to record one
(``op='review'`` records too — ``test_pcb_review_job.py``).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler

from .test_pcb_declared_pins import _mini_design

pytestmark = pytest.mark.db


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _counts(store: Any) -> tuple[int, int]:
    with store._pcb_conn() as conn:
        runs = conn.execute("SELECT count(*) FROM pcb_drc_runs").fetchone()[0]
        rows = conn.execute("SELECT count(*) FROM pcb_drc_findings").fetchone()[0]
    return int(runs), int(rows)


@pytest.mark.parametrize(
    ("view", "args"),
    [
        ("check", None),
        ("check", {"phase": "netlist"}),
        ("check", {"phase": "fab"}),
        ("drc", None),
    ],
)
def test_get_check_views_write_no_run(
    pcb: PcbHandler, store: Any, view: str, args: dict[str, Any] | None
) -> None:
    pcb.put(id="ro-board", args=_mini_design())
    before = _counts(store)
    out = pcb.get(id="ro-board", view=view, args=args)
    assert _counts(store) == before
    assert "unrecorded run" in out.body
    assert "args={'op':'check'}" in out.body


def test_gerber_drc_banner_writes_no_run(pcb: PcbHandler, store: Any) -> None:
    """``view='gerber'`` asks the same DRC question — also a read."""
    pcb.put(id="ro-gerber", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="ro-gerber")
    before = _counts(store)
    banner, _n_err = pcb._gerber_drc_banner(ref.id, "ro-gerber")
    assert _counts(store) == before
    assert "unrecorded run" in banner


def test_op_check_records_and_get_compares_against_it(
    pcb: PcbHandler, store: Any
) -> None:
    pcb.put(id="ro-rec", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="ro-rec")
    runs0, rows0 = _counts(store)

    rec = pcb.put(id="ro-rec", args={"op": "check"})
    run_id, findings = store.pcb_drc_findings_latest(ref.id)
    assert run_id is not None
    assert _counts(store) == (runs0 + 1, rows0 + len(findings))
    assert f"run {run_id[:8]}" in rec.body
    assert "unrecorded run" not in rec.body

    # A later read names the recorded run and still writes nothing.
    before = _counts(store)
    out = pcb.get(id="ro-rec", view="check")
    assert _counts(store) == before
    assert f"vs run {run_id[:8]}" in out.body
    assert "unrecorded run" in out.body
    assert store.pcb_drc_findings_latest(ref.id)[0] == run_id


def test_op_check_takes_the_check_args(pcb: PcbHandler, store: Any) -> None:
    """``op='check'`` is ``view='check'`` plus the write: its phase
    argument scopes the recorded run the same way."""
    pcb.put(id="ro-phase", args=_mini_design())
    out = pcb.put(id="ro-phase", args={"op": "check", "phase": "netlist"})
    assert "phase netlist (args)" in out.body
    ref = store.get_ref(kind="pcb", id="ro-phase")
    board = store.pcb_load(ref.id)["board"]
    run_id, _ = store.pcb_drc_findings_latest(ref.id)
    with store._pcb_conn() as conn:
        meta = conn.execute(
            "SELECT meta FROM pcb_boards WHERE board_id = %s", (board["board_id"],)
        ).fetchone()[0]
    assert meta["drc_runs_by_phase"]["netlist"] == run_id


def test_drc_run_default_is_read_only(pcb: PcbHandler, store: Any) -> None:
    pcb.put(id="ro-default", args=_mini_design())
    ref = store.get_ref(kind="pcb", id="ro-default")
    before = _counts(store)
    ran = pcb._drc_run(ref.id, store.pcb_load(ref.id))
    assert ran is not None and ran.run_id is None
    assert _counts(store) == before
    recorded = pcb._drc_run(ref.id, store.pcb_load(ref.id), record=True)
    assert recorded is not None and recorded.run_id is not None
    assert _counts(store)[0] == before[0] + 1
