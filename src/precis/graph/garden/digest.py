"""The held-proposal digest — where judge-surviving holds reach Reto (gr739890).

Machine proposals on human-written nodes used to be held with nowhere to
go: the consumer recorded the hold (a non-terminal ``garden_review`` row)
and nothing said who sees it next. The ruling (Reto, Claude voice,
2026-11-20) sets the shape:

1. The judge reviews held proposals like any other, so weak ones end
   there — the consumer no longer holds a protected memory before its
   review; an approved survivor is held carrying the judge's verdict.
2. The survivors reach Reto as a single digest, not one ask per proposal.
   :func:`maybe_mint_digest` runs after each consume pass and mints one
   ``kind='todo'`` row (open tag :data:`DIGEST_TAG`) listing every held
   decision proposal — only when at least one exists, at most one per
   day, and never while an unwalked digest still lists live items.
3. The digest is linked, not flat: each item links the node it would
   change (a ``get`` handle), the proposal (its review id) with its
   diff (the payload) and its evidence. It is a list to be walked, one
   item at a time, each item carrying its gist, the judge's verdict and
   a recommended answer; each answer is recorded before the next.

The digest todo deliberately carries no ``ask-user``/``waiting-for:``
machinery of its own beyond a ``waiting-for:reto`` marker — one parked
todo per digest, not one ask per proposal (gr738860). Working through it
happens in a session with an agent: per item, ``precis garden accept
<review_id>`` or ``precis garden reject <review_id>``.

An accept is a ruling (gr739841): :func:`~precis.graph.garden.proposals.
record_human_ruling` writes the ``garden_ruling`` row that
:func:`~precis.graph.garden.apply.human_ruled` checks, and the accept
then runs the proposal's apply door — the only way anything is ever
applied to a human-origin or considered node. A reject closes the
proposal; the key stays closed while the target is unchanged.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.graph.garden.paper_gate import HOLD_REASONS
from precis.graph.garden.proposals import (
    GardenProposal,
    pending_proposals,
    proposal_holds,
)
from precis.todo import TERMINAL_TODO_STATUSES
from precis.utils.handle_registry import try_format as _format_handle

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

#: The open-namespace tag every digest todo carries.
DIGEST_TAG = "garden-digest"

#: Holds whose follow-up is a human ruling, not operator repair: the
#: consumer's human-origin/considered protection and the paper identity
#: gate's ambiguous pairs. ``apply_error``/``unresolved``/explicit-apply
#: holds name a repair or an explicit door instead — they stay out of
#: the digest and keep their ``precis garden proposals`` next step.
DECISION_HOLDS: frozenset[str] = frozenset({"human", *HOLD_REASONS})

#: The digest appears at most once in this window — Claude's default
#: from the ruling: when there is something in it, at most daily.
MIN_MINT_INTERVAL = timedelta(hours=24)

_ITEM_JSON_CAP = 600
_TOTAL_BODY_CAP = 8000


@dataclass(frozen=True, slots=True)
class _DigestRow:
    ref_id: int
    created_at: Any
    status: str
    item_ids: tuple[int, ...] = field(default_factory=tuple)


def _digest_todos(store: Store) -> list[_DigestRow]:
    """Every digest todo, newest first, with its STATUS and item ids."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT r.ref_id, r.created_at, r.meta, "
            "  COALESCE((SELECT t2.value FROM ref_tags rt2 "
            "            JOIN tags t2 ON t2.tag_id = rt2.tag_id "
            "            WHERE rt2.ref_id = r.ref_id "
            "              AND t2.namespace = 'STATUS' LIMIT 1), 'open') "
            "FROM refs r "
            "WHERE r.kind = 'todo' AND r.retired_at IS NULL "
            "  AND EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t "
            "              ON t.tag_id = rt.tag_id "
            "              WHERE rt.ref_id = r.ref_id "
            "                AND t.namespace = 'OPEN' AND t.value = %s) "
            "ORDER BY r.created_at DESC, r.ref_id DESC",
            (DIGEST_TAG,),
        ).fetchall()
    out: list[_DigestRow] = []
    for ref_id, created_at, meta, status in rows:
        items = (meta or {}).get("items") or []
        ids = tuple(
            int(item["review_id"])
            for item in items
            if isinstance(item, dict) and isinstance(item.get("review_id"), int)
        )
        out.append(_DigestRow(int(ref_id), created_at, str(status), ids))
    return out


def held_decision_items(store: Store) -> list[tuple[GardenProposal, dict[str, Any]]]:
    """Pending proposals held for a human ruling, oldest first.

    The walk order: Reto works top to bottom, so the digest lists the
    oldest hold first instead of the pending view's newest-first.
    """
    props = pending_proposals(store, limit=500)
    holds = proposal_holds(store, [p.review_id for p in props])
    out = [
        (p, holds[p.review_id])
        for p in reversed(props)
        if holds.get(p.review_id, {}).get("held") in DECISION_HOLDS
    ]
    return out


def _bounded(value: Any, cap: int = _ITEM_JSON_CAP) -> str:
    text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    return text if len(text) <= cap else text[: cap - 1] + "…"


