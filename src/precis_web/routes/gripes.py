"""Gripes workbench — write surface for the dev bug tracker (``kind='gripe'``).

Read-only browsing already exists at ``/refs/gripe/{id}`` (the generic
ref browser, ``routes/refs.py``); this tab is that kind's
mutation-capable *workbench* — list/triage/comment/retire — same
relationship ``/flags`` and ``/alerts`` have to their own kinds. Zero
data-layer work: every mutation dispatches an existing verb (``tag`` /
``put`` / ``delete``) through the in-process runtime so validation and
tree guards stay single-sourced with the MCP surface (see ``deps.py``).

* ``GET /gripes`` — open gripes inline plus one status chip per other
  status (gr520298). The load is a single ``COUNT … GROUP BY`` status
  plus the open rows; every other status's rows lazy-load on the chip's
  first expand. Retired gripes (merged/duplicate/junk soft-deletes)
  bucket under a ``retired`` pseudo-status chip; carries the filing form.
* ``GET /gripes/rows?status=<s>`` — the chips' shared lazy-rows
  fragment: one status's rows, on the same ``_rows`` query the list
  used to run for every status at page load.
* ``POST /gripes`` — ``put`` verb create. The filed text gets an
  attribution line naming the signed-in user appended to it (see
  ``_attribute``): a gripe's stored text *is* its whole record, and
  the human who hit friction is the one worth going back to.
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
from precis_web.auth import current_user
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
#: a close-as-done should name the fixing commit in a closing comment
#: (the agents' flow), which a bare button click can't supply; the
#: ``retire`` route below handles junk/test artifacts via ``delete``.
STATUS_VALUES: tuple[str, ...] = (
    "open",
    "triaged",
    "ready_for_fix",
    "in_review",
    "verify",
    "wontfix",
)

#: Terminal STATUS values the "live" queue (and the nav badge,
#: ``nav.py::_gripes_count``) excludes. ``done`` = fixed/resolved
#: (the documented terminal state since gr207238 blessed the de facto
#: practice — see ``precis-gripe-help``'s lifecycle table); ``wontfix``
#: = decided not to act. ``delete`` retires junk/test artifacts only.
TERMINAL_VALUES: tuple[str, ...] = ("done", "wontfix")

#: Every STATUS value the list can render, in display order — the
#: transition vocabulary plus the ``done`` terminal state, terminal
#: states last. Drives the list's CASE-rank sort and grouping.
_RANKED_VALUES: tuple[str, ...] = (
    "open",
    "triaged",
    "ready_for_fix",
    "in_review",
    "verify",
    "done",
    "wontfix",
)

#: SQL ``CASE`` fragment for the _RANKED_VALUES workflow rank — built
#: once since the vocabulary is fixed, not user input.
_STATUS_RANK_SQL = " ".join(
    f"WHEN '{v}' THEN {i}" for i, v in enumerate(_RANKED_VALUES)
)

#: Badge colour per status, distinct from Alerts' severity palette so
#: the two tabs don't visually blur together. ``retired`` is the
#: pseudo-status the grouped count gives soft-deleted rows (merged/
#: duplicate/junk) — greyed further than open since those are gone.
_STATUS_BADGE: dict[str, str] = {
    "open": "bg-surface-muted text-fg border-line-strong",
    "triaged": "bg-info-muted text-info-deep border-info-strong",
    "ready_for_fix": "bg-warning-muted text-warning-deep border-warning-strong",
    "in_review": "bg-accent-muted text-accent-deep border-accent-strong",
    "verify": "bg-teal-muted text-teal-deep border-teal-strong",
    "done": "bg-success-muted text-success-deep border-success-strong",
    "wontfix": "bg-danger-muted text-danger-deep border-danger-strong",
    "retired": "bg-surface-muted text-fg-faint border-line",
}

#: Chunk-kind slugs the gripe handler seeds (migration
#: 0005_gripe_first_class_and_jobs.sql) — mirrored here as plain string
#: literals rather than imported since ``precis.handlers.gripe`` keeps
#: them module-private (``_BODY_KIND`` / ``_COMMENT_KIND``).
_BODY_KIND = "gripe_body"
_COMMENT_KIND = "gripe_comment"


#: Characters of ``ref.title`` the list query pulls per row. The title is
#: the whole filed complaint (up to ~8 KB); the row shows the first line cut
#: to 100 chars (``_title_preview``), so shipping the rest over the wire
#: (~1 MB across all gripes, gr477858) is pure cost.
_PREVIEW_FETCH_CHARS = 400


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
        if (
            getattr(t, "namespace", None) == "closed"
            and getattr(t, "prefix", None) == "STATUS"
        ):
            return str(getattr(t, "value", "open"))
    return "open"


def _prio_of(tags: Any) -> str | None:
    """The ``PRIO:`` tag value, if the gripe carries one."""
    for t in tags:
        if (
            getattr(t, "namespace", None) == "closed"
            and getattr(t, "prefix", None) == "PRIO"
        ):
            return str(getattr(t, "value", ""))
    return None


def _filed_by(request: Request) -> str:
    """Who the attribution line should name.

    ``current_user`` returns ``None`` when the gate never ran
    (``PRECIS_WEB_AUTH`` off) — which is *not* "anonymous", it's "nobody
    knows who this is" (see ``auth.py::current_user``). Say that in the
    text rather than inventing a name: a gripe attributed to a guess is
    worse than one that admits the server wasn't checking.
    """
    user = current_user(request)
    return user.login if user is not None else "unknown (HTTP auth disabled)"


def _attribute(text: str, who: str) -> str:
    """Append the ``— filed by …`` line to a gripe body.

    Appended, not prefixed: a gripe's ``ref.title`` is the filed text
    verbatim and the list preview is its *first* line
    (``_title_preview``), so a leading attribution would push the actual
    complaint out of every row.
    """
    return f"{text.strip()}\n\n— filed by {who} via the /gripes web form"


def _rows(store: Store, *, status_filter: str) -> list[dict[str, Any]]:
    """Live gripes with their STATUS/PRIO tags, workflow- then recency-sorted.

    ``status_filter``: ``'live'`` (everything but the TERMINAL_VALUES),
    ``'all'``, ``'retired'`` (the soft-deleted merged/duplicate/junk
    bucket), or any one status value — the chips pass whatever the
    grouped count reported, so off-vocabulary drift round-trips too.
    The status value is always a bind parameter, never interpolated.
    Shape mirrors ``alerts.py::_rows`` — one ``refs``/``ref_tags``/``tags``
    join keyed on the STATUS axis, plus a correlated subquery for the
    optional ``PRIO:`` tag (a plain join would duplicate rows for a
    multi-PRIO-tagged gripe).
    """
    # A gripe with no STATUS tag counts as ``open`` (the migration-0176
    # trigger makes that unreachable, but the list must never hide one).
    clauses = ["r.kind = 'gripe'"]
    params: list[Any] = []
    if status_filter == "all":
        pass
    elif status_filter == "live":
        clauses.append("r.retired_at IS NULL")
        clauses.append("NOT (COALESCE(t.value, 'open') = ANY(%s))")
        params.append(list(TERMINAL_VALUES))
    elif status_filter == "retired":
        clauses.append("r.retired_at IS NOT NULL")
    else:
        clauses.append("r.retired_at IS NULL")
        clauses.append("COALESCE(t.value, 'open') = %s")
        params.append(status_filter)
    sql = f"""
        SELECT r.ref_id,
               left(ltrim(r.title, E' \\t\\r\\n'), {_PREVIEW_FETCH_CHARS}),
               r.created_at,
               r.updated_at,
               COALESCE(t.value, 'open') AS status,
               (SELECT p.value FROM ref_tags prt
                  JOIN tags p ON p.tag_id = prt.tag_id
                 WHERE prt.ref_id = r.ref_id AND p.namespace = 'PRIO'
                 LIMIT 1) AS prio
          FROM refs r
          LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id
               AND rt.tag_id IN (SELECT tag_id FROM tags WHERE namespace = 'STATUS')
          LEFT JOIN tags t ON t.tag_id = rt.tag_id
         WHERE {" AND ".join(clauses)}
         ORDER BY CASE COALESCE(t.value, 'open') {_STATUS_RANK_SQL} ELSE {len(_RANKED_VALUES)} END,
                  r.updated_at DESC
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql, params).fetchall()
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


