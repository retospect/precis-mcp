"""Asks tab — todos waiting on the user for input.

Surfaces open ``kind='todo'`` refs carrying an ``ask-user`` open tag.
The tag *value* carries the
question itself (``ask-user:<question>``), so this view renders the
question inline beneath the todo's title — no extra lookup needed
to see what's being asked.

This is the web mirror of ``search(kind='todo', view='ask-user')``.
The broader ``view='attention'`` union (child-failed parents, halts)
is intentionally not folded in here — those signals need an
operator decision but they're not "user input" in the literal sense.

Each ask row carries an answer form. Submitting it (1) appends the
operator's response to the todo body via ``edit(mode='replace')``
so the answer is preserved for the planner, then (2) strips every
``ask-user`` tag on the todo so the doable rotation can pick it up
again.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from precis_web.deps import await_dispatch, get_store, redirect_or_error, templates
from precis_web.linkify import popover_chip

router = APIRouter(prefix="/asks", tags=["asks"])


def _ask_value(store: Any, ref_id: int, tag_value: str) -> str:
    """Turn an ``ask-user`` tag into the human question text.

    Strips the ``ask-user:`` prefix, then routes the value through
    ``store.resolve_ask_question`` so a ``see-chunk-N`` overflow redirect
    (the form the tag takes when the question exceeds the 80-char tag cap)
    de-references to the real prose in the ``tag_overflow`` chunk — the
    reader must show the actual request, not the opaque ``see-chunk-0``
    slug (this is the draft-reader behaviour, mirrored here). Returns
    ``""`` for the prefix-less ``ask-user`` form — an "any human will do"
    marker with no inline question.
    """
    prefix = "ask-user:"
    if not tag_value.startswith(prefix):
        return ""
    return store.resolve_ask_question(ref_id, tag_value[len(prefix) :])


def _load_asks(
    store: Any, *, limit: int = 100, offset: int = 0
) -> list[dict[str, Any]]:
    """Open todos carrying ask-user tags. One row per todo.

    Aggregates tag values so multiple asks on the same todo collapse
    into one row carrying every question and every raw tag (the latter
    feeds the unlock form's hidden ``remove`` inputs). Closed todos
    (``done`` / ``won't-do``) are excluded — same filter the
    ``search(view='ask-user')`` SQL uses.

    Paginated via ``limit`` / ``offset`` (newest-first); the caller
    passes ``limit+1`` to probe for a next page.
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            SELECT r.ref_id, r.title, r.created_at,
                   array_agg(t.value ORDER BY t.value) AS ask_tags
              FROM refs r
              JOIN ref_tags rt ON rt.ref_id = r.ref_id
              JOIN tags t ON t.tag_id = rt.tag_id
             WHERE r.kind = 'todo' AND r.deleted_at IS NULL
               AND t.namespace = 'OPEN'
               AND (t.value = 'ask-user' OR t.value LIKE 'ask-user:%%')
               AND COALESCE(
                     (SELECT t2.value FROM ref_tags rt2
                        JOIN tags t2 ON t2.tag_id = rt2.tag_id
                       WHERE rt2.ref_id = r.ref_id
                         AND t2.namespace = 'STATUS' LIMIT 1),
                     'open'
                   ) NOT IN ('done', 'won''t-do')
             GROUP BY r.ref_id, r.title, r.created_at
             ORDER BY r.created_at DESC
             LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for ref_id, title, created_at, ask_tags in rows:
        raw_tags = [str(t) for t in (ask_tags or [])]
        questions = [
            q for q in (_ask_value(store, int(ref_id), t) for t in raw_tags) if q
        ]
        rid = int(ref_id)
        # The source object the question is about — this row's own todo.
        # Reuses the shared click-target resolver (``/r/{kind}/{id}``,
        # ``preview.py``) + hover-preview chip (``popover_chip``, the same
        # helper the Items/Tags-refs lists use) rather than hand-rolling a
        # new link, so the reader can jump straight to the todo's full
        # context (project, body, parent chain) instead of the generic
        # queue landing the row title used to point at.
        source_link = popover_chip(
            f"todo #{rid}", f"/r/todo/{rid}", f"/preview/todo/{rid}"
        )
        out.append(
            {
                "id": rid,
                "title": title,
                "created_at": created_at,
                "questions": questions,
                "tags": raw_tags,
                "source_link": source_link,
            }
        )
    return out


#: Rows per page on the asks queue.
_PAGE_SIZE = 50


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(request: Request, page: int = 1) -> HTMLResponse:
    """List todos that need a user response. Paged via ``?page=N``."""
    store = get_store(request)
    page = max(1, page)
    offset = (page - 1) * _PAGE_SIZE
    asks = _load_asks(store, limit=_PAGE_SIZE + 1, offset=offset)
    has_next = len(asks) > _PAGE_SIZE
    asks = asks[:_PAGE_SIZE]
    return templates.TemplateResponse(
        request,
        "asks/index.html.j2",
        {
            "active_tab": "asks",
            "asks": asks,
            "page": page,
            "has_next": has_next,
        },
    )


@router.post("/{ref_id}/answer")
async def answer(
    request: Request,
    ref_id: int,
    response: str = Form(...),
    remove: list[str] = Form(default=[]),
    next: str = Form(default=""),
) -> Response:
    """Append response to the todo body and clear its ask-user tags.

    Two-step dispatch so the answer is captured in the body *before*
    the unlock fires — if the edit fails the tags stay (the todo
    remains blocked). The ``remove`` list comes from hidden form
    inputs the index template emits per ask tag, so the submit path
    doesn't have to re-query.

    ``next`` (optional, same-origin path only) lets a caller other than
    the Asks tab — e.g. the draft reader's inline ask form — return the
    operator to where they answered instead of the global queue.
    """
    # Same-origin guard: only honour a relative path, never an absolute
    # URL (open-redirect) — fall back to the Asks queue otherwise.
    dest = next if next.startswith("/") and not next.startswith("//") else "/asks"
    answer_text = response.strip()
    if not answer_text:
        return RedirectResponse(url=dest, status_code=303)
    store = get_store(request)
    refs = store.fetch_refs_by_ids([ref_id])
    if ref_id not in refs:
        return RedirectResponse(url="/asks", status_code=303)
    original = refs[ref_id].title or ""
    new_text = f"{original.rstrip()}\n\n---\nResponse: {answer_text}"

    body, is_error = await await_dispatch(
        request,
        "edit",
        {"kind": "todo", "id": ref_id, "mode": "replace", "text": new_text},
    )
    if is_error:
        return templates.TemplateResponse(
            request,
            "error.html.j2",
            {"title": "Request error", "detail": body, "status": 400},
            status_code=400,
        )

    if remove:
        return await redirect_or_error(
            request,
            "tag",
            {"kind": "todo", "id": ref_id, "remove": list(remove)},
            redirect=dest,
        )

    return RedirectResponse(url=dest, status_code=303)


@router.post("/{ref_id}/terminate")
async def terminate(
    request: Request,
    ref_id: int,
    remove: list[str] = Form(default=[]),
) -> Response:
    """Dismiss an ask without answering — close the todo for good.

    The X on a row. One ``tag`` call flips the todo to
    ``STATUS:won't-do`` *and* strips every ``ask-user`` tag, so it
    leaves the asks queue and never re-enters the doable rotation.
    The ``remove`` list mirrors the answer form's hidden inputs.
    """
    return await redirect_or_error(
        request,
        "tag",
        {
            "kind": "todo",
            "id": ref_id,
            "add": ["STATUS:won't-do"],
            "remove": list(remove),
        },
        redirect="/asks",
    )
