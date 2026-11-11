"""Gripes workbench — write surface for the dev bug tracker (``kind='gripe'``).

Read-only browsing already exists at ``/refs/gripe/{id}`` (the generic
ref browser, ``routes/refs.py``); this tab is that kind's
mutation-capable *workbench* — list/triage/comment/retire — same
relationship ``/flags`` and ``/alerts`` have to their own kinds. Zero
data-layer work: every mutation dispatches an existing verb (``tag`` /
``put`` / ``delete``) through the in-process runtime so validation and
tree guards stay single-sourced with the MCP surface (see ``deps.py``).

* ``GET /gripes[?status=<s>][&sort=<k>]`` — the selected filter's
  gripes inline (default ``triaged``, the pick list; ``attention`` —
  the cohort the nav badge counts — plus ``live`` / ``all`` / any one
  status by ``status=``) under a row of real filter buttons with
  counts (gr549794): the daily cohorts as buttons, every other status
  the grouped count surfaces behind a ``more`` overflow, each carrying
  a ``title=`` tooltip naming what it covers (gr549769). Only the
  selected filter's rows render — the earlier per-status sections
  duplicated a cohort filter's rows across the status sections below
  it. The load is a single ``COUNT … GROUP BY`` status plus the
  selected rows. Retired gripes (merged/duplicate/junk soft-deletes)
  bucket under a ``retired`` pseudo-status; carries the filing form.
  ``sort=`` picks the queue order (gr595573) — ``drain`` (default:
  the dispatch score first, then size s<m<l<unsized, then oldest),
  ``prio`` (the human rungs first — unset reads as normal — then
  size, then oldest; gr604514),
  ``oldest``/``newest``/``size``/``source``/``status`` — the same
  vocabulary ``search(kind='gripe', sort=…)`` takes, from the shared
  :mod:`precis.utils.gripe_order` helper.
* ``GET /gripes/rows?status=<s>[&offset=<n>][&sort=<k>]`` — the rows
  paging fragment: one status's rows, on the same ``_rows`` query.
  Paged ``_ROWS_PAGE`` at a time — the done bucket alone is ~1.9k rows,
  so one filter switch must not ship the whole cohort; a trailing
  "show more" row fetches the next page and swaps into the tbody.
  ``status`` is allowlisted to ``_STATUS_PARAM_VALUES`` plus whatever
  the grouped count surfaces — a drift status's filter stays
  reachable; an unknown value not in the DB is a 400 naming the
  allowed list. ``sort`` is allowlisted to
  ``gripe_order.SORT_VALUES`` the same way.
* ``POST /gripes`` — ``put`` verb create. The filed text gets an
  attribution line naming the signed-in user appended to it (see
  ``_attribute``): a gripe's stored text *is* its whole record, and
  the human who hit friction is the one worth going back to.
* ``GET /gripes/{id}`` — title + body + append-only comment timeline, a
  comment box, and the status controls. Legacy titles can contain the
  whole complaint, so the heading takes their first non-blank line;
  an identical first body line is omitted from display only.
  Filing origin comes from server-stamped ``meta.filed_by``; read-only
  legacy rows retain their ``agent:`` / ``source:agent_ro`` tag attribution.
  A recorded agentlog can resolve a model after the run finishes; interactive
  models remain unknown unless the client declared one. Trust uses the same
  stamp as the MCP get footer.
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

import logging
from datetime import UTC
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis.errors import NotFound
from precis.filing_origin import agentlog_model
from precis.handlers._gripe_title import first_line
from precis.runtime.trust_verb import trust_footer
from precis.utils import dispatch_score as _dscore
from precis.utils.gripe_order import (
    DEFAULT_SORT,
    SORT_VALUES,
    STATUS_RANK,
    order_by_sql,
    size_letter_sql,
    source_label_sql,
)
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
log = logging.getLogger(__name__)

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

#: The list's default filter: the triaged pick list. "attention",
#: "live" and "all" stay reachable by ``?status=``.
DEFAULT_STATUS = "triaged"

#: STATUS cohort the nav badge counts (gr548577): the actionable,
#: not-properly-closed set — newly filed (``open``), on the pick list
#: (``triaged``), or landed and awaiting the release check
#: (``auto_verify``). ``fixing`` is leased in-flight work,
#: ``needs_spec``/``parked`` are shelved off every queue, and
#: ``user_verify`` already surfaces through the Needs-you ask todo.
BADGE_STATUSES: tuple[str, ...] = ("open", "triaged", "auto_verify")

#: The ``?status=`` cohort the badge links to — ``BADGE_STATUSES`` as
#: one list, so a clicked count shows exactly the set it numbered.
BADGE_FILTER = "attention"

#: Terminal STATUS values the "live" filter excludes. ``done`` = fixed/resolved
#: (the documented terminal state since gr207238 blessed the de facto
#: practice — see ``precis-gripe-help``'s lifecycle table); ``wontfix``
#: = decided not to act. ``delete`` retires junk/test artifacts only.
TERMINAL_VALUES: tuple[str, ...] = ("done", "wontfix")

#: Every STATUS value the list can render, in display order — the
#: transition vocabulary plus the ``done`` terminal state, terminal
#: states last. Shared with the ``sort='status'`` rank and the MCP
#: browse's drain order via ``precis.utils.gripe_order``.
_RANKED_VALUES: tuple[str, ...] = STATUS_RANK

#: Values ``GET /gripes/rows?status=`` accepts: the renderable STATUS
#: vocabulary plus the ``retired`` pseudo-status (the soft-deleted
#: bucket's chip) and the ``all``/``live``/``attention`` cohort
#: filters — plus, at
#: request time, every status value the grouped count returns, so an
#: off-vocabulary drift chip stays expandable. Anything else is a 400
#: naming this list — the value used to bind straight into the WHERE
#: clause and hand back an empty state that read as "no gripes", which
#: silently masked typos and off-vocabulary guesses.
_STATUS_PARAM_VALUES: tuple[str, ...] = (
    *_RANKED_VALUES,
    "retired",
    "all",
    "live",
    BADGE_FILTER,
)

#: The filter button row on ``GET /gripes`` (gr549794): the cohorts
#: reached for daily — ``attention`` (the badge's set), the pick list,
#: the verify queues, plus ``live`` / ``all``. Every other status the
#: grouped count surfaces sits behind the ``more`` overflow — same
#: ``?status=`` links, same allowlist.
_FILTER_BUTTONS: tuple[str, ...] = (
    BADGE_FILTER,
    "open",
    "triaged",
    "auto_verify",
    "user_verify",
    "needs_spec",
    "live",
    "all",
)

#: ``title=`` mouseover per filter (gr549769): which STATUS values or
#: tags the filter covers and why a gripe lands there. One dict next to
#: ``_STATUS_PARAM_VALUES`` so the texts stay beside the vocabulary;
#: an off-vocabulary drift status gets a generated line in
#: ``_filter_buttons``.
_STATUS_TOOLTIPS: dict[str, str] = {
    BADGE_FILTER: (
        "open + triaged + auto_verify — the nav badge's cohort: "
        "gripes that need an eye now"
    ),
    "open": "STATUS:open — newly filed, not yet triaged",
    "triaged": "STATUS:triaged — reviewed, on the pick list",
    "fixing": "STATUS:fixing — claimed, work in flight",
    "ready_for_fix": "STATUS:ready_for_fix — specced, waiting for a builder",
    "in_review": "STATUS:in_review — a fix is in review",
    "verify": "STATUS:verify — legacy verify bucket",
    "auto_verify": ("STATUS:auto_verify — a fix landed, awaiting the release check"),
    "user_verify": ("STATUS:user_verify — waiting on the human to confirm the fix"),
    "needs_spec": ("STATUS:needs_spec — blocked on a spec or a ruling"),
    "parked": "STATUS:parked — deliberately shelved, off every queue",
    "done": "STATUS:done — fixed and verified",
    "wontfix": "STATUS:wontfix — won't act, kept on record",
    "retired": "soft-deleted gripes — merged, duplicate or junk",
    "live": "every live gripe — all statuses except done and wontfix",
    "all": "every gripe, including the retired bucket",
}

#: ``title=`` mouseover per ``?sort=`` value (gr595573) — what the queue
#: order means, in the same row-of-links style as the status filters.
_SORT_TOOLTIPS: dict[str, str] = {
    "drain": (
        "the drain order — dispatch score first (higher = hotter; a set "
        "PRIO multiplies it), then size s < m < l < unsized, oldest first "
        "within a size"
    ),
    "oldest": "oldest filed first",
    "newest": "newest filed first",
    "prio": (
        "the human PRIO rungs first — urgent … low, unset counts as "
        "normal — then size s < m < l, oldest inside"
    ),
    "size": "size s < m < l < unsized, oldest first inside",
    "source": "who filed — human, then agent, then watchdog",
    "status": "workflow order — open through wontfix, oldest first inside",
}

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
    sort: str = DEFAULT_SORT,
    limit: int | None = None,
    offset: int = 0,
) -> list[dict[str, Any]]:
    """Live gripes with their STATUS/PRIO tags, ``sort=``-ordered.

    ``status_filter``: ``'live'`` (everything but the TERMINAL_VALUES),
    ``'all'``, ``'retired'`` (the soft-deleted merged/duplicate/junk
    bucket), ``BADGE_FILTER`` (the nav badge's BADGE_STATUSES union),
    or any one status value. The ``/gripes/rows`` route
    allowlists to ``_STATUS_PARAM_VALUES`` first; this helper itself
    still takes any string — the status value is always a bind
    parameter, never interpolated. ``sort`` is a
    ``gripe_order.SORT_VALUES`` name (allowlisted by the routes); its
    ORDER BY comes from ``gripe_order.order_by_sql`` — the same
    fragment the MCP browse reads, so the web list and
    ``search(kind='gripe')`` can't order differently (gr595573).
    ``limit``/``offset`` page the result
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
    elif status_filter == BADGE_FILTER:
        clauses.append("r.retired_at IS NULL")
        clauses.append("COALESCE(t.value, 'open') = ANY(%s)")
        params.append(list(BADGE_STATUSES))
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
               COALESCE(r.prio::text,
                        (SELECT p.value FROM ref_tags prt
                           JOIN tags p ON p.tag_id = prt.tag_id
                          WHERE prt.ref_id = r.ref_id AND p.namespace = 'PRIO'
                          LIMIT 1)) AS prio,
               (SELECT pj.value FROM ref_tags prj
                  JOIN tags pj ON pj.tag_id = prj.tag_id
                 WHERE prj.ref_id = r.ref_id AND pj.namespace = 'OPEN'
                   AND pj.value LIKE 'project:%%'
                 ORDER BY pj.value LIMIT 1) AS project,
               {size_letter_sql()} AS size,
               {source_label_sql()} AS source,
               round(({_dscore.score_sql()})::numeric, 2) AS score
          FROM refs r
          LEFT JOIN ref_tags rt ON rt.ref_id = r.ref_id
               AND rt.tag_id IN (SELECT tag_id FROM tags WHERE namespace = 'STATUS')
          LEFT JOIN tags t ON t.tag_id = rt.tag_id
         WHERE {" AND ".join(clauses)}
         ORDER BY {order_by_sql(sort)}
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
                "size": r[7],
                "source": r[8],
                "score": float(r[9]) if r[9] is not None else None,
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

    ``live`` / ``all`` / ``BADGE_FILTER`` are cohort filters, not stored
    statuses, so they get a synthetic leading chip whose count is summed
    from the grouped count (``live`` = every non-retired, non-terminal
    status; ``all`` = everything including the retired bucket;
    ``BADGE_FILTER`` = the BADGE_STATUSES union). A selected status with
    no rows (``triaged`` on an empty pick list) still renders, at 0.
    """
    out = list(chips)
    if selected == BADGE_FILTER:
        count = sum(c["count"] for c in chips if c["status"] in BADGE_STATUSES)
    elif selected == "live":
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


def _status_tooltip(status: str) -> str:
    """The ``title=`` text for one filter (gr549769) — the dict's
    wording for known values, a generated line for drift."""
    return _STATUS_TOOLTIPS.get(
        status,
        f"STATUS:{status} — a status outside the current vocabulary (legacy drift).",
    )


def _filter_buttons(
    counts: list[dict[str, Any]], selected: str
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """The button row + ``more`` overflow for ``GET /gripes`` (gr549794).

    ``buttons`` is ``_FILTER_BUTTONS`` in order with cohort counts —
    the same sums ``_filter_chips`` computes for its synthetic leading
    chip (``attention`` = the ``BADGE_STATUSES`` union, ``live`` =
    every non-retired non-terminal status, ``all`` = everything);
    a real status takes its grouped count, a not-present one 0.
    ``more`` is every other status the grouped count surfaced —
    ``fixing``/``parked``/``done``/``wontfix``/``retired`` and drift
    values — plus the selected value itself when it's neither a button
    nor counted, so a selected filter always renders somewhere.
    """
    by_status = {str(c["status"]): int(c["count"]) for c in counts}
    cohort = {
        BADGE_FILTER: sum(by_status.get(s, 0) for s in BADGE_STATUSES),
        "live": sum(
            n
            for s, n in by_status.items()
            if s != "retired" and s not in TERMINAL_VALUES
        ),
        "all": sum(by_status.values()),
    }
    buttons = [
        {
            "status": s,
            "count": cohort.get(s, by_status.get(s, 0)),
            "selected": s == selected,
            "tooltip": _status_tooltip(s),
        }
        for s in _FILTER_BUTTONS
    ]
    more_statuses = [s for s in by_status if s not in _FILTER_BUTTONS]
    if selected not in _FILTER_BUTTONS and selected not in more_statuses:
        more_statuses.append(selected)
    more = [
        {
            "status": s,
            "count": by_status.get(s, 0),
            "selected": s == selected,
            "tooltip": _status_tooltip(s),
        }
        for s in more_statuses
    ]
    return buttons, more


def _comment_stamp(dt: Any) -> str:
    """The comment-header timestamp (gr549420): ``%Y-%m-%d %H:%MZ`` —
    the same format the MCP ``view='comments'`` header prints
    (``_comment_timeline._stamp``)."""
    if dt is None:
        return ""
    return dt.astimezone(UTC).strftime("%Y-%m-%d %H:%MZ")


@router.get("", response_class=HTMLResponse)
async def gripes(
    request: Request, status: str = DEFAULT_STATUS, sort: str = DEFAULT_SORT
) -> HTMLResponse:
    """The filter button row + the selected filter's rows (gr549794).

    The default filter is ``triaged`` — the pick list. The nav badge
    (``nav.py::_gripes_count``) counts the wider ``attention`` cohort
    (``BADGE_STATUSES``), which ``?status=attention`` lists.
    ``?status=live`` / ``all`` / any one status reaches the rest — the
    buttons link to all of them with counts; statuses outside
    ``_FILTER_BUTTONS`` sit behind ``more`` so the row stays short.
    ``sort=`` picks the shared queue order (gr595573, default
    ``drain``) and rides along on every filter link.

    Two queries at load: the grouped count and the selected rows — the
    terminal/retired cohort (thousands of rows in prod) never leaves
    the DB until its filter is picked. ``status`` takes the same
    allowlist as ``/gripes/rows``."""
    if sort not in SORT_VALUES:
        return _unknown_sort(request, sort)
    store = get_store(request)
    counts = _status_counts(store)
    allowed = _allowed_statuses(counts)
    if status not in allowed:
        return _unknown_status(request, status, allowed)
    buttons, more_filters = _filter_buttons(counts, status)
    selected_rows = _rows(store, status_filter=status, sort=sort, limit=_ROWS_PAGE + 1)
    return templates.TemplateResponse(
        request,
        "gripes/list.html.j2",
        {
            "active_tab": "gripes",
            "buttons": buttons,
            "more_filters": more_filters,
            "selected": status,
            "sort": sort,
            "sorts": _sort_links(status, sort),
            "sort_qs": _sort_qs(sort),
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


def _unknown_sort(request: Request, sort: str) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {
            "title": "Unknown gripe sort",
            "detail": f"unknown sort {sort!r} — allowed: " + ", ".join(SORT_VALUES),
            "status": 400,
        },
        status_code=400,
    )


def _sort_qs(sort: str) -> str:
    """The ``&sort=`` query-string tail a link carries — empty at the
    default so ``?status=open`` stays the canonical clean URL."""
    return "" if sort == DEFAULT_SORT else f"&sort={sort}"


def _sort_links(status: str, sort: str) -> list[dict[str, Any]]:
    """The sort picker row under the filter buttons (gr595573): one
    link per ``SORT_VALUES`` name preserving the selected filter. The
    default sorts to the bare ``?status=`` URL — clicking ``drain`` is
    how a non-default sort resets."""
    return [
        {
            "sort": s,
            "selected": s == sort,
            "href": f"/gripes?status={status}{_sort_qs(s)}",
            "tooltip": _SORT_TOOLTIPS[s],
        }
        for s in SORT_VALUES
    ]


@router.get("/rows", response_class=HTMLResponse)
async def status_rows(
    request: Request, status: str = "open", offset: int = 0, sort: str = DEFAULT_SORT
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
    it. ``sort`` is the same ``SORT_VALUES`` allowlist as ``/gripes``
    — the fragment re-lists under the picker's order, so the
    continuation URL carries it along."""
    if sort not in SORT_VALUES:
        return _unknown_sort(request, sort)
    store = get_store(request)
    allowed = _allowed_statuses(_status_counts(store))
    if status not in allowed:
        return _unknown_status(request, status, allowed)
    offset = max(offset, 0)
    rows = _rows(
        store, status_filter=status, sort=sort, limit=_ROWS_PAGE + 1, offset=offset
    )
    return templates.TemplateResponse(
        request,
        "gripes/_rows.html.j2",
        {
            "rows": rows[:_ROWS_PAGE],
            "rows_status": status,
            "next_offset": (offset + _ROWS_PAGE if len(rows) > _ROWS_PAGE else None),
            "append": offset > 0,
            "sort_qs": _sort_qs(sort),
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


def _filing_origin(store: Store, meta: Any, tags: Any) -> dict[str, Any]:
    """Page-safe origin, with existing read-only tags when no stamp exists."""
    stored = meta.get("filed_by") if isinstance(meta, dict) else None
    origin: dict[str, Any] = {}
    if isinstance(stored, dict) and stored:
        origin = {
            key: value
            for key, value in stored.items()
            if isinstance(value, str | int) and value
        }
        run_id = origin.get("agentlog_id")
        if isinstance(run_id, int):
            model = agentlog_model(store.pool, run_id)
            if model:
                origin.update(model=model, model_source="agentlog")
    else:
        origin["tags"] = [
            str(tag)
            for tag in tags
            if str(tag).startswith("agent:") or str(tag) == "source:agent_ro"
        ]
    return origin


@router.get("/{id}", response_class=HTMLResponse)
async def detail(request: Request, id: int) -> HTMLResponse:
    """Title + body + comment timeline + status controls + comment box."""
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
            # Author + UTC stamp (gr549420) — the same data the MCP
            # ``view='comments'`` header renders. ``meta.authored_by``
            # is stamped by ``tl.append_comment`` from the revision
            # context; 'agent' covers rows written before it existed.
            comments.append(
                {
                    "pos": b.ord,
                    "text": b.text or "",
                    "author": (getattr(b, "meta", None) or {}).get("authored_by")
                    or "agent",
                    "stamp": _comment_stamp(getattr(b, "created_at", None)),
                }
            )

    title = first_line(ref.title or "")
    body_first, _, body_rest = body_text.lstrip().partition("\n")
    if title and body_first.strip() == title:
        body_text = body_rest.lstrip("\r\n")

    tags = store.tags_for(ref.id)
    status = _status_of(tags)
    origin = _filing_origin(store, getattr(ref, "meta", None), tags)
    try:
        footer = trust_footer(store, "gripe", ref.id)
    except Exception:
        log.debug("gripe trust stamp unavailable", exc_info=True)
        footer = None
    score: float | None = None
    with store.pool.connection() as conn:
        row = conn.execute(
            f"SELECT round(({_dscore.score_sql()})::numeric, 2) "
            "FROM refs r WHERE r.ref_id = %s",
            (id,),
        ).fetchone()
        if row and row[0] is not None:
            score = float(row[0])

    return templates.TemplateResponse(
        request,
        "gripes/detail.html.j2",
        {
            "active_tab": "gripes",
            "id": ref.id,
            "title": title,
            "created": getattr(ref, "created_at", None),
            "updated": getattr(ref, "updated_at", None),
            "set_by": getattr(ref, "set_by", None),
            "filing_origin": origin,
            "trust_stamp": footer.stamp if footer else None,
            "body": body_text,
            "comments": comments,
            "status": status,
            "status_values": STATUS_VALUES,
            "status_badge": _status_badge(status),
            "retired": getattr(ref, "retired_at", None) is not None,
            "prio": _prio_of(tags),
            "score": score,
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
