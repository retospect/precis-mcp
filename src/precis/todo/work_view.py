"""The work view — one read-only hierarchy of what is in flight.

``search(kind='todo', view='work')`` renders one collapsed line per live
quest — handle, the derived run-state (``running``, ``queued``,
``idle``), STATUS, prio, agents, live jobs, the next doable leaf and the
newest change — grouped needs-you → running → queued → idle →
blocked-work, then prio (td573295, td573923: an idle quest whose live
leaves are all blocked sorts top when Reto can unblock it, bottom when
only other work can — gr573557).
A running or queued quest expands in place to the todos that ``serves``
it → their subtrees, one row per todo with STATUS, prio, the
``claimed-by:`` lease, the number of fleet agents on it and its child
jobs folded into one ``jobs=N`` cell (a job is execution detail, not a
row), plus one line per live job. Then the strategic roots that serve no
quest ("unassigned"); then the fleet hosts and when they last reported.
A todo that serves the quest directly while also sitting inside another
served root renders once, in the larger tree. Dark-factory step 1
(td540840, ``docs/backlog/dark-factory.md``): every later step reads
this tree.

:func:`work_model` computes the hierarchy once as frozen dataclasses
(:class:`WorkModel`); :func:`render_work` formats it as MCP text and the
``/work`` web page (``precis_web.routes.work``) formats the same model as
HTML, so the two surfaces cannot drift (td540843).

Queue rows show open dependencies and stored hold reasons. Placement
previews use the first configured ladder rung only for undispatched plan
leaves; they are advisory, while live job lines show stored placement.
Dependencies and job fields are fetched once per root, and gate reasons
come from stored results so rendering never evaluates a gate (td573296).

Done / won't-do todos stay off the board — a finished row is history,
not work — though the walk still descends past one to live children.
The ``done=`` toggle (page-wide or per quest id) adds them back under
each quest as :class:`CompletedRow` lines, joined by that subtree's
terminal jobs, inside a seven-day window ending at ``now``; older
history lives in the quest's logbook (td573298). An abandoned quest
only boards when the toggle reaches it.

:func:`render_tree_ring` is the todo tree as an eye ring — parent,
siblings, children off ``refs.parent_id`` — appended by
:meth:`precis.handlers.todo.TodoHandler.eye` at ``fisheye+1hop`` and up,
because the link neighbourhood walks ``links`` rows only and the tree is
a column (gr541348).

Row grammar (MCP-TOKEN-FIX rulings, 2026-10-30): one header line per
block, bare values, every row starts with its handle so it is a click or
a ``get``. Gripes join the tree in slice td540842.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from precis.plans.lanes import ladder, load_lanes
from precis.plans.nodes import node_kind
from precis.response import Response
from precis.todo import blocker_terminal_sql, views, work_gripes, work_quest
from precis.todo.work_gripes import GripeBlock
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section
from precis.workers.job_describe import (
    UPKEEP_QUEST_HANDLE,
    job_sentence,
    quest_id_from_meta,
    quest_ids_for_structures,
    structure_ref_id_from_meta,
)

if TYPE_CHECKING:
    from precis.plans.lanes import Lane
    from precis.store import Ref, Store

#: Rows shown per served root before the view points at ``view='tree'``.
MAX_ROWS_PER_ROOT = 40
#: Siblings / children listed in the eye ring before the ``… +N`` line.
MAX_RING_ROWS = 12


def _h(kind: str, ref_id: int) -> str:
    return handle_registry.try_format(kind, ref_id) or f"{kind}:{ref_id}"


def _first_line(title: str | None, cap: int = 72) -> str:
    line = (title or "").split("\n", 1)[0].strip()
    return line if len(line) <= cap else line[: cap - 1].rstrip() + "…"


def _age(ts: Any, now: datetime) -> str:
    if isinstance(ts, datetime):
        then = ts
    elif isinstance(ts, str) and ts:
        try:
            then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            return "?"
    else:
        return "never"
    mins = (now - then).total_seconds() / 60
    if mins < 90:
        return f"{mins:.0f}m ago"
    if mins < 48 * 60:
        return f"{mins / 60:.0f}h ago"
    return f"{mins / 1440:.0f}d ago"


# ── fleet rows → agents per todo ──────────────────────────────────


def _fleet_rows(store: Store) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """``(agent rows, host rows)`` from the fleet kind; dead agents dropped."""
    agents: list[dict[str, Any]] = []
    hosts: list[dict[str, Any]] = []
    for r in store.list_refs(kind="fleet", limit=5000):
        m = dict(r.meta or {})
        if m.get("type") == "host":
            hosts.append(m)
        elif m.get("type") == "agent" and m.get("state") != "dead":
            agents.append(m)
    return agents, hosts


def _agent_haystack(m: dict[str, Any]) -> str:
    return " ".join(
        str(m.get(k) or "")
        for k in ("purpose", "tree", "branch", "window_name", "assigned", "slice")
    ).lower()


def agents_on(
    agents: list[dict[str, Any]], ref_id: int, tags: list[str]
) -> list[dict[str, Any]]:
    """Fleet agent rows working this todo: the row names the todo's
    handle, or the lease holder's name, in its purpose / tree / branch /
    assigned fields. Matching is by text because a fleet row is a
    host observation, not a link; the reporter never knows todo ids."""
    needles = {_h("todo", ref_id).lower()}
    for t in tags:
        if t.startswith("claimed-by:") and len(t) > len("claimed-by:"):
            needles.add(t[len("claimed-by:") :].lower())
    out = []
    for m in agents:
        hay = _agent_haystack(m)
        if any(n in hay for n in needles):
            out.append(m)
    return out


# ── the model: computed once, formatted by each surface ───────────


@dataclass(frozen=True)
class WorkRow:
    """One todo in the hierarchy. ``depth`` is 1 for a served root."""

    ref_id: int
    kind: str
    handle: str
    title: str
    status: str
    icon: str
    prio: int | None
    agents: int
    jobs: int
    jobs_live: int
    depth: int
    claimed_by: str | None
    waiting_for: str | None
    #: ``updated_at`` falling back to ``created_at`` — the web age column.
    updated_at: datetime | None
    #: Plan node kind off ``meta`` (:func:`precis.plans.nodes.node_kind`):
    #: ``task`` / ``phase`` / ``gate`` / ``loop`` / ``generator``, ``""`` for
    #: a plain todo (td542097).
    node: str = ""
    #: The node's predicate / count line (``gate sha-in-prod abc1234 — why``,
    #: ``loop 1/3 until answer``, ``generator slicer from …``; ``phase`` when
    #: ``auto_close``); ``""`` when there is nothing to say.
    node_text: str = ""
    #: ``meta.lane`` of the newest child job, ``""`` with none.
    lane: str = ""
    #: ``N`` from an ``attempt:<N>`` tag.
    attempt: int | None = None
    #: Open blocked-by target handles, in id order.
    after: tuple[str, ...] = ()
    #: Waiting tag, ask-user, or the stored closed gate's explanation.
    hold: str = ""
    #: Advisory first ladder rung for an undispatched plan leaf.
    preview: str = ""

    @property
    def stage(self) -> str:
        """The web page's stage column: the STATUS value (a gripe row's
        stage is its STATUS too)."""
        return self.status


@dataclass(frozen=True)
class Unblocker:
    """One todo whose action unblocks a blocked leaf (gr573557,
    td573923): the leaf itself (``waiting-for:reto``, or the leaf is
    the ask), or an open ``ask-user`` todo the leaf is ``blocked-by``.
    ``why`` is ``ask-user`` or ``waiting-for:reto``."""

    handle: str
    title: str
    why: str


@dataclass(frozen=True)
class RootBlock:
    """One served root's rows; ``hidden`` counts rows past the row cap.
    ``gripes`` are the root's open-gripe rows
    (:mod:`precis.todo.work_gripes`, td540842 / td541980). ``jobs`` are
    the live job lines under this root (td573295); ``running_jobs`` /
    ``claimable_jobs`` feed the quest's run-state. ``live_leaves`` /
    ``blocked_leaves`` / ``unblockers`` are the td573923 leaf
    classification, computed over every row before the row cap."""

    root_id: int
    rows: tuple[WorkRow, ...]
    hidden: int
    gripes: GripeBlock = field(default_factory=GripeBlock)
    jobs: tuple[str, ...] = ()
    running_jobs: int = 0
    claimable_jobs: int = 0
    live_leaves: int = 0
    blocked_leaves: int = 0
    unblockers: tuple[Unblocker, ...] = ()


@dataclass(frozen=True)
class CompletedRow:
    """One completed todo or terminal job under a toggled quest
    (td573298). ``outcome`` is the STATUS value (``done`` /
    ``won't-do`` for a todo; ``succeeded`` / ``failed`` / ``cancelled``
    for a job); ``detail`` the sha of a todo's newest ``landed:``
    comment or a job's ``meta.lane``; ``at`` the STATUS tag's
    ``ref_tags.created_at`` and ``when`` its age string."""

    handle: str
    kind: str
    title: str
    outcome: str
    detail: str
    when: str
    at: datetime


@dataclass(frozen=True)
class QuestBlock:
    """One quest's collapsed board line plus its expansion (td573295).

    ``run_state`` is derived — ``running`` while a served todo holds a
    live lease or a child job runs, else ``queued`` while a doable leaf
    or a claimable job waits, else ``idle``. ``jobs`` are the live job
    lines under the quest; ``next_doable`` the first doable leaf's
    handle (``""`` when none); ``newest`` the freshest row stamp.
    ``completed`` holds the quest's done/won't-do todos and terminal
    jobs inside the seven-day window — populated only when the
    ``done=`` toggle names this quest or is page-wide (td573298).

    ``board`` is the td573923 placement: an idle quest whose live
    leaves are all blocked is ``needs-you`` when Reto can unblock one
    (it sorts top) or ``blocked-work`` when only other work can (it
    sorts bottom); ``""`` keeps the run-state group. ``unblockers``
    names what unblocks a ``needs-you`` quest, deduplicated in handle
    order."""

    ref_id: int
    handle: str
    status: str
    prio: int | None
    title: str
    roots: tuple[RootBlock, ...]
    run_state: str
    agents: int
    running_jobs: int
    queued_jobs: int
    next_doable: str
    newest: datetime | None
    jobs: tuple[str, ...]
    completed: tuple[CompletedRow, ...] = ()
    board: str = ""
    unblockers: tuple[Unblocker, ...] = ()


@dataclass(frozen=True)
class UnassignedRoot:
    ref_id: int
    handle: str
    status: str
    title: str


@dataclass(frozen=True)
class HostRow:
    host: str
    reported_at: str | None
    age: str


@dataclass(frozen=True)
class WorkModel:
    quests: tuple[QuestBlock, ...]
    unassigned: tuple[UnassignedRoot, ...]
    hosts: tuple[HostRow, ...]
    n_agents: int
    #: The "unplaced gripes" bucket (td540842 / td541980).
    unplaced_gripes: GripeBlock = field(default_factory=GripeBlock)
    #: Live job lines with no quest path — the W5 unassigned bucket
    #: (td573299). Rendered lines, same shape as ``QuestBlock.jobs``.
    unassigned_jobs: tuple[str, ...] = ()
    #: Live upkeep jobs with no todo parent, filed under the platform
    #: quest — a count, never per-job lines.
    platform_jobs: int = 0
    #: ``derived_drain 2, plan_dispatch 1`` — the platform bucket by type.
    platform_summary: str = ""

    @property
    def n_running(self) -> int:
        return sum(q.run_state == "running" for q in self.quests)

    @property
    def n_queued(self) -> int:
        return sum(q.run_state == "queued" for q in self.quests)

    @property
    def n_idle(self) -> int:
        return sum(q.run_state == "idle" for q in self.quests)

    @property
    def n_needs_you(self) -> int:
        return sum(q.board == "needs-you" for q in self.quests)

    @property
    def n_blocked_work(self) -> int:
        return sum(q.board == "blocked-work" for q in self.quests)


# ── the view ──────────────────────────────────────────────────────


def _quest_status(store: Store, quest_id: int) -> str:
    """The quest's ``STATUS:`` value; a quest without one is active
    (the quest kind's default, unlike a todo's ``open``)."""
    for t in store.tags_for(quest_id):
        if t.prefix == "STATUS":
            return str(t.value)
    return "active"


def _todo_servers(store: Store, quest_id: int) -> list[Ref]:
    links = store.links_for(quest_id, direction="in", relation="serves")
    ids = {ln.src_ref_id for ln in links}
    refs = store.fetch_refs_by_ids(ids, include_deleted=False)
    return sorted(
        (r for r in refs.values() if r.kind == "todo"),
        key=lambda r: (r.prio if r.prio is not None else 5, r.id),
    )


def _tag_value(tags: list[str], prefix: str) -> str | None:
    return next((t[len(prefix) :] for t in tags if t.startswith(prefix)), None)


#: Text-row glyph standing in for the status icon on plan nodes.
_NODE_GLYPH = {"gate": "⊢", "loop": "↻", "generator": "⇶"}
#: ``gate_result.why`` is cut to this many characters in ``node_text``.
_WHY_CAP = 60


def _gate_line(spec: Any) -> str:
    """``<kind> <args one-liner>`` for a gate spec (arg values, in order)."""
    if not isinstance(spec, dict):
        return ""
    args = spec.get("args")
    vals = [str(v) for v in args.values()] if isinstance(args, dict) else []
    return " ".join([str(spec.get("kind") or "?"), *vals])


def _node_text(node: str, meta: dict[str, Any]) -> str:
    if node == "gate":
        text = f"gate {_gate_line(meta.get('gate'))}"
        res = meta.get("gate_result")
        why = str(res.get("why") or "").strip() if isinstance(res, dict) else ""
        if why:
            if len(why) > _WHY_CAP:
                why = why[: _WHY_CAP - 1].rstrip() + "…"
            text += f" — {why}"
        return text
    if node == "loop":
        loop = meta.get("loop")
        loop = loop if isinstance(loop, dict) else {}
        return (
            f"loop {loop.get('count') or 0}/{loop.get('max', '?')} "
            f"until {_gate_line(loop.get('until'))}"
        )
    if node == "generator":
        gen = meta.get("generate")
        gen = gen if isinstance(gen, dict) else {}
        return f"generator {gen.get('role', '?')} from {gen.get('from', '?')}"
    if node == "phase":
        return "phase"
    return ""


def _attempt(tags: list[str]) -> int | None:
    raw = _tag_value(tags, "attempt:")
    return int(raw) if raw is not None and raw.isdigit() else None


def _row(
    node: dict[str, Any],
    *,
    depth: int,
    statuses: dict[int, str],
    tags_by_ref: dict[int, list[str]],
    prios: dict[int, int | None],
    stamps: dict[int, datetime | None],
    agents: list[dict[str, Any]],
    jobs: tuple[int, int] = (0, 0),
    metas: dict[int, dict[str, Any]] | None = None,
    lanes: dict[int, str] | None = None,
    blockers: dict[int, tuple[int, ...]] | None = None,
    preview_leaf: bool = False,
    lane_table: tuple[Lane, ...] = (),
) -> WorkRow:
    rid = int(node["id"])
    tags = tags_by_ref.get(rid, [])
    status = statuses.get(rid, "open")
    claimed = next((t for t in tags if t.startswith("claimed-by:")), None)
    waiting = next((t for t in tags if t.startswith("waiting-for:")), None)
    asking = any(t == "ask-user" or t.startswith("ask-user:") for t in tags)
    kind = node.get("kind") or "todo"
    meta = (metas or {}).get(rid) or {}
    plan = node_kind(meta) or ""
    result = meta.get("gate_result")
    hold = waiting or ("ask-user" if asking else "")
    if (
        not hold
        and plan == "gate"
        and isinstance(result, dict)
        and result.get("open") is False
    ):
        hold = _node_text(plan, meta)
    preview = ""
    if (
        preview_leaf
        and kind == "todo"
        and status == "open"
        and not claimed
        and not jobs[0]
        and plan in ("", "task")
    ):
        rungs = ladder(meta.get("task_class") or "code", lanes=lane_table)
        if rungs:
            preview = f"{rungs[0].lane} {rungs[0].model or ''}".strip()
    return WorkRow(
        ref_id=rid,
        kind=kind,
        handle=_h(kind, rid),
        title=_first_line(node["title"]),
        status=status,
        icon=views._status_icon(
            status, claimed=claimed, waiting=waiting, asking=asking
        ),
        prio=prios.get(rid),
        agents=len(agents_on(agents, rid, tags)),
        jobs=jobs[0],
        jobs_live=jobs[1],
        depth=depth,
        claimed_by=_tag_value(tags, "claimed-by:"),
        waiting_for=_tag_value(tags, "waiting-for:"),
        updated_at=stamps.get(rid),
        node=plan,
        node_text=_node_text(plan, meta),
        lane=(lanes or {}).get(rid, ""),
        attempt=_attempt(tags),
        after=tuple(_h("todo", i) for i in (blockers or {}).get(rid, ())),
        hold=hold,
        preview=preview,
    )


def _format_row(r: WorkRow) -> str:
    glyph = _NODE_GLYPH.get(r.node)
    # a gate / loop / generator row leads with its glyph in place of the
    # status icon, so its STATUS is spelled out as a bracketed cell
    cells = [r.handle, glyph, f"[{r.status}]"] if glyph else [r.handle, r.icon]
    if r.prio is not None:
        cells.append(f"prio={r.prio}")
    if r.agents:
        cells.append(f"agents={r.agents}")
    if r.jobs:
        cells.append(
            f"jobs={r.jobs}" + (f"({r.jobs_live} live)" if r.jobs_live else "")
        )
    if r.lane:
        cells.append(f"lane={r.lane}")
    if r.after:
        cells.append(f"after {','.join(r.after)}")
    if r.hold:
        cells.append(r.hold)
    if r.preview:
        cells.append(f"preview: {r.preview}")
    if r.attempt is not None:
        cells.append(f"attempt:{r.attempt}")
    line = f"{'  ' * r.depth}{' '.join(cells)} — {r.title}"
    return f"{line} · {r.node_text}" if r.node_text else line


#: Job STATUS values that count as live in the ``jobs=N(M live)`` cell.
_LIVE_JOB = frozenset({"queued", "running"})

#: STATUS values that take a todo off the board by default (td573298).
_DONE_TODO = frozenset({"done", "won't-do"})

#: Terminal job STATUS values listed by the completed toggle — mirrors
#: ``store._tags_ops._JOB_TERMINAL``; local constant so this module
#: stays out of the store import chain.
_DONE_JOB = frozenset({"succeeded", "failed", "cancelled"})

#: How far back the completed toggle looks (td573298); the quest's
#: logbook holds the older history.
_DONE_WINDOW = timedelta(days=7)

#: The sha on a ``landed: <sha> — <subject>`` comment (``land_slice``).
#: ``landed: nothing`` (a no-diff landing) deliberately does not match.
_LANDED_RE = re.compile(r"landed:\s*([0-9a-f]{7,40})\b", re.IGNORECASE)

#: Job STATUS values for the run-state derivation and the quest's live
#: job lines — mirror ``precis.workers.executors._common.RUNNING`` /
#: ``CLAIMABLE`` (queued, yielded); local constants so this module stays
#: out of the executor import chain.
_JOB_RUNNING = "running"
_JOB_CLAIMABLE = frozenset({"queued", "yielded"})
#: A job worth a board line — running or claimable.
_BOARD_JOB = _JOB_CLAIMABLE | {_JOB_RUNNING}

#: Group order on the board: the run-state groups (td573295) plus the
#: td573923 placements — a blocked-only quest Reto can unblock sorts
#: top, one only other work can unblock sorts bottom (gr573557).
_BOARD_RANK = {
    "needs-you": 0,
    "running": 1,
    "queued": 2,
    "idle": 3,
    "blocked-work": 4,
}

#: STATUS values that keep a todo row out of the live-leaf set
#: (td573923): done / won't-do leave the board, paused parks the row.
_NOT_LIVE = _DONE_TODO | {"paused"}


def _run_state(leased: int, running_jobs: int, claimable_jobs: int, doable: int) -> str:
    """The quest's run-state (td573295): ``running`` while a served todo
    is leased or a job runs; else ``queued`` while a doable leaf or a
    claimable job waits; else ``idle``."""
    if leased or running_jobs:
        return "running"
    if doable or claimable_jobs:
        return "queued"
    return "idle"


def _live_claims(store: Store, ref_ids: list[int]) -> set[int]:
    """Ref ids among ``ref_ids`` carrying a live ``claimed-by:`` lease —
    ``ref_tags.expires_at`` in the future (NULL or past is not live)."""
    if not ref_ids:
        return set()
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id"
            " WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN'"
            "   AND t.value LIKE 'claimed-by:%%'"
            "   AND rt.expires_at IS NOT NULL AND rt.expires_at > now()",
            (ref_ids,),
        ).fetchall()
    return {int(r[0]) for r in rows}


def _prios_for(store: Store, ref_ids: list[int]) -> dict[int, int | None]:
    """``{ref_id: refs.prio}`` for the given ids — one query (td573295)."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, prio FROM refs WHERE ref_id = ANY(%s)",
            (ref_ids,),
        ).fetchall()
    return {int(r[0]): r[1] for r in rows}


