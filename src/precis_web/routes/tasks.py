"""Tasks tab — the hierarchical todo tree.

Reads assemble a structured tree directly off the DB (the
"work-off-the-db" principle); writes route through the in-process
runtime (``dispatch``) so the todo handler's level-gradient guard,
depth check, and STATUS vocabulary stay single-sourced.

Routes:

* ``GET  /tasks``                       — dashboard (tree + doable)
* ``POST /tasks/roots``                 — create a strategic root
* ``POST /tasks/{parent_id}/children``  — create a child leaf
* ``POST /tasks/{id}/status``           — set STATUS
* ``POST /tasks/{id}/move``             — reparent (link rel='parent')
* ``POST /tasks/{id}/delete``           — soft-delete

The move route is a thin shell over the reserved virtual relation
``link(kind='todo', rel='parent')`` so the cycle / depth / owner
guards stay single-sourced in the handler — the web layer never
touches ``parent_id`` directly.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from fastapi import APIRouter, Form, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from precis.errors import NotFound
from precis.utils.rake import keyword_summary
from precis.utils.workspace import Workspace
from precis_web.deps import (
    await_dispatch,
    get_store,
    get_web_config,
    redirect_or_error,
    templates,
)

router = APIRouter(prefix="/tasks", tags=["tasks"])

#: STATUS values the UI offers as a dropdown. Mirrors the todo
#: handler's closed vocabulary (``precis.store.types._CLOSED_VOCAB``).
STATUS_CHOICES: tuple[str, ...] = (
    "open",
    "doing",
    "blocked",
    "paused",
    "done",
    "won't-do",
)

_CLOSED = {"done", "won't-do"}

#: STATUS values that count as "actively in flight" for the row rollup.
_ACTIVE_STATUSES = {"open", "doing"}

#: STATUS values that count as "waiting on something" for the rollup.
#: Free tags (``waiting-for:*``, ``ask-user``, ``halt``, …) also push a
#: child into the waiting bucket — see :func:`_classify_row`.
_WAITING_STATUSES = {"blocked", "paused"}


def _gist(title: str | None) -> str:
    """A 3-keyword RAKE summary for a prompt-like (multi-line / long)
    todo body — the compact row label instead of dumping the whole
    planner prompt in the row. Short single-line todos read as their own
    summary, so they get no gist (the first line is shown verbatim)."""
    if not title or ("\n" not in title and len(title) <= 80):
        return ""
    return keyword_summary(title, top_k=3, separator=" · ")


def _row_waits_on_tag(tags: list[str]) -> bool:
    """True when any tag marks the row as waiting on an external event.

    Mirrors the ``_DOABLE_EXCLUSION_TAGS`` shape — anything the doable
    rotation skips counts as "waiting" for the rollup. Keeps the
    rollup and the doable queue's view of "in flight" consistent.
    """
    for t in tags:
        if t == "halt" or t.startswith("halt:"):
            return True
        if t.startswith("waiting-for:"):
            return True
        if t == "ask-user" or t.startswith("ask-user:"):
            return True
        if t.startswith("child-failed:"):
            return True
    return False


def _classify_row(status: str, tags: list[str]) -> str:
    """Return ``'done'`` / ``'waiting'`` / ``'active'`` for the row rollup."""
    if status in _CLOSED:
        return "done"
    if status in _WAITING_STATUSES or _row_waits_on_tag(tags):
        return "waiting"
    return "active"


def _attention_icons(tags: list[str]) -> list[dict[str, str]]:
    """Map free tags to small status icons rendered next to the title.

    Returns ``[{icon, title, href}]`` per signal so the template can
    walk a single loop. Each icon links to the surface where the
    operator handles it (Asks tab for ask-user; the paper viewer for
    a known DOI / arxiv id).
    """
    out: list[dict[str, str]] = []
    has_ask = False
    has_paper = False
    paper_ref = ""
    for t in tags:
        if (t == "ask-user" or t.startswith("ask-user:")) and not has_ask:
            has_ask = True
            out.append(
                {
                    "icon": "🔔",
                    "title": "ask-user — needs your response",
                    "href": "/asks",
                }
            )
        if t.startswith("waiting-for:paper:") and not has_paper:
            has_paper = True
            paper_ref = t.removeprefix("waiting-for:paper:")
            out.append(
                {
                    "icon": "📝",
                    "title": f"waiting on paper {paper_ref}",
                    "href": f"/papers?q={paper_ref}" if paper_ref else "/papers",
                }
            )
    return out


def _resolve_workspace_pdf(
    precis_root: Path | None, meta: dict[str, Any] | None
) -> Path | None:
    """Return the compiled ``main.pdf`` for a todo's workspace, or None.

    The cascade compiles ``latexmk`` at a workspace-root ``STATUS:done``
    (``utils/compile_guard``), producing ``<entrypoint-stem>.pdf`` in the
    workspace dir under ``PRECIS_ROOT``. This resolves that path and
    returns it only when the file actually exists — so the paper icon
    renders exactly when there's something to view.

    Distinct from the corpus-PDF path the papers viewer uses: generated
    manuscripts live under ``PRECIS_ROOT``, not ``PRECIS_CORPUS_DIR``.
    """
    if precis_root is None:
        return None
    workspace = Workspace.from_meta(meta)
    if workspace is None:
        return None
    ws_root = workspace.absolute_root(precis_root)
    pdf_path = ws_root / (Path(workspace.entrypoint).stem + ".pdf")
    # Guard against a malformed workspace escaping PRECIS_ROOT even after
    # Workspace's own relative-path validation.
    try:
        pdf_path.relative_to(precis_root.resolve())
    except ValueError:
        return None
    return pdf_path if pdf_path.is_file() else None


def _tasks_url(
    require: list[str],
    exclude: list[str],
    focus: int | None = None,
    show_jobs: str | None = None,
    tree: int | None = None,
) -> str:
    """Build the ``/tasks`` URL with the current filter + focus applied.

    Each value becomes its own ``require=`` / ``exclude=`` param so a
    filter with multiple tags round-trips through the form / redirect
    cycle without re-joining. ``focus`` rides along as a scalar
    ``focus=<id>`` so a write inside a drilled-down subtree lands back
    on the same subtree. ``show_jobs='all'`` opts the closed-job
    auto-hide off; default behaviour omits the param.
    """
    params: list[tuple[str, str]] = [("require", r) for r in require] + [
        ("exclude", x) for x in exclude
    ]
    if focus is not None:
        params.append(("focus", str(focus)))
    if show_jobs:
        params.append(("show_jobs", show_jobs))
    if tree:
        params.append(("tree", str(tree)))
    qs = urlencode(params)
    return f"/tasks?{qs}" if qs else "/tasks"


#: Allowed depths for the mermaid tree view. The values come from a
#: closed list so the URL param can't request a 100-deep render that
#: takes a second to draw client-side.
_TREE_DEPTHS: tuple[int, ...] = (1, 2, 3, 5, 10)


def _build_mermaid_tree(
    rows: list[dict[str, Any]],
    root_id: int,
    max_depth: int,
) -> str:
    """Return Mermaid ``graph TD`` source for the subtree of ``root_id``.

    Visited todos render as boxed nodes labelled ``#<id> <title>`` and
    coloured by the same active / waiting / done classification the
    rollup chips use. ``max_depth`` caps the walk so a 200-node
    strategic tree doesn't make the browser draw a wall. Jobs are
    excluded — they're attempts, not structure.
    """
    if max_depth < 1:
        return ""
    by_id: dict[int, dict[str, Any]] = {r["id"]: r for r in rows if r["kind"] == "todo"}
    if root_id not in by_id:
        return ""
    children_of: dict[int | None, list[int]] = {}
    for r in by_id.values():
        children_of.setdefault(r["parent_id"], []).append(r["id"])

    nodes: list[int] = []
    edges: list[tuple[int, int]] = []
    truncated: set[int] = set()

    def walk(nid: int, depth: int) -> None:
        nodes.append(nid)
        kids = sorted(children_of.get(nid, []))
        if depth >= max_depth:
            if kids:
                truncated.add(nid)
            return
        for cid in kids:
            if cid in by_id:
                edges.append((nid, cid))
                walk(cid, depth + 1)

    walk(root_id, 0)

    def _label(n: dict[str, Any]) -> str:
        title = (n["title"] or "").split("\n", 1)[0]
        if len(title) > 50:
            title = title[:50].rstrip() + "…"
        # Mermaid breaks on quotes / brackets in labels — strip them.
        return (
            f"#{n['id']} {title}".replace('"', "'").replace("[", "(").replace("]", ")")
        )

    lines: list[str] = ["graph TD"]
    for nid in nodes:
        n = by_id[nid]
        cls = _classify_row(n["status"], n.get("tags", []))
        suffix = " …" if nid in truncated else ""
        lines.append(f'  N{nid}["{_label(n)}{suffix}"]:::{cls}')
    for src, dst in edges:
        lines.append(f"  N{src} --> N{dst}")
    # Highlight the focused root so the eye finds it immediately.
    lines.append(f"  class N{root_id} root")
    lines.append("  classDef active fill:#e0f2fe,stroke:#0369a1,color:#0c4a6e")
    lines.append("  classDef waiting fill:#fef3c7,stroke:#b45309,color:#78350f")
    lines.append(
        "  classDef done fill:#d1fae5,stroke:#047857,color:#064e3b,stroke-dasharray:3 3"
    )
    lines.append("  classDef root stroke-width:3px,font-weight:bold")
    return "\n".join(lines)


def _focus_rows(
    rows: list[dict[str, Any]], focus_id: int | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Drill down to ``focus_id``'s subtree + build the ancestor breadcrumb.

    Returns ``(focused_rows, breadcrumb)``. ``focused_rows`` carries
    the focused node + every descendant (todos and jobs); ``depth`` is
    rebased so the focused node renders at depth 0. ``breadcrumb`` is
    the ancestor chain root → parent (the focused node itself is not
    in the breadcrumb — it's the page heading).

    A missing / non-todo ``focus_id`` is a silent no-op (returns the
    rows unchanged + an empty breadcrumb) so a stale query string
    doesn't crash the page after a delete.
    """
    if focus_id is None:
        return rows, []
    by_id = {r["id"]: r for r in rows}
    if focus_id not in by_id or by_id[focus_id]["kind"] != "todo":
        return rows, []

    children_of: dict[int | None, list[int]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_id"), []).append(r["id"])

    keep: set[int] = set()
    stack = [focus_id]
    while stack:
        nid = stack.pop()
        if nid in keep:
            continue
        keep.add(nid)
        stack.extend(children_of.get(nid, []))

    breadcrumb: list[dict[str, Any]] = []
    cur = by_id[focus_id].get("parent_id")
    while cur is not None and cur in by_id:
        n = by_id[cur]
        first = (n["title"] or "").split("\n", 1)[0]
        if len(first) > 60:
            first = first[:60].rstrip() + "…"
        breadcrumb.append({"id": n["id"], "title": first})
        cur = n.get("parent_id")
    breadcrumb.reverse()

    focus_depth = by_id[focus_id]["depth"]
    focused: list[dict[str, Any]] = []
    for r in rows:
        if r["id"] not in keep:
            continue
        r2 = dict(r)
        r2["depth"] = max(0, r["depth"] - focus_depth)
        focused.append(r2)
    return focused, breadcrumb


#: Pseudo-tag namespaces synthesised from a row's structured columns so
#: the filter form can treat them like free tags. Centralised here so
#: the clickable-badge URLs in the template and the matching logic
#: stay in sync.
_PSEUDO_TAG_COLUMNS: tuple[str, ...] = ("kind", "status", "level")


def _row_filterable_tags(row: dict[str, Any]) -> set[str]:
    """Tags + pseudo-tags ``_filter_rows`` matches against.

    Each pseudo-tag is ``<column>:<value>`` so the input form can offer
    them in the same syntax as a free tag. Empty / missing values are
    skipped (a level-less todo won't expose ``level:``).
    """
    out: set[str] = set(row.get("tags", []))
    for col in _PSEUDO_TAG_COLUMNS:
        v = row.get(col)
        if v:
            out.add(f"{col}:{v}")
    # STATUS:<v> is also recognised in upper-case form because that's
    # how the closed tag literally lives in the DB (``STATUS:done``).
    if row.get("status"):
        out.add(f"STATUS:{row['status']}")
    return out


#: Job STATUS values considered "closed attempts" — failed, succeeded,
#: done, won't-do. By default the dashboard hides job rows in these
#: states so a flurry of retries doesn't drown the tree; the operator
#: opts in with ``show_jobs=all``.
_CLOSED_JOB_STATUSES: frozenset[str] = frozenset(
    {"failed", "succeeded", "done", "won't-do"}
)


def _hide_inactive_jobs(
    rows: list[dict[str, Any]], *, show_all: bool
) -> list[dict[str, Any]]:
    """Drop job rows in a terminal state unless the operator opts in.

    Plan_tick / fix_gripe / etc. retries pile up under their parent
    todo as kind='job' children with ``STATUS:failed`` (and an
    expired lease). Once a job is over, it's a history entry — visible
    via the per-todo History panel — not progress state. Hiding closed
    jobs keeps the tree about *what's in flight* rather than a wall
    of post-mortems. ``show_all=True`` puts them back.
    """
    if show_all:
        return rows
    return [
        r for r in rows if r["kind"] != "job" or r["status"] not in _CLOSED_JOB_STATUSES
    ]


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    require: list[str],
    exclude: list[str],
) -> list[dict[str, Any]]:
    """Keep rows matching the require/exclude tag sets, plus ancestors.

    Match semantics (AND on both lists): a todo matches when **every**
    ``require`` tag is on it and **no** ``exclude`` tag is. The match
    set is the union of the todo's free tags + its structured
    columns rendered as pseudo-tags (``status:<v>`` /
    ``STATUS:<v>`` / ``level:<v>`` / ``kind:<v>``), so the operator
    can filter by any visible badge using the same syntax as a tag.

    Tree context is preserved — every matched todo also pulls its
    ancestor chain into the kept set so a deep match doesn't render as
    a context-less orphan. Job rows ride along with their parent todo.
    """
    if not require and not exclude:
        return rows
    req_set = set(require)
    exc_set = set(exclude)

    matching: set[int] = set()
    for r in rows:
        if r["kind"] != "todo":
            continue
        row_tags = _row_filterable_tags(r)
        if req_set and not req_set.issubset(row_tags):
            continue
        if exc_set & row_tags:
            continue
        matching.add(r["id"])

    keep = set(matching)
    by_id = {r["id"]: r for r in rows if r["kind"] == "todo"}
    for rid in list(matching):
        cur = by_id[rid].get("parent_id")
        while cur is not None and cur in by_id and cur not in keep:
            keep.add(cur)
            cur = by_id[cur].get("parent_id")

    return [
        r
        for r in rows
        if (r["kind"] == "todo" and r["id"] in keep)
        or (r["kind"] == "job" and r["parent_id"] in keep)
    ]


