"""The work view — one read-only hierarchy of what is in flight.

``search(kind='todo', view='work')`` renders quest → the todos that
``serves`` it → their subtrees, one row per node with STATUS, prio, the
``claimed-by:`` lease and the number of fleet agents on it; then the
strategic roots that serve no quest ("unassigned"); then the fleet hosts
and when they last reported. Dark-factory step 1 (td540840,
``docs/backlog/dark-factory.md``): every later step reads this tree.

:func:`render_tree_ring` is the todo tree as an eye ring — parent,
siblings, children off ``refs.parent_id`` — appended by
:meth:`precis.handlers.todo.TodoHandler.eye` at ``fisheye+1hop`` and up,
because the link neighbourhood walks ``links`` rows only and the tree is
a column (gr541348).

Row grammar (MCP-TOKEN-FIX rulings, 2026-10-30): one header line per
block, bare values, every row starts with its handle so it is a click or
a ``get``. Gripes join the tree in slice td540842; the web page is
td540843.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.response import Response
from precis.todo import views
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


def _row(
    node: dict[str, Any],
    *,
    depth: int,
    statuses: dict[int, str],
    tags_by_ref: dict[int, list[str]],
    prios: dict[int, int | None],
    agents: list[dict[str, Any]],
) -> str:
    rid = int(node["id"])
    tags = tags_by_ref.get(rid, [])
    status = statuses.get(rid, "open")
    claimed = next((t for t in tags if t.startswith("claimed-by:")), None)
    waiting = next((t for t in tags if t.startswith("waiting-for:")), None)
    asking = any(t == "ask-user" or t.startswith("ask-user:") for t in tags)
    icon = views._status_icon(status, claimed=claimed, waiting=waiting, asking=asking)
    n_agents = len(agents_on(agents, rid, tags))
    prio = prios.get(rid)
    cells = [_h(node.get("kind") or "todo", rid), icon]
    if prio is not None:
        cells.append(f"prio={prio}")
    if n_agents:
        cells.append(f"agents={n_agents}")
    indent = "  " * depth
    return f"{indent}{' '.join(cells)} — {_first_line(node['title'])}"


def _render_root(
    store: Store,
    root: Ref,
    *,
    agents: list[dict[str, Any]],
    out: list[str],
) -> None:
    rows = views._fetch_subtree(store, root.id)
    if not rows:
        return
    ids = [r["id"] for r in rows]
    statuses = views._status_of_many(store, ids)
    tags_by_ref = views._all_open_tags(store, ids)
    refs = store.fetch_refs_by_ids(set(ids))
    prios = {i: (refs[i].prio if i in refs else None) for i in ids}
    by_parent: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        by_parent.setdefault(r["parent_id"], []).append(r)
    for children in by_parent.values():
        children.sort(
            key=lambda c: (
                prios.get(c["id"]) if prios.get(c["id"]) is not None else 5,
                c["id"],
            )
        )
    shown = 0
    hidden = 0

    def walk(node: dict[str, Any], depth: int) -> None:
        nonlocal shown, hidden
        if shown >= MAX_ROWS_PER_ROOT:
            hidden += 1
        else:
            out.append(
                _row(
                    node,
                    depth=depth,
                    statuses=statuses,
                    tags_by_ref=tags_by_ref,
                    prios=prios,
                    agents=agents,
                )
            )
            shown += 1
        for c in by_parent.get(node["id"], []):
            walk(c, depth + 1)

    root_row = next(r for r in rows if r["id"] == root.id)
    walk(root_row, 1)
    if hidden:
        out.append(
            f"    … +{hidden} more — get(kind='todo', id={root.id}, view='tree')"
        )


def render_work(store: Store, *, now: datetime | None = None) -> Response:
    """``search(kind='todo', view='work')`` — the hierarchy, read-only."""
    now = now or datetime.now(UTC)
    agents, hosts = _fleet_rows(store)
    quests = [
        q for q in store.list_refs(kind="quest", limit=500) if q.retired_at is None
    ]
    served: set[int] = set()
    blocks: list[tuple[tuple[int, int], list[str]]] = []
    for q in quests:
        roots = _todo_servers(store, q.id)
        if not roots:
            continue
        status = _quest_status(store, q.id)
        prio = f" prio={q.prio}" if q.prio is not None else ""
        lines = [f"◆ {_h('quest', q.id)} [{status}]{prio} — {_first_line(q.title)}"]
        for root in roots:
            served.add(root.id)
            _render_root(store, root, agents=agents, out=lines)
        rank = 0 if status == "active" else 1
        blocks.append(((rank, q.prio if q.prio is not None else 5), lines))
    blocks.sort(key=lambda b: b[0])

    out: list[str] = [
        f"# work — {len(blocks)} quest{'' if len(blocks) == 1 else 's'} with "
        f"work in service · {len(agents)} live agent{'' if len(agents) == 1 else 's'}"
    ]
    for _k, lines in blocks:
        out.append("")
        out.extend(lines)

    unassigned = [
        r
        for r in store.list_refs(kind="todo", limit=2000)
        if r.parent_id is None
        and (r.meta or {}).get("rotation_root")
        and r.id not in served
    ]
    if unassigned:
        out += ["", f"unassigned roots ({len(unassigned)}) — serve no quest"]
        st = views._status_of_many(store, [r.id for r in unassigned])
        for r in sorted(unassigned, key=lambda r: r.id):
            out.append(
                f"  {_h('todo', r.id)} [{st.get(r.id, 'open')}] — {_first_line(r.title)}"
            )

    out += ["", "hosts"]
    if hosts:
        for hm in sorted(hosts, key=lambda m: str(m.get("host"))):
            out.append(
                f"  {hm.get('host')} reported {_age(hm.get('reported_at'), now)}"
            )
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
