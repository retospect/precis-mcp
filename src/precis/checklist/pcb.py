"""The pcb bridge — fingerprints and tool-item checkers for ``pcb``
targets (checklist-kind.md slice 2, "the tool-item bridges").

Both halves read the store's durable pcb record and never recompute
geometry: ``drc-clean`` reads the latest persisted DRC run exactly as the
``netlist_drc_clean`` evaluator does, ``all-nets-routed`` reads
:func:`precis.pcb.route_state.route_state` like ``route_complete``, and
``netlist-exceptions-clean`` reads the connectivity graph (``Store.pcb_graph``) for pins on no net and
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
from precis.pcb.route_state import route_state

#: How many offending names a tool result quotes before truncating.
_EVIDENCE_LIMIT = 20

MECH_ENV_CHECKLIST = "pcb-mech-env"
MECH_ENV_INPUT = "pcb:mech-env"
MECH_ENV_META_KEYS = frozenset(
    {"flex", "coating", "enclosure", "environment", "outdoor", "wet"}
)
MECH_ENV_FEATURES = frozenset(
    {"bend_zone", "stiffener", "coverlay_opening", "tape_mask"}
)


def mech_env_applies(design: dict[str, Any]) -> bool:
    """Authored flex/fab declarations, or the metadata fallback (gr519925).

    Key presence is deliberate: even an incomplete environment declaration
    calls for review. A layer count alone says nothing about flexibility.
    """
    board = design.get("board") or {}
    meta = board.get("meta") or {}
    return bool(
        MECH_ENV_META_KEYS.intersection(meta)
        or str(meta.get("process") or "").startswith("flex-")
        or board.get("fold_lines")
        or any(
            f.get("ftype") in MECH_ENV_FEATURES for f in design.get("features") or []
        )
    )


def mech_env_design(store: Any, ref_id: int) -> dict[str, Any]:
    """Review geometry omitted by pcb_graph; use the existing stored rows."""
    graph = _graph(store, ref_id)
    board = dict(graph.get("board") or {})
    board["meta"] = {
        k: v for k, v in (board.get("meta") or {}).items() if k != "drc_runs_by_phase"
    }
    board_id = board.get("board_id")
    return {
        "board": board,
        "features": store.pcb_features_list(ref_id),
        "copper": store.pcb_copper_list(board_id) if board_id is not None else [],
        "footprints": store.pcb_footprints_for(ref_id),
        "local_footprints": store.pcb_local_footprints_for(ref_id),
    }


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _graph(store: Any, ref_id: int) -> dict[str, Any]:
    graph = dict(store.pcb_graph(ref_id))
    board = graph.get("board")
    if isinstance(board, dict) and (
        {"drc_revisions", "drc_scopes"} & set(board.get("meta") or {})
    ):
        # gr492096/gr496242: recording a DRC run stamps meta['drc_revisions']
        # and meta['drc_scopes']; that bookkeeping is not a design change
        # and must not stale a verdict.
        meta = {
            k: v
            for k, v in board["meta"].items()
            if k not in ("drc_revisions", "drc_scopes")
        }
        graph["board"] = {**board, "meta": meta}
    return graph


def _check_drc(store: Any, ref_id: int) -> ToolResult:
    run_id, findings = store.pcb_drc_findings_latest(ref_id)
    if run_id is None:
        return ToolResult(
            None,
            reason=(
                "no DRC run recorded — put(kind='pcb', id=..., args={'op':'check'})"
            ),
        )
    if store.pcb_drc_run_stale(ref_id, run_id):
        return ToolResult(
            None,
            reason=(
                f"stale, re-check — the board changed since DRC run "
                f"{run_id[:12]}; put(kind='pcb', id=..., args={{'op':'check'}})"
            ),
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
    rs = route_state(store, ref_id, drc=False)
    if not rs.nets:
        return ToolResult(None, reason="design has no nets")
    pending = [r["name"] for r in rs.pending]
    counts: dict[str, int] = {}
    for r in rs.nets:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    # No route run yet reads "fail", not "not checkable": the item asks
    # whether the nets are routed, and they are not.
    return ToolResult(
        "pass" if rs.routed else "fail",
        {"nets": len(rs.nets), "counts": counts, "pending": pending[:_EVIDENCE_LIMIT]},
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


def _current_input_revisions() -> dict[str, Any]:
    """Input name → current revision for verdict ``inputs`` staleness
    (slice 4). Read through the module attribute at CALL time so a
    policy bump lands the moment the constant changes — and a test can
    monkeypatch it. Later slices add part-fact ids (slice 5) and rule /
    skill revisions to this map."""
    from precis import pcb as _pcb_pkg

    return {"margin:fixed-derating": _pcb_pkg.PASSIVE_MARGIN_REVISION}


class PcbBridge:
    """:class:`precis.checklist.status.KindBridge` for ``pcb`` refs."""

    def fingerprint(
        self, store: Any, ref_id: int, anchors: Sequence[str]
    ) -> Fingerprint:
        graph = _graph(store, ref_id)
        if not anchors:
            # A check run records its run id under ``board.meta`` (the
            # phase baseline pointer); that is bookkeeping, not a design
            # change — ``pcb_review`` runs its own check every job, and a
            # fingerprint that moved with it would turn every current
            # ``not checkable`` verdict stale on an unchanged design.
            board = dict(graph.get("board") or {})
            meta = {
                k: v
                for k, v in (board.get("meta") or {}).items()
                if k != "drc_runs_by_phase"
            }
            graph = {**graph, "board": {**board, "meta": meta}}
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

    def input_revision(self, store: Any, ref_id: int, name: str) -> Any:
        if name == MECH_ENV_INPUT:
            return _digest(mech_env_design(store, ref_id))
        # ``part:<C#>`` — the part's datasheet-facts rev (slice 7's
        # pcb_review records it per consumed part): a corrected fact
        # marks every verdict that read it stale. A part with no ref yet
        # reads 0, the same as never extracted, so a verdict that found
        # it fact-less goes stale once the part is pulled and extracted.
        if name.startswith("part:"):
            from precis.pcb.datasheet_facts import facts_revision

            part_ref_id = store.part_ref_id(name[len("part:") :].strip().upper())
            return 0 if part_ref_id is None else facts_revision(store, part_ref_id)
        return _current_input_revisions().get(name)


__all__ = ["TOOL_CHECKERS", "PcbBridge"]
