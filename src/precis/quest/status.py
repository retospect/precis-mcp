"""Quest ops status — a read-only consolidated view for `precis quest status`.

An operator debugging a stuck/misbehaving quest otherwise runs five separate
by-hand queries: the logbook tail, the candidate structures + their measures +
`ruled-out:*` tags, the sim-job status roll (`struct_relax`/`catpath_explore`
churn under each candidate), the autonomous coordinator loop's own
`quest_tick` job_event trail, and the per-quest LLM spend/errors from
`llm_call_log`. This module gathers all five in one pass; the CLI (`precis
quest status <id>`) is a thin printer over it. Read-only — no writes, no
mutation of any kind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from precis.quest.logbook import LOG_KIND as _LOG_KIND

if TYPE_CHECKING:
    from precis.store import Store

#: The compute-lane job_types a quest's candidates mint (barrier + stability).
_SIM_JOB_TYPES = ("struct_relax", "catpath_explore")

#: The `job_event` chunk kind every job dispatcher writes its forensics to
#: (see ``precis.workers.executors._common.JOB_EVENT_KIND``).
_JOB_EVENT_KIND = "job_event"


@dataclass(frozen=True)
class LogbookLine:
    pos: int
    entry_type: str
    by: str
    text: str


@dataclass(frozen=True)
class CandidateRow:
    ref_id: int
    handle: str
    name: str
    converged: bool
    measures: dict[str, float]
    ruled_out: list[str]


@dataclass(frozen=True)
class SimJobRow:
    job_id: int
    job_type: str
    parent_handle: str
    status: str | None
    created_at: str


@dataclass(frozen=True)
class TickEvent:
    job_id: int
    text: str


@dataclass(frozen=True)
class LlmSpend:
    calls: int
    real_usd: float
    errors: int
    last_ts: str | None
    recent_errors: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass(frozen=True)
class QuestStatus:
    quest_id: int
    handle: str
    title: str
    logbook_tail: list[LogbookLine]
    candidates: list[CandidateRow]
    sim_jobs: list[SimJobRow]
    tick_events: list[TickEvent]
    llm_spend: LlmSpend


def _logbook_tail(store: Store, quest_id: int, *, n: int) -> list[LogbookLine]:
    lines = [
        b for b in store.list_blocks_for_ref(quest_id) if b.chunk_kind == _LOG_KIND
    ]
    lines.sort(key=lambda b: b.pos)
    tail = lines[-n:] if n > 0 else lines
    return [
        LogbookLine(
            pos=b.pos,
            entry_type=str((b.meta or {}).get("entry_type", "?")),
            by=str((b.meta or {}).get("by", "?")),
            text=b.text,
        )
        for b in tail
    ]


def _candidate_rows(
    store: Store, quest_id: int
) -> tuple[list[CandidateRow], list[int]]:
    from precis.quest.frontier import _candidate_from_structure
    from precis.quest.gaps import _live_servers

    structures = [s for s in _live_servers(store, quest_id) if s.kind == "structure"]
    rows: list[CandidateRow] = []
    for s in structures:
        cand = _candidate_from_structure(store, s)
        ruled_out = [
            str(t) for t in store.tags_for(s.id) if str(t).startswith("ruled-out:")
        ]
        rows.append(
            CandidateRow(
                ref_id=s.id,
                handle=cand.handle,
                name=cand.name,
                converged=cand.converged,
                measures=cand.measures,
                ruled_out=ruled_out,
            )
        )
    return rows, [s.id for s in structures]


def _sim_job_rows(
    store: Store, candidate_ids: list[int], *, limit: int
) -> list[SimJobRow]:
    """The `struct_relax`/`catpath_explore` jobs under any of ``candidate_ids``,
    newest first, with their current STATUS — so cancelled/retried churn (a job
    that keeps getting re-minted or stolen) is visible at a glance."""
    from precis.utils import handle_registry

    if not candidate_ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT j.ref_id, j.meta->>'job_type', j.parent_id, j.created_at, "
            "  (SELECT t.value FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "   WHERE rt.ref_id = j.ref_id AND t.namespace = 'STATUS' "
            "   ORDER BY rt.ref_id DESC LIMIT 1) AS status "
            "FROM refs j "
            "WHERE j.kind = 'job' AND j.deleted_at IS NULL "
            "AND j.parent_id = ANY(%s) AND j.meta->>'job_type' = ANY(%s) "
            "ORDER BY j.created_at DESC LIMIT %s",
            (list(candidate_ids), list(_SIM_JOB_TYPES), limit),
        ).fetchall()
    out: list[SimJobRow] = []
    for job_id, job_type, parent_id, created_at, status in rows:
        handle = handle_registry.try_format("structure", parent_id) or (
            f"structure:{parent_id}"
        )
        out.append(
            SimJobRow(
                job_id=int(job_id),
                job_type=str(job_type or "?"),
                parent_handle=handle,
                status=str(status) if status else None,
                created_at=str(created_at),
            )
        )
    return out


def _tick_events(store: Store, quest_id: int, *, n: int) -> list[TickEvent]:
    """The latest `quest_tick` coordinator job's own `job_event` trail (the
    autonomous loop's tick/await/tick cycle) — dark today (rung 4d), so an
    empty list is the common case, not a bug."""
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id FROM refs "
            "WHERE kind = 'job' AND deleted_at IS NULL "
            "AND meta->>'job_type' = 'quest_tick' "
            "AND (meta #>> '{params,quest_id}')::bigint = %s "
            "ORDER BY ref_id DESC LIMIT 1",
            (quest_id,),
        ).fetchone()
    if row is None:
        return []
    job_id = int(row[0])
    events = [
        b for b in store.list_blocks_for_ref(job_id) if b.chunk_kind == _JOB_EVENT_KIND
    ]
    events.sort(key=lambda b: b.pos)
    tail = events[-n:] if n > 0 else events
    return [TickEvent(job_id=job_id, text=b.text) for b in tail]


def _llm_spend(store: Store, quest_id: int, *, error_limit: int = 5) -> LlmSpend:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*)::int, "
            "  COALESCE(sum(cost_usd) FILTER (WHERE cost_usd IS NOT NULL), 0)::float, "
            "  count(*) FILTER (WHERE errored)::int, "
            "  max(ts)::text "
            "FROM llm_call_log WHERE ref_id = %s",
            (quest_id,),
        ).fetchone()
        err_rows = conn.execute(
            "SELECT ts::text, source, error FROM llm_call_log "
            "WHERE ref_id = %s AND errored ORDER BY ts DESC LIMIT %s",
            (quest_id, error_limit),
        ).fetchall()
    calls, real_usd, errors, last_ts = row if row else (0, 0.0, 0, None)
    recent_errors = [
        (str(ts), str(source or "?"), str(err or "")) for ts, source, err in err_rows
    ]
    return LlmSpend(
        calls=int(calls),
        real_usd=float(real_usd),
        errors=int(errors),
        last_ts=last_ts,
        recent_errors=recent_errors,
    )


def gather_quest_status(
    store: Store,
    quest_id: int,
    *,
    logbook_n: int = 10,
    tick_n: int = 10,
    sim_job_limit: int = 20,
) -> QuestStatus | None:
    """Assemble the five-part read-only ops report for one quest.

    Returns ``None`` if the quest ref doesn't exist. Every part degrades to
    empty independently (no logbook yet, no candidates yet, the autonomous
    loop never armed, no LLM spend yet) — a fresh quest reports cleanly rather
    than raising.
    """
    from precis.utils import handle_registry

    qref = store.get_ref(kind="quest", id=quest_id)
    if qref is None:
        return None
    handle = handle_registry.try_format("quest", quest_id) or f"quest:{quest_id}"
    title = (qref.title or "").splitlines()[0] if qref.title else handle

    candidates, candidate_ids = _candidate_rows(store, quest_id)
    return QuestStatus(
        quest_id=quest_id,
        handle=handle,
        title=title,
        logbook_tail=_logbook_tail(store, quest_id, n=logbook_n),
        candidates=candidates,
        sim_jobs=_sim_job_rows(store, candidate_ids, limit=sim_job_limit),
        tick_events=_tick_events(store, quest_id, n=tick_n),
        llm_spend=_llm_spend(store, quest_id),
    )


def _fmt_measures(measures: dict[str, float]) -> str:
    if not measures:
        return "(no measures yet)"
    return ", ".join(f"{k}={v:g}" for k, v in sorted(measures.items()))


def render_quest_status(status: QuestStatus) -> str:
    """Render a :class:`QuestStatus` as the CLI's plain-text report."""
    lines: list[str] = [f"quest status — {status.handle} ({status.title})", ""]

    lines.append(f"logbook (last {len(status.logbook_tail)}):")
    if not status.logbook_tail:
        lines.append("  (empty)")
    for entry in status.logbook_tail:
        lines.append(f"  #{entry.pos + 1} [{entry.entry_type}/{entry.by}] {entry.text}")
    lines.append("")

    lines.append(f"candidates ({len(status.candidates)}):")
    if not status.candidates:
        lines.append("  (none yet)")
    for c in status.candidates:
        conv = "converged" if c.converged else "unconverged"
        out = f"  {c.handle} ({c.name}) — {conv}, {_fmt_measures(c.measures)}"
        if c.ruled_out:
            out += f" — {', '.join(c.ruled_out)}"
        lines.append(out)
    lines.append("")

    lines.append(f"sim jobs ({len(status.sim_jobs)}, newest first):")
    if not status.sim_jobs:
        lines.append("  (none)")
    for j in status.sim_jobs:
        lines.append(
            f"  job:{j.job_id} {j.job_type} on {j.parent_handle} — "
            f"STATUS:{j.status or '?'} @ {j.created_at}"
        )
    lines.append("")

    lines.append(f"coordinator tick events (last {len(status.tick_events)}):")
    if not status.tick_events:
        lines.append("  (none — the autonomous loop is dark or never armed)")
    for e in status.tick_events:
        lines.append(f"  job:{e.job_id}  {e.text}")
    lines.append("")

    spend = status.llm_spend
    lines.append(
        f"llm spend: {spend.calls} call(s), ${spend.real_usd:.4f}, "
        f"{spend.errors} error(s)"
        + (f", last @ {spend.last_ts}" if spend.last_ts else "")
    )
    for ts, source, err in spend.recent_errors:
        lines.append(f"  error @ {ts} [{source}]: {err[:200]}")

    return "\n".join(lines)


__all__ = [
    "CandidateRow",
    "LlmSpend",
    "LogbookLine",
    "QuestStatus",
    "SimJobRow",
    "TickEvent",
    "gather_quest_status",
    "render_quest_status",
]
