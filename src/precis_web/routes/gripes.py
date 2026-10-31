"""Gripes workbench — write surface for the dev bug tracker (``kind='gripe'``).

Read-only browsing already exists at ``/refs/gripe/{id}`` (the generic
ref browser, ``routes/refs.py``); this tab is that kind's
mutation-capable *workbench* — list/triage/comment/retire — same
relationship ``/flags`` and ``/alerts`` have to their own kinds. Zero
data-layer work: every mutation dispatches an existing verb (``tag`` /
``put`` / ``delete``) through the in-process runtime so validation and
tree guards stay single-sourced with the MCP surface (see ``deps.py``).

* ``GET /gripes[?status=<s>]`` — the selected filter's gripes inline
  (default ``triaged``, the pick list the nav badge counts; ``live`` /
  ``all`` / any one status by ``status=``) plus one status chip per
  status with its count (gr520298). The load is a single
  ``COUNT … GROUP BY`` status plus the selected rows; every other
  status's rows lazy-load on the chip's first expand. Retired gripes
  (merged/duplicate/junk soft-deletes) bucket under a ``retired``
  pseudo-status chip; carries the filing form.
* ``GET /gripes/rows?status=<s>[&offset=<n>]`` — the chips' shared
  lazy-rows fragment: one status's rows, on the same ``_rows`` query
  the list used to run for every status at page load. Paged
  ``_ROWS_PAGE`` at a time — the done bucket alone is ~1.9k rows, so
  one chip click must not ship the whole cohort; a trailing
  "show more" row fetches the next page and swaps into the tbody.
  ``status`` is allowlisted to ``_STATUS_PARAM_VALUES`` plus whatever
  the grouped count surfaces — a drift status's chip stays expandable;
  an unknown value not in the DB is a 400 naming the allowed list.
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
    "auto_verify",
    "user_verify",
    "needs_spec",
    "wontfix",
)

#: The list's default filter and what the nav badge counts
#: (``nav.py::_gripes_count``): the triaged pick list. "live" and "all"
#: stay reachable by ``?status=``.
DEFAULT_STATUS = "triaged"

#: Terminal STATUS values the "live" filter excludes. ``done`` = fixed/resolved
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
    "fixing",
    "ready_for_fix",
    "in_review",
    "verify",
    "auto_verify",
    "user_verify",
    "needs_spec",
    "parked",
    "done",
    "wontfix",
)

#: SQL ``CASE`` fragment for the _RANKED_VALUES workflow rank — built
#: once since the vocabulary is fixed, not user input.
_STATUS_RANK_SQL = " ".join(
    f"WHEN '{v}' THEN {i}" for i, v in enumerate(_RANKED_VALUES)
)

#: Values ``GET /gripes/rows?status=`` accepts: the renderable STATUS
#: vocabulary plus the ``retired`` pseudo-status (the soft-deleted
#: bucket's chip) and the ``all``/``live`` cohort filters — plus, at
#: request time, every status value the grouped count returns, so an
#: off-vocabulary drift chip stays expandable. Anything else is a 400
#: naming this list — the value used to bind straight into the WHERE
#: clause and hand back an empty state that read as "no gripes", which
#: silently masked typos and off-vocabulary guesses.
_STATUS_PARAM_VALUES: tuple[str, ...] = (*_RANKED_VALUES, "retired", "all", "live")

#: Rows per ``/gripes/rows`` page — the cap that keeps a closed-status
#: chip's first expand from shipping its whole cohort (the done bucket
#: is ~1.9k rows). The fragment's show-more row fetches the next page
#: via ``offset=``; the inline open section caps the same way.
_ROWS_PAGE = 100

#: Badge colour per status, distinct from Alerts' severity palette so
#: the two tabs don't visually blur together. ``retired`` is the
#: pseudo-status the grouped count gives soft-deleted rows (merged/
#: duplicate/junk) — greyed further than open since those are gone.
_STATUS_BADGE: dict[str, str] = {
    "open": "bg-surface-muted text-fg border-line-strong",
    "triaged": "bg-info-muted text-info-deep border-info-strong",
    "fixing": "bg-warning-muted text-warning-deep border-warning-strong",
    "ready_for_fix": "bg-warning-muted text-warning-deep border-warning-strong",
    "in_review": "bg-accent-muted text-accent-deep border-accent-strong",
    "verify": "bg-teal-muted text-teal-deep border-teal-strong",
    "auto_verify": "bg-teal-muted text-teal-deep border-teal-strong",
    "user_verify": "bg-accent-muted text-accent-deep border-accent-strong",
    "needs_spec": "bg-warning-muted text-warning-deep border-warning-strong",
    "parked": "bg-surface-muted text-fg-faint border-line-strong",
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


def _project_of(tags: Any) -> str | None:
    """The ``project:<slug>`` open-tag value, if the gripe carries one
    (gr543639) — ``None`` reads as the ``precis`` default."""
    for t in tags:
        v = str(getattr(t, "value", "") or "")
        if getattr(t, "namespace", None) == "open" and v.startswith("project:"):
            return v
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


def _rows(
    store: Store,
    *,
    status_filter: str,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Live gripes with their STATUS/PRIO tags, workflow- then recency-sorted.

    ``status_filter``: ``'live'`` (everything but the TERMINAL_VALUES),
    ``'all'``, ``'retired'`` (the soft-deleted merged/duplicate/junk
    bucket), or any one status value. The ``/gripes/rows`` route
    allowlists to ``_STATUS_PARAM_VALUES`` first; this helper itself
    still takes any string — the status value is always a bind
    parameter, never interpolated. ``limit``/``offset`` page the result
    (both bind parameters; callers fetch ``_ROWS_PAGE + 1`` to detect a
    next page). Shape mirrors ``alerts.py::_rows`` — one
    ``refs``/``ref_tags``/``tags`` join keyed on the STATUS axis, plus
    a correlated subquery for the optional ``PRIO:`` tag (a plain join
    would duplicate rows for a multi-PRIO-tagged gripe).
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
    page = ""
    if limit is not None:
        page = "LIMIT %s OFFSET %s"
        params += [limit, max(offset, 0)]
    sql = f"""
        SELECT r.ref_id,
               left(ltrim(r.title, E' \\t\\r\\n'), {_PREVIEW_FETCH_CHARS}),
               r.created_at,
               r.updated_at,
               COALESCE(t.value, 'open') AS status,
               (SELECT p.value FROM ref_tags prt
                  JOIN tags p ON p.tag_id = prt.tag_id
                 WHERE prt.ref_id = r.ref_id AND p.namespace = 'PRIO'
                 LIMIT 1) AS prio,
               (SELECT pj.value FROM ref_tags prj
                  JOIN tags pj ON pj.tag_id = prj.tag_id
                 WHERE prj.ref_id = r.ref_id AND pj.namespace = 'OPEN'
                   AND pj.value LIKE 'project:%%'
                 ORDER BY pj.value LIMIT 1) AS project
          FROM refs r
          LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id
               AND rt.tag_id IN (SELECT tag_id FROM tags WHERE namespace = 'STATUS')
          LEFT JOIN tags t ON t.tag_id = rt.tag_id
         WHERE {" AND ".join(clauses)}
         ORDER BY CASE COALESCE(t.value, 'open') {_STATUS_RANK_SQL} ELSE {len(_RANKED_VALUES)} END,
                  r.updated_at DESC,
                  r.ref_id DESC
         {page}
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
                "project": r[6],
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