def _split_tags(raw: str) -> list[str]:
    """Split a comma/space separated tag string into a clean list."""
    if not raw:
        return []
    parts = [p.strip() for chunk in raw.split(",") for p in chunk.split()]
    return [p for p in parts if p]


def _load_freeform_tags(store: Any, ref_ids: list[int]) -> dict[int, list[str]]:
    """Return removable free tags per ref (canonical strings).

    Excludes ``STATUS:`` (dedicated dropdown) and ``level:`` (dedicated
    badge) since those have their own controls. ``OPEN`` namespace tags
    store the full ``key:value`` in ``value``; closed namespaces render
    as ``NAMESPACE:value``.
    """
    out: dict[int, list[str]] = {rid: [] for rid in ref_ids}
    if not ref_ids:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT rt.ref_id, t.namespace, t.value FROM ref_tags rt "
            "JOIN tags t ON t.tag_id = rt.tag_id WHERE rt.ref_id = ANY(%s)",
            (ref_ids,),
        ).fetchall()
    for ref_id, namespace, value in rows:
        rid = int(ref_id)
        tag_str = str(value) if namespace == "OPEN" else f"{namespace}:{value}"
        if tag_str.startswith(("STATUS:", "level:")):
            continue
        out[rid].append(tag_str)
    for tags in out.values():
        tags.sort()
    return out


