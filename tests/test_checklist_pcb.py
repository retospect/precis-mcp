"""checklist kind, slice 2+3 (docs/backlog/checklist-kind.md): the pcb
pre-tapeout instance, the pcb bridge (fingerprints + tool checkers),
kind-default assignments, argument-thread rendering, and the
``checklist_clean`` evaluator — all on the real test DB.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.checklist import compute_statuses, current_fingerprint
from precis.checklist.pcb import TOOL_CHECKERS, PcbBridge
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers.checklist import ChecklistHandler
from precis.jobs.checklist_sync import (
    bundled_checklists_dir,
    check_drift,
    load_checklist_file,
    sync_all,
)
from precis.store import Store
from precis.workers.auto_check_evaluators import (
    REGISTRY,
    checklist_clean,
    validate_auto_check_spec,
)

_TAPEOUT = Path(__file__).resolve().parents[1] / (
    "src/precis/data/checklists/pcb-tapeout.yaml"
)


def _handler(store: Store) -> ChecklistHandler:
    return ChecklistHandler(hub=Hub(store=store))


def _board(store: Store, slug: str = "cl-board") -> int:
    """A two-part board: U1 (3 pins) and R1 (2 pins), nets VCC/GND/SIG;
    U1.3 is left unconnected on purpose (a netlist exception)."""
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[
            {
                "refdes": "U1",
                "label": "MCU",
                "pins": [{"name": "1"}, {"name": "2"}, {"name": "3"}],
            },
            {"refdes": "R1", "label": "R", "pins": [{"name": "1"}, {"name": "2"}]},
        ],
        nets=[{"name": "VCC"}, {"name": "GND"}, {"name": "SIG"}],
        connections=[
            {"net": "VCC", "refdes": "U1", "pin": "1"},
            {"net": "VCC", "refdes": "R1", "pin": "1"},
            {"net": "GND", "refdes": "U1", "pin": "2"},
            {"net": "GND", "refdes": "R1", "pin": "2"},
        ],
    )
    return int(ref.id)


def _local_checklist(store: Store, name: str = "cl", **extra: Any) -> ChecklistHandler:
    h = _handler(store)
    h.put(
        id=name,
        items=[
            {
                "name": "judge-me",
                "phase": "schematic",
                "severity": "blocking",
                "prevents": "a judgment failure ships",
            },
            {
                "name": "drc-clean",
                "phase": "layout",
                "severity": "blocking",
                "decidability": "tool",
                "prevents": "a DRC error ships",
            },
            {
                "name": "advice",
                "phase": "fab",
                "severity": "advisory",
                "prevents": "an advisory failure ships",
            },
        ],
        **extra,
    )
    return h


# ── shipped content ─────────────────────────────────────────────────────


class TestShippedTapeout:
    def test_yaml_loads_with_phases_and_mandatory_prevents(self) -> None:
        cf = load_checklist_file(_TAPEOUT)
        assert cf.name == "pcb-tapeout"
        assert cf.default_for == ("pcb",)
        assert len(cf.items) >= 40
        phases = []
        for it in cf.items:
            assert str(it.get("prevents") or "").strip(), it["name"]
            assert it["severity"] in ("blocking", "advisory")
            if it["phase"] not in phases:
                phases.append(it["phase"])
        assert phases == ["schematic", "netlist", "layout", "fab"]

    def test_every_tool_item_has_a_checker_and_vice_versa(self) -> None:
        cf = load_checklist_file(_TAPEOUT)
        tool_items = {it["name"] for it in cf.items if it.get("decidability") == "tool"}
        assert tool_items == set(TOOL_CHECKERS)

    def test_item_names_unique(self) -> None:
        cf = load_checklist_file(_TAPEOUT)
        names = [it["name"] for it in cf.items]
        assert len(names) == len(set(names))

    def test_bundled_sync_lands_pcb_tapeout_as_pcb_default(self, store: Store) -> None:
        out = sync_all(store, src_dir=bundled_checklists_dir())
        assert out["status"] == "ok"
        cl = store.checklist_get("pcb-tapeout")
        assert cl is not None
        assert cl["origin"] == "shipped"
        assert cl["default_for"] == ["pcb"]
        items = store.checklist_items_current(cl["id"])
        assert [it["phase"] for it in items][:1] == ["schematic"]
        assert items[-1]["phase"] == "fab"
        assert all(it["origin"] == "shipped" for it in items)
        assert check_drift(store, src_dir=bundled_checklists_dir()) == []

    def test_sync_default_for_change_is_applied_and_drift_seen(
        self, tmp_path: Path, store: Store
    ) -> None:
        yaml_path = tmp_path / "cl.yaml"
        yaml_path.write_text(
            "name: dflt\nitems:\n  - name: a\n    prevents: x\n", encoding="utf-8"
        )
        sync_all(store, src_dir=tmp_path)
        cl = store.checklist_get("dflt")
        assert cl is not None and cl["default_for"] == []
        yaml_path.write_text(
            "name: dflt\ndefault_for: [pcb]\nitems:\n  - name: a\n    prevents: x\n",
            encoding="utf-8",
        )
        assert check_drift(store, src_dir=tmp_path) == [
            {
                "checklist": "dflt",
                "item": None,
                "reason": "default_for differs from the shipped file",
            }
        ]
        sync_all(store, src_dir=tmp_path)
        cl = store.checklist_get("dflt")
        assert cl is not None and cl["default_for"] == ["pcb"]
        assert check_drift(store, src_dir=tmp_path) == []


# ── pcb bridge: fingerprints ─────────────────────────────────────────────


class TestPcbFingerprint:
    def test_whole_target_changes_on_any_edit(self, store: Store) -> None:
        ref_id = _board(store)
        before = current_fingerprint(store, kind="pcb", ref_id=ref_id)
        assert before is not None and not before.missing
        store.pcb_apply(
            slug="cl-board",
            title="cl-board",
            components=[{"refdes": "C1", "pins": [{"name": "1"}, {"name": "2"}]}],
            nets=[],
            connections=[{"net": "GND", "refdes": "C1", "pin": "1"}],
        )
        after = current_fingerprint(store, kind="pcb", ref_id=ref_id)
        assert after is not None and after.digest != before.digest

    def test_anchored_ignores_unrelated_edits_and_sees_own(self, store: Store) -> None:
        ref_id = _board(store)
        bridge = PcbBridge()
        r1_before = bridge.fingerprint(store, ref_id, ["R1"])
        sig_before = bridge.fingerprint(store, ref_id, ["SIG"])
        # An unrelated component joining GND changes GND's members but not R1...
        # — R1 is ON GND, so anchor to SIG instead for the "unrelated" case.
        store.pcb_apply(
            slug="cl-board",
            title="cl-board",
            components=[{"refdes": "C1", "pins": [{"name": "1"}]}],
            nets=[],
            connections=[{"net": "VCC", "refdes": "C1", "pin": "1"}],
        )
        assert bridge.fingerprint(store, ref_id, ["SIG"]).digest == sig_before.digest
        # R1 sits on VCC, so its neighbourhood changed.
        assert bridge.fingerprint(store, ref_id, ["R1"]).digest != r1_before.digest

    def test_missing_anchor_reported(self, store: Store) -> None:
        ref_id = _board(store)
        fp = PcbBridge().fingerprint(store, ref_id, ["U1", "U99"])
        assert fp.missing == ("U99",)

    def test_no_bridge_kind_returns_none(self, store: Store) -> None:
        store.insert_ref(kind="paper", slug="p", title="p")
        ref = store.get_ref(kind="paper", id="p")
        assert ref is not None
        assert current_fingerprint(store, kind="paper", ref_id=ref.id) is None


# ── pcb bridge: tool checkers ────────────────────────────────────────────


class TestPcbToolCheckers:
    def test_drc_not_run_then_fail_then_pass(self, store: Store) -> None:
        ref_id = _board(store)
        bridge = PcbBridge()
        res = bridge.tool_check(store, ref_id, "drc-clean")
        assert res is not None and res.verdict is None
        assert "no DRC run" in (res.reason or "")
        board_id = store.pcb_ensure_board(ref_id)
        store.pcb_write_drc_findings(
            board_id,
            "run-1",
            [
                {"rule": "clearance", "severity": "error", "objects": [], "detail": ""},
                {"rule": "silk", "severity": "warn", "objects": [], "detail": ""},
            ],
        )
        res = bridge.tool_check(store, ref_id, "drc-clean")
        assert res is not None and res.verdict == "fail"
        assert res.evidence["run_id"] == "run-1"
        assert res.evidence["by_rule"] == {"clearance": 1}
        # A run with only warnings is clean. (A run with ZERO findings
        # writes no row at all — pcb_drc_findings_latest cannot see it;
        # that is pcb's record, not the bridge's, so it is not papered
        # over here.)
        store.pcb_write_drc_findings(
            board_id,
            "run-2",
            [{"rule": "silk", "severity": "warn", "objects": [], "detail": ""}],
        )
        res = bridge.tool_check(store, ref_id, "drc-clean")
        assert res is not None and res.verdict == "pass"
        assert res.evidence["run_id"] == "run-2"
        assert res.evidence["warnings"] == 1

    def test_all_nets_routed_reads_route_status(self, store: Store) -> None:
        ref_id = _board(store)
        bridge = PcbBridge()
        res = bridge.tool_check(store, ref_id, "all-nets-routed")
        assert res is not None and res.verdict == "fail"
        assert set(res.evidence["pending"]) == {"VCC", "GND", "SIG"}
        board_id = store.pcb_ensure_board(ref_id)
        store.pcb_routes_write(
            ref_id, board_id, {n: {"status": "realized"} for n in ("VCC", "GND", "SIG")}
        )
        res = bridge.tool_check(store, ref_id, "all-nets-routed")
        assert res is not None and res.verdict == "pass"

    def test_netlist_exceptions_lists_unconnected_and_dangling(
        self, store: Store
    ) -> None:
        ref_id = _board(store)
        res = PcbBridge().tool_check(store, ref_id, "netlist-exceptions-clean")
        assert res is not None and res.verdict == "fail"
        assert res.evidence["unconnected_pins"] == ["U1.3"]
        assert res.evidence["dangling_nets"] == ["SIG"]

    def test_unknown_item_has_no_checker(self, store: Store) -> None:
        ref_id = _board(store)
        assert PcbBridge().tool_check(store, ref_id, "judge-me") is None


# ── handler on a pcb target ──────────────────────────────────────────────


class TestHandlerOnPcb:
    def test_verdict_computes_fingerprint_and_stores_anchors(
        self, store: Store
    ) -> None:
        ref_id = _board(store)
        h = _local_checklist(store)
        h.edit(id="cl", op="assign", target="pcb:cl-board")
        resp = h.edit(
            id="cl",
            op="verdict",
            target="pcb:cl-board",
            item="judge-me",
            verdict="pass",
            anchors=["U1"],
        )
        assert "fingerprint over U1" in resp.body
        cl = store.checklist_get("cl")
        assert cl is not None
        v = store.checklist_verdict_latest(
            target_ref_id=ref_id, checklist_id=cl["id"], item_name="judge-me"
        )
        assert v is not None
        assert v["anchors"] == ["U1"]
        assert v["fingerprint"] == PcbBridge().fingerprint(store, ref_id, ["U1"]).digest

    def test_unknown_anchor_rejected(self, store: Store) -> None:
        _board(store)
        h = _local_checklist(store)
        with pytest.raises(NotFound, match="U99"):
            h.edit(
                id="cl",
                op="verdict",
                target="pcb:cl-board",
                item="judge-me",
                verdict="pass",
                anchors=["U99"],
            )

    def test_status_goes_stale_when_anchored_scope_changes(self, store: Store) -> None:
        _board(store)
        h = _local_checklist(store)
        h.edit(id="cl", op="assign", target="pcb:cl-board")
        h.edit(
            id="cl",
            op="verdict",
            target="pcb:cl-board",
            item="judge-me",
            verdict="pass",
            anchors=["SIG"],
        )
        body = h.get(id="cl", target="pcb:cl-board").body
        assert "stale" not in body
        # unrelated edit: SIG untouched
        store.pcb_apply(
            slug="cl-board",
            title="cl-board",
            components=[{"refdes": "C1", "pins": [{"name": "1"}]}],
            nets=[],
            connections=[{"net": "VCC", "refdes": "C1", "pin": "1"}],
        )
        assert "stale" not in h.get(id="cl", target="pcb:cl-board").body
        # SIG gains a member: the anchored verdict is stale
        store.pcb_apply(
            slug="cl-board",
            title="cl-board",
            components=[],
            nets=[],
            connections=[{"net": "SIG", "refdes": "U1", "pin": "3"}],
        )
        assert "stale (target changed)" in h.get(id="cl", target="pcb:cl-board").body

    def test_tool_item_reads_live_checker_and_waiver_overrides(
        self, store: Store
    ) -> None:
        ref_id = _board(store)
        h = _local_checklist(store)
        h.edit(id="cl", op="assign", target="pcb:cl-board")
        body = h.get(id="cl", target="pcb:cl-board").body
        assert "not run (no DRC run" in body
        assert "INCOMPLETE" in body
        board_id = store.pcb_ensure_board(ref_id)
        store.pcb_write_drc_findings(
            board_id,
            "run-9",
            [{"rule": "clearance", "severity": "error", "objects": [], "detail": ""}],
        )
        body = h.get(id="cl", target="pcb:cl-board").body
        assert "run run-9" in body and "BLOCKED" in body
        with pytest.raises(BadInput, match="rationale"):
            h.edit(
                id="cl",
                op="verdict",
                target="pcb:cl-board",
                item="drc-clean",
                verdict="waived",
            )
        h.edit(
            id="cl",
            op="verdict",
            target="pcb:cl-board",
            item="drc-clean",
            verdict="waived",
            evidence={"rationale": "clearance finding is on a test pad we accept"},
        )
        body = h.get(id="cl", target="pcb:cl-board").body
        assert "waived" in body and "BLOCKED" not in body

    def test_kind_default_assignment_counts_as_assigned(self, store: Store) -> None:
        _board(store)
        h = _local_checklist(store, default_for=["pcb"])
        body = h.get(id="cl", target="pcb:cl-board").body
        assert "kind default for pcb" in body
        assert "no checklist assigned" not in body
        with pytest.raises(BadInput, match="kind default"):
            h.edit(id="cl", op="unassign", target="pcb:cl-board")
        listing = h.get(target="pcb:cl-board").body
        assert "cl" in listing and "kind default" in listing

    def test_get_target_without_id_lists_applicable(self, store: Store) -> None:
        _board(store)
        h = _local_checklist(store)
        assert "no checklist assigned" in h.get(target="pcb:cl-board").body
        h.edit(id="cl", op="assign", target="pcb:cl-board")
        body = h.get(target="pcb:cl-board").body
        assert "explicit" in body

    def test_threads_render_open_questions_and_dangling_anchors(
        self, store: Store
    ) -> None:
        _board(store)
        h = _local_checklist(store)
        h.edit(id="cl", op="assign", target="pcb:cl-board")
        h.edit(
            id="cl",
            op="add_note",
            target="pcb:cl-board",
            item="judge-me",
            name="q1",
            note_kind="question",
            body="does U1 need a pull-up on pin 3?",
            about=["U1", "U42"],
        )
        body = h.get(id="cl", target="pcb:cl-board").body
        assert "1 open question(s)" in body
        assert "OPEN" in body and "dangling: U42" in body
        h.edit(
            id="cl",
            op="add_note",
            target="pcb:cl-board",
            item="judge-me",
            name="d1",
            note_kind="decision",
            body="yes — 10k to VCC",
            re="q1",
        )
        body = h.get(id="cl", target="pcb:cl-board").body
        assert "0 open question(s)" in body

    def test_anchors_on_bridgeless_kind_rejected(self, store: Store) -> None:
        store.insert_ref(kind="paper", slug="p", title="p")
        h = _local_checklist(store)
        with pytest.raises(BadInput, match="bridge"):
            h.edit(
                id="cl",
                op="verdict",
                target="paper:p",
                item="judge-me",
                verdict="pass",
                anchors=["x"],
            )


# ── status engine directly ───────────────────────────────────────────────


def test_compute_statuses_three_values_on_pcb(store: Store) -> None:
    ref_id = _board(store)
    h = _local_checklist(store)
    cl = store.checklist_get("cl")
    assert cl is not None
    h.edit(id="cl", op="assign", target="pcb:cl-board")
    h.edit(id="cl", op="verdict", target="pcb:cl-board", item="advice", verdict="n/a")
    items = store.checklist_items_current(cl["id"], target_ref_id=ref_id)
    verdicts = store.checklist_verdicts_latest_for_target(
        target_ref_id=ref_id, checklist_id=cl["id"]
    )
    by_name = {
        st.name: st
        for st in compute_statuses(
            store, items=items, verdicts=verdicts, kind="pcb", ref_id=ref_id
        )
    }
    assert by_name["judge-me"].status == "not checked"
    assert by_name["drc-clean"].status.startswith("not run")
    assert by_name["drc-clean"].source == "tool"
    assert by_name["advice"].status == "n/a" and by_name["advice"].settled


# ── checklist_clean evaluator ────────────────────────────────────────────


class TestChecklistCleanEvaluator:
    def test_registered_with_validator(self) -> None:
        assert REGISTRY["checklist_clean"] is checklist_clean.evaluate
        with pytest.raises(BadInput):
            validate_auto_check_spec({"type": "checklist_clean", "checklist": "x"})
        validate_auto_check_spec(
            {"type": "checklist_clean", "checklist": "x", "target": "pcb:y"}
        )

    def test_none_until_assigned_and_settled_false_on_fail_true_when_clean(
        self, store: Store
    ) -> None:
        ref_id = _board(store)
        h = _local_checklist(store)
        spec = {"type": "checklist_clean", "checklist": "cl", "target": "pcb:cl-board"}
        assert checklist_clean.evaluate(store, spec) is None  # not assigned
        h.edit(id="cl", op="assign", target="pcb:cl-board")
        assert checklist_clean.evaluate(store, spec) is None  # nothing checked
        h.edit(
            id="cl",
            op="verdict",
            target="pcb:cl-board",
            item="judge-me",
            verdict="fail",
        )
        assert checklist_clean.evaluate(store, spec) is False
        h.edit(
            id="cl",
            op="verdict",
            target="pcb:cl-board",
            item="judge-me",
            verdict="pass",
        )
        assert checklist_clean.evaluate(store, spec) is None  # drc not run
        board_id = store.pcb_ensure_board(ref_id)
        store.pcb_write_drc_findings(
            board_id,
            "run-1",
            [{"rule": "silk", "severity": "warn", "objects": [], "detail": ""}],
        )
        # advisory item still unchecked — never blocks
        assert checklist_clean.evaluate(store, spec) is True

    def test_unknown_checklist_is_spec_error(self, store: Store) -> None:
        with pytest.raises(BadInput, match="not found"):
            checklist_clean.evaluate(
                store, {"checklist": "nope", "target": "pcb:whatever"}
            )
