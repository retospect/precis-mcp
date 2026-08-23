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

Each row renders with its reading context so the operator can answer
in place: an ancestor breadcrumb, the draft passage the ask is about
(``meta.anchor`` or a ``dc``/``pe`` handle in the ask prose → focus
chunk + ±1 sibling paragraphs + a ``?focus=`` smartdraft deep link,
falling back to the project's ``draft-of`` document), and the prior
Q/A rounds parsed back out of the body — a re-ask badges as a
follow-up, not a context-free repeat. The row markup is the shared
``asks/_row.html.j2`` partial, reused by the Needs-you landing.

Each ask row carries an answer form. Submitting it (1) appends an
``Asked: <question>`` / ``Response: <answer>`` block to the todo body
via ``edit(mode='replace')`` — the question is restated because the
unlock strips the tag that carried it, and the planner prompt directs
the re-ticked agent to judge the answer and re-yield a follow-up
``ask-user:`` when it's inadequate — then (2) strips every
``ask-user`` tag on the todo so the doable rotation can pick it up
again.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from fastapi import APIRouter, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse

from precis.utils import handle_registry
from precis_web.deps import await_dispatch, get_store, redirect_or_error, templates
from precis_web.linkify import popover_chip

if TYPE_CHECKING:
    from precis.store.store import Store

router = APIRouter(prefix="/asks", tags=["asks"])

#: Draft/plan chunk handles named in ask prose (``dc123`` / ``pe45``) —
#: the "what passage is this about" signal when the todo carries no
#: ``meta.anchor``. Bare word-bounded form only, matching linkify's
#: reading of the same handles.
_HANDLE_RE = re.compile(r"\b(dc|pe)(\d+)\b")

#: Char clamp per inline context paragraph — enough to read the passage,
#: not the whole section (the draft link carries the full view).
_CTX_CLAMP = 420

#: Ancestor hops when hunting the section heading above a focus chunk.
_HEADING_HOPS = 6

#: Q/A exchange delimiter appended to the todo body by the answer route.
#: ``Response:`` is the token the planner prompt trains agents to look
#: for, so it must survive any reshaping of this block.
_QA_SPLIT_RE = re.compile(r"\n---\n(?=(?:Asked|Response):)")


def _clamp(text: str, cap: int = _CTX_CLAMP) -> str:
    """One-line, char-capped rendering of a context paragraph."""
    flat = " ".join((text or "").split())
    return flat if len(flat) <= cap else flat[:cap].rstrip() + "…"


def _qa_history(body: str) -> tuple[str, list[dict[str, str]]]:
    """Split a todo body into (base text, prior Q/A exchanges).

    The answer route appends ``---\\nAsked: <q>\\nResponse: <a>`` per
    answered ask (legacy entries carry only the ``Response:`` line), so
    the exchanges parse back out here — the row shows the discussion so
    far and a re-ask reads as a follow-up, not a context-free repeat.
    """
    parts = _QA_SPLIT_RE.split(body or "")
    history: list[dict[str, str]] = []
    for seg in parts[1:]:
        seg = seg.strip()
        q, a = "", ""
        m = re.match(r"Asked:\s*(.*?)(?:\nResponse:\s*(.*))?\Z", seg, re.DOTALL)
        if m:
            q, a = m.group(1).strip(), (m.group(2) or "").strip()
        elif seg.startswith("Response:"):
            a = seg[len("Response:") :].strip()
        else:  # pragma: no cover - defensive; split guarantees a prefix
            a = seg
        if q or a:
            history.append({"question": q, "answer": a})
    return parts[0].rstrip(), history


def _crumb(store: Store, ref_id: int, *, max_depth: int = 8) -> list[dict[str, Any]]:
    """Ancestor chain of the ask's todo (root-first), for the breadcrumb.

    One recursive query up ``refs.parent_id`` — the project root lands
    first so the crumb reads ``project › … › parent``. The ask's own
    todo is excluded (its title already heads the row).
    """
    with store.pool.connection() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE chain AS (
                SELECT r.parent_id, 0 AS depth FROM refs r
                 WHERE r.ref_id = %s AND r.deleted_at IS NULL
                UNION ALL
                SELECT r.parent_id, c.depth + 1 FROM refs r
                  JOIN chain c ON r.ref_id = c.parent_id
                 WHERE r.deleted_at IS NULL AND c.depth < %s
            )
            SELECT r.ref_id, r.kind, r.title, c.depth
              FROM chain c JOIN refs r ON r.ref_id = c.parent_id
             WHERE r.deleted_at IS NULL
             ORDER BY c.depth DESC
            """,
            (ref_id, max_depth),
        ).fetchall()
    return [
        {
            "id": int(rid),
            "kind": str(kind),
            "title": (title or "").split("\n", 1)[0][:80] or f"#{rid}",
        }
        for rid, kind, title, _depth in rows
    ]


def _project_draft(store: Store, ancestor_ids: list[int]) -> dict[str, str] | None:
    """The draft bound ``draft-of`` to any ancestor of the ask's todo —
    the document an ask belongs to even when no specific chunk is named.

    Matches the whole ancestor chain, not just the presumed root: the
    crumb walk is depth-capped, so on a deep tree its first entry may
    not be the project the draft is actually bound to."""
    if not ancestor_ids:
        return None
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT l.src_ref_id FROM links l
              JOIN refs r ON r.ref_id = l.src_ref_id
             WHERE l.dst_ref_id = ANY(%s) AND l.relation = 'draft-of'
               AND r.deleted_at IS NULL
             LIMIT 1
            """,
            (ancestor_ids,),
        ).fetchone()
    if row is None:
        return None
    # ``refs`` has no ``slug`` column — the agent-facing slug lives in
    # ``ref_identifiers`` and only the Ref mapper knows how to source it,
    # so resolve the row instead of projecting a column that isn't there.
    rid = int(row[0])
    doc = store.fetch_refs_by_ids([rid]).get(rid)
    slug = getattr(doc, "slug", None) if doc is not None else None
    title = (getattr(doc, "title", None) or "").split("\n", 1)[0][:120]
    return {
        "title": title or f"draft #{rid}",
        "url": f"/smartdraft/{slug or rid}",
    }


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _chunk_context(store: Any, *, anchor: str, prose: str) -> dict[str, Any] | None:
    """Inline reading context for an ask: the draft passage it is about.

    The focus chunk comes from the todo's ``meta.anchor`` (change-request
    todos) or, failing that, the first ``dc``/``pe`` handle named in the
    ask prose. Around it: the ±1 sibling paragraphs (the reading window
    ``draft_relative_chunk_ids`` already models), the nearest ancestor
    heading as a section label, and a ``?focus=`` deep link into the
    smartdraft reader. ``None`` when nothing resolves — the row then
    falls back to the project-level draft crumb.
    """
    candidates: list[tuple[str, str]] = []  # (kind, handle)
    a = (anchor or "").strip()
    if a:
        parsed = handle_registry.parse(a)
        if parsed is not None and parsed[1] and parsed[0] in ("draft", "plan"):
            candidates.append((parsed[0], a))
        else:
            # Legacy ``¶<base58>`` / bare base-58 anchor — draft namespace;
            # get_draft_chunk bares the ``¶`` itself.
            candidates.append(("draft", a))
    for code, digits in _HANDLE_RE.findall(prose or ""):
        kind = "draft" if code == "dc" else "plan"
        candidates.append((kind, f"{code}{digits}"))

    focus = None
    kind = "draft"
    seen: set[str] = set()
    for kind_i, handle in candidates:
        if handle in seen:
            continue
        seen.add(handle)
        focus = store.drafts.get_draft_chunk(handle, kind=kind_i)
        if focus is not None:
            kind = kind_i
            break
    if focus is None:
        return None

    # ±1 sibling window around the focus — the surrounding paragraphs.
    before = after = None
    window = store.drafts.draft_relative_chunk_ids(f"{focus.dc}-1..1", kind=kind) or []
    idx = window.index(focus.chunk_id) if focus.chunk_id in window else -1
    code = "dc" if kind == "draft" else "pe"
    if idx > 0:
        before = store.drafts.get_draft_chunk(f"{code}{window[idx - 1]}", kind=kind)
    if 0 <= idx < len(window) - 1:
        after = store.drafts.get_draft_chunk(f"{code}{window[idx + 1]}", kind=kind)

    # Nearest ancestor heading — the § the passage sits under.
    section = ""
    cur = focus
    for _ in range(_HEADING_HOPS):
        if cur.parent_chunk_id is None:
            break
        parent = store.drafts.get_draft_chunk(f"{code}{cur.parent_chunk_id}", kind=kind)
        if parent is None:
            break
        if parent.chunk_kind == "heading":
            section = _clamp(parent.text, 120)
            break
        cur = parent

    doc = store.fetch_refs_by_ids([focus.ref_id]).get(focus.ref_id)
    slug = getattr(doc, "slug", None) if doc is not None else None
    title = (getattr(doc, "title", None) or "").split("\n", 1)[0][:120]
    url = (
        f"/smartdraft/{slug or focus.ref_id}?focus={focus.dc}"
        if kind == "draft"
        else f"/r/plan/{focus.ref_id}"
    )
    return {
        "draft": {"title": title or f"{kind} #{focus.ref_id}", "url": url},
        "section": section,
        "handle": focus.dc,
        "before": _clamp(before.text) if before is not None else "",
        "focus": _clamp(focus.text, 900),
        "after": _clamp(after.text) if after is not None else "",
    }


