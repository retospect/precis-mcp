"""Pins a net-class layer lock strands — the pre-routing estimate's view of
Rulings 2026-09-19 item 7 (a ``pcb_net_classes.rules`` entry naming
``"layers"``, e.g. ``["B.Cu"]`` for the EWOD escape nets).

A locked net whose pad sits on a forbidden layer can only be routed from
an authored fixed-copper island that reaches an allowed layer — the plaza
via, on the EWOD board. :func:`precis.pcb.realize._resolve_route_end` is
the router's side of that rule: it substitutes the island terminal, and
fails the segment as ``layer_lock`` when there is none, because the maze
router never drops a via at a pad itself. This module answers the same
question before any routing runs, from the same two inputs the router
reads (:func:`precis.pcb.realize.pads_for_ir`'s pads and
:func:`precis.pcb.connectivity.fixed_copper_pin_terminals`), so
``view='feasibility'`` can no longer report a board as trivially routable
while its net classes forbid the layer its pads are on
(docs/backlog/pcb-pre-routing-estimates-are-green-on-a-board-that-fails.md).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from precis.pcb.connectivity import FixedTerminal

__all__ = ["LayerLockReport", "layer_locked_pins"]


@dataclass(frozen=True)
class LayerLockReport:
    """``bridged`` pins reach an allowed layer through authored copper;
    ``stranded`` pins do not, and the router will fail their segments.
    Both hold ``(net, refdes, pin)`` triples, sorted."""

    bridged: tuple[tuple[str, str, str], ...]
    stranded: tuple[tuple[str, str, str], ...]

    @property
    def forced(self) -> int:
        """Pins whose own pad layer their net class forbids."""
        return len(self.bridged) + len(self.stranded)


def layer_locked_pins(
    pads: Iterable[Mapping[str, Any]],
    class_by_net: Mapping[str, str | None],
    class_rules: Mapping[str, Mapping[str, Any]] | None,
    terminals: Mapping[tuple[str, str], tuple[FixedTerminal, ...]],
) -> LayerLockReport:
    """Every pad on a layer-locked net whose ``layer`` is outside the
    lock, split by whether its fixed-copper island offers a terminal on an
    allowed layer. A pad with no net, or on a net whose class names no
    ``"layers"``, never appears — the negative control: a board with no
    locked class reports nothing."""
    bridged: list[tuple[str, str, str]] = []
    stranded: list[tuple[str, str, str]] = []
    for pad in pads:
        net = str(pad.get("net") or "")
        if not net:
            continue
        cls = class_by_net.get(net)
        allowed = ((class_rules or {}).get(cls or "") or {}).get("layers")
        if not allowed or str(pad.get("layer")) in allowed:
            continue
        key = (str(pad["refdes"]), str(pad["pin"]))
        triple = (net, *key)
        if any(t.layer in allowed for t in terminals.get(key, ())):
            bridged.append(triple)
        else:
            stranded.append(triple)
    return LayerLockReport(tuple(sorted(bridged)), tuple(sorted(stranded)))
