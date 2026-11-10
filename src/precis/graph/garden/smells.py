"""Read-only memory proposal ledger and complete pending detector counts.

Decisions carry ``garden.resolves`` rather than the original evidence. Join
them by proposal id so reopening a key preserves its earlier decisions.
The 30-day window applies to decisions only; an old open proposal stays
visible. No review stamps or proposals are created by these readers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.graph.garden.proposals import GARDEN_ACTOR

if TYPE_CHECKING:
    from precis.store import Store

MEMORY_DETECTORS = (
    "dangling_mention",
    "anchor_rot",
    "grab_bag",
    "thread_shape",
    "review_due",
    "title_shape",
    "negative_undated",
    "source_drift",
    "entity_claim_stale",
    "restated_claim",
    "host_facts",
)
RECENT_DAYS = 30


@dataclass(frozen=True)
class MemorySmell:
    review_id: int
    ref_id: int
    detector: str
    action: str
    evidence: Any
    verdict: str
    proposed_at: datetime
    decided_at: datetime | None


def _garden(note: str | None) -> dict[str, Any] | None:
    try:
        body = json.loads(note or "null")
    except (TypeError, ValueError):
        return None
    garden = body.get("garden") if isinstance(body, dict) else None
    return garden if isinstance(garden, dict) else None


def memory_smells(
    store: Store,
    *,
    include_ref_ids: list[int] | None = None,
    exclude_ref_ids: list[int] | None = None,
) -> list[MemorySmell]:
    """Read live memories' open and recently decided proposals without a cap."""
    clauses = [
        "v.target_kind = 'ref'",
        "r.kind = 'memory'",
        "r.retired_at IS NULL",
        "v.note LIKE %s",
    ]
    params: list[Any] = ['%"garden"%']
    if include_ref_ids is not None:
        clauses.append("r.ref_id = ANY(%s)")
        params.append(include_ref_ids)
    if exclude_ref_ids:
        clauses.append("NOT (r.ref_id = ANY(%s))")
        params.append(exclude_ref_ids)
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT v.review_id, v.target_id, v.actor, v.verdict, v.note, v.at "
            "FROM reviews v JOIN refs r ON r.ref_id = v.target_id WHERE "
            + " AND ".join(clauses)
            + " ORDER BY v.at DESC, v.review_id DESC",
            params,
        ).fetchall()
    decisions: dict[tuple[int, int], tuple[str, datetime]] = {}
    proposals: list[tuple[Any, dict[str, Any]]] = []
    for row in rows:
        garden = _garden(row[4])
        if garden is None or garden.get("proposer_reject"):
            continue
        resolves = garden.get("resolves")
        if row[3] in ("approved", "rejected") and isinstance(resolves, int):
            decisions.setdefault((int(row[1]), resolves), (str(row[3]), row[5]))
        elif row[2] == GARDEN_ACTOR and row[3] == "proposed":
            proposals.append((row, garden))
    cutoff = datetime.now(UTC) - timedelta(days=RECENT_DAYS)
    result: list[MemorySmell] = []
    for row, garden in proposals:
        decision = decisions.get((int(row[1]), int(row[0])))
        if decision is not None and decision[1] < cutoff:
            continue
        result.append(
            MemorySmell(
                review_id=int(row[0]),
                ref_id=int(row[1]),
                detector=str(garden.get("detector") or "(unknown)"),
                action=str(garden.get("action") or "(unknown)"),
                evidence=garden.get("evidence"),
                verdict=decision[0] if decision else "proposed",
                proposed_at=row[5],
                decided_at=decision[1] if decision else None,
            )
        )
    return result


def pending_detector_counts(store: Store) -> dict[str, int]:
    """Count every pending key, without the CLI proposal listing's row cap."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT target_kind, target_id, verdict, note FROM reviews "
            "WHERE note LIKE %s ORDER BY at DESC, review_id DESC",
            ('%"garden"%',),
        ).fetchall()
    seen: set[tuple[str, int, str]] = set()
    counts: dict[str, int] = {}
    for kind, target, verdict, note in rows:
        garden = _garden(note)
        if garden is None or garden.get("proposer_reject"):
            continue
        key = (str(kind), int(target), str(garden.get("key") or ""))
        if key in seen:
            continue
        seen.add(key)
        if verdict == "proposed":
            detector = str(garden.get("detector") or "(unknown)")
            counts[detector] = counts.get(detector, 0) + 1
    return counts