# store stays Any: tests pass a hand-rolled fake narrower than Store
def _ask_value(store: Any, ref_id: int, tag_value: str) -> str:
    """Turn an ``ask-user`` tag into the human question text.

    Strips the ``ask-user:`` prefix, then routes the value through
    ``store.drafts.resolve_ask_question`` so a ``see-chunk-N`` overflow redirect
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
    return store.drafts.resolve_ask_question(ref_id, tag_value[len(prefix) :])


def _load_asks(
    store: Store, *, limit: int = 100, offset: int = 0
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
            SELECT r.ref_id, r.title, r.created_at, r.meta->>'anchor',
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
             GROUP BY r.ref_id, r.title, r.created_at, r.meta->>'anchor'
             ORDER BY r.created_at DESC
             LIMIT %s OFFSET %s
            """,
            (limit, offset),
        ).fetchall()
    out: list[dict[str, Any]] = []
    for ref_id, title, created_at, anchor, ask_tags in rows:
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
        # Prior Q/A exchanges live in the body (the answer route appends
        # them) — a re-ask renders as a follow-up with its history.
        base, history = _qa_history(title or "")
        # The passage the ask is about: anchored chunk or a handle named
        # in the ask prose, with its surrounding paragraphs.
        prose = " ".join([base, *questions])
        context = _chunk_context(store, anchor=anchor or "", prose=prose)
        crumb = _crumb(store, rid)
        # No chunk-level context — at least name the document: the draft
        # bound to any ancestor this todo hangs under.
        if context is None and crumb:
            doc = _project_draft(store, [c["id"] for c in crumb])
            if doc is not None:
                context = {
                    "draft": doc,
                    "section": "",
                    "handle": "",
                    "before": "",
                    "focus": "",
                    "after": "",
                }
        out.append(
            {
                "id": rid,
                "title": title,
                "created_at": created_at,
                "questions": questions,
                "tags": raw_tags,
                "source_link": source_link,
                "context": context,
                "crumb": crumb,
                "history": history,
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
    # Record the question WITH the answer: the ask-user tags are stripped
    # on unlock, so without this line the question text is gone and a
    # later re-ask (or a human reading the todo) sees answers to nothing.
    # ``Response:`` stays the leading token contract the planner reads.
    asked = "; ".join(q for t in remove if (q := _ask_value(store, ref_id, t)))
    qa = (
        f"Asked: {asked}\nResponse: {answer_text}"
        if asked
        else (f"Response: {answer_text}")
    )
    new_text = f"{original.rstrip()}\n\n---\n{qa}"

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