def _node_handle(store: Store, prop: GardenProposal) -> str:
    if prop.target_kind != "ref":
        return f"{prop.target_kind}:{prop.target_id}"
    refs = store.fetch_refs_by_ids([prop.target_id], include_deleted=True)
    ref = refs.get(prop.target_id)
    kind = str(ref.kind) if ref is not None else "ref"
    return _format_handle(kind, prop.target_id) or f"{kind}:{prop.target_id}"


def _item_lines(
    store: Store, n: int, prop: GardenProposal, hold: dict[str, Any]
) -> list[str]:
    """One digest item: gist, judge verdict, recommendation and links."""
    node = _node_handle(store, prop)
    kind = node.split(":", 1)[0] if ":" in node else "memory"
    verdict = str(hold.get("verdict") or "")
    judge = (
        f"{verdict} — {hold.get('judge_why') or hold.get('why', '')}"
        + (f" (by {hold['by_model']})" if hold.get("by_model") else "")
        if verdict
        else "no judge review on file — held before the judge-review ruling (gr739890)"
    )
    recommend = (
        "accept — the judge approved it; your accept is the ruling that lets "
        "the apply run"
        if verdict == "approve"
        else "none — decide from the node, the diff and the evidence"
    )
    return [
        f"## {n}. {prop.action} {node} — {prop.title}{prop.stale_marker()}",
        f"   node: {node} — open with get(kind='{kind}', id='{node}')",
        f"   proposal: review {prop.review_id} "
        "(`precis garden proposals` shows it with its hold)",
        f"   gist: {prop.rationale or prop.title}",
        f"   judge: {judge}",
        f"   recommend: {recommend}",
        f"   diff: {_bounded(prop.payload)}",
        f"   evidence: {_bounded(prop.evidence)}",
        f"   answer: precis garden accept {prop.review_id}   |   "
        f"precis garden reject {prop.review_id}",
    ]


def _digest_body(
    store: Store, items: list[tuple[GardenProposal, dict[str, Any]]]
) -> tuple[str, list[dict[str, Any]]]:
    """The walkable body and the machine-readable ``meta.items`` list."""
    header = [
        f"{len(items)} held garden proposal(s) need your ruling — walk top to "
        "bottom, one item at a time; each answer is recorded before the next.",
        "",
        "  precis garden accept <review_id> — your ruling; it applies the "
        "proposal (undoable via the apply id the command prints)",
        "  precis garden reject <review_id> — closes it; the key stays "
        "closed while the target is unchanged",
        "",
        "Every item links the node it would change, the proposal with its "
        "diff, and its evidence — open any of them before answering. When "
        "the last item is answered, close this todo (STATUS:done); newly "
        "held proposals arrive in the next digest (at most one a day).",
        "",
    ]
    lines = list(header)
    meta_items: list[dict[str, Any]] = []
    for n, (prop, hold) in enumerate(items, 1):
        block = _item_lines(store, n, prop, hold)
        if sum(len(ln) + 1 for ln in lines) + sum(len(ln) + 1 for ln in block) > (
            _TOTAL_BODY_CAP
        ):
            lines.append(
                f"… {len(items) - n + 1} more item(s) held — they stay on "
                "`precis garden proposals` until the next digest."
            )
            break
        lines.extend(block)
        meta_items.append(
            {
                "review_id": prop.review_id,
                "target": prop.target_id,
                "node": _node_handle(store, prop),
                "action": prop.action,
                "held": hold.get("held"),
                "verdict": hold.get("verdict"),
            }
        )
    return "\n".join(lines), meta_items


def maybe_mint_digest(store: Store, *, now: datetime | None = None) -> int | None:
    """Mint the held-decision digest todo when one is due, else ``None``.

    The guards, in order: nothing held → no digest; an open digest that
    still lists a live held item → walk it first; a digest minted within
    the last :data:`MIN_MINT_INTERVAL` → at most daily. Otherwise one
    todo lists every held decision proposal, oldest first.
    """
    if now is None:
        now = datetime.now(UTC)
    elif now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    items = held_decision_items(store)
    if not items:
        return None
    digests = _digest_todos(store)
    held_ids = {p.review_id for p, _ in items}
    open_rows = [d for d in digests if d.status not in TERMINAL_TODO_STATUSES]
    if any(set(d.item_ids) & held_ids for d in open_rows):
        return None
    if (
        digests
        and digests[0].created_at is not None
        and digests[0].created_at > now - MIN_MINT_INTERVAL
    ):
        return None
    body, meta_items = _digest_body(store, items)
    from precis.dispatch import Hub

    Hub(store=store).sibling("todo").put(
        text=f"Garden held proposals: {len(meta_items)} decision(s) waiting",
        body=body,
        tags=[DIGEST_TAG, "waiting-for:reto"],
        meta={"items": meta_items, "llm_tier": None},
    )
    fresh = _digest_todos(store)
    return int(fresh[0].ref_id) if fresh else None


__all__ = [
    "DECISION_HOLDS",
    "DIGEST_TAG",
    "MIN_MINT_INTERVAL",
    "held_decision_items",
    "maybe_mint_digest",
]
