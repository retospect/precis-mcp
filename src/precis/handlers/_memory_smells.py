"""Memory smell views: render existing proposals, never run a detector.

The node view keeps evidence beside its terminal verdict; the hub view
counts the same window by detector and section tag. Unknown or multiple
section tags stay explicit so every proposal contributes to one group.
Scope ids come from the dispatcher's existing ``args={'under': ...}``
part-of traversal, including descendants below summary nodes.

``args={'detector': ..., 'members': True}`` names the nodes behind the
hub view's rows (gr688168). That list is handles only and capped per
row: evidence is unbounded per node and truncated the batched node
view, so it stays on ``get(view='smells')``.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.format import render_agent_table
from precis.graph.garden.smells import RECENT_DAYS, MemorySmell, memory_smells
from precis.handlers._value_entity import coerce_bool
from precis.response import Response
from precis.store import Tag
from precis.store._tag_filter import split_tag_polarity
from precis.utils.match_all import fold_match_all_q
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
    from precis.store import Store
    from precis.store.types import Ref

#: Handles one rollup row lists under ``members=``; the rest is a count.
MEMBER_CAP = 50

_MEMBERS_NEXT = (
    "search(kind='memory', view='smells', "
    "args={'under': 'me<hub>', 'detector': '<detector>', 'members': True})"
)


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
        (
            f"get(kind='memory', id='me{ref.id}', extent='fisheye+1hop')",
            "read the node",
        )
    ]
    return Response(
        body="\n".join(lines) + render_next_section(hints),
        result_count=len(proposals),
    )


def _scoped_proposals(
    store: Store,
    *,
    include_ref_ids: list[int] | None,
    exclude_ref_ids: list[int] | None,
    tags: list[str] | None,
) -> list[tuple[MemorySmell, set[str]]]:
    """Ledger proposals in scope that pass the ``tags=`` filter, each
    paired with its node's effective tag strings. Shared by the rollup
    and :func:`count_smell_nodes` — the domain footer must count the
    same corpus the view renders."""
    proposals = memory_smells(
        store, include_ref_ids=include_ref_ids, exclude_ref_ids=exclude_ref_ids
    )
    wanted: list[str] = Tag.normalize_filter(tags, kind="memory") or []
    # ``'!<tag>'`` entries (gr568354) exclude — every positive tag must
    # be present and no negated one, the same polarity split
    # ``build_tag_filter`` applies on the SQL search paths.
    wanted_pos, wanted_neg = split_tag_polarity(wanted)
    tag_map = store.ref_tags_bulk(sorted({p.ref_id for p in proposals}))
    scoped: list[tuple[MemorySmell, set[str]]] = []
    for p in proposals:
        tag_strings = {
            value if namespace in ("OPEN", "FLAG") else f"{namespace}:{value}"
            for namespace, value in tag_map.get(p.ref_id, [])
        }
        if not all(tag in tag_strings for tag in wanted_pos) or any(
            tag in tag_strings for tag in wanted_neg
        ):
            continue
        scoped.append((p, tag_strings))
    return scoped


def _detector_name(detector: str | None) -> str | None:
    """The ``detector=`` filter as one stripped name, or ``None``."""
    if detector is None:
        return None
    if not isinstance(detector, str) or not detector.strip():
        raise BadInput(
            f"detector= takes one detector name, got {detector!r}",
            next=_MEMBERS_NEXT,
        )
    return detector.strip()


def count_smell_nodes(
    store: Store,
    *,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
    tags: list[str] | None = None,
    detector: str | None = None,
) -> int:
    """Distinct memories with proposals in scope — the domain-footer's
    before/after counter (a memory with no proposals is not a smells
    entry at all)."""
    wanted = _detector_name(detector)
    return len(
        {
            p.ref_id
            for p, _tags in _scoped_proposals(
                store,
                include_ref_ids=include_ref_ids,
                exclude_ref_ids=exclude_ref_ids,
                tags=tags,
            )
            if wanted is None or p.detector == wanted
        }
    )


def _member_lines(groups: dict[tuple[str, str], list[MemorySmell]]) -> list[str]:
    """One handle-only line per rollup row, nodes with a pending
    proposal first. Capped per row: the evidence stays on the node view."""
    lines = [
        f"members — handles only, at most {MEMBER_CAP} per row; "
        "evidence: get(kind='memory', id='me<N>', view='smells')"
    ]
    for (detector, node_type), group in sorted(groups.items()):
        pending = sorted({p.ref_id for p in group if p.verdict == "proposed"})
        decided = sorted({p.ref_id for p in group} - set(pending))
        shown = {
            "pending": pending[:MEMBER_CAP],
            "decided": decided[: max(MEMBER_CAP - len(pending), 0)],
        }
        hidden = len(pending) + len(decided) - sum(map(len, shown.values()))
        lines.append(
            f"- {detector} · {node_type}: "
            + "; ".join(
                f"{label} " + " ".join(f"me{ref_id}" for ref_id in ids)
                for label, ids in shown.items()
                if ids
            )
            + (
                f" (+{hidden} more — narrow with tags= or a deeper under=)"
                if hidden
                else ""
            )
        )
    return lines


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
    detector: str | None = None,
    members: bool | str | None = None,
) -> Response:
    """Aggregate the full scoped ledger, not a search-result page.

    ``detector=`` keeps one detector's rows; ``members=`` names the
    nodes behind each row (gr688168)."""
    if fold_match_all_q(q) or mode or scope or status is not None or page != 1:
        raise BadInput(
            "view='smells' does not apply q/mode/scope/status/page filters; "
            "detector= narrows the rows and members= lists their nodes",
            next=_MEMBERS_NEXT,
        )
    if under is not None and include_ref_ids is None:
        raise BadInput(
            "under= reached the handler without resolved members",
            next="use search(kind='memory', view='smells', args={'under': 'me<hub>'})",
        )
    wanted = _detector_name(detector)
    show_members = coerce_bool(members)
    if members is not None and show_members is None:
        raise BadInput(
            f"members= takes true or false, got {members!r}",
            next=_MEMBERS_NEXT,
        )
    proposals = _scoped_proposals(
        store,
        include_ref_ids=include_ref_ids,
        exclude_ref_ids=exclude_ref_ids,
        tags=tags,
    )
    groups: dict[tuple[str, str], list[MemorySmell]] = defaultdict(list)
    for p, tag_strings in proposals:
        if wanted is not None and p.detector != wanted:
            continue
        types = sorted(t for t in tag_strings if t.startswith("section:"))
        groups[(p.detector, ",".join(types) or "(untyped)")].append(p)
    rows: list[dict[str, Any]] = []
    for (row_detector, node_type), group in sorted(groups.items()):
        pending = sum(p.verdict == "proposed" for p in group)
        rows.append(
            {
                "detector": row_detector,
                "type": node_type,
                "pending": pending,
                "decided": len(group) - pending,
                "nodes": len({p.ref_id for p in group}),
            }
        )
    heading = f"smells: {under or 'all live memories'} — open + decisions in the last {RECENT_DAYS} days"
    hints = [("precis garden stats", "see detector agreement")]
    if rows:
        body = render_agent_table(
            rows, schema=["detector", "type", "pending", "decided", "nodes"]
        )
        if show_members:
            body += "\n" + "\n".join(_member_lines(groups))
        else:
            echo = [
                f"{key!r}: {value!r}"
                for key, value in (
                    ("under", None if under is None else str(under)),
                    ("detector", wanted),
                    ("members", True),
                )
                if value is not None
            ]
            hints.append(
                (
                    f"search(kind='memory', view='smells', args={{{', '.join(echo)}}})",
                    "list the node handles behind each row"
                    + ("" if wanted else "; 'detector': '<name>' keeps one detector"),
                )
            )
    elif wanted is None:
        body = "No open or recently decided garden proposals in this scope."
    else:
        present = sorted({p.detector for p, _tags in proposals})
        body = (
            "No open or recently decided garden proposals from detector "
            f"{wanted!r} in this scope."
            + (
                f" Detectors with proposals here: {', '.join(present)}."
                if present
                else ""
            )
        )
    if wanted is not None:
        heading += f" — detector {wanted}"
    return Response(
        body=f"{heading}\n{body}" + render_next_section(hints),
        result_count=sum(len(group) for group in groups.values()),
    )