def _status_counts(store: Store) -> list[dict[str, Any]]:
    """One ``COUNT … GROUP BY`` over every gripe's effective status —
    the chip counts (gr520298). Retired rows bucket under the
    ``'retired'`` pseudo-status; a live gripe with no STATUS counts as
    ``open``. Every value present gets a chip — the vocabulary is
    app-level, so off-vocabulary drift shows up rather than hiding
    rows. Workflow order first, unknowns alphabetical last."""
    sql = """
        SELECT CASE WHEN r.retired_at IS NOT NULL THEN 'retired'
                    ELSE COALESCE(t.value, 'open') END,
               COUNT(*)
          FROM refs r
          LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id
               AND rt.tag_id IN (SELECT tag_id FROM tags WHERE namespace = 'STATUS')
          LEFT JOIN tags t ON t.tag_id = rt.tag_id
         WHERE r.kind = 'gripe'
         GROUP BY 1
    """
    with store.pool.connection() as conn:
        rows = conn.execute(sql).fetchall()
    rank = {v: i for i, v in enumerate(_RANKED_VALUES)}
    return [
        {"status": status, "count": count, "badge": _status_badge(status)}
        for status, count in sorted(
            ((str(r[0]), int(r[1])) for r in rows),
            key=lambda kv: (rank.get(kv[0], len(_RANKED_VALUES)), kv[0]),
        )
    ]


