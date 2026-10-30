"""``plan_dispatch`` job_type — the dark-factory tick that drives plan subtrees.

Spec: ``docs/backlog/dark-factory.md`` § "Plans" → "The dispatcher tick" and
"Job lifecycle per task"; the kill switch is step 7's. A plan is a todo
subtree whose nodes are shaped by :mod:`precis.plans.nodes`. One pass:

1. **gates** — every live, unblocked gate todo is evaluated
   (:func:`precis.plans.gates.evaluate`); the verdict lands on
   ``meta.gate_result = {open, why, at}`` (written when it changes) and an
   open gate goes ``STATUS:done``, which frees its ``blocked-by`` dependants.
2. **loops** — once every current child of a loop is closed: ``until`` open
   or ``meta.loop.count`` (default 0) at ``max`` closes the loop; otherwise
   the body is re-minted under it (:func:`precis.plans.templates.mint_body`)
   and the count bumped. A body with no minter closes the loop with a
   comment naming the gap.
3. **phases** — closes every node whose work is finished: a phase
   (``meta.auto_close``) or a generator with children, once every child is
   closed; a mechanical task (``meta.op`` other than ``run_slice``) whose
   newest job succeeded. Deepest first, so nested phases cascade in one pass.
4. **dispatch** — each doable task (``node_kind`` task, a generator with no
   children yet, or a plain leaf under a plan root; ``STATUS:open``, no live
   ``blocked-by`` target on it or an ancestor, no doable-exclusion tag other
   than ``child-failed:``, no paused ancestor, no live child job, no
   ``claimed-by:`` tag, under its retry cap) gets one job parented to it:
   ``meta.op`` resolved to ``{type, params}`` (a bare name is
   ``{type: name, params: {todo_id}}``), default ``run_slice {todo_id}``.
   The tick owns plan retries, so a retry drops the ``child-failed:`` tags
   the failure bubble left.
5. **hygiene** — a ``claimed-by:`` tag older than 4 h on a todo with no live
   child job is released (``STATUS:open``); a task whose newest job ended
   (failed, or succeeded without closing it) with attempts at
   ``meta.retries`` (default 2) gets one ask-user todo under the plan root
   (``meta.ask`` kind ``ruling``, tags ``ask-user`` + ``showstopper``) that it
   is ``blocked-by``. Attempts count the ended jobs since the newest ask on
   the task (or the ``attempt:<n>`` tag ``run_slice`` writes, before any ask),
   so answering the ask re-arms the task.

Plan roots: the ids named by ``plan:<id>`` tags on live todos, plus every
live todo whose meta makes it a gate / loop / generator / phase; a root
inside another root's subtree is folded into it. Closed roots are skipped.

**Kill switch.** ``PRECIS_FACTORY=off`` in the worker's env, a
``service_config`` row for ``plan_dispatch`` with ``prio = 0`` (any host), or
``params.dry_run`` makes the pass read-only: every step runs on the
in-memory model and counts what it would have done, nothing is written. The
job summary's first line is
``plan_dispatch: roots=N gates=o/c loops=minted/closed phases=closed
dispatched=N released=N asks=N dry_run=bool``; the actions follow it.

**Arming** (an operator write, not done by any deploy) — the recurring todo
the minter turns into a job every 5 minutes, like ``graph_garden``::

    put(kind='todo', text='plan_dispatch tick (dark factory)',
        meta={'schedule': {'every': '5m'}, 'executor': 'job_inproc',
              'job_type': 'plan_dispatch'})

``dedupe_pending`` coalesces a tick whose previous run is still pending.
Workers never import handlers: every write here is a store call.
"""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from precis.errors import BadInput
from precis.plans import gates as _gates
from precis.plans import templates as _templates
from precis.plans.nodes import node_kind
from precis.store._todo_sql import _DOABLE_EXCLUSION_TAGS
from precis.store.types import TODO_BODY_KIND, ChunkInsert, Tag
from precis.todo import BLOCKER_TERMINAL_STATUSES, TERMINAL_TODO_STATUSES
from precis.todo.guards import check_ask_meta
from precis.todo.views import _fetch_subtree
from precis.workers.job_types import JobTypeSpec, get_job_type

