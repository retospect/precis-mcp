"""The pcb bridge — fingerprints and tool-item checkers for ``pcb``
targets (checklist-kind.md slice 2, "the tool-item bridges").

Both halves read the store's durable pcb record and never recompute
geometry: ``drc-clean`` reads the latest persisted DRC run exactly as the
``netlist_drc_clean`` evaluator does, ``all-nets-routed`` reads
``pcb_routes`` like ``route_complete``, and ``netlist-exceptions-clean``
reads the connectivity graph (``Store.pcb_graph``) for pins on no net and
nets with fewer than two members — the ERC-shaped exceptions the design
session named. Checkers are keyed by the shipped item NAME
(``pcb-tapeout.yaml``), so the YAML stays data and the bridge stays the
only pcb-specific code in the kind.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from typing import Any

from precis.checklist.status import Fingerprint, ToolResult

#: How many offending names a tool result quotes before truncating.
_EVIDENCE_LIMIT = 20


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _graph(store: Any, ref_id: int) -> dict[str, Any]:
    return dict(store.pcb_graph(ref_id))


def _check_drc(store: Any, ref_id: int) -> ToolResult:
    run_id, findings = store.pcb_drc_findings_latest(ref_id)
    if run_id is None:
        return ToolResult(
            None,
            reason="no DRC run recorded — get(kind='pcb', id=..., view='drc')",
        )
    live = [f for f in findings if not f.get("waived_by")]
    errors = [f for f in live if f["severity"] == "error"]
    warnings = [f for f in live if f["severity"] == "warn"]
    by_rule: dict[str, int] = {}
    for f in errors:
        by_rule[f["rule"]] = by_rule.get(f["rule"], 0) + 1
    return ToolResult(
        "fail" if errors else "pass",
        {
            "run_id": run_id,
            "errors": len(errors),
            "warnings": len(warnings),
            "by_rule": by_rule,
        },
    )


def _check_routed(store: Any, ref_id: int) -> ToolResult:
    rows = store.pcb_route_status(ref_id)
    if not rows:
        return ToolResult(None, reason="design has no nets")
    pending = [r["name"] for r in rows if r["status"] != "realized"]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return ToolResult(
        "fail" if pending else "pass",
        {"nets": len(rows), "counts": counts, "pending": pending[:_EVIDENCE_LIMIT]},
    )


def _check_netlist_exceptions(store: Any, ref_id: int) -> ToolResult:
    graph = _graph(store, ref_id)
    if not graph.get("instances"):
        return ToolResult(None, reason="design has no components")
    unconnected = [f"{u['refdes']}.{u['pin']}" for u in graph.get("unconnected") or []]
    dangling = [n["name"] for n in graph.get("nets") or [] if len(n["members"]) < 2]
    return ToolResult(
        "fail" if (unconnected or dangling) else "pass",
        {
            "unconnected_pins": unconnected[:_EVIDENCE_LIMIT],
            "unconnected_pin_count": len(unconnected),
            "dangling_nets": dangling[:_EVIDENCE_LIMIT],
            "dangling_net_count": len(dangling),
        },
    )


#: Tool-item checkers by shipped item name — one coarse item per checker,
#: never a decomposition of its rule set.
TOOL_CHECKERS: dict[str, Callable[[Any, int], ToolResult]] = {
    "drc-clean": _check_drc,
    "all-nets-routed": _check_routed,
    "netlist-exceptions-clean": _check_netlist_exceptions,
}


class PcbBridge:
    """:class:`precis.checklist.status.KindBridge` for ``pcb`` refs."""

    def fingerprint(
        self, store: Any, ref_id: int, anchors: Sequence[str]
    ) -> Fingerprint:
        graph = _graph(store, ref_id)
        if not anchors:
            return Fingerprint(_digest(graph))
        instances = {i["refdes"]: i for i in graph.get("instances") or []}
        nets = {n["name"]: n for n in graph.get("nets") or []}
        scope: dict[str, Any] = {}
        missing: list[str] = []
        for anchor in anchors:
            inst = instances.get(anchor)
            net = nets.get(anchor)
            if inst is None and net is None:
                missing.append(anchor)
                continue
            payload: dict[str, Any] = {}
            if inst is not None:
                payload["instance"] = inst
                payload["pins"] = store.pcb_instance_neighbors(ref_id, anchor)
            if net is not None:
                payload["net"] = net
            scope[anchor] = payload
        return Fingerprint(
            _digest({"anchors": scope, "missing": sorted(missing)}),
            tuple(sorted(missing)),
        )

    def tool_check(self, store: Any, ref_id: int, item_name: str) -> ToolResult | None:
        checker = TOOL_CHECKERS.get(item_name)
        return None if checker is None else checker(store, ref_id)

    def anchor_exists(self, store: Any, ref_id: int, anchor: str) -> bool:
        return (
            store.pcb_instance_neighbors(ref_id, anchor) is not None
            or store.pcb_net_members(ref_id, anchor) is not None
        )


__all__ = ["TOOL_CHECKERS", "PcbBridge"]