def _filter_chips(chips: list[dict[str, Any]], selected: str) -> list[dict[str, Any]]:
    """The page's chips with the selected filter guaranteed present.

    ``live`` / ``all`` are cohort filters, not stored statuses, so they
    get a synthetic leading chip whose count is summed from the grouped
    count (``live`` = every non-retired, non-terminal status; ``all``
    = everything including the retired bucket). A selected status with
    no rows (``triaged`` on an empty pick list) still renders, at 0.
    """
    out = list(chips)
    if selected == "live":
        count = sum(
            c["count"]
            for c in chips
            if c["status"] != "retired" and c["status"] not in TERMINAL_VALUES
        )
    elif selected == "all":
        count = sum(c["count"] for c in chips)
    elif any(c["status"] == selected for c in chips):
        return out
    else:
        count = 0
    out.insert(
        0, {"status": selected, "count": count, "badge": _status_badge(selected)}
    )
    return out


@router.get("", response_class=HTMLResponse)
async def gripes(request: Request, status: str = DEFAULT_STATUS) -> HTMLResponse:
    """The selected filter's rows inline + one chip per status (gr520298).

    The default filter is ``triaged`` — the pick list, the same count
    the nav badge shows (``nav.py::_gripes_count``). ``?status=live`` /
    ``all`` / any one status reaches the rest; the chips carry counts
    per status so the big list is one click away.

    Two queries at load: the grouped count and the selected rows — the
    terminal/retired cohort (thousands of rows in prod) never leaves
    the DB until its chip is expanded. ``status`` takes the same
    allowlist as ``/gripes/rows``."""
    store = get_store(request)
    counts = _status_counts(store)
    allowed = _allowed_statuses(counts)
    if status not in allowed:
        return _unknown_status(request, status, allowed)
    chips = _filter_chips(counts, status)
    selected_rows = _rows(store, status_filter=status, limit=_ROWS_PAGE + 1)
    return templates.TemplateResponse(
        request,
        "gripes/list.html.j2",
        {
            "active_tab": "gripes",
            "chips": chips,
            "selected": status,
            "selected_rows": selected_rows[:_ROWS_PAGE],
            "selected_next_offset": (
                _ROWS_PAGE if len(selected_rows) > _ROWS_PAGE else None
            ),
            "filed_by": _filed_by(request),
        },
    )


