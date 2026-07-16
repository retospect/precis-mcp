"""quest_tick — one bounded step of a quest's autonomous research loop.

Slice 4a of the quest layer (docs/proposals/quest-layer.md §The autonomous
research loop). This is the **skeleton** of the loop: a single, in-process,
structured LLM step routed through the ADR-0046 seam (``dispatch(LlmRequest)``)
that reads the quest's rolling context — its striving statement, the current
dossier, the slice-3 gaps + momentum, and the recent logbook tail — and returns
two things:

* **logbook entries** — 1–4 dated observations / hypotheses / decisions
  reflecting one step of thinking, appended to the WORM logbook; and
* a **rewritten dossier** — the living synthesis (current understanding, best
  leads, what's ruled out, open questions), whole-replaced in place.

No compute is dispatched yet (that is rung 4b — the tick learns to mint
``structure``/``pathway`` sims and read their measures). No autonomous
scheduling yet (rung 4d — a dispatcher picks which quest ticks when a slot
frees). So this rung is **dark**: nothing mints a tick automatically; it runs
only from ``precis quest tick <id>`` or an explicit caller. The
``PRECIS_QUEST_LOOP_ENABLED`` flag (:func:`quest_loop_enabled`) is defined here
for the future autonomous dispatcher to gate on.

The single model call is injectable (``dispatch_fn``) so the tick is
deterministically unit-testable without a live model.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.quest import dossier as dossier_mod
from precis.quest import gaps as gaps_mod
from precis.quest.logbook import (
    ENTRY_TYPES,
    LOG_KIND,
    append_entry,
    clamp_entry_type,
)

if TYPE_CHECKING:
    from precis.store import Ref, Store

QUEST_LOOP_ENABLED_ENV = "PRECIS_QUEST_LOOP_ENABLED"

#: How many trailing logbook entries to feed the tick as episodic context.
_LOGBOOK_TAIL = 8


def quest_loop_enabled() -> bool:
    """True when the autonomous quest loop is switched on (default OFF).

    Gates the *autonomous* dispatcher (rung 4d), not the manual CLI tick — a
    human running ``precis quest tick`` is explicit intent.
    """
    return os.environ.get(QUEST_LOOP_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


@dataclass(frozen=True)
class QuestTickOutcome:
    quest_id: int
    status: str  # "succeeded" | "failed"
    logbook_added: int
    dossier_rewritten: bool
    cost_usd: float | None
    note: str


# ── context assembly ──────────────────────────────────────────────────


def _logbook_tail(store: Store, quest_id: int, n: int = _LOGBOOK_TAIL) -> list[str]:
    """The last ``n`` logbook entries, formatted one per line (oldest first)."""
    entries = [
        b for b in store.list_blocks_for_ref(quest_id) if b.chunk_kind == LOG_KIND
    ]
    lines: list[str] = []
    for b in entries[-n:]:
        meta = b.meta or {}
        etype = meta.get("entry_type", "note")
        by = meta.get("by", "?")
        stamp = b.created_at.date().isoformat() if b.created_at else "?"
        cost = meta.get("cost")
        cost_s = f" cost={cost:g}" if cost else ""
        first = (b.text or "").splitlines()[0] if b.text else ""
        lines.append(f"- [{etype} · {stamp} · {by}{cost_s}] {first[:160]}")
    return lines


def _servers_summary(store: Store, quest_id: int) -> list[str]:
    """One line per server kind: count + a couple of titles."""
    live = gaps_mod._live_servers(store, quest_id)
    by_kind: dict[str, list[str]] = {}
    for r in live:
        title = (r.title or "").splitlines()[0] if r.title else ""
        by_kind.setdefault(r.kind, []).append(title[:50])
    out: list[str] = []
    for kind in sorted(by_kind):
        titles = [t for t in by_kind[kind] if t][:3]
        sample = ("; ".join(titles)) if titles else ""
        out.append(f"- {kind} ({len(by_kind[kind])}): {sample}")
    return out


def build_tick_prompt(store: Store, quest: Ref) -> str:
    """Assemble the full rolling-context prompt for one tick."""
    qid = quest.id
    stmt = quest.title or f"quest {qid}"
    prio = quest.prio if quest.prio is not None else "unset"
    _did, _h, dossier_text = dossier_mod.read_dossier(store, qid)
    gaps = gaps_mod.quest_gaps(store, qid)
    momentum = gaps_mod.quest_momentum(store, qid)

    gap_lines = [f"- {g.kind}: {g.detail}" for g in gaps] or ["- (none)"]
    tail = _logbook_tail(store, qid) or ["- (no logbook entries yet)"]
    servers = _servers_summary(store, qid) or ["- (nothing serves this quest yet)"]

    return _PROMPT_TEMPLATE.format(
        statement=stmt,
        prio=prio,
        momentum=momentum.label,
        momentum_detail=(
            f"{momentum.recent_entries} recent log · "
            f"{momentum.recent_server_events} server events · "
            f"{momentum.open_todo_servers} open todos · "
            f"{momentum.blocked_todo_servers} blocked"
        ),
        dossier=dossier_text or "(no dossier yet)",
        gaps="\n".join(gap_lines),
        logbook="\n".join(tail),
        servers="\n".join(servers),
        entry_types=", ".join(sorted(ENTRY_TYPES)),
    )


_PROMPT_TEMPLATE = """\
You are advancing a long-running research programme toward a perpetual striving \
(a "quest"). This is ONE bounded step of local reasoning — not the whole \
project. Ground everything in the context below; do not invent results you have \
no evidence for.