def _job_line(
    job_id: int,
    status: str,
    title: str | None,
    parent_handle: str | None,
    fields: tuple[str, str, str] = ("", "", ""),
    sentence: str | None = None,
) -> str:
    """One live job's board line: handle, STATUS, the describe sentence
    (falling back to the title), parent todo, placement fields.
    ``parent_handle`` falsy (a job with no live todo parent) drops the
    ``· under td<id>`` cell (td573299)."""
    text = sentence or _first_line(title)
    line = f"  {_h('job', job_id)} [{status}] {text}"
    if parent_handle:
        line += f" · under {parent_handle}"
    parts = [
        f"{key}={value}"
        for key, value in zip(("lane", "model", "host"), fields, strict=True)
        if value
    ]
    return line + (" · " + " ".join(parts) if parts else "")


def _open_blockers(store: Store, ref_ids: list[int]) -> dict[int, tuple[int, ...]]:
    """Read dependencies once per block, using the doable view's terminal policy."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            f"""SELECT l.src_ref_id, l.dst_ref_id FROM links l
                JOIN refs b ON b.ref_id = l.dst_ref_id
                WHERE l.src_ref_id = ANY(%s) AND l.relation = 'blocked-by'
                  AND b.retired_at IS NULL
                  AND NOT EXISTS (
                    SELECT 1 FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
                    WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS'
                      AND t.value IN {blocker_terminal_sql()})
                ORDER BY l.src_ref_id, l.dst_ref_id""",
            (ref_ids,),
        ).fetchall()
    grouped: dict[int, list[int]] = {}
    for src, dst in rows:
        grouped.setdefault(int(src), []).append(int(dst))
    return {src: tuple(targets) for src, targets in grouped.items()}


def _job_fields(store: Store, job_ids: list[int]) -> dict[int, tuple[str, str, str]]:
    """Stored execution placement, batched independently of the row count."""
    if not job_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, meta->>'lane', meta->>'model', meta->>'lease_host' "
            "FROM refs WHERE ref_id = ANY(%s)",
            (job_ids,),
        ).fetchall()
    return {
        int(i): (lane or "", model or "", host or "") for i, lane, model, host in rows
    }


def _job_sentences(
    store: Store,
    job_rows: list[dict[str, Any]],
    statuses: dict[int, str],
) -> dict[int, str]:
    """``{job_id: describe sentence}`` for the board's live job lines
    (td573331) — the per-type
    :func:`precis.workers.job_describe.job_sentence`, never the stored
    title's "(unlinked)"/"(materializer i/n)". Three bulk reads (metas +
    latest job_summary/job_event chunk heads + the open ``quest:<id>``
    tags for every structure a job names) plus one quest-title read; a
    job row whose meta names no ``job_type`` (a bare test fixture)
    keeps its title instead.
    """
    ids = [r["id"] for r in job_rows]
    if not ids:
        return {}
    with store.pool.connection() as conn:
        metas = {
            int(rid): (m or {})
            for rid, m in conn.execute(
                "SELECT ref_id, meta FROM refs WHERE ref_id = ANY(%s)",
                (ids,),
            ).fetchall()
        }
        latest: dict[tuple[int, str], str] = {}
        for rid, ckind, text in conn.execute(
            """
            SELECT DISTINCT ON (c.ref_id, c.chunk_kind)
                   c.ref_id, c.chunk_kind, c.text
              FROM chunks c
             WHERE c.ref_id = ANY(%s)
               AND c.chunk_kind IN ('job_summary', 'job_event')
             ORDER BY c.ref_id, c.chunk_kind, c.ord DESC
            """,
            (ids,),
        ).fetchall():
            latest[(int(rid), ckind)] = text
        structure_quests = quest_ids_for_structures(
            conn,
            [
                sid
                for m in metas.values()
                if (sid := structure_ref_id_from_meta(m)) is not None
            ],
        )
    qid_by_rid = {
        rid: qid
        for rid, m in metas.items()
        if (qid := quest_id_from_meta(m, structure_quests)) is not None
    }
    titles = (
        {
            i: (ref.title or "")
            for i, ref in store.fetch_refs_by_ids(set(qid_by_rid.values())).items()
        }
        if qid_by_rid
        else {}
    )
    out: dict[int, str] = {}
    for r in job_rows:
        rid = int(r["id"])
        meta = metas.get(rid, {})
        if not meta.get("job_type"):
            continue
        qid = qid_by_rid.get(rid)
        headline = titles.get(qid) or f"qu{qid}" if qid is not None else None
        out[rid] = job_sentence(
            str(meta["job_type"]),
            params=meta.get("params"),
            meta=meta,
            status=statuses.get(rid, "open"),
            summary=latest.get((rid, "job_summary")),
            last_event=latest.get((rid, "job_event")),
            quest_headline=headline,
            error=None,
        )
    return out


def _newest_job_lanes(store: Store, job_ids: list[int]) -> dict[int, str]:
    """``{parent id: lane}`` from the newest of the given jobs per parent
    (``meta.lane``; one query). A parent whose newest job names no lane is
    absent."""
    if not job_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT DISTINCT ON (parent_id) parent_id, meta->>'lane' "
            "FROM refs WHERE ref_id = ANY(%s) AND parent_id IS NOT NULL "
            "ORDER BY parent_id, created_at DESC, ref_id DESC",
            (job_ids,),
        ).fetchall()
    return {int(pid): str(lane) for pid, lane in rows if lane}


def _done_on(done: bool | frozenset[int], quest_id: int) -> bool:
    """Whether the ``done=`` toggle covers ``quest_id`` (td573298)."""
    return done is True or (isinstance(done, frozenset) and quest_id in done)


def _terminal_status_times(
    store: Store, ref_ids: list[int]
) -> dict[int, tuple[str, datetime]]:
    """``{ref_id: (STATUS value, ref_tags.created_at)}`` for the done /
    terminal values — the tag's creation time is the completion time
    (the ``_now_jobs`` trick, td573298). One query for the whole board."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id, t.value, rt.created_at "
            "FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id "
            "WHERE rt.ref_id = ANY(%s) AND t.namespace = 'STATUS' "
            "AND t.value = ANY(%s)",
            (ref_ids, sorted(_DONE_TODO | _DONE_JOB)),
        ).fetchall()
    return {int(r[0]): (str(r[1]), r[2]) for r in rows}