def _allowed_statuses(counts: list[dict[str, Any]]) -> tuple[str, ...]:
    """``_STATUS_PARAM_VALUES`` plus every status the grouped count
    returned — a drift status's chip stays selectable."""
    return tuple(
        dict.fromkeys((*_STATUS_PARAM_VALUES, *(str(c["status"]) for c in counts)))
    )


def _unknown_status(
    request: Request, status: str, allowed: tuple[str, ...]
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {
            "title": "Unknown gripe status",
            "detail": f"unknown status {status!r} — allowed: " + ", ".join(allowed),
            "status": 400,
        },
        status_code=400,
    )


@router.get("/rows", response_class=HTMLResponse)
async def status_rows(
    request: Request, status: str = "open", offset: int = 0
) -> HTMLResponse:
    """One status's rows — the shared lazy fragment every chip loads on
    first expand (gr520298), paged ``_ROWS_PAGE`` at a time.

    ``status`` is allowlisted to ``_STATUS_PARAM_VALUES`` (the
    vocabulary plus ``retired``/``all``/``live``) plus every value the
    grouped count returns — a drift status the list renders as a chip
    (a legacy ``closed``, say) must stay expandable. An unknown value
    absent from the DB is a 400 naming the allowed list rather than an
    empty state that read as "no gripes". ``offset`` is the show-more
    row's continuation: an offset render is a bare ``<tr>`` page that
    swaps into the tbody in place of the show-more row that fetched
    it."""
    store = get_store(request)
    allowed = _allowed_statuses(_status_counts(store))
    if status not in allowed:
        return _unknown_status(request, status, allowed)
    offset = max(offset, 0)
    rows = _rows(store, status_filter=status, limit=_ROWS_PAGE + 1, offset=offset)
    return templates.TemplateResponse(
        request,
        "gripes/_rows.html.j2",
        {
            "rows": rows[:_ROWS_PAGE],
            "rows_status": status,
            "next_offset": (offset + _ROWS_PAGE if len(rows) > _ROWS_PAGE else None),
            "append": offset > 0,
        },
    )


@router.post("", response_model=None)
async def file_gripe(
    request: Request, text: str = Form(...), project: str = Form("")
) -> Response:
    """File a new gripe via ``put`` (no id — the create path).

    Blank text is a no-op redirect, mirroring the comment route: an
    empty gripe carries nothing to triage. The optional ``project``
    field rides the same ``args={'project': …}`` channel the verb
    documents (gr543639) — blank means the ``precis`` default.
    """
    if not text.strip():
        return RedirectResponse(url="/gripes", status_code=303)
    user = current_user(request)
    args: dict[str, Any] = {
        "kind": "gripe",
        "text": _attribute(text, _filed_by(request)),
    }
    if project.strip():
        args["project"] = project.strip()
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
            "project": _project_of(tags),
        },
    )


def _move_back_to_new_args(
    current: str | None, value: str, *, gid: int, actor: str
) -> tuple[str, dict[str, Any]]:
    """gr543698: ``needs_spec`` → ``open`` is "move back to new" — a
    comment records the human push so triage re-reads the thread (a bare
    tag would be silent). Comment + STATUS in one ``put`` transaction;
    everything else stays a bare ``tag``. Returns ``(verb, args)``."""
    if current == "needs_spec" and value == "open":
        return "put", {
            "kind": "gripe",
            "id": gid,
            "text": f"move back to new — was needs_spec ({actor})",
            "tags": ["STATUS:open"],
        }
    return "tag", {"kind": "gripe", "id": gid, "add": [f"STATUS:{value}"]}


@router.post("/{id}/status", response_model=None)
async def set_status(request: Request, id: int, value: str = Form(...)) -> Response:
    """Change STATUS via the closed-axis ``tag`` verb (replace-on-add).

    An off-vocabulary value is a no-op redirect (never dispatched) —
    mirrors ``flags.py``'s unknown-flag guard. htmx requests get back
    just the re-rendered status fragment (in-place swap); the no-JS
    fallback redirects to the detail page. A ``needs_spec`` → ``open``
    click is "move back to new" — it routes through ``put`` so a
    comment records the push (:func:`_move_back_to_new_args`).
    """
    if value not in STATUS_VALUES:
        return RedirectResponse(url=f"/gripes/{id}", status_code=303)
    store = get_store(request)
    current = next(
        (str(t) for t in store.tags_for(id) if str(t).startswith("STATUS:")),
        None,
    )
    current = current.split(":", 1)[1] if current else None
    user = current_user(request)
    verb, args = _move_back_to_new_args(
        current, value, gid=id, actor=user.login if user is not None else "web"
    )
    if request.headers.get("HX-Request") != "true":
        return await redirect_or_error(
            request,
            verb,
            args,
            redirect=f"/gripes/{id}",
            error_title="Status error",
        )
    body, is_error = await await_dispatch(request, verb, args)
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