log = logging.getLogger(__name__)

NAME = "plan_dispatch"

#: ``service_config.service`` of the kill switch (a ``prio = 0`` row).
SERVICE = "plan_dispatch"

#: A ``claimed-by:`` tag older than this, with no live job, is released.
CLAIM_STALE = timedelta(hours=4)

#: Default ``meta.retries`` of a task.
DEFAULT_RETRIES = 2

#: The job type a task without ``meta.op`` (and every generator) runs.
DEFAULT_OP = "run_slice"

#: The todo's comment chunk kind (``handlers.todo._COMMENT_KIND``; restated —
#: workers never import handlers).
_COMMENT_KIND = "todo_comment"

_JOB_TERMINAL = frozenset({"succeeded", "failed", "cancelled"})
_JOB_ENDED = frozenset({"succeeded", "failed"})
_ATTEMPT_RE = re.compile(r"^attempt:(\d+)$")

#: Doable exclusions the tick honours: the shared registry minus
#: ``child-failed:`` — plan retries are the tick's, not the bubble's.
_EXCLUSIONS: tuple[str, ...] = tuple(
    t for t in _DOABLE_EXCLUSION_TAGS if t != "child-failed:"
)

#: Executor preference when a job type is compatible with several.
_EXECUTOR_ORDER: tuple[str, ...] = (
    "job_inproc",
    "coordinator",
    "claude_inproc",
    "ssh_node",
)

#: ``meta.ask.context`` cap (``todo.guards._ASK_CONTEXT_CAP``).
_ASK_CONTEXT_CAP = 600
_ASK_OPTIONS: list[str] = [
    "retry with a different lane/model",
    "rewrite the brief",
    "won't do",
]
#: Action lines kept in the job summary.
_NOTES_CAP = 60

PARAMS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        # Run the pass read-only: count and report, write nothing.
        "dry_run": {"type": "boolean"},
    },
    "additionalProperties": False,
}

DESCRIPTION: str = (
    "Dark-factory plan tick: evaluate gates, advance loops, close finished "
    "phases, mint one job per doable plan task (run_slice by default), "
    "release stale claims and file an ask-user once a task hits its retry "
    "cap. Read-only while PRECIS_FACTORY=off or service_config "
    "plan_dispatch prio 0."
)


# ── Model ──────────────────────────────────────────────────────────


@dataclass
class _Node:
    id: int
    kind: str  # 'todo' | 'job'
    parent_id: int | None
    title: str
    meta: dict[str, Any]
    status: str
    created_at: datetime
    prio: int | None
    depth: int
    root: int
    #: open tag value -> when it was set (todos only).
    open_tags: dict[str, datetime] = field(default_factory=dict)
    children: list[int] = field(default_factory=list)


@dataclass
class _Blocker:
    id: int
    status: str
    is_ask: bool
    created_at: datetime


@dataclass
class _Plan:
    roots: list[int]
    nodes: dict[int, _Node]
    #: blocked todo id -> its ``blocked-by`` targets.
    blockers: dict[int, list[_Blocker]]

    def todos(self) -> list[_Node]:
        return sorted(
            (n for n in self.nodes.values() if n.kind == "todo"), key=lambda n: n.id
        )

    def child_todos(self, n: _Node) -> list[_Node]:
        return [self.nodes[c] for c in n.children if self.nodes[c].kind == "todo"]

    def jobs(self, n: _Node) -> list[_Node]:
        """Child jobs, oldest first."""
        return sorted(
            (self.nodes[c] for c in n.children if self.nodes[c].kind == "job"),
            key=lambda j: (j.created_at, j.id),
        )

    def live_job(self, n: _Node) -> bool:
        return any(j.status not in _JOB_TERMINAL for j in self.jobs(n))

    def status_of(self, b: _Blocker) -> str:
        node = self.nodes.get(b.id)
        return node.status if node is not None else b.status

    def ancestors(self, n: _Node) -> list[_Node]:
        out: list[_Node] = []
        seen = {n.id}
        cur = self.nodes.get(n.parent_id) if n.parent_id is not None else None
        while cur is not None and cur.id not in seen:
            out.append(cur)
            seen.add(cur.id)
            cur = self.nodes.get(cur.parent_id) if cur.parent_id is not None else None
        return out

    def blocked(self, n: _Node) -> bool:
        """A live ``blocked-by`` target on ``n`` or any ancestor in the plan."""
        for node in (n, *self.ancestors(n)):
            for b in self.blockers.get(node.id, []):
                if self.status_of(b) not in BLOCKER_TERMINAL_STATUSES:
                    return True
        return False

    def paused(self, n: _Node) -> bool:
        return any(a.status == "paused" for a in (n, *self.ancestors(n)))

    def open_asks(self, n: _Node) -> list[_Blocker]:
        return [
            b
            for b in self.blockers.get(n.id, [])
            if b.is_ask and self.status_of(b) not in BLOCKER_TERMINAL_STATUSES
        ]