def _landed_shas(store: Store, ref_ids: list[int]) -> dict[int, str]:
    """``{ref_id: sha-or-""}`` — the sha on each todo's newest
    ``landed:`` comment, one query for the board (td573298)."""
    if not ref_ids:
        return {}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.ref_id, c.text FROM chunks c "
            "WHERE c.ref_id = ANY(%s) AND c.chunk_kind = 'todo_comment' "
            "AND c.retired_at IS NULL AND c.text LIKE 'landed:%%' "
            "ORDER BY c.ref_id, c.ord",
            (ref_ids,),
        ).fetchall()
    out: dict[int, str] = {}
    for rid, text in rows:  # ``ord`` ascending — the newest lands last
        m = _LANDED_RE.match(str(text))
        out[int(rid)] = m.group(1) if m else ""
    return out


def _asking(tags: list[str]) -> bool:
    """An open ``ask-user`` / ``ask-user:*`` tag — same test ``_row`` runs."""
    return any(t == "ask-user" or t.startswith("ask-user:") for t in tags)


def _leaf_verdicts(
    store: Store,
    rows: list[dict[str, Any]],
    *,
    by_parent: dict[int | None, list[dict[str, Any]]],
    statuses: dict[int, str],
    tags_by_ref: dict[int, list[str]],
    metas: dict[int, dict[str, Any]],
    blockers: dict[int, tuple[int, ...]],
) -> tuple[int, int, tuple[Unblocker, ...]]:
    """``(live leaves, blocked leaves, unblockers)`` over every todo
    row — before the ``MAX_ROWS_PER_ROOT`` cap (td573923).

    A live leaf is a todo row whose STATUS is not done / won't-do /
    paused and which has no live child row (jobs are execution detail,
    not rows). It is blocked on an open ``blocked-by`` target, a hold
    (``waiting-for:*`` / ``ask-user`` / a closed gate) or STATUS
    ``blocked``. Reto can unblock when the leaf carries
    ``waiting-for:reto``, an open blocked-by target is an open
    ``ask-user`` todo, or the leaf is itself an open ask — one bulk
    tag read and one ref fetch for the targets."""
    target_ids = sorted({d for ds in blockers.values() for d in ds})
    target_refs = store.fetch_refs_by_ids(target_ids) if target_ids else {}
    target_tags = views._all_open_tags(store, target_ids) if target_ids else {}
    live = 0
    blocked = 0
    unblockers: dict[str, Unblocker] = {}
    for r in rows:
        if r["kind"] == "job":
            continue
        rid = r["id"]
        status = statuses.get(rid, "open")
        if status in _NOT_LIVE or any(
            statuses.get(c["id"], "open") not in _NOT_LIVE
            for c in by_parent.get(rid, [])
        ):
            continue
        live += 1
        tags = tags_by_ref.get(rid, [])
        meta = metas.get(rid) or {}
        result = meta.get("gate_result")
        hold = (
            any(t.startswith("waiting-for:") for t in tags)
            or _asking(tags)
            or (
                node_kind(meta) == "gate"
                and isinstance(result, dict)
                and result.get("open") is False
            )
        )
        if not (blockers.get(rid) or hold or status == "blocked"):
            continue
        blocked += 1
        if "waiting-for:reto" in tags:
            unblockers.setdefault(
                _h("todo", rid),
                Unblocker(_h("todo", rid), _first_line(r["title"]), "waiting-for:reto"),
            )
        for dst in blockers.get(rid, ()):
            tref = target_refs.get(dst)
            if (
                tref is not None
                and tref.kind == "todo"
                and _asking(target_tags.get(dst, []))
            ):
                unblockers.setdefault(
                    _h("todo", dst),
                    Unblocker(_h("todo", dst), _first_line(tref.title), "ask-user"),
                )
        if _asking(tags):
            unblockers.setdefault(
                _h("todo", rid),
                Unblocker(_h("todo", rid), _first_line(r["title"]), "ask-user"),
            )
    return live, blocked, tuple(unblockers[h] for h in sorted(unblockers))