@router.get("", response_class=HTMLResponse)
async def gripes(request: Request) -> HTMLResponse:
    """Open gripes inline + one chip per other status (gr520298).

    Two queries at load: the grouped count and the open rows — the
    terminal/retired cohort (thousands of rows in prod) never leaves
    the DB until its chip is expanded."""
    store = get_store(request)
    chips = _status_counts(store)
    if not any(c["status"] == "open" for c in chips):
        chips.insert(
            0,
            {"status": "open", "count": 0, "badge": _status_badge("open")},
        )
    return templates.TemplateResponse(
        request,
        "gripes/list.html.j2",
        {
            "active_tab": "gripes",
            "chips": chips,
            "open_rows": _rows(store, status_filter="open"),
            "filed_by": _filed_by(request),
        },
    )


@router.get("/rows", response_class=HTMLResponse)
async def status_rows(request: Request, status: str = "open") -> HTMLResponse:
    """One status's rows — the shared lazy fragment every chip loads on
    first expand (gr520298). ``status`` round-trips whatever the grouped
    count reported, including ``retired`` and off-vocabulary values."""
    store = get_store(request)
    return templates.TemplateResponse(
        request,
        "gripes/_rows.html.j2",
        {"rows": _rows(store, status_filter=status), "rows_status": status},
    )


@router.post("", response_model=None)
async def file_gripe(request: Request, text: str = Form(...)) -> Response:
    """File a new gripe via ``put`` (no id — the create path).

    Blank text is a no-op redirect, mirroring the comment route: an
    empty gripe carries nothing to triage.
    """
    if not text.strip():
        return RedirectResponse(url="/gripes", status_code=303)
    user = current_user(request)
    args: dict[str, Any] = {
        "kind": "gripe",
        "text": _attribute(text, _filed_by(request)),
    }
    if user is not None:
        # Server-side filer tag — the trusted "filed by" channel (the
        # body attribution line is untrusted text; triage's owner
        # rulings key on the tag).
        args["tags"] = [f"user:{user.login}"]
    return await redirect_or_error(
        request,
        "put",
        args,
        redirect="/gripes",
        error_title="Filing error",
    )


@router.get("/{id}", response_class=HTMLResponse)
async def detail(request: Request, id: int) -> HTMLResponse:
    """Body + comment timeline + status controls + comment box."""
    store = get_store(request)
    # include_deleted: the retired chip's rows must land somewhere —
    # a merged/duplicate gripe's body stays readable, controls hidden.
    refs = store.fetch_refs_by_ids([id], include_deleted=True)
    ref = refs.get(id)
    if ref is None or ref.kind != "gripe":
        raise NotFound(f"gripe id={id} not found")

    # Body chunk mirrors ref.title verbatim (see _title_preview); fall
    # back to the title itself for pre-chunk-schema gripes that somehow
    # lack a gripe_body row (same defensive fallback GripeHandler's own
    # _render_one uses).
    body_text = ref.title or ""
    comments: list[dict[str, Any]] = []
    for b in store.chunks.list_chunks_for_ref(ref.id):
        chunk_kind = getattr(b, "chunk_kind", None)
        if chunk_kind == _BODY_KIND:
            body_text = b.text or body_text
        elif chunk_kind == _COMMENT_KIND:
            comments.append({"pos": b.ord, "text": b.text or ""})

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
            "retired": getattr(ref, "retired_at", None) is not None,
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
