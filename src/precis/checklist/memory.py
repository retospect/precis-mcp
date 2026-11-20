"""The memory bridge — the fingerprint bridge for ``memory`` checklist
targets (gr677471: a venture-gate checklist verdict rests on a business
plan's memory root).

A memory verdict covers the root's live body plus the live bodies of its
``part-of`` subtree: a plan's band-index verdict must go stale when any
section under the plan root moves, and stay current when an unrelated
memory does. Anchors are not supported — a memory node has no named
sub-objects, so every anchor is reported missing and ``anchors=`` is
refused at verdict-write time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from typing import Any

from precis.checklist.status import Fingerprint, ToolResult


def _digest(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class MemoryBridge:
    """:class:`precis.checklist.status.KindBridge` for ``memory`` refs."""

    def fingerprint(
        self, store: Any, ref_id: int, anchors: Sequence[str]
    ) -> Fingerprint:
        # The part-of walk keeps its built-in 64-level depth guard; one
        # plan root's subtree sits far below it.
        scope: list[list[Any]] = [[ref_id, store.chunks.live_body_text_for_ref(ref_id)]]
        for rid in sorted(store.part_of_descendants(ref_id)):
            scope.append([rid, store.chunks.live_body_text_for_ref(rid)])
        # No anchor resolves on memory, so anchors= comes back missing —
        # the handler refuses the verdict naming them.
        return Fingerprint(_digest(scope), tuple(sorted(anchors)))

    def tool_check(self, store: Any, ref_id: int, item_name: str) -> ToolResult | None:
        return None

    def anchor_exists(self, store: Any, ref_id: int, anchor: str) -> bool:
        return False

    def input_revision(self, store: Any, ref_id: int, name: str) -> Any:
        # No named revisable inputs on a memory target.
        return None


__all__ = ["MemoryBridge"]