def _render_root(
    store: Store,
    root: Ref,
    rows: list[dict[str, Any]],
    *,
    agents: list[dict[str, Any]],
) -> RootBlock | None:
    """The rows for one served root; ``rows`` is its
    :func:`views._fetch_subtree` result (todos and their child jobs)."""
    if not rows:
        return None
    ids = [r["id"] for r in rows]
    statuses = views._status_of_many(store, ids)
    tags_by_ref = views._all_open_tags(store, ids)
    todo_ids = [r["id"] for r in rows if r["kind"] != "job"]
    refs = store.fetch_refs_by_ids(set(todo_ids))
    prios = {i: (refs[i].prio if i in refs else None) for i in todo_ids}
    stamps = {
        i: ((refs[i].updated_at or refs[i].created_at) if i in refs else None)
        for i in todo_ids
    }
    metas = {i: dict(refs[i].meta or {}) for i in todo_ids if i in refs}
    blockers = _open_blockers(store, ids)
    job_fields = _job_fields(store, [r["id"] for r in rows if r["kind"] == "job"])
    planned = any(t.startswith("plan:") for tags in tags_by_ref.values() for t in tags)
    lane_table = load_lanes(store) if planned else ()
    lanes = _newest_job_lanes(store, [r["id"] for r in rows if r["kind"] == "job"])
    sentences = _job_sentences(
        store,
        [
            r
            for r in rows
            if r["kind"] == "job" and statuses.get(r["id"], "open") in _BOARD_JOB
        ],
        statuses,
    )
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    job_counts: dict[int, tuple[int, int]] = {}
    job_lines: list[str] = []
    running_jobs = 0
    claimable_jobs = 0
    for r in rows:
        if r["kind"] == "job":
            n, live = job_counts.get(r["parent_id"], (0, 0))
            status = statuses.get(r["id"], "open")
            job_counts[r["parent_id"]] = (n + 1, live + int(status in _LIVE_JOB))
            if status == _JOB_RUNNING:
                running_jobs += 1
            elif status in _JOB_CLAIMABLE:
                claimable_jobs += 1
            if status in _BOARD_JOB:
                job_lines.append(
                    _job_line(
                        r["id"],
                        status,
                        r["title"],
                        _h("todo", r["parent_id"]),
                        job_fields.get(r["id"], ("", "", "")),
                        sentence=sentences.get(r["id"]),
                    )
                )
            continue
        by_parent.setdefault(r["parent_id"], []).append(r)
    for children in by_parent.values():
        children.sort(
            key=lambda c: (
                prios.get(c["id"]) if prios.get(c["id"]) is not None else 5,
                c["id"],
            )
        )
    shown: list[WorkRow] = []
    hidden = 0

    def walk(node: dict[str, Any], depth: int) -> None:
        nonlocal hidden
        # A done / won't-do row leaves the board (td573298); its
        # children still render at their own depth, and the skipped row
        # counts against neither the cap nor the ``+N more`` tail.
        done_row = statuses.get(node["id"], "open") in _DONE_TODO
        if not done_row:
            if len(shown) >= MAX_ROWS_PER_ROOT:
                hidden += 1
            else:
                shown.append(
                    _row(
                        node,
                        depth=depth,
                        statuses=statuses,
                        tags_by_ref=tags_by_ref,
                        prios=prios,
                        stamps=stamps,
                        agents=agents,
                        jobs=job_counts.get(node["id"], (0, 0)),
                        metas=metas,
                        lanes=lanes,
                        blockers=blockers,
                        preview_leaf=planned and not by_parent.get(node["id"]),
                        lane_table=lane_table,
                    )
                )
        for c in by_parent.get(node["id"], []):
            walk(c, depth + 1)

    root_row = next(r for r in rows if r["id"] == root.id)
    walk(root_row, 1)
    live_leaves, blocked_leaves, unblockers = _leaf_verdicts(
        store,
        rows,
        by_parent=by_parent,
        statuses=statuses,
        tags_by_ref=tags_by_ref,
        metas=metas,
        blockers=blockers,
    )
    return RootBlock(
        root_id=root.id,
        rows=tuple(shown),
        hidden=hidden,
        gripes=work_gripes.root_gripes(store, tags_by_ref.get(root.id, [])),
        jobs=tuple(job_lines),
        running_jobs=running_jobs,
        claimable_jobs=claimable_jobs,
        live_leaves=live_leaves,
        blocked_leaves=blocked_leaves,
        unblockers=unblockers,
    )