def _closed(n: _Node) -> bool:
    return n.status in TERMINAL_TODO_STATUSES


def _candidate_roots(conn: Any) -> list[int]:
    """Live, non-closed todos that root a plan (see the module docstring)."""
    ids: set[int] = set()
    for (value,) in conn.execute(
        """
        SELECT DISTINCT t.value
          FROM ref_tags rt JOIN tags t USING (tag_id)
          JOIN refs r ON r.ref_id = rt.ref_id
         WHERE t.namespace = 'OPEN' AND t.value LIKE 'plan:%%'
           AND r.kind = 'todo' AND r.retired_at IS NULL
        """
    ).fetchall():
        tail = str(value)[5:]
        if tail.isdigit():
            ids.add(int(tail))
    for ref_id, meta in conn.execute(
        """
        SELECT ref_id, meta FROM refs
         WHERE kind = 'todo' AND retired_at IS NULL
           AND meta ?| array['gate', 'loop', 'generate', 'auto_close']
        """
    ).fetchall():
        if node_kind(meta) not in (None, "task"):
            ids.add(int(ref_id))
    if not ids:
        return []
    rows = conn.execute(
        """
        SELECT r.ref_id FROM refs r
         WHERE r.ref_id = ANY(%s) AND r.kind = 'todo' AND r.retired_at IS NULL
           AND COALESCE(
                 (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                   WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1),
                 'open'
               ) <> ALL(%s)
         ORDER BY r.ref_id
        """,
        (sorted(ids), sorted(TERMINAL_TODO_STATUSES)),
    ).fetchall()
    return [int(r[0]) for r in rows]


