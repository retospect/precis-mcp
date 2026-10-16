"""gr492096 (Reto 2026-10-16, option (2)): a recorded DRC run goes stale
when the board changes after it.

Every recorded run (``op='check'``/``op='review'``) stores the design
revision it checked. The four readers of the latest run — the
``netlist_drc_clean`` gate, the checklist drc-clean item, argue, and the
quest board — call a run whose revision differs from the board's current
one "stale, re-check", and a stale run never counts as clean.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest

from precis.checklist.pcb import _check_drc
from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.pcb.argue import vitals
from precis.workers.auto_check_evaluators import netlist_drc_clean
from precis_web.quest_board import _drc_section

from .test_pcb_declared_pins import _mini_design

pytestmark = pytest.mark.db


@pytest.fixture
def pcb(store):
    return PcbHandler(hub=Hub(store=store))


def _readers(store: Any, ref_id: int) -> dict[str, Any]:
    return {
        "gate": netlist_drc_clean.evaluate(store, {"pcb": ref_id}),
        "checklist": _check_drc(store, ref_id),
        "argue": vitals(store, ref_id)["drc"],
        "quest": _drc_section(store, ref_id),
    }


def _recorded(pcb: PcbHandler, store: Any, slug: str) -> int:
    pcb.put(id=slug, args=_mini_design())
    pcb.put(id=slug, args={"op": "check"})
    return store.get_ref(kind="pcb", id=slug).id


def test_recorded_run_with_no_edit_is_current(pcb: PcbHandler, store: Any) -> None:
    ref_id = _recorded(pcb, store, "st-same")
    run_id, _ = store.pcb_drc_findings_latest(ref_id)
    assert not store.pcb_drc_run_stale(ref_id, run_id)
    # A get view in between writes nothing and changes nothing.
    pcb.get(id="st-same", view="check")
    r = _readers(store, ref_id)
    # _mini_design carries real errors: the verdict is a fail, not "unknown".
    assert r["gate"] is False
    assert r["checklist"].verdict == "fail"
    assert isinstance(r["argue"], dict) and r["argue"]
    assert r["quest"]["has_run"] and not r["quest"]["stale"]


def test_edit_after_check_makes_all_four_readers_stale(
    pcb: PcbHandler, store: Any
) -> None:
    ref_id = _recorded(pcb, store, "st-edit")
    pcb.put(id="st-edit", args={"op": "move", "refdes": "U1", "x": 7.0, "y": 3.0})
    r = _readers(store, ref_id)
    assert r["gate"] is None
    assert r["checklist"].verdict is None
    assert "stale, re-check" in (r["checklist"].reason or "")
    assert r["argue"] == "stale, re-check"
    assert r["quest"]["stale"] is True
    # Re-recording clears it.
    pcb.put(id="st-edit", args={"op": "check"})
    assert _readers(store, ref_id)["quest"]["stale"] is False


def test_run_without_a_revision_stamp_is_stale(pcb: PcbHandler, store: Any) -> None:
    """A run recorded before revision stamping cannot be shown current."""
    ref_id = _recorded(pcb, store, "st-legacy")
    run_id, _ = store.pcb_drc_findings_latest(ref_id)
    with store._pcb_tx() as conn:
        conn.execute(
            "UPDATE pcb_boards SET meta = meta - 'drc_revisions' WHERE ref_id = %s",
            (ref_id,),
        )
    assert store.pcb_drc_run_stale(ref_id, run_id)
    assert netlist_drc_clean.evaluate(store, {"pcb": ref_id}) is None


def test_recording_does_not_change_the_revision(pcb: PcbHandler, store: Any) -> None:
    ref_id = _recorded(pcb, store, "st-rev")
    rev = store.pcb_design_revision(ref_id)
    pcb.put(id="st-rev", args={"op": "check", "phase": "netlist"})
    assert store.pcb_design_revision(ref_id) == rev


def _board_meta(store: Any, ref_id: int) -> Any:
    with store._pcb_conn() as conn:
        return conn.execute(
            "SELECT meta FROM pcb_boards WHERE ref_id = %s AND name = 'main'",
            (ref_id,),
        ).fetchone()[0]


def test_get_views_leave_the_board_meta_untouched(pcb: PcbHandler, store: Any) -> None:
    ref_id = _recorded(pcb, store, "st-meta")
    before = _board_meta(store, ref_id)
    assert before["drc_revisions"]
    for view in ("check", "drc"):
        pcb.get(id="st-meta", view=view)
    assert _board_meta(store, ref_id) == before


def test_phase_claim_changes_the_revision_but_a_cart_record_does_not(
    pcb: PcbHandler, store: Any
) -> None:
    """``op='phase'`` re-tiers severities, so it stales the run; the
    ``jlc_order`` cart record ``op='order'`` writes changes no verdict."""
    ref_id = _recorded(pcb, store, "st-keys")
    rev = store.pcb_design_revision(ref_id)
    store.pcb_merge_board_meta(ref_id, {"jlc_order": {"cart": "x"}})
    assert store.pcb_design_revision(ref_id) == rev
    pcb.put(id="st-keys", args={"op": "phase", "phase": "fab"})
    assert store.pcb_design_revision(ref_id) != rev


def test_check_view_flags_a_stale_baseline_and_a_fresh_run_clears_it(
    pcb: PcbHandler, store: Any
) -> None:
    ref_id = _recorded(pcb, store, "st-view")
    run_id, _ = store.pcb_drc_findings_latest(ref_id)
    assert "stale baseline" not in pcb.get(id="st-view", view="check").body
    pcb.put(id="st-view", args={"op": "move", "refdes": "U1", "x": 7.0, "y": 3.0})
    body = pcb.get(id="st-view", view="check").body
    assert f"vs run {run_id[:8]}" in body
    fresh = store.pcb_drc_run_freshness(ref_id, run_id)
    assert fresh["stale"]
    assert (
        f"stale baseline: that run was recorded "
        f"{fresh['recorded_at'].astimezone(UTC):%Y-%m-%d %H:%MZ} at revision "
        f"{fresh['recorded_revision'][:8]}; the board is now at revision "
        f"{fresh['current_revision'][:8]}"
    ) in body
    assert "stale baseline" in pcb.get(id="st-view", view="drc").body
    # Recording names the stale baseline once; the next read is current.
    assert "stale baseline" in pcb.put(id="st-view", args={"op": "check"}).body
    assert "stale baseline" not in pcb.get(id="st-view", view="check").body


def test_op_check_with_no_edit_keeps_its_baseline_current(
    pcb: PcbHandler, store: Any
) -> None:
    """Recording prunes the superseded run's stamp; freshness is taken
    before the write, so an unchanged board never reads as stale."""
    _recorded(pcb, store, "st-twice")
    assert "stale baseline" not in pcb.put(id="st-twice", args={"op": "check"}).body