def work_model(
    store: Store,
    *,
    now: datetime | None = None,
    done: bool | frozenset[int] = False,
) -> WorkModel:
    """The board as data: every live quest one collapsed line ordered
    run-state → prio → ref_id, its served roots' rows as the expansion,
    the unassigned roots, the fleet hosts. ``abandoned`` quests stay off
    unless the ``done=`` toggle reaches them (``True`` = every quest, a
    frozenset = those quest ids, td573298)."""
    now = now or datetime.now(UTC)
    agents, hosts = _fleet_rows(store)
    quests = [
        q for q in store.list_refs(kind="quest", limit=500) if q.retired_at is None
    ]
    # One doable pass for the whole board — a quest's doable leaves are
    # this set intersected with its served subtree ids (td573295).
    doable_ids = {
        leaf["id"] for leaf in views._doable_rows(store, under=None, limit=5000)
    }
    served: set[int] = set()
    # Pass 1 — the pre-existing per-quest shape: status, served roots,
    # subtrees, rendered root blocks. The td573295 run-state inputs are
    # set-based below: one ``_live_claims`` and one ``_prios_for`` query
    # for the whole board, so a page load adds a constant number of
    # queries regardless of quest count.
    pending: list[dict[str, Any]] = []
    for q in quests:
        status = _quest_status(store, q.id)
        if status == "abandoned" and not _done_on(done, q.id):
            continue
        roots = _todo_servers(store, q.id)
        served.update(r.id for r in roots)
        # A served todo that also sits inside another served root's
        # subtree renders once, in the containing tree.
        subtrees = {r.id: views._fetch_subtree(store, r.id) for r in roots}
        nested = {
            row["id"]
            for rid, rows in subtrees.items()
            for row in rows
            if row["id"] != rid
        }
        root_blocks: list[RootBlock] = []
        for root in roots:
            if root.id in nested:
                continue
            block = _render_root(store, root, subtrees[root.id], agents=agents)
            if block is not None:
                root_blocks.append(block)
        pending.append(
            {
                "q": q,
                "status": status,
                "ids": {row["id"] for rows in subtrees.values() for row in rows},
                "kinds": {
                    row["id"]: row["kind"] for rows in subtrees.values() for row in rows
                },
                "titles": {
                    row["id"]: row["title"]
                    for rows in subtrees.values()
                    for row in rows
                },
                "roots": root_blocks,
            }
        )
    leased_all = _live_claims(store, sorted({i for e in pending for i in e["ids"]}))
    for e in pending:
        e["leased"] = e["ids"] & leased_all
        e["doable"] = (e["ids"] & doable_ids) - e["leased"]
    dprios = _prios_for(store, sorted({i for e in pending for i in e["doable"]}))
    # The completed toggle's inputs (td573298): one STATUS-times query,
    # one landed-comment query and one job-meta query for the whole
    # board — skipped entirely when no quest is toggled.
    toggled = [e for e in pending if _done_on(done, e["q"].id)]
    status_at: dict[int, tuple[str, datetime]] = {}
    landed: dict[int, str] = {}
    job_lanes: dict[int, str] = {}
    if toggled:
        status_at = _terminal_status_times(
            store, sorted({i for e in toggled for i in e["ids"]})
        )
        landed = _landed_shas(
            store,
            sorted({i for e in toggled for i, k in e["kinds"].items() if k == "todo"}),
        )
        job_lanes = {
            i: fields[0]
            for i, fields in _job_fields(
                store,
                sorted(
                    {i for e in toggled for i, k in e["kinds"].items() if k == "job"}
                ),
            ).items()
        }
    cutoff = now - _DONE_WINDOW
    # W5 (td573299): every live job resolves to a quest — its parent
    # todo chain up to a root that ``serves`` one, else its own meta.
    # A job that resolves to a quest but sits outside its served trees
    # joins that quest's job lines for display only (run_state,
    # running_jobs and queued_jobs do not move); jobs that resolve
    # nowhere split into the platform upkeep count and the unassigned
    # bucket.
    split = work_quest.classify_live_jobs(store)
    extra_by_quest: dict[int, list[dict[str, Any]]] = {}
    for e in pending:
        extra = [
            r for r in split.by_quest.get(e["q"].id, []) if r["id"] not in e["kinds"]
        ]
        if extra:
            extra_by_quest[e["q"].id] = extra
    line_rows = list(split.unassigned) + [
        r for rows in extra_by_quest.values() for r in rows
    ]
    line_sentences = _job_sentences(
        store, line_rows, {r["id"]: r["status"] for r in line_rows}
    )
    line_fields = _job_fields(store, [r["id"] for r in line_rows])

    def _board_job_line(r: dict[str, Any]) -> str:
        parent = r.get("todo_parent")
        return _job_line(
            r["id"],
            r["status"],
            r["title"],
            _h("todo", parent) if parent else "",
            line_fields.get(r["id"], ("", "", "")),
            sentence=line_sentences.get(r["id"]),
        )

    extra_lines = {
        qid: tuple(_board_job_line(r) for r in rows)
        for qid, rows in extra_by_quest.items()
    }
    platform_types = Counter(
        str((r["meta"] or {}).get("job_type") or "job") for r in split.platform
    )
    blocks: list[tuple[tuple[int, int, int], QuestBlock]] = []
    for e in pending:
        q = e["q"]
        quest_doable: set[int] = e["doable"]
        next_doable = ""
        if quest_doable:
            best = min(
                quest_doable,
                key=lambda i: (dprios.get(i) or 5, i),
            )
            next_doable = _h("todo", best)
        running_n = sum(b.running_jobs for b in e["roots"])
        claimable_n = sum(b.claimable_jobs for b in e["roots"])
        run_state = _run_state(
            len(e["leased"]), running_n, claimable_n, len(quest_doable)
        )
        completed: tuple[CompletedRow, ...] = ()
        if _done_on(done, q.id):
            rows_done: list[CompletedRow] = []
            for rid, kind in e["kinds"].items():
                hit = status_at.get(rid)
                if hit is None:
                    continue
                outcome, at = hit
                if at is None or at < cutoff:
                    continue
                if kind == "todo":
                    if outcome not in _DONE_TODO:
                        continue
                    detail = landed.get(rid, "")
                else:
                    if outcome not in _DONE_JOB:
                        continue
                    detail = job_lanes.get(rid, "")
                rows_done.append(
                    CompletedRow(
                        handle=_h(kind, rid),
                        kind=kind,
                        title=_first_line(e["titles"].get(rid)),
                        outcome=outcome,
                        detail=detail,
                        when=_age(at, now),
                        at=at,
                    )
                )
            completed = tuple(
                sorted(rows_done, key=lambda c: (c.at, c.handle), reverse=True)
            )
        stamps = [
            r.updated_at for b in e["roots"] for r in b.rows if r.updated_at is not None
        ]
        # td573923 (gr573557): an idle quest whose live leaves are all
        # blocked is placed by who can unblock — ``needs-you`` when at
        # least one blocked leaf resolves to Reto (waiting-for:reto,
        # an open ask-user blocker, or the leaf is the ask), else
        # ``blocked-work``. Meta-resolved display jobs never count.
        live = sum(b.live_leaves for b in e["roots"])
        n_blocked = sum(b.blocked_leaves for b in e["roots"])
        unblockers: dict[str, Unblocker] = {}
        for b in e["roots"]:
            for u in b.unblockers:
                unblockers.setdefault(u.handle, u)
        board = ""
        if run_state == "idle" and live and live == n_blocked:
            board = "needs-you" if unblockers else "blocked-work"
        quest_block = QuestBlock(
            ref_id=q.id,
            handle=_h("quest", q.id),
            status=e["status"],
            prio=q.prio,
            title=_first_line(q.title),
            roots=tuple(e["roots"]),
            run_state=run_state,
            agents=sum(r.agents for b in e["roots"] for r in b.rows),
            running_jobs=running_n,
            queued_jobs=claimable_n,
            next_doable=next_doable,
            newest=max(stamps) if stamps else (q.updated_at or q.created_at),
            jobs=tuple(j for b in e["roots"] for j in b.jobs)
            + extra_lines.get(q.id, ()),
            completed=completed,
            board=board,
            unblockers=(
                tuple(unblockers[h] for h in sorted(unblockers))
                if board == "needs-you"
                else ()
            ),
        )
        blocks.append(
            (
                (
                    _BOARD_RANK[board or run_state],
                    q.prio if q.prio is not None else 5,
                    q.id,
                ),
                quest_block,
            )
        )
    blocks.sort(key=lambda b: b[0])

    unassigned = sorted(
        (
            r
            for r in store.list_refs(kind="todo", limit=2000)
            if r.parent_id is None
            and (r.meta or {}).get("rotation_root")
            and r.id not in served
        ),
        key=lambda r: r.id,
    )
    st = views._status_of_many(store, [r.id for r in unassigned])
    return WorkModel(
        quests=tuple(b for _k, b in blocks),
        unassigned=tuple(
            UnassignedRoot(
                ref_id=r.id,
                handle=_h("todo", r.id),
                status=st.get(r.id, "open"),
                title=_first_line(r.title),
            )
            for r in unassigned
        ),
        hosts=tuple(
            HostRow(
                host=str(hm.get("host")),
                reported_at=hm.get("reported_at"),
                age=_age(hm.get("reported_at"), now),
            )
            for hm in sorted(hosts, key=lambda m: str(m.get("host")))
        ),
        n_agents=len(agents),
        unplaced_gripes=work_gripes.unplaced_gripes(store, served),
        unassigned_jobs=tuple(_board_job_line(r) for r in split.unassigned),
        platform_jobs=len(split.platform),
        platform_summary=", ".join(
            f"{jt} {n}"
            for jt, n in sorted(platform_types.items(), key=lambda kv: (-kv[1], kv[0]))
        ),
    )