def _load_tags(store: Any, ref_ids: list[int]) -> dict[int, dict[str, str]]:
    """Bulk-fetch STATUS + ``level:`` for each todo in one query.

    Returns ``{ref_id: {'status': ..., 'level': ...}}`` with sensible
    defaults (``status='open'``, ``level=''``).
    """
    out: dict[int, dict[str, str]] = {
        rid: {"status": "open", "level": ""} for rid in ref_ids
    }
    if not ref_ids:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT rt.ref_id, t.namespace, t.value
              FROM ref_tags rt JOIN tags t ON t.tag_id = rt.tag_id
             WHERE rt.ref_id = ANY(%s)
               AND (t.namespace = 'STATUS'
                    OR (t.namespace = 'OPEN' AND t.value LIKE 'level:%%'))
            """,
            (ref_ids,),
        ).fetchall()
    for ref_id, namespace, value in rows:
        rid = int(ref_id)
        if namespace == "STATUS":
            out[rid]["status"] = str(value)
        elif str(value).startswith("level:"):
            out[rid]["level"] = str(value).split(":", 1)[1]
    return out


def _child_jobs(store: Any, todo_ids: list[int]) -> list[dict[str, Any]]:
    """Return ``kind='job'`` children of the given todos.

    Jobs are where processing actually happens — a worker claims a
    job ref and writes ``meta.lease_until`` for the run window. We
    surface them under their parent todo so the lock/lease badges have
    a node to attach to. Degrades to ``[]`` cleanly when the query
    returns nothing (and under the test fake's empty cursor).
    """
    if not todo_ids:
        return []
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, parent_id, title, meta->>'lease_until' "
            "FROM refs WHERE kind = 'job' AND deleted_at IS NULL "
            "AND parent_id = ANY(%s)",
            (todo_ids,),
        ).fetchall()
    return [
        {
            "id": int(r[0]),
            "parent_id": int(r[1]) if r[1] is not None else None,
            "title": r[2],
            "lease_until": r[3],
        }
        for r in rows
    ]


def _job_notes(store: Any, job_ids: list[int]) -> dict[int, dict[str, Any]]:
    """Bulk-fetch the ``job_result`` / ``job_event`` / ``job_summary`` chunks.

    Three signals per job:

    * ``job_result`` — the structured per-tick audit (the parsed
      tick-conclusion verdict + the subtasks/citations/findings counts).
      The most legible single chunk; surfaced first.
    * ``job_event`` — why a job failed (``_record_failure`` reason).
    * ``job_summary`` — the captured stdout.

    The tree itself only shows a bare status badge; surfacing these
    turns "#6689 failed" / "#6689 succeeded" into a legible account on
    hover + in the history panel.

    Returns ``{job_id: {'result': str, 'events': [str, ...], 'summary': str}}``.
    Degrades to empty dicts under the test fake (no chunks table).
    """
    out: dict[int, dict[str, Any]] = {
        jid: {"result": "", "events": [], "summary": ""} for jid in job_ids
    }
    if not job_ids:
        return out
    try:
        with store.pool.connection() as conn:
            rows = conn.execute(
                "SELECT ref_id, meta->>'chunk_kind' AS kind, text "
                "FROM chunks "
                "WHERE ref_id = ANY(%s) "
                "AND meta->>'chunk_kind' IN "
                "  ('job_result', 'job_event', 'job_summary') "
                "ORDER BY ref_id, ord",
                (job_ids,),
            ).fetchall()
    except Exception:  # pragma: no cover - defensive (fake cursor)
        return out
    summaries: dict[int, list[str]] = {jid: [] for jid in job_ids}
    results: dict[int, list[str]] = {jid: [] for jid in job_ids}
    for ref_id, kind, text in rows:
        rid = int(ref_id)
        if rid not in out:
            continue
        if kind == "job_result":
            results[rid].append(text)
        elif kind == "job_event":
            out[rid]["events"].append(text)
        elif kind == "job_summary":
            summaries[rid].append(text)
    for jid in job_ids:
        out[jid]["result"] = "\n".join(results[jid])
        out[jid]["summary"] = "\n".join(summaries[jid])
    return out


def _lease_active(lease_until: str | None) -> bool:
    """True when ``lease_until`` parses and lies in the future."""
    if not lease_until:
        return False
    try:
        ts = datetime.fromisoformat(lease_until)
    except ValueError:
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts > datetime.now(UTC)


def _build_rows(store: Any, *, precis_root: Path | None = None) -> list[dict[str, Any]]:
    """Flatten the todo tree (with child jobs) into DFS-ordered rows.

    Each row carries ``id, kind, title, status, level, depth, done,
    total`` plus processing signals ``locked`` (a live ``pg_locks``
    row lock) and ``lease_until`` / ``lease_active`` (the durable
    marker a worker writes). Roots are ``parent_id IS NULL``; orphans
    (parent missing) surface as roots so nothing silently disappears.

    ``precis_root`` (default: ``$PRECIS_ROOT``) is where workspace PDFs
    live; when a todo's workspace has a compiled PDF on disk the row
    gets a 📄 attention icon linking to ``/tasks/{id}/pdf``.
    """
    if precis_root is None:
        raw = os.environ.get("PRECIS_ROOT")
        precis_root = Path(raw).expanduser() if raw else None
    todos = store.list_refs(kind="todo", limit=5000)
    by_id = {r.id: r for r in todos}
    todo_ids = [r.id for r in todos]
    jobs = _child_jobs(store, todo_ids)

    all_ids = todo_ids + [j["id"] for j in jobs]
    tags = _load_tags(store, all_ids)
    freeform = _load_freeform_tags(store, todo_ids)
    locked = store.locked_ref_ids(all_ids)
    # Failure reason / summary chunks for each job, so a job row's
    # bare status badge gets a hover tooltip explaining what happened.
    job_notes = _job_notes(store, [j["id"] for j in jobs])

    # Normalise todos + jobs into a single node dict so the walk is
    # kind-agnostic.
    nodes: dict[int, dict[str, Any]] = {}
    for r in todos:
        nodes[r.id] = {
            "id": r.id,
            "kind": "todo",
            "title": r.title,
            "parent_id": r.parent_id if r.parent_id in by_id else None,
            "lease_until": None,
            "meta": getattr(r, "meta", None),
        }
    for j in jobs:
        # A job whose parent todo vanished is dropped (no orphan jobs).
        if j["parent_id"] not in by_id:
            continue
        nodes[j["id"]] = {
            "id": j["id"],
            "kind": "job",
            "title": j["title"],
            "parent_id": j["parent_id"],
            "lease_until": j["lease_until"],
        }

    children: dict[int | None, list[dict[str, Any]]] = {}
    for n in nodes.values():
        children.setdefault(n["parent_id"], []).append(n)
    for kids in children.values():
        kids.sort(key=lambda n: n["id"])

    rows: list[dict[str, Any]] = []

    # Memoise PDF resolution per workspace path: every todo in a project
    # subtree inherits the same ``meta.workspace``, so resolving once per
    # distinct path keeps this to one filesystem stat per project.
    pdf_by_ws: dict[str, Path | None] = {}

    def _todo_pdf_memo(meta: dict[str, Any] | None) -> Path | None:
        ws = Workspace.from_meta(meta)
        if ws is None:
            return None
        if ws.path not in pdf_by_ws:
            pdf_by_ws[ws.path] = _resolve_workspace_pdf(precis_root, meta)
        return pdf_by_ws[ws.path]

    def walk(node: dict[str, Any], depth: int) -> None:
        kids = children.get(node["id"], [])
        # Three-bucket rollup over direct todo children (jobs aren't
        # progress units — they're attempts at the parent's work).
        rollup_done = 0
        rollup_waiting = 0
        rollup_active = 0
        for k in kids:
            if k["kind"] != "todo":
                continue
            cls = _classify_row(tags[k["id"]]["status"], freeform.get(k["id"], []))
            if cls == "done":
                rollup_done += 1
            elif cls == "waiting":
                rollup_waiting += 1
            else:
                rollup_active += 1
        lease_until = node["lease_until"]
        # Job rows carry a hover tooltip built from their failure /
        # summary chunks; todos have none (they get the history panel).
        note = ""
        if node["kind"] == "job":
            jn = job_notes.get(node["id"], {})
            # Order: structured result (verdict + counts) first, then
            # any failure events, then the raw stdout summary.
            parts = []
            if jn.get("result"):
                parts.append(jn["result"])
            parts.extend(jn.get("events", []))
            if jn.get("summary"):
                parts.append(jn["summary"])
            note = "\n".join(p for p in parts if p).strip()
        row_tags = freeform.get(node["id"], []) if node["kind"] == "todo" else []
        attention_icons = _attention_icons(row_tags)
        # Compiled-PDF affordance: when this todo's workspace has a PDF on
        # disk, link to it. Memoised per workspace path so a project
        # subtree of N todos costs one stat, not N.
        if node["kind"] == "todo":
            pdf = _todo_pdf_memo(node.get("meta"))
            if pdf is not None:
                attention_icons = [
                    *attention_icons,
                    {
                        "icon": "📄",
                        "title": "view compiled PDF",
                        "href": f"/tasks/{node['id']}/pdf",
                    },
                ]
        rollup_total = rollup_done + rollup_waiting + rollup_active
        rows.append(
            {
                "id": node["id"],
                "kind": node["kind"],
                "parent_id": node["parent_id"],
                "title": node["title"],
                "gist": _gist(node["title"]),
                "status": tags[node["id"]]["status"],
                "level": tags[node["id"]]["level"],
                "depth": depth,
                "rollup": {
                    "active": rollup_active,
                    "waiting": rollup_waiting,
                    "done": rollup_done,
                    "total": rollup_total,
                },
                "is_leaf": not kids,
                "locked": node["id"] in locked,
                "lease_until": lease_until,
                "lease_active": _lease_active(lease_until),
                "tags": row_tags,
                "attention_icons": attention_icons,
                "note": note,
            }
        )
        for k in kids:
            walk(k, depth + 1)

    for root in children.get(None, []):
        walk(root, 0)
    return rows


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    require: list[str] = Query(default=[]),
    exclude: list[str] = Query(default=[]),
    focus: int | None = Query(default=None),
    show_jobs: str = Query(default="active"),
    tree: int | None = Query(default=None),
) -> HTMLResponse:
    """Strategic tree + doable queue.

    Four orthogonal narrowing controls:

    * ``focus=<id>`` — drill down to one node's subtree (with an
      ancestor breadcrumb back to the root).
    * ``require`` / ``exclude`` repeating params — tag filter, AND
      within each list, evaluated *after* focus.
    * ``show_jobs=active|all`` — default ``active`` hides job rows
      in a terminal state (failed / succeeded / done / won't-do).
      ``all`` shows every attempt — the same set the per-todo History
      panel already exposes.
    * They compose: focus narrows the universe, the job hide trims
      attempt detritus, and the tag filter narrows what's left.
    """
    store = get_store(request)
    rows = _build_rows(store)
    require = [r for r in require if r]
    exclude = [x for x in exclude if x]
    rows = _hide_inactive_jobs(rows, show_all=(show_jobs == "all"))
    focused_rows, breadcrumb = _focus_rows(rows, focus)
    filtered = _filter_rows(focused_rows, require=require, exclude=exclude)
    doable_body, _ = await await_dispatch(
        request, "search", {"kind": "todo", "view": "doable", "page_size": 20}
    )
    focus_row: dict[str, Any] | None = None
    if focus is not None:
        # The focused node itself is the heading; everything below it
        # in ``focused_rows`` is the subtree. Pull it out so the
        # template can render the heading separately.
        for r in focused_rows:
            if r["id"] == focus:
                focus_row = r
                break

    # Mermaid tree: built only when the operator explicitly opted in
    # (``?tree=N``) and we have a focus. Validate against the closed
    # depth list so a hand-crafted URL can't request a 100-deep render.
    tree_depth: int | None = None
    tree_diagram: str = ""
    if tree and focus is not None:
        if tree in _TREE_DEPTHS:
            tree_depth = tree
        else:
            # Snap to the nearest allowed depth so a stale link
            # (``?tree=4`` after we tightened the list) still renders.
            tree_depth = min(_TREE_DEPTHS, key=lambda d: abs(d - tree))
        tree_diagram = _build_mermaid_tree(focused_rows, focus, tree_depth)
    return templates.TemplateResponse(
        request,
        "tasks/dashboard.html.j2",
        {
            "active_tab": "tasks",
            "rows": filtered,
            "total_rows": len(rows),
            "filtered_rows": len(filtered),
            "filter_active": bool(require or exclude),
            "require_tags": require,
            "exclude_tags": exclude,
            "focus_id": focus,
            "focus_row": focus_row,
            "breadcrumb": breadcrumb,
            "show_jobs": show_jobs,
            "tree_depth": tree_depth,
            "tree_diagram": tree_diagram,
            "tree_depths": _TREE_DEPTHS,
            "doable_body": doable_body,
            "status_choices": STATUS_CHOICES,
        },
    )


@router.get("/{ref_id}/pdf")
async def task_pdf(request: Request, ref_id: int) -> FileResponse:
    """Stream a todo's compiled workspace PDF inline (paper-viewer style).

    Mirrors ``/papers/{id}/pdf`` but resolves under ``PRECIS_ROOT`` (the
    cascade's workspace store) rather than the corpus dir, since a
    generated manuscript isn't an ingested paper. 404s with the path it
    looked at when no PDF exists yet (e.g. the cascade hasn't reached the
    compile step).
    """
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id], include_deleted=False)
    ref = refs.get(ref_id)
    if ref is None or ref.kind != "todo":
        raise NotFound(f"todo id={ref_id} not found")
    cfg = get_web_config(request)
    path = _resolve_workspace_pdf(cfg.precis_root, getattr(ref, "meta", None))
    if path is None:
        raise NotFound(
            f"no compiled PDF for todo id={ref_id}. Either it has no "
            "workspace, PRECIS_ROOT is unset for the web process, or the "
            "cascade hasn't compiled main.pdf yet (the PDF is produced at "
            "the workspace-root STATUS:done compile step)."
        )
    return FileResponse(
        path,
        media_type="application/pdf",
        headers={"Content-Disposition": f'inline; filename="todo-{ref_id}.pdf"'},
    )


@router.get("/{ref_id}/children-popup", response_class=HTMLResponse)
async def children_popup(
    request: Request,
    ref_id: int,
    depth: int = 0,
) -> HTMLResponse:
    """Return the immediate ``kind='todo'`` children of ``ref_id``.

    Rendered as an HTML fragment for htmx-driven drill-down popups
    triggered from rollup chips on the Tasks dashboard. Each child row
    carries its own chip pointing back at this same route (depth + 1)
    so the popup chain is purely template-recursive.

    ``depth`` is the chain position (0 = chip on the dashboard row, 1
    = inside the first popup, …). At ``depth >= _POPUP_MAX_DEPTH`` the
    fragment renders a "drill further in the Mermaid view" pointer
    instead of yet another nested list — keeps the popover stack
    bounded.
    """
    store = get_store(request)
    rows = _build_rows(store)
    children_of: dict[int | None, list[dict[str, Any]]] = {}
    for r in rows:
        children_of.setdefault(r.get("parent_id"), []).append(r)
    direct = [c for c in children_of.get(ref_id, []) if c["kind"] == "todo"]
    return templates.TemplateResponse(
        request,
        "tasks/_children_popup.html.j2",
        {
            "parent_id": ref_id,
            "depth": depth,
            "children": direct,
            "max_depth": _POPUP_MAX_DEPTH,
        },
    )


#: Hard cap on the children-popup chain. Past this the popup links to
#: the Mermaid tree view instead of nesting further. Picked empirically
#: — 4 levels covers the strategic-tactical-subtask depth we actually
#: use, and visually the cascading menu starts to feel cramped past 5.
_POPUP_MAX_DEPTH = 4


@router.post("/roots")
async def create_root(
    request: Request,
    text: str = Form(...),
    level: str = Form("strategic"),
    require: list[str] = Form(default=[]),
    exclude: list[str] = Form(default=[]),
    focus: int | None = Form(default=None),
    show_jobs: str = Form(default="active"),
) -> Response:
    """Create a top-level (strategic) root."""
    tags = [f"level:{level}"] if level else None
    return await redirect_or_error(
        request,
        "put",
        {"kind": "todo", "text": text, "tags": tags},
        redirect=_tasks_url(
            require, exclude, focus, show_jobs if show_jobs != "active" else None
        ),
    )


@router.post("/{parent_id}/children")
async def create_child(
    request: Request,
    parent_id: int,
    text: str = Form(...),
    level: str = Form("subtask"),
    require: list[str] = Form(default=[]),
    exclude: list[str] = Form(default=[]),
    focus: int | None = Form(default=None),
    show_jobs: str = Form(default="active"),
) -> Response:
    """Create a child under ``parent_id``."""
    tags = [f"level:{level}"] if level else None
    return await redirect_or_error(
        request,
        "put",
        {"kind": "todo", "text": text, "parent_id": parent_id, "tags": tags},
        redirect=_tasks_url(
            require, exclude, focus, show_jobs if show_jobs != "active" else None
        ),
    )


@router.post("/{ref_id}/status")
async def set_status(
    request: Request,
    ref_id: int,
    status: str = Form(...),
    require: list[str] = Form(default=[]),
    exclude: list[str] = Form(default=[]),
    focus: int | None = Form(default=None),
    show_jobs: str = Form(default="active"),
) -> Response:
    """Set a todo's STATUS via the tag verb (closed-prefix replace)."""
    return await redirect_or_error(
        request,
        "tag",
        {"kind": "todo", "id": ref_id, "add": [f"STATUS:{status}"]},
        redirect=_tasks_url(
            require, exclude, focus, show_jobs if show_jobs != "active" else None
        ),
    )


@router.post("/{ref_id}/move")
async def move_task(
    request: Request,
    ref_id: int,
    new_parent_id: str = Form(""),
    require: list[str] = Form(default=[]),
    exclude: list[str] = Form(default=[]),
    focus: int | None = Form(default=None),
    show_jobs: str = Form(default="active"),
) -> Response:
    """Reparent a todo via the reserved ``link(rel='parent')`` surface.

    An empty / blank ``new_parent_id`` detaches the todo to a root
    (``mode='remove'``); otherwise the todo moves under that parent
    (``mode='add'``). All tree guards (cycle / depth / owner) fire in
    the handler — a rejected move returns the handler's BadInput.
    """
    redirect = _tasks_url(
        require, exclude, focus, show_jobs if show_jobs != "active" else None
    )
    npid = new_parent_id.strip()
    if npid:
        return await redirect_or_error(
            request,
            "link",
            {
                "kind": "todo",
                "id": ref_id,
                "target": f"todo:{int(npid)}",
                "rel": "parent",
                "mode": "add",
            },
            redirect=redirect,
        )
    return await redirect_or_error(
        request,
        "link",
        {"kind": "todo", "id": ref_id, "rel": "parent", "mode": "remove"},
        redirect=redirect,
    )


@router.post("/{ref_id}/edit")
async def edit_text(
    request: Request,
    ref_id: int,
    text: str = Form(""),
    require: list[str] = Form(default=[]),
    exclude: list[str] = Form(default=[]),
    focus: int | None = Form(default=None),
    show_jobs: str = Form(default="active"),
) -> Response:
    """Rewrite a todo's text in place via the ``edit`` verb.

    Same id, parent, links, and tags survive; the old body is audited
    in ``ref_events``. Multiline text is preserved verbatim. An empty /
    whitespace ``text`` is a no-op. Owner-only on strategic / tactical
    nodes — the web process runs as owner, so the guard passes here.
    """
    redirect = _tasks_url(
        require, exclude, focus, show_jobs if show_jobs != "active" else None
    )
    if not text.strip():
        return RedirectResponse(url=redirect, status_code=303)
    return await redirect_or_error(
        request,
        "edit",
        {"kind": "todo", "id": ref_id, "mode": "replace", "text": text.strip()},
        redirect=redirect,
    )


@router.post("/{ref_id}/tags")
async def edit_tags(
    request: Request,
    ref_id: int,
    add: str = Form(""),
    remove: str = Form(""),
    require: list[str] = Form(default=[]),
    exclude: list[str] = Form(default=[]),
    focus: int | None = Form(default=None),
    show_jobs: str = Form(default="active"),
) -> Response:
    """Add and/or remove free tags on a todo via the ``tag`` verb.

    ``add`` is a comma/space separated tag string the operator typed;
    ``remove`` is a single tag (from a chip's remove button). Both flow
    through the handler so tag-vocabulary validation stays single-
    sourced — an invalid tag now renders the handler's BadInput inline
    instead of silently redirecting (the operator was typing tags that
    failed validation with no feedback).
    """
    add_list = _split_tags(add)
    remove_list = _split_tags(remove)
    args: dict[str, Any] = {"kind": "todo", "id": ref_id}
    if add_list:
        args["add"] = add_list
    if remove_list:
        args["remove"] = remove_list
    redirect = _tasks_url(
        require, exclude, focus, show_jobs if show_jobs != "active" else None
    )
    if not add_list and not remove_list:
        return RedirectResponse(url=redirect, status_code=303)
    return await redirect_or_error(request, "tag", args, redirect=redirect)


@router.get("/{ref_id}/history", response_class=HTMLResponse)
async def history(request: Request, ref_id: int) -> HTMLResponse:
    """Lazy (htmx) history fragment for one todo.

    Two strands the tree itself doesn't surface inline:

    * **Attempts** — every child ``kind='job'`` (one execution attempt
      each), newest first, with its STATUS so succeeded/failed/running
      runs are all legible in one place.
    * **Event log** — ``ref_events`` for this todo (e.g. ``status:done``
      with its timestamp + source).

    Rendered as a bare fragment so htmx can swap it into the row's
    expander without a full page reload.
    """
    store = get_store(request)
    jobs = _child_jobs(store, [ref_id])
    job_ids = [j["id"] for j in jobs]
    job_status = _load_tags(store, job_ids)
    notes = _job_notes(store, job_ids)
    attempts = [
        {
            "id": j["id"],
            "title": j["title"],
            "status": job_status.get(j["id"], {}).get("status", "open"),
            "result": notes.get(j["id"], {}).get("result", ""),
            "events": notes.get(j["id"], {}).get("events", []),
            "summary": notes.get(j["id"], {}).get("summary", ""),
        }
        for j in jobs
    ]
    attempts.sort(key=lambda a: a["id"], reverse=True)

    events: list[dict[str, Any]] = []
    for e in store.events_for(ref_id, limit=50):
        events.append(
            {
                "ts": e.ts.strftime("%Y-%m-%d %H:%M") if e.ts else "",
                "event": e.event,
                "source": e.source,
            }
        )
    return templates.TemplateResponse(
        request,
        "tasks/_history.html.j2",
        {"ref_id": ref_id, "attempts": attempts, "events": events},
    )


@router.post("/{ref_id}/delete")
async def delete_task(
    request: Request,
    ref_id: int,
    require: list[str] = Form(default=[]),
    exclude: list[str] = Form(default=[]),
    focus: int | None = Form(default=None),
    show_jobs: str = Form(default="active"),
) -> RedirectResponse:
    """Soft-delete a todo (children re-parent to NULL via FK)."""
    await await_dispatch(request, "delete", {"kind": "todo", "id": ref_id})
    return RedirectResponse(
        url=_tasks_url(
            require, exclude, focus, show_jobs if show_jobs != "active" else None
        ),
        status_code=303,
    )
