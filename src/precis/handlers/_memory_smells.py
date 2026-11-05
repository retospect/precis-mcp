"""Memory smell views: render existing proposals, never run a detector.

The node view keeps evidence beside its terminal verdict; the hub view
counts the same window by detector and section tag. Unknown or multiple
section tags stay explicit so every proposal contributes to one group.
Scope ids come from the dispatcher's existing ``args={'under': ...}``
part-of traversal, including descendants below summary nodes.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.format import render_agent_table
from precis.graph.garden.smells import RECENT_DAYS, MemorySmell, memory_smells
from precis.response import Response
from precis.store import Tag
from precis.utils.match_all import fold_match_all_q
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Ref


def _when(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def render_memory_smells(store: Store, ref: Ref) -> Response:
    """Render all open proposals and decisions from the last 30 days."""
    proposals = memory_smells(store, include_ref_ids=[ref.id])
    lines = [f"smells: me{ref.id} — open + decisions in the last {RECENT_DAYS} days"]
    by_detector: dict[str, list[MemorySmell]] = defaultdict(list)
    for proposal in proposals:
        by_detector[proposal.detector].append(proposal)
    for detector, group in sorted(by_detector.items()):
        lines.append(f"## {detector}")
        for p in group:
            lines.append(
                f"- review {p.review_id}: {p.action} — {p.verdict}; "
                f"proposed {_when(p.proposed_at)}"
                + (f"; decided {_when(p.decided_at)}" if p.decided_at else "")
            )
            evidence = json.dumps(p.evidence, ensure_ascii=False, sort_keys=True)
            lines.append(f"  evidence: {evidence}")
    if not proposals:
        lines.append(
            "No open or recently decided garden proposals; this is not a clean review."
        )
    hints = [
        (f"get(kind='memory', id='me{ref.id}', view='fisheye+1hop')", "read the node")
    ]
    return Response(
        body="\n".join(lines) + render_next_section(hints),
        result_count=len(proposals),
    )


def render_smell_rollup(
    store: Store,
    *,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
    tags: list[str] | None = None,
    under: str | int | None = None,
    q: str | None = None,
    page: int = 1,
    mode: str | None = None,
    scope: str | None = None,
    status: str | None = None,
) -> Response:
    """Aggregate the full scoped ledger, not a search-result page."""
    if fold_match_all_q(q) or mode or scope or status is not None or page != 1:
        raise BadInput(
            "view='smells' does not apply q/mode/scope/status/page filters",
            next="search(kind='memory', view='smells', args={'under': 'me<hub>'})",
        )
    if under is not None and include_ref_ids is None:
        raise BadInput(
            "under= reached the handler without resolved members",
            next="use search(kind='memory', view='smells', args={'under': 'me<hub>'})",
        )
    proposals = memory_smells(
        store, include_ref_ids=include_ref_ids, exclude_ref_ids=exclude_ref_ids
    )
    tag_map = store.ref_tags_bulk(sorted({p.ref_id for p in proposals}))
    wanted: list[str] = Tag.normalize_filter(tags, kind="memory") or []
    groups: dict[tuple[str, str], list[MemorySmell]] = defaultdict(list)
    for p in proposals:
        node_tags: list[tuple[str, str]] = tag_map.get(p.ref_id, [])
        tag_strings = {
            value if namespace in ("OPEN", "FLAG") else f"{namespace}:{value}"
            for namespace, value in node_tags
        }
        if not all(tag in tag_strings for tag in wanted):
            continue
        types = sorted(t for t in tag_strings if t.startswith("section:"))
        groups[(p.detector, ",".join(types) or "(untyped)")].append(p)
    rows: list[dict[str, Any]] = []
    for (detector, node_type), group in sorted(groups.items()):
        pending = sum(p.verdict == "proposed" for p in group)
        rows.append(
            {
                "detector": detector,
                "type": node_type,
                "pending": pending,
                "decided": len(group) - pending,
                "nodes": len({p.ref_id for p in group}),
            }
        )
    heading = f"smells: {under or 'all live memories'} — open + decisions in the last {RECENT_DAYS} days"
    body = (
        render_agent_table(
            rows, schema=["detector", "type", "pending", "decided", "nodes"]
        )
        if rows
        else "No open or recently decided garden proposals in this scope."
    )
    return Response(
        body=f"{heading}\n{body}"
        + render_next_section([("precis garden stats", "see detector agreement")]),
        result_count=sum(len(group) for group in groups.values()),
    )
