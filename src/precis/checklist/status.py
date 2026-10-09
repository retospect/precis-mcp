"""Per-item status derivation shared by the handler's status view and the
``checklist_clean`` evaluator. See the package docstring for the two
layers (tool vs judgment) and the staleness rules this encodes.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

VERDICTS: tuple[str, ...] = ("pass", "fail", "n/a", "waived")


@dataclass(frozen=True)
class Fingerprint:
    """A bridge's digest over the target scope plus the anchors it could
    not resolve (a removed component is itself a target change)."""

    digest: str
    missing: tuple[str, ...] = ()


@dataclass(frozen=True)
class ToolResult:
    """One tool item's live checker outcome. ``verdict`` is ``'pass'`` /
    ``'fail'``, or ``None`` when the checker could not fire — ``reason``
    then says why, and the status renders ``not run (<reason>)``."""

    verdict: str | None
    evidence: dict[str, Any] = field(default_factory=dict)
    reason: str | None = None


class KindBridge(Protocol):
    """What a target kind supplies to make checklists bite on it."""

    def fingerprint(
        self, store: Any, ref_id: int, anchors: Sequence[str]
    ) -> Fingerprint: ...

    def tool_check(self, store: Any, ref_id: int, item_name: str) -> ToolResult | None:
        """Live result for a ``decidability='tool'`` item, or ``None`` when
        this kind has no checker registered under that item name."""
        ...

    def anchor_exists(self, store: Any, ref_id: int, anchor: str) -> bool: ...


@dataclass(frozen=True)
class ItemStatus:
    item: dict[str, Any]
    verdict: dict[str, Any] | None
    status: str
    source: str  # "ledger" | "tool"
    evidence: dict[str, Any] = field(default_factory=dict)

    @property
    def name(self) -> str:
        return str(self.item["name"])

    @property
    def blocking(self) -> bool:
        return self.item["severity"] == "blocking"

    @property
    def settled(self) -> bool:
        """A recorded, current verdict (pass/fail/n/a/waived) — as opposed
        to not checked, stale, or a checker that could not run."""
        return self.status in VERDICTS


def bridge_for(kind: str) -> KindBridge | None:
    """The bridge registered for a ref kind, or ``None``. Imported lazily
    so this module stays import-light for the evaluator."""
    if kind == "pcb":
        from precis.checklist.pcb import PcbBridge

        return PcbBridge()
    return None


def current_fingerprint(
    store: Any, *, kind: str, ref_id: int, anchors: Sequence[str] = ()
) -> Fingerprint | None:
    """The target's current fingerprint over ``anchors`` (empty = whole
    target), or ``None`` when its kind has no bridge."""
    bridge = bridge_for(kind)
    if bridge is None:
        return None
    return bridge.fingerprint(store, ref_id, anchors)


def _ledger_status(
    item: dict[str, Any],
    verdict: dict[str, Any] | None,
    *,
    store: Any,
    ref_id: int,
    bridge: KindBridge | None,
    caller_fingerprint: str | None,
) -> ItemStatus:
    if verdict is None:
        return ItemStatus(item, None, "not checked", "ledger")
    if verdict["item_rev"] < item["rev"]:
        return ItemStatus(item, verdict, "stale (item revised)", "ledger")
    stored = verdict.get("fingerprint")
    if stored is not None:
        current: str | None = None
        if bridge is not None:
            fp = bridge.fingerprint(store, ref_id, list(verdict.get("anchors") or []))
            current = fp.digest
            if fp.missing:
                gone = ", ".join(fp.missing)
                return ItemStatus(
                    item,
                    verdict,
                    f"stale (target changed: {gone} gone)",
                    "ledger",
                    dict(verdict.get("evidence") or {}),
                )
        elif caller_fingerprint is not None:
            current = caller_fingerprint
        if current is not None and current != stored:
            return ItemStatus(
                item,
                verdict,
                "stale (target changed)",
                "ledger",
                dict(verdict.get("evidence") or {}),
            )
    return ItemStatus(
        item,
        verdict,
        str(verdict["verdict"]),
        "ledger",
        dict(verdict.get("evidence") or {}),
    )


def compute_statuses(
    store: Any,
    *,
    items: list[dict[str, Any]],
    verdicts: dict[str, dict[str, Any]],
    kind: str,
    ref_id: int,
    caller_fingerprint: str | None = None,
) -> list[ItemStatus]:
    """Three-valued status per current item for one (target, checklist).

    ``items`` are the current revs (:meth:`Store.checklist_items_current`),
    ``verdicts`` the latest live verdict per item name
    (:meth:`Store.checklist_verdicts_latest_for_target`). Never "clean" by
    omission: no verdict is ``not checked``; a checker that cannot fire is
    ``not run (...)``.
    """
    bridge = bridge_for(kind)
    out: list[ItemStatus] = []
    for item in items:
        verdict = verdicts.get(item["name"])
        ledger = _ledger_status(
            item,
            verdict,
            store=store,
            ref_id=ref_id,
            bridge=bridge,
            caller_fingerprint=caller_fingerprint,
        )
        tool: ToolResult | None = None
        if item["decidability"] == "tool" and bridge is not None:
            tool = bridge.tool_check(store, ref_id, item["name"])
        if tool is None:
            out.append(ledger)
            continue
        # A current waiver (with its rationale in the thread) is the one
        # ledger verdict that overrides the live checker; everything else
        # about a tool item is read off the checker's record.
        if ledger.status == "waived":
            out.append(ItemStatus(item, verdict, "waived", "ledger", ledger.evidence))
            continue
        if tool.verdict is None:
            status = f"not run ({tool.reason or 'checker could not fire'})"
        else:
            status = tool.verdict
        out.append(ItemStatus(item, verdict, status, "tool", dict(tool.evidence)))
    return out


__all__ = [
    "VERDICTS",
    "Fingerprint",
    "ItemStatus",
    "KindBridge",
    "ToolResult",
    "bridge_for",
    "compute_statuses",
    "current_fingerprint",
]