## The striving
{statement}
(priority {prio}; momentum: {momentum} — {momentum_detail})

## Current dossier (the living synthesis — you will rewrite it)
{dossier}

## Gaps (the exploration queue — what is thin or unanswered)
{gaps}

## Recent logbook (episodic — what happened, most recent last)
{logbook}

## What serves this quest
{servers}

## Your step
Do ONE increment of thinking: interpret the state, pick the most promising \
next direction to close a gap, and note what you'd try. Then rewrite the \
dossier to reflect current understanding.

Respond with EXACTLY ONE JSON object and nothing else:
{{
  "logbook": [
    {{"entry_type": "<one of: {entry_types}>", "text": "<one concise entry>"}}
  ],
  "dossier_markdown": "<the FULL rewritten dossier in markdown: current \
understanding, best leads so far, what's ruled out, open questions>"
}}

Give 1–4 logbook entries. A `hypothesis` you'd test, an `observation` from the \
state, a `decision` on direction, or a `dead-end` to stop re-treading are the \
most useful. Keep the dossier tight."""


# ── model call + parsing ──────────────────────────────────────────────


def _resolve_tier(tier: Any) -> Any:
    from precis.utils.llm.router import Tier

    if tier is None:
        return Tier.CLOUD_SMALL
    if isinstance(tier, Tier):
        return tier
    return Tier(str(tier))


def _extract_json(text: str) -> dict[str, Any] | None:
    """Best-effort parse: the last balanced ``{...}`` block in ``text``."""
    if not text:
        return None
    depth = 0
    start = -1
    candidate: str | None = None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    candidate = text[start : i + 1]
    if candidate is None:
        return None
    try:
        obj = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _payload_from_result(res: Any) -> dict[str, Any] | None:
    """Prefer the router's parsed ``.data``; fall back to parsing ``.text``."""
    data = getattr(res, "data", None)
    if isinstance(data, dict) and data:
        return data
    return _extract_json(getattr(res, "text", "") or "")


# ── the tick ──────────────────────────────────────────────────────────


def run_quest_tick(
    store: Store,
    quest_id: int,
    *,
    tier: Any = None,
    dispatch_fn: Callable[[Any], Any] | None = None,
    by: str = "agent",
) -> QuestTickOutcome:
    """Run one structured research step against ``quest_id``.

    ``dispatch_fn`` is injectable (defaults to the real router ``dispatch``) so
    the tick is unit-testable with a canned ``LlmResult``.
    """
    qref = store.get_ref(kind="quest", id=quest_id)
    if qref is None or qref.deleted_at is not None:
        return QuestTickOutcome(quest_id, "failed", 0, False, None, "quest not found")

    prompt = build_tick_prompt(store, qref)

    from precis.utils.llm.router import LlmRequest
    from precis.utils.llm.router import dispatch as _dispatch

    disp = dispatch_fn if dispatch_fn is not None else _dispatch
    res = disp(LlmRequest(tier=_resolve_tier(tier), prompt=prompt, source="quest_tick"))
    cost = getattr(res, "cost_usd", None)
    if getattr(res, "error", None):
        return QuestTickOutcome(
            quest_id, "failed", 0, False, cost, f"llm error: {res.error}"
        )

    payload = _payload_from_result(res)
    if payload is None:
        return QuestTickOutcome(
            quest_id, "failed", 0, False, cost, "unparseable model output"
        )

    # Apply logbook entries (clamp unknown entry types rather than reject).
    added = 0
    for e in payload.get("logbook") or []:
        if not isinstance(e, dict):
            continue
        text = str(e.get("text") or "").strip()
        if not text:
            continue
        etype = clamp_entry_type(e.get("entry_type"))
        raw_cost = e.get("cost")
        cost_val = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
        append_entry(store, quest_id, text=text, entry_type=etype, by=by, cost=cost_val)
        added += 1

    # Rewrite the dossier (the rolling context) if the model produced one.
    md = str(payload.get("dossier_markdown") or "").strip()
    rewritten = False
    if md:
        dossier_mod.rewrite_dossier(store, quest_id, md)
        rewritten = True

    note = "ok" if (added or rewritten) else "no-op (empty output)"
    return QuestTickOutcome(quest_id, "succeeded", added, rewritten, cost, note)


__all__ = [
    "QUEST_LOOP_ENABLED_ENV",
    "QuestTickOutcome",
    "build_tick_prompt",
    "quest_loop_enabled",
    "run_quest_tick",
]