def _quest_line(q: QuestBlock, now: datetime) -> str:
    """The quest's collapsed board line (td573295)."""
    cells = [f"◆ {q.handle}", q.run_state, f"[{q.status}]"]
    if q.board == "blocked-work":
        cells.append("blocked by other work")
    if q.prio is not None:
        cells.append(f"prio={q.prio}")
    if q.agents:
        cells.append(f"agents={q.agents}")
    if q.jobs:
        cells.append(f"jobs={len(q.jobs)}")
    if q.next_doable:
        cells.append(f"next={q.next_doable}")
    cells.append(_age(q.newest, now))
    line = f"{' '.join(cells)} — {q.title}"
    if q.board == "needs-you" and q.unblockers:
        names = ", ".join(f"{u.handle} ({u.why})" for u in q.unblockers[:3])
        if len(q.unblockers) > 3:
            names += f" +{len(q.unblockers) - 3} more"
        line += f" · needs you: {names}"
    return line


def render_work(store: Store, *, now: datetime | None = None) -> Response:
    """``search(kind='todo', view='work')`` — the hierarchy, read-only."""
    now = now or datetime.now(UTC)
    model = work_model(store, now=now)
    n_q = len(model.quests)
    n_a = model.n_agents
    head = (
        f"# work — {n_q} quest{'' if n_q == 1 else 's'}: "
        f"{model.n_running} running · {model.n_queued} queued · "
        f"{model.n_idle} idle"
    )
    if model.n_needs_you:
        head += f" · {model.n_needs_you} need you"
    out: list[str] = [f"{head} · {n_a} live agent{'' if n_a == 1 else 's'}"]
    # td573923: blocked-only quests leave the idle group — needs-you
    # expands like running/queued, blocked-work stays collapsed.
    groups: tuple[tuple[str, list[QuestBlock], bool], ...] = (
        ("needs you", [q for q in model.quests if q.board == "needs-you"], True),
        ("running", [q for q in model.quests if q.run_state == "running"], True),
        ("queued", [q for q in model.quests if q.run_state == "queued"], True),
        (
            "idle",
            [q for q in model.quests if q.run_state == "idle" and not q.board],
            False,
        ),
        (
            "blocked by other work",
            [q for q in model.quests if q.board == "blocked-work"],
            False,
        ),
    )
    for label, group, expand in groups:
        if not group:
            continue
        out += ["", f"{label} ({len(group)})"]
        for q in group:
            out.append(_quest_line(q, now))
            if expand:
                for root in q.roots:
                    out.extend(_format_row(r) for r in root.rows)
                    if root.hidden:
                        out.append(
                            f"    … +{root.hidden} more — "
                            f"get(kind='todo', id={root.root_id}, view='tree')"
                        )
                    out.extend(work_gripes.render_root_gripes(root.gripes))
            # Idle keeps its roots collapsed, but a job that joined
            # q.jobs through meta resolution (outside the served trees,
            # so it never moved run_state) still lists — else the line
            # would vanish from the text view (td573854).
            out.extend(q.jobs)

    if model.unassigned:
        out += ["", f"unassigned roots ({len(model.unassigned)}) — serve no quest"]
        for u in model.unassigned:
            out.append(f"  {u.handle} [{u.status}] — {u.title}")

    if model.unassigned_jobs:
        out += [
            "",
            f"unassigned jobs ({len(model.unassigned_jobs)}) — live, no quest path",
        ]
        out.extend(model.unassigned_jobs)

    if model.platform_jobs:
        out += [
            "",
            f"platform ({model.platform_jobs}) — live jobs with no todo "
            f"parent, filed under {UPKEEP_QUEST_HANDLE}: {model.platform_summary}",
        ]

    out.extend(work_gripes.render_unplaced(model.unplaced_gripes))

    out += ["", "hosts"]
    if model.hosts:
        for h in model.hosts:
            out.append(f"  {h.host} reported {h.age}")
    else:
        out.append("  none reported — install scripts/fleet-report (fleet-help)")
    out.append("release: not reported by any host (dark-factory step 4)")

    body = "\n".join(out)
    body += render_next_section(
        [
            ("get(kind='todo', id=N, view='tree')", "one root in full"),
            (
                "get(kind='todo', id=N, extent='fisheye+1hop')",
                "a node with its tree ring",
            ),
            ("search(kind='todo', view='attention')", "what waits on a human"),
            ("get(kind='fleet')", "the agents by host"),
        ]
    )
    return Response(body=body)


