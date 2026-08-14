"""Gripes workbench — write surface for the dev bug tracker (``kind='gripe'``).

Read-only browsing already exists at ``/refs/gripe/{id}`` (the generic
ref browser, ``routes/refs.py``); this tab is that kind's
mutation-capable *workbench* — list/triage/comment/retire — same
relationship ``/flags`` and ``/alerts`` have to their own kinds. Zero
data-layer work: every mutation dispatches an existing verb (``tag`` /
``put`` / ``delete``) through the in-process runtime so validation and
tree guards stay single-sourced with the MCP surface (see ``deps.py``).

* ``GET /gripes`` — live gripes (default: everything but the terminal
  ``STATUS:done`` / ``STATUS:wontfix``), grouped by status,
  workflow-stage then recency ordered. ``?status=wontfix`` /
  ``?status=all`` widen the view.
* ``GET /gripes/{id}`` — body + append-only comment timeline, a
  comment box, and the status controls.
* ``POST /gripes/{id}/status`` — ``tag`` verb, closed ``STATUS:`` axis
  (replace-on-add, no explicit remove — see ``precis.handlers.gripe``).
  htmx re-renders just the status fragment; no-JS redirects back.
* ``POST /gripes/{id}/comment`` — ``put`` verb append (the id-present
  path appends a ``gripe_comment`` chunk; the body chunk itself is
  immutable by design, an audit trail).
* ``POST /gripes/{id}/retire`` — ``delete`` verb (soft-delete — the
  "a fix merged" resolution, distinct from ``STATUS:wontfix`` which
  keeps the gripe on record). The template gates this behind a confirm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis.errors import NotFound
from precis_web.deps import (
    await_dispatch,
    get_store,
    redirect_or_error,
    templates,
)
from precis_web.timefmt import ago as _ago

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(prefix="/gripes", tags=["gripes"])

#: STATUS transitions the detail page offers, in workflow order.
#: Mirrors ``GripeHandler.default_tags_on_create`` + the lifecycle
#: documented in ``precis-gripe-help``. Deliberately no "done" button —
#: a gripe whose fix has landed is *retired* (``delete``), not statused;
#: see the ``retire`` route below.
STATUS_VALUES: tuple[str, ...] = (
    "open",
    "triaged",
    "ready_for_fix",
    "in_review",
    "wontfix",
)

#: Terminal STATUS values the "live" queue (and the nav badge,
#: ``nav.py::_gripes_count``) excludes. ``wontfix`` is the vocabulary's
#: own final state; ``done`` isn't in the gripe lifecycle at all
#: (retire = ``delete``) but the STATUS axis vocabulary is a cross-kind
#: union (``store/types.py::_CLOSED_VOCAB``), so agents drifting into
#: the todo lifecycle do tag gripes ``STATUS:done`` — treat those as
#: resolved, not live.
TERMINAL_VALUES: tuple[str, ...] = ("done", "wontfix")

#: Every STATUS value the list can render, in display order — the
#: transition vocabulary plus the tolerated ``done`` drift, terminal
#: states last. Drives the list's CASE-rank sort and grouping.
_RANKED_VALUES: tuple[str, ...] = (
    "open",
    "triaged",
    "ready_for_fix",
    "in_review",
    "done",
    "wontfix",
)

#: SQL ``CASE`` fragment for the _RANKED_VALUES workflow rank — built
#: once since the vocabulary is fixed, not user input.
_STATUS_RANK_SQL = " ".join(
    f"WHEN '{v}' THEN {i}" for i, v in enumerate(_RANKED_VALUES)
)

#: Badge colour per status, distinct from Alerts' severity palette so
#: the two tabs don't visually blur together.
_STATUS_BADGE: dict[str, str] = {
    "open": "bg-slate-100 text-slate-800 border-slate-300",
    "triaged": "bg-sky-100 text-sky-800 border-sky-300",
    "ready_for_fix": "bg-amber-100 text-amber-800 border-amber-300",
    "in_review": "bg-violet-100 text-violet-800 border-violet-300",
    "done": "bg-emerald-100 text-emerald-800 border-emerald-300",
    "wontfix": "bg-rose-100 text-rose-800 border-rose-300",
}

#: Chunk-kind slugs the gripe handler seeds (migration
#: 0005_gripe_first_class_and_jobs.sql) — mirrored here as plain string
#: literals rather than imported since ``precis.handlers.gripe`` keeps
#: them module-private (``_BODY_KIND`` / ``_COMMENT_KIND``).
_BODY_KIND = "gripe_body"
_COMMENT_KIND = "gripe_comment"


def _status_badge(status: str) -> str:
    return _STATUS_BADGE.get(status, _STATUS_BADGE["open"])


def _title_preview(text: str, *, limit: int = 100) -> str:
    """First non-empty line of the gripe body, truncated.

    A gripe's ``ref.title`` *is* the full filed text verbatim
    (``file_gripe_readonly`` / ``GripeHandler._create`` both store the
    whole complaint as the title — there's no separate title field) —
    often several sentences. A list row wants a short preview, not the
    whole thing.
    """
    first = next((ln.strip() for ln in (text or "").splitlines() if ln.strip()), "")
    if len(first) > limit:
        return first[: limit - 1].rstrip() + "…"
    return first or "(empty)"


def _status_of(tags: Any) -> str:
    """The live ``STATUS:`` tag value off a ``tags_for`` list, default ``open``."""
    for t in tags:
        if getattr(t, "namespace", None) == "STATUS":
            return str(getattr(t, "value", "open"))
    return "open"


def _prio_of(tags: Any) -> str | None:
    """The ``PRIO:`` tag value, if the gripe carries one."""
    for t in tags:
        if getattr(t, "namespace", None) == "PRIO":
            return str(getattr(t, "value", ""))
    return None


def _rows(store: Store, *, status_filter: str) -> list[dict[str, Any]]:
    """Live gripes with their STATUS/PRIO tags, workflow- then recency-sorted.

    ``status_filter``: ``'live'`` (default — everything but the
    TERMINAL_VALUES), ``'wontfix'``, or ``'all'``. Shape mirrors
    ``alerts.py::_rows`` — one ``refs``/``ref_tags``/``tags`` join keyed
    on the STATUS axis, plus a correlated subquery for the optional
    ``PRIO:`` tag (a plain join would duplicate rows for a
    multi-PRIO-tagged gripe).
    """
    clauses = ["r.kind = 'gripe'", "r.deleted_at IS NULL", "t.namespace = 'STATUS'"]
    if status_filter == "wontfix":
        clauses.append("t.value = 'wontfix'")
    elif status_filter != "all":
        terminals = ", ".join(f"'{v}'" for v in TERMINAL_VALUES)
        clauses.append(f"t.value NOT IN ({terminals})")
    sql = f"""
        SELECT r.ref_id,
               r.title,
               r.created_at,
               r.updated_at,
               t.value AS status,
               (SELECT p.value FROM ref_tags prt
                  JOIN tags p ON p.tag_id = prt.tag_id
                 WHERE prt.ref_id = r.ref_id AND p.namespace = 'PRIO'
                 LIMIT 1) AS prio
          FROM refs r
          JOIN ref_tags rt ON rt.ref_id = r.ref_id
          JOIN tags t ON t.tag_id = rt.tag_id
         WHERE {" AND ".join(clauses)}
         ORDER BY CASE t.value {_STATUS_RANK_SQL} ELSE {len(_RANKED_VALUES)} END,
                  r.updated_at DESC
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "ref_id": int(r[0]),
                "title_preview": _title_preview(r[1] or ""),
                "created": _ago(r[2]),
                "updated": _ago(r[3]),
                "status": r[4] or "open",
                "prio": r[5],
            }
        )
    return out


