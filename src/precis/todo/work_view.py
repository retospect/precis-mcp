"""The work view — one read-only hierarchy of what is in flight.

``search(kind='todo', view='work')`` renders quest → the todos that
``serves`` it → their subtrees, one row per todo with STATUS, prio, the
``claimed-by:`` lease, the number of fleet agents on it and its child jobs
folded into one ``jobs=N`` cell (a job is execution detail, not a row);
then the strategic roots that serve no quest ("unassigned"); then the
fleet hosts and when they last reported. A todo that serves the quest
directly while also sitting inside another served root renders once, in
the larger tree. Dark-factory step 1 (td540840,
``docs/backlog/dark-factory.md``): every later step reads this tree.

:func:`work_model` computes the hierarchy once as frozen dataclasses
(:class:`WorkModel`); :func:`render_work` formats it as MCP text and the
``/work`` web page (``precis_web.routes.work``) formats the same model as
HTML, so the two surfaces cannot drift (td540843).

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

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.response import Response
from precis.todo import views, work_gripes
from precis.todo.work_gripes import GripeBlock
from precis.utils import handle_registry
from precis.utils.next_block import render_next_section

if TYPE_CHECKING:
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
    if not isinstance(ts, str) or not ts:
        return "never"
    try:
        then = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return "?"
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

    @property
    def stage(self) -> str:
        """The web page's stage column: the STATUS value (a gripe row's
        stage is its STATUS too)."""
        return self.status


@dataclass(frozen=True)
class RootBlock:
    """One served root's rows; ``hidden`` counts rows past the row cap.
    ``gripes`` are the root's open-gripe rows
    (:mod:`precis.todo.work_gripes`, td540842 / td541980)."""

    root_id: int
    rows: tuple[WorkRow, ...]
    hidden: int
    gripes: GripeBlock = field(default_factory=GripeBlock)


@dataclass(frozen=True)
class QuestBlock:
    ref_id: int
    handle: str
    status: str
    prio: int | None
    title: str
    roots: tuple[RootBlock, ...]


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
) -> WorkRow:
    rid = int(node["id"])
    tags = tags_by_ref.get(rid, [])
    status = statuses.get(rid, "open")
    claimed = next((t for t in tags if t.startswith("claimed-by:")), None)
    waiting = next((t for t in tags if t.startswith("waiting-for:")), None)
    asking = any(t == "ask-user" or t.startswith("ask-user:") for t in tags)
    kind = node.get("kind") or "todo"
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
    )


def _format_row(r: WorkRow) -> str:
    cells = [r.handle, r.icon]
    if r.prio is not None:
        cells.append(f"prio={r.prio}")
    if r.agents:
        cells.append(f"agents={r.agents}")
    if r.jobs:
        cells.append(
            f"jobs={r.jobs}" + (f"({r.jobs_live} live)" if r.jobs_live else "")
        )
    return f"{'  ' * r.depth}{' '.join(cells)} — {r.title}"


#: Job STATUS values that count as live in the ``jobs=N(M live)`` cell.
_LIVE_JOB = frozenset({"queued", "running"})


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
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    job_counts: dict[int, tuple[int, int]] = {}
    for r in rows:
        if r["kind"] == "job":
            n, live = job_counts.get(r["parent_id"], (0, 0))
            is_live = statuses.get(r["id"], "open") in _LIVE_JOB
            job_counts[r["parent_id"]] = (n + 1, live + int(is_live))
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
                )
            )
        for c in by_parent.get(node["id"], []):
            walk(c, depth + 1)

    root_row = next(r for r in rows if r["id"] == root.id)
    walk(root_row, 1)
    return RootBlock(
        root_id=root.id,
        rows=tuple(shown),
        hidden=hidden,
        gripes=work_gripes.root_gripes(store, tags_by_ref.get(root.id, [])),
    )


def work_model(store: Store, *, now: datetime | None = None) -> WorkModel:
    """The hierarchy as data: quests (active first, then by prio) with
    their served roots' rows, the unassigned roots, the fleet hosts."""
    now = now or datetime.now(UTC)
    agents, hosts = _fleet_rows(store)
    quests = [
        q for q in store.list_refs(kind="quest", limit=500) if q.retired_at is None
    ]
    served: set[int] = set()
    blocks: list[tuple[tuple[int, int], QuestBlock]] = []
    for q in quests:
        roots = _todo_servers(store, q.id)
        if not roots:
            continue
        status = _quest_status(store, q.id)
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
        rank = 0 if status == "active" else 1
        quest_block = QuestBlock(
            ref_id=q.id,
            handle=_h("quest", q.id),
            status=status,
            prio=q.prio,
            title=_first_line(q.title),
            roots=tuple(root_blocks),
        )
        blocks.append(((rank, q.prio if q.prio is not None else 5), quest_block))
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
    )


def render_work(store: Store, *, now: datetime | None = None) -> Response:
    """``search(kind='todo', view='work')`` — the hierarchy, read-only."""
    model = work_model(store, now=now)
    n_q = len(model.quests)
    n_a = model.n_agents
    out: list[str] = [
        f"# work — {n_q} quest{'' if n_q == 1 else 's'} with "
        f"work in service · {n_a} live agent{'' if n_a == 1 else 's'}"
    ]
    for q in model.quests:
        prio = f" prio={q.prio}" if q.prio is not None else ""
        out += ["", f"◆ {q.handle} [{q.status}]{prio} — {q.title}"]
        for root in q.roots:
            out.extend(_format_row(r) for r in root.rows)
            if root.hidden:
                out.append(
                    f"    … +{root.hidden} more — "
                    f"get(kind='todo', id={root.root_id}, view='tree')"
                )
            out.extend(work_gripes.render_root_gripes(root.gripes))

    if model.unassigned:
        out += ["", f"unassigned roots ({len(model.unassigned)}) — serve no quest"]
        for u in model.unassigned:
            out.append(f"  {u.handle} [{u.status}] — {u.title}")

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