# ── the tree as an eye ring ───────────────────────────────────────


def render_tree_ring(store: Store, ref_id: int) -> str:
    """``— tree —`` section for a todo eye: parent, siblings, children,
    each with its status icon. Empty string for a lone root with no
    children. Reads ``refs.parent_id`` directly (gr541348)."""
    focus = store.fetch_refs_by_ids({ref_id}).get(ref_id)
    if focus is None:
        return ""
    with store.pool.connection() as conn:
        children = conn.execute(
            "SELECT ref_id, title FROM refs WHERE parent_id = %s"
            " AND kind IN ('todo', 'job') AND retired_at IS NULL"
            " ORDER BY COALESCE(prio, 5), ref_id",
            (ref_id,),
        ).fetchall()
        siblings: list[tuple[Any, ...]] = []
        parent: tuple[Any, ...] | None = None
        if focus.parent_id is not None:
            parent = conn.execute(
                "SELECT ref_id, title FROM refs WHERE ref_id = %s AND retired_at IS NULL",
                (focus.parent_id,),
            ).fetchone()
            siblings = conn.execute(
                "SELECT ref_id, title FROM refs WHERE parent_id = %s AND ref_id <> %s"
                " AND kind = 'todo' AND retired_at IS NULL"
                " ORDER BY COALESCE(prio, 5), ref_id",
                (focus.parent_id, ref_id),
            ).fetchall()
    if parent is None and not children:
        return ""
    ids = [int(r[0]) for r in children + siblings] + (
        [int(parent[0])] if parent else []
    )
    statuses = views._status_of_many(store, ids)
    tags_by_ref = views._all_open_tags(store, ids)

    def line(row: tuple[Any, ...]) -> str:
        rid = int(row[0])
        tags = tags_by_ref.get(rid, [])
        icon = views._status_icon(
            statuses.get(rid, "open"),
            claimed=next((t for t in tags if t.startswith("claimed-by:")), None),
            waiting=next((t for t in tags if t.startswith("waiting-for:")), None),
            asking=any(t == "ask-user" or t.startswith("ask-user:") for t in tags),
        )
        return f"  {_h('todo', rid)} {icon} — {_first_line(row[1])}"

    out = ["— tree —"]
    if parent is not None:
        out.append("part-of:")
        out.append(line(parent))
    if siblings:
        out.append(f"peers ({len(siblings)}):")
        out += [line(r) for r in siblings[:MAX_RING_ROWS]]
        if len(siblings) > MAX_RING_ROWS:
            out.append(
                f"  … +{len(siblings) - MAX_RING_ROWS} more — "
                f"get(kind='todo', id={focus.parent_id}, view='tree')"
            )
    if children:
        out.append(f"contains ({len(children)}):")
        out += [line(r) for r in children[:MAX_RING_ROWS]]
        if len(children) > MAX_RING_ROWS:
            out.append(
                f"  … +{len(children) - MAX_RING_ROWS} more — "
                f"get(kind='todo', id={ref_id}, view='tree')"
            )
    return "\n".join(out)