def _group_by_status(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group by STATUS, sections in workflow order (open → … → wontfix)."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        groups.setdefault(r["status"], []).append(r)
    rank = {v: i for i, v in enumerate(_RANKED_VALUES)}
    return [
        {
            "status": status,
            "gripes": items,
            "count": len(items),
            "badge": _status_badge(status),
        }
        for status, items in sorted(
            groups.items(), key=lambda kv: rank.get(kv[0], len(_RANKED_VALUES))
        )
    ]


@router.get("", response_class=HTMLResponse)
async def gripes(request: Request, status: str = "live") -> HTMLResponse:
    """List gripes — default view excludes ``wontfix`` (the "live" queue)."""
    store = get_store(request)
    status = status if status in ("live", "wontfix", "all") else "live"
    rows = _rows(store, status_filter=status)
    return templates.TemplateResponse(
        request,
        "gripes/list.html.j2",
        {
            "active_tab": "gripes",
            "status": status,
            "groups": _group_by_status(rows),
            "total": len(rows),
        },
    )


@router.get("/{id}", response_class=HTMLResponse)
async def detail(request: Request, id: int) -> HTMLResponse:
    """Body + comment timeline + status controls + comment box."""
    store = get_store(request)
    refs = store.fetch_refs_by_ids([id], include_deleted=False)
    ref = refs.get(id)
    if ref is None or ref.kind != "gripe":
        raise NotFound(f"gripe id={id} not found")

    # Body chunk mirrors ref.title verbatim (see _title_preview); fall
    # back to the title itself for pre-chunk-schema gripes that somehow
    # lack a gripe_body row (same defensive fallback GripeHandler's own
    # _render_one uses).
    body_text = ref.title or ""
    comments: list[dict[str, Any]] = []
    for b in store.blocks.list_blocks_for_ref(ref.id):
        chunk_kind = getattr(b, "chunk_kind", None)
        if chunk_kind == _BODY_KIND:
            body_text = b.text or body_text
        elif chunk_kind == _COMMENT_KIND:
            comments.append({"pos": b.pos, "text": b.text or ""})

    tags = store.tags_for(ref.id)
    status = _status_of(tags)

    return templates.TemplateResponse(
        request,
        "gripes/detail.html.j2",
        {
            "active_tab": "gripes",
            "id": ref.id,
            "created": getattr(ref, "created_at", None),
            "updated": getattr(ref, "updated_at", None),
            "set_by": getattr(ref, "set_by", None),
            "body": body_text,
            "comments": comments,
            "status": status,
            "status_values": STATUS_VALUES,
            "status_badge": _status_badge(status),
            "prio": _prio_of(tags),
        },
    )


@router.post("/{id}/status", response_model=None)
async def set_status(request: Request, id: int, value: str = Form(...)) -> Response:
    """Change STATUS via the closed-axis ``tag`` verb (replace-on-add).

    An off-vocabulary value is a no-op redirect (never dispatched) —
    mirrors ``flags.py``'s unknown-flag guard. htmx requests get back
    just the re-rendered status fragment (in-place swap); the no-JS
    fallback redirects to the detail page.
    """
    if value not in STATUS_VALUES:
        return RedirectResponse(url=f"/gripes/{id}", status_code=303)
    args = {"kind": "gripe", "id": id, "add": [f"STATUS:{value}"]}
    if request.headers.get("HX-Request") != "true":
        return await redirect_or_error(
            request,
            "tag",
            args,
            redirect=f"/gripes/{id}",
            error_title="Status error",
        )
    body, is_error = await await_dispatch(request, "tag", args)
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Status error", "detail": body, "status": 400},
            status_code=400,
        )
    return templates.TemplateResponse(
        request,
        "gripes/_gripe_status.html.j2",
        {
            "id": id,
            "status": value,
            "status_values": STATUS_VALUES,
            "status_badge": _status_badge(value),
        },
    )


@router.post("/{id}/comment", response_model=None)
async def comment(request: Request, id: int, text: str = Form(...)) -> Response:
    """Append a comment via ``put`` (id-present on ``put`` routes to append)."""
    if not text.strip():
        return RedirectResponse(url=f"/gripes/{id}", status_code=303)
    return await redirect_or_error(
        request,
        "put",
        {"kind": "gripe", "id": id, "text": text},
        redirect=f"/gripes/{id}",
        error_title="Comment error",
    )


@router.post("/{id}/retire", response_model=None)
async def retire(request: Request, id: int) -> Response:
    """Soft-delete via ``delete`` — the "a fix merged" resolution.

    Distinct from ``STATUS:wontfix`` (kept on record, not deleted); the
    template gates the posting form behind a confirm since this is
    destructive.
    """
    return await redirect_or_error(
        request,
        "delete",
        {"kind": "gripe", "id": id},
        redirect="/gripes",
        error_title="Retire error",
    )