def _load(store: Any) -> _Plan:
    """The in-memory model of every plan subtree."""
    with store.pool.connection() as conn:
        candidates = _candidate_roots(conn)
    subtrees = {root: _fetch_subtree(store, root) for root in candidates}
    contained: set[int] = set()
    for root, rows in subtrees.items():
        contained.update(r["id"] for r in rows if r["id"] != root)
    roots = [r for r in candidates if r not in contained]
    placement: dict[int, tuple[int, int]] = {}  # id -> (root, depth)
    for root in roots:
        for row in subtrees[root]:
            placement.setdefault(row["id"], (root, int(row["depth"])))
    if not placement:
        return _Plan(roots=roots, nodes={}, blockers={})
    ids = sorted(placement)
    with store.pool.connection() as conn:
        ref_rows = conn.execute(
            """
            SELECT r.ref_id, r.kind, r.parent_id, r.title, r.meta, r.created_at,
                   r.prio,
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = r.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM refs r
             WHERE r.ref_id = ANY(%s) AND r.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
        tag_rows = conn.execute(
            """
            SELECT rt.ref_id, t.value, rt.created_at
              FROM ref_tags rt JOIN tags t USING (tag_id)
             WHERE rt.ref_id = ANY(%s) AND t.namespace = 'OPEN'
            """,
            (ids,),
        ).fetchall()
        link_rows = conn.execute(
            """
            SELECT l.src_ref_id, b.ref_id, b.created_at, b.meta ? 'ask',
                   EXISTS (SELECT 1 FROM ref_tags rt JOIN tags t USING (tag_id)
                            WHERE rt.ref_id = b.ref_id AND t.namespace = 'OPEN'
                              AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')),
                   (SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id)
                     WHERE rt.ref_id = b.ref_id AND t.namespace = 'STATUS' LIMIT 1)
              FROM links l JOIN refs b ON b.ref_id = l.dst_ref_id
             WHERE l.relation = 'blocked-by' AND l.src_ref_id = ANY(%s)
               AND b.retired_at IS NULL
            """,
            (ids,),
        ).fetchall()
    nodes: dict[int, _Node] = {}
    for ref_id, kind, parent_id, title, meta, created_at, prio, status in ref_rows:
        root, depth = placement[int(ref_id)]
        nodes[int(ref_id)] = _Node(
            id=int(ref_id),
            kind=str(kind),
            parent_id=int(parent_id) if parent_id is not None else None,
            title=str(title or ""),
            meta=dict(meta or {}),
            status=str(status or ("open" if kind == "todo" else "queued")),
            created_at=created_at,
            prio=int(prio) if prio is not None else None,
            depth=depth,
            root=root,
        )
    for ref_id, value, set_at in tag_rows:
        node = nodes.get(int(ref_id))
        if node is not None:
            node.open_tags[str(value)] = set_at
    for node in nodes.values():
        if node.parent_id is not None and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node.id)
    blockers: dict[int, list[_Blocker]] = {}
    for src, dst, created_at, has_ask, ask_tag, status in link_rows:
        blockers.setdefault(int(src), []).append(
            _Blocker(
                id=int(dst),
                status=str(status or "open"),
                is_ask=bool(has_ask) or bool(ask_tag),
                created_at=created_at,
            )
        )
    return _Plan(roots=roots, nodes=nodes, blockers=blockers)


# ── Writes (no-ops when read-only) ─────────────────────────────────


@dataclass
class PassResult:
    """What one pass did (or, read-only, would have done)."""

    dry_run: bool
    reason: str | None = None
    roots: int = 0
    gates_opened: int = 0
    gates_closed: int = 0
    loops_minted: int = 0
    loops_closed: int = 0
    phases_closed: int = 0
    dispatched: int = 0
    released: int = 0
    asks: int = 0
    notes: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        return {
            "roots": self.roots,
            "gates_opened": self.gates_opened,
            "gates_closed": self.gates_closed,
            "loops_minted": self.loops_minted,
            "loops_closed": self.loops_closed,
            "phases_closed": self.phases_closed,
            "dispatched": self.dispatched,
            "released": self.released,
            "asks": self.asks,
            "dry_run": self.dry_run,
        }

    def summary(self) -> str:
        return (
            f"plan_dispatch: roots={self.roots} "
            f"gates={self.gates_opened}/{self.gates_closed} "
            f"loops={self.loops_minted}/{self.loops_closed} "
            f"phases={self.phases_closed} dispatched={self.dispatched} "
            f"released={self.released} asks={self.asks} dry_run={self.dry_run}"
        )

    def render(self) -> str:
        lines = [self.summary()]
        if self.reason:
            lines.append(f"read-only ({self.reason}): the counts are what it would do")
        shown = self.notes[:_NOTES_CAP]
        lines += [f"- {n}" for n in shown]
        if len(self.notes) > len(shown):
            lines.append(f"- … {len(self.notes) - len(shown)} more")
        return "\n".join(lines)


class _Writer:
    """Store writes for one pass; every method is a no-op when ``dry_run``."""

    def __init__(self, store: Any, dry_run: bool) -> None:
        self.store = store
        self.dry_run = dry_run

    def set_status(self, n: _Node, value: str) -> None:
        n.status = value
        if self.dry_run:
            return
        self.store.add_tag(
            n.id, Tag.closed("STATUS", value), set_by="system", replace_prefix=True
        )
        if value == "done":
            # Picks-7d accounting, as the todo handler's tag path writes it.
            self.store.append_event(n.id, source=NAME, event="status:done")

    def stamp_meta(self, n: _Node, updates: dict[str, Any]) -> None:
        n.meta.update(updates)
        if not self.dry_run:
            self.store.stamp_ref_meta(n.id, updates)

    def remove_tags(self, n: _Node, values: list[str]) -> None:
        for value in values:
            n.open_tags.pop(value, None)
            if not self.dry_run:
                self.store.remove_tag(n.id, Tag.open(value))

    def comment(self, n: _Node, text: str) -> None:
        if self.dry_run:
            return
        existing = list(self.store.chunks.list_chunks_for_ref(n.id))
        ord_ = max((int(c.ord) for c in existing), default=0) + 1
        self.store.chunks.insert_chunks(
            n.id,
            [ChunkInsert(ord=ord_, text=text, meta={"chunk_kind": _COMMENT_KIND})],
        )


def _iso(ts: datetime) -> str:
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ── (1) gates ──────────────────────────────────────────────────────


def _step_gates(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    for n in plan.todos():
        if node_kind(n.meta) != "gate" or _closed(n) or plan.paused(n):
            continue
        if plan.blocked(n):
            continue
        verdict = _gates.evaluate(store, n.meta["gate"], todo_id=n.id, now=now)
        prev = n.meta.get("gate_result") or {}
        if not isinstance(prev, dict) or (prev.get("open"), prev.get("why")) != (
            verdict.open,
            verdict.why,
        ):
            w.stamp_meta(
                n,
                {
                    "gate_result": {
                        "open": verdict.open,
                        "why": verdict.why,
                        "at": _iso(now),
                    }
                },
            )
        if verdict.open:
            w.set_status(n, "done")
            res.gates_opened += 1
            res.notes.append(f"gate td{n.id} open: {verdict.why}")
        else:
            res.gates_closed += 1


# ── (2) loops ──────────────────────────────────────────────────────


def _close_loop(w: _Writer, res: PassResult, n: _Node, why: str) -> None:
    w.comment(n, f"plan_dispatch: loop closed — {why}")
    w.set_status(n, "done")
    res.loops_closed += 1
    res.notes.append(f"loop td{n.id} closed: {why}")


def _step_loops(
    store: Any, plan: _Plan, w: _Writer, res: PassResult, now: datetime
) -> None:
    for n in plan.todos():
        if node_kind(n.meta) != "loop" or _closed(n) or plan.paused(n):
            continue
        if plan.blocked(n):
            continue
        if not all(_closed(c) for c in plan.child_todos(n)):
            continue
        spec = dict(n.meta.get("loop") or {})
        try:
            count = int(spec.get("count") or 0)
            cap = int(spec.get("max") or 0)
        except (TypeError, ValueError):
            _close_loop(w, res, n, f"meta.loop count/max unreadable: {spec!r}")
            continue
        until = _gates.evaluate(store, spec.get("until") or {}, todo_id=n.id, now=now)
        if until.open:
            _close_loop(w, res, n, f"until is met ({until.why})")
            continue
        if count >= cap:
            _close_loop(w, res, n, f"max {cap} iteration(s) reached ({until.why})")
            continue
        body = spec.get("body")
        if not isinstance(body, str) or body not in _templates.BODIES:
            _close_loop(
                w,
                res,
                n,
                f"loop body {body!r} has no minter in "
                f"precis.plans.templates.BODIES (known: {sorted(_templates.BODIES)}); "
                "register it, then reopen this loop",
            )
            continue
        if not w.dry_run:
            try:
                _templates.mint_body(store, n.id, body, iteration=count + 1)
            except BadInput as exc:
                _close_loop(w, res, n, f"loop body {body!r} refused: {exc.cause}")
                continue
        w.stamp_meta(n, {"loop": {**spec, "count": count + 1}})
        res.loops_minted += 1
        res.notes.append(f"loop td{n.id}: minted {body} iteration {count + 1}/{cap}")


# ── (3) phases ─────────────────────────────────────────────────────


def _op_type(n: _Node) -> str:
    op = n.meta.get("op")
    if isinstance(op, dict):
        return str(op.get("type") or DEFAULT_OP)
    if isinstance(op, str) and op.strip():
        return op.strip()
    return DEFAULT_OP


def _step_phases(plan: _Plan, w: _Writer, res: PassResult) -> None:
    for n in sorted(plan.todos(), key=lambda x: (-x.depth, x.id)):
        if _closed(n) or n.status == "paused":
            continue
        kind = node_kind(n.meta)
        kids = plan.child_todos(n)
        if kind in ("phase", "generator"):
            if kind == "generator" and plan.live_job(n):
                continue
            if kids and all(_closed(c) for c in kids):
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(f"{kind} td{n.id} closed: every child is closed")
        elif kind == "task" and not kids and _op_type(n) != DEFAULT_OP:
            jobs = plan.jobs(n)
            if jobs and jobs[-1].status == "succeeded":
                w.set_status(n, "done")
                res.phases_closed += 1
                res.notes.append(
                    f"task td{n.id} closed: {_op_type(n)} job:{jobs[-1].id} succeeded"
                )


# ── (4) dispatch ───────────────────────────────────────────────────


def _dispatchable(plan: _Plan, n: _Node) -> bool:
    """Whether ``n`` is a node the tick runs jobs for (by shape only)."""
    if "ask" in n.meta:
        return False
    kind = node_kind(n.meta)
    kids = plan.child_todos(n)
    if kind == "task" or kind == "generator":
        return not kids
    return kind is None and not kids and n.id not in plan.roots


def _excluded(n: _Node) -> bool:
    for value in n.open_tags:
        for t in _EXCLUSIONS:
            if (t.endswith(":") and value.startswith(t)) or value == t:
                return True
    return False


def _claims(n: _Node) -> list[str]:
    return [v for v in n.open_tags if v.startswith("claimed-by:")]


def _attempt_tag(n: _Node) -> int:
    best = 0
    for value in n.open_tags:
        m = _ATTEMPT_RE.match(value)
        if m:
            best = max(best, int(m.group(1)))
    return best


def _retries(n: _Node) -> int:
    raw = n.meta.get("retries", DEFAULT_RETRIES)
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_RETRIES


def _attempts(plan: _Plan, n: _Node) -> int:
    """Ended jobs since the newest ask on ``n`` (all of them, floored by the
    ``attempt:<n>`` tag, when there was never an ask)."""
    asks = [b for b in plan.blockers.get(n.id, []) if b.is_ask]
    since = max((b.created_at for b in asks), default=None)
    ended = [
        j
        for j in plan.jobs(n)
        if j.status in _JOB_ENDED and (since is None or j.created_at > since)
    ]
    if since is None:
        return max(len(ended), _attempt_tag(n))
    return len(ended)


def _capped(plan: _Plan, n: _Node) -> bool:
    jobs = plan.jobs(n)
    if not jobs or jobs[-1].status not in _JOB_ENDED:
        return False
    return _attempts(plan, n) >= _retries(n)


def _resolve_op(n: _Node) -> tuple[str, dict[str, Any]]:
    """``meta.op`` as ``(job_type, params)``."""
    op = n.meta.get("op")
    if isinstance(op, dict):
        job_type = str(op.get("type") or DEFAULT_OP)
        params = dict(op.get("params") or {})
        if job_type == DEFAULT_OP:
            params.setdefault("todo_id", n.id)
        return job_type, params
    if isinstance(op, str) and op.strip():
        return op.strip(), {"todo_id": n.id}
    return DEFAULT_OP, {"todo_id": n.id}


def _params_problem(schema: dict[str, Any], params: dict[str, Any]) -> str | None:
    """The submit path's static check: required keys, no unknown keys."""
    missing = [k for k in schema.get("required", []) if k not in params]
    if missing:
        return f"params miss {missing}"
    if schema.get("additionalProperties") is False:
        unknown = sorted(set(params) - set(schema.get("properties", {})))
        if unknown:
            return f"params carry unknown keys {unknown}"
    return None


def _pick_executor(compatible: frozenset[str]) -> str:
    for name in _EXECUTOR_ORDER:
        if name in compatible:
            return name
    return sorted(compatible)[0]


def _mint_job(
    store: Any, n: _Node, job_type: str, executor: str, params: dict[str, Any]
) -> int:
    from precis.workers._mint import mint_job_once

    stale = [v for v in n.open_tags if v.startswith("child-failed:")]
    with store.tx() as conn:
        job_id, _reused = mint_job_once(
            store,
            conn=conn,
            job_type=job_type,
            executor=executor,
            title=f"{job_type} td{n.id}: {n.title.splitlines()[0][:70] if n.title else ''}",
            params=params,
            parent_id=n.id,
            idem_key=f"{NAME}:td{n.id}",
            prio=n.prio,
            meta_extra={"plan_root": n.root},
        )
        for value in stale:
            store.remove_tag(n.id, Tag.open(value), conn=conn)
        store.append_event(
            n.id,
            source=NAME,
            event="job-minted",
            payload={"job_id": job_id, "job_type": job_type, "executor": executor},
            conn=conn,
        )
    for value in stale:
        n.open_tags.pop(value, None)
    return job_id


def _step_dispatch(plan: _Plan, w: _Writer, res: PassResult) -> None:
    for n in sorted(
        plan.todos(), key=lambda x: (x.prio if x.prio is not None else 5, x.id)
    ):
        if not _dispatchable(plan, n) or n.status != "open":
            continue
        if _excluded(n) or _claims(n) or plan.live_job(n):
            continue
        if plan.blocked(n) or plan.paused(n) or _capped(plan, n):
            continue
        jobs = plan.jobs(n)
        if jobs and jobs[-1].status == "cancelled":
            res.notes.append(
                f"td{n.id}: newest job:{jobs[-1].id} was cancelled; not redispatched"
            )
            continue
        job_type, params = _resolve_op(n)
        spec = get_job_type(job_type)
        if spec is None:
            res.notes.append(
                f"td{n.id}: meta.op job_type {job_type!r} is not registered"
            )
            continue
        problem = _params_problem(spec.params_schema, params)
        if problem is None and not w.dry_run and spec.validate_submit is not None:
            problem = spec.validate_submit(w.store, gripe_id=None, params=params)
        if problem:
            res.notes.append(f"td{n.id}: {job_type} not dispatched — {problem}")
            continue
        executor = _pick_executor(spec.compatible_executors)
        if w.dry_run:
            res.notes.append(f"td{n.id}: would dispatch {job_type} on {executor}")
        else:
            job_id = _mint_job(w.store, n, job_type, executor, params)
            res.notes.append(
                f"td{n.id}: dispatched {job_type} job:{job_id} on {executor}"
            )
        res.dispatched += 1


# ── (5) hygiene ────────────────────────────────────────────────────


def _failure_tail(store: Any, job_id: int, limit: int) -> str:
    """The tail of the job's newest ``job_event`` chunk (its failure reason)."""
    try:
        chunks = [
            c
            for c in store.chunks.list_chunks_for_ref(job_id)
            if c.chunk_kind == "job_event" and c.text
        ]
    except Exception:  # a missing tail must not stop the ask
        log.warning("plan_dispatch: reading job:%d chunks failed", job_id)
        return ""
    if not chunks:
        return ""
    text = str(max(chunks, key=lambda c: (c.ord, c.id)).text).strip()
    return text[-limit:]


def _mint_ask(store: Any, plan: _Plan, n: _Node, attempts: int) -> int:
    newest = plan.jobs(n)[-1]
    head = (
        f"job:{newest.id} {newest.status} after {attempts} attempt(s) "
        f"(meta.retries={_retries(n)})"
    )
    tail = _failure_tail(store, newest.id, _ASK_CONTEXT_CAP - len(head) - 3)
    context = f"{head}: {tail}" if tail else head
    meta: dict[str, Any] = {
        "ask": {
            "kind": "ruling",
            "options": list(_ASK_OPTIONS),
            "blocks": f"td{n.id}",
            "context": context[:_ASK_CONTEXT_CAP],
        }
    }
    check_ask_meta(meta)
    title = f"td{n.id} hit its retry cap: retry, rewrite the brief, or drop it?"
    body = (
        f"The plan tick stopped dispatching td{n.id} ({n.title.splitlines()[0] if n.title else ''}): "
        f"{head}. Answer with the option you pick and close this todo; closing "
        f"it re-arms td{n.id} (a fresh retry budget). Pick 'won't do' by closing "
        f"td{n.id} STATUS:won't-do."
    )
    with store.tx() as conn:
        ref = store.insert_ref(
            kind="todo",
            slug=None,
            title=title,
            meta=meta,
            parent_id=n.root,
            conn=conn,
        )
        store.add_tag(
            ref.id,
            Tag.closed("STATUS", "open"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
        for value in ("ask-user", "showstopper", f"plan:{n.root}"):
            store.add_tag(ref.id, Tag.open(value), set_by="system", conn=conn)
        store.chunks.insert_chunks(
            ref.id,
            [ChunkInsert(ord=0, text=body, meta={"chunk_kind": TODO_BODY_KIND})],
            conn=conn,
        )
        store.add_link(
            src_ref_id=n.id, dst_ref_id=ref.id, relation="blocked-by", conn=conn
        )
    return int(ref.id)


def _step_hygiene(plan: _Plan, w: _Writer, res: PassResult, now: datetime) -> None:
    for n in plan.todos():
        if _closed(n):
            continue
        stale = [v for v in _claims(n) if now - n.open_tags[v] > CLAIM_STALE]
        if stale and not plan.live_job(n):
            w.remove_tags(n, stale)
            if n.status == "doing":
                w.set_status(n, "open")
            res.released += 1
            res.notes.append(f"td{n.id}: released stale {', '.join(stale)}")
    for n in plan.todos():
        if _closed(n) or not _dispatchable(plan, n) or not _capped(plan, n):
            continue
        if plan.open_asks(n):
            continue
        attempts = _attempts(plan, n)
        if w.dry_run:
            res.notes.append(f"td{n.id}: would file an ask (retry cap {_retries(n)})")
        else:
            ask_id = _mint_ask(w.store, plan, n, attempts)
            res.notes.append(f"td{n.id}: retry cap hit, asked in td{ask_id}")
        res.asks += 1


# ── Pass + dispatch ────────────────────────────────────────────────


def read_only_reason(store: Any, params: dict[str, Any] | None = None) -> str | None:
    """Why this pass must not write, or ``None`` when it may."""
    if os.environ.get("PRECIS_FACTORY", "").strip().lower() == "off":
        return "PRECIS_FACTORY=off"
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT host FROM service_config WHERE service = %s AND prio = 0 LIMIT 1",
            (SERVICE,),
        ).fetchone()
    if row is not None:
        return f"service_config {SERVICE} prio 0 on host {row[0]}"
    if (params or {}).get("dry_run"):
        return "params.dry_run"
    return None


def run_pass(
    store: Any,
    *,
    dry_run: bool = False,
    reason: str | None = None,
    now: datetime | None = None,
) -> PassResult:
    """One dispatcher pass over every plan subtree (module docstring)."""
    now = now or datetime.now(UTC)
    res = PassResult(dry_run=dry_run, reason=reason)
    plan = _load(store)
    res.roots = len(plan.roots)
    w = _Writer(store, dry_run)
    _step_gates(store, plan, w, res, now)
    _step_loops(store, plan, w, res, now)
    _step_phases(plan, w, res)
    _step_dispatch(plan, w, res)
    _step_hygiene(plan, w, res, now)
    return res


def _dispatch(ctx: Any, spec: JobTypeSpec) -> None:
    """``job_inproc`` plugin dispatcher: one pass, summary on the job."""
    del spec
    params = dict(ctx.meta.get("params") or {})
    reason = read_only_reason(ctx.store, params)
    res = run_pass(ctx.store, dry_run=reason is not None, reason=reason)
    ctx.set_meta(plan_dispatch=res.counts())
    ctx.append_chunk("job_summary", res.render())
    log.info("%s", res.summary())


SPEC = JobTypeSpec(
    name=NAME,
    params_schema=PARAMS_SCHEMA,
    compatible_executors=frozenset({"job_inproc"}),
    requires=frozenset(),
    description=DESCRIPTION,
    dispatch=_dispatch,
    # A whole-graph pass: a second run while one is pending adds nothing.
    dedupe_pending=True,
    # It reads live plan state at claim time; nothing enqueued rots.
    queue_ttl_exempt=True,
)


def load() -> JobTypeSpec:
    return SPEC


__all__ = [
    "CLAIM_STALE",
    "DEFAULT_RETRIES",
    "NAME",
    "PARAMS_SCHEMA",
    "SERVICE",
    "SPEC",
    "PassResult",
    "load",
    "read_only_reason",
    "run_pass",
]
