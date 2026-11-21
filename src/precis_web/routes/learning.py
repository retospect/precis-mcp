"""Private study activity, current-context cards and optional check-ins.

The authenticated web account supplies the stable owner inside ``user_scope``;
form and query values never select an owner. The learning store revalidates
that account on every operation. These writes bypass generic ref dispatch so
private notes do not enter shared ref, tool-result or mastery ledgers.

``LearningPrivacyMiddleware`` sits outside the Basic gate, giving even an
authentication or validation refusal the page's no-store policy. The existing
Basic gate also checks Origin/Referer before any state-changing request.

The page's *current context* panel (gr664414) reuses the card grouping
(``precis_web.routes.anki._card_groups``): this login's live cards that
represent a concept, grouped by that concept — every card authored in
precis (forge, agent, or by hand), read-only Anki imports excepted
(gr742681). That read is owner-scoped by ``refs.owner_login`` and never
touches the private learning ledger; the card writes it offers
(drop / known / boost) are the ``/anki`` routes' own.
"""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import quote
from uuid import UUID, uuid4

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from starlette.concurrency import run_in_threadpool
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from precis.anki.cloze import has_cloze
from precis.errors import BadInput, Conflict, Denied, NotFound
from precis.eye_visits import WALK_LIMIT, EyeVisitStore
from precis.learning import LearningStore, timezone_choices
from precis.reading.known import known_undo_entries
from precis.user_context import user_scope
from precis.users import WebUser
from precis_web.auth import current_user
from precis_web.deps import get_store, templates
from precis_web.routes.anki import _card_groups

router = APIRouter(prefix="/learning", tags=["learning"])
_NO_STORE = {"Cache-Control": "no-store"}
log = logging.getLogger(__name__)


class LearningPrivacyMiddleware:
    """Prevent caching every response on the private learning route tree."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (
            path == "/learning" or path.startswith("/learning/")
        ):
            await self.app(scope, receive, send)
            return

        started = False

        async def private_send(message: Message) -> None:
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                message = dict(message)
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"cache-control"
                ]
                headers.append((b"cache-control", b"no-store"))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, private_send)
        except Exception as exc:
            if started:
                raise
            # Starlette's outer 500 handler runs outside user middleware.
            # Keep this failure private too, without logging a note-bearing
            # exception message or traceback from the underlying operation.
            log.error("learning request failed (%s)", type(exc).__name__)
            response = HTMLResponse(
                "<h1>Learning unavailable</h1><p>Please retry the learning page.</p>",
                status_code=500,
                headers=_NO_STORE,
            )
            await response(scope, receive, private_send)


def _refusal(request: Request, detail: str, status: int) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "error.html.j2",
        {
            "title": "Learning unavailable" if status == 503 else "Request error",
            "detail": detail,
            "status": status,
        },
        status_code=status,
        headers=_NO_STORE,
    )


def _walk_rows(request: Request) -> list[dict[str, Any]]:
    """This login's recent /eye steps, hrefs precomputed for the template.

    The handle is URL-quoted with the eye route's own ``safe`` set — focus
    handles carry ``~``/``:``/``#`` that a naive template ``urlencode``
    would mangle or leave raw."""
    rows = [
        {"href": f"/eye/{quote(step.handle, safe=':~')}", "step": step}
        for step in EyeVisitStore(get_store(request)).recent(limit=WALK_LIMIT)
    ]
    return rows


def _render(
    request: Request,
    *,
    user: WebUser,
    event: str = "",
    error: str = "",
    notice: str = "",
    status: int = 200,
) -> HTMLResponse:
    learning = LearningStore(get_store(request))
    settings = learning.settings()
    activity = learning.activity(days=84)
    # Live cards as the page's "current context" (gr664414, widened by
    # gr742681): owned, non-retired anki refs representing a concept —
    # forge-, agent-, or hand-authored — grouped by concept, the same
    # grouping /anki renders but without the forge-only filter. Cards
    # carry no private learning data, and the panel's per-row writes go
    # to the /anki routes.
    groups = _card_groups(get_store(request), user.login, forge_only=False)
    answered = request.query_params.get("answered", "")
    selected = None
    if event:
        try:
            event_id = int(event)
        except ValueError as exc:
            raise BadInput(
                "The check-in identifier must be a whole number.",
                next="Choose a recorded date from your activity calendar.",
            ) from exc
        selected = learning.get_event(event_id)
    return templates.TemplateResponse(
        request,
        "learning/index.html.j2",
        {
            "active_tab": "learning",
            "settings": settings,
            "activity": activity,
            "groups": groups,
            "walk": _walk_rows(request),
            "answered": answered,
            "attempt": uuid4(),
            "undo_entries": known_undo_entries(get_store(request), user),
            "return_to": "learning",
            "selected": selected,
            "timezones": timezone_choices(settings.timezone),
            "error": error,
            "notice": notice,
            "studied_key": uuid4().hex,
            "rest_key": uuid4().hex,
        },
        status_code=status,
        headers=_NO_STORE,
    )


def _error_status(exc: BadInput | Denied | NotFound) -> int:
    if isinstance(exc, NotFound):
        return 404
    if isinstance(exc, Denied):
        return 403
    return 409 if isinstance(exc, Conflict) else 400


def _optional_int(value: str, name: str) -> int | None:
    if not value.strip():
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise BadInput(
            f"{name} must be a whole number or empty.",
            next=f"Enter whole minutes for {name}, or leave it empty.",
        ) from exc


def _field(form: Any, name: str, default: str = "") -> str:
    value = form.get(name, default)
    if not isinstance(value, str):
        raise BadInput(
            f"{name} must be text.", next="Use the learning page's check-in form."
        )
    return value


def _page(request: Request, user: WebUser) -> HTMLResponse:
    with user_scope(user.id):
        try:
            saved = request.query_params.get("saved", "")
            notice = {
                "checkin": "Study check-in saved.",
                "undo-known": "Dismissal undone. Restored cards will sync back to Anki.",
                "settings": "Learning preferences saved.",
                "walk-cleared": "Your recent walk was cleared.",
            }.get(saved, "")
            return _render(
                request,
                user=user,
                event=request.query_params.get("event", ""),
                notice=notice,
            )
        except (BadInput, Denied, NotFound) as exc:
            return _refusal(request, str(exc), _error_status(exc))


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
def index(request: Request) -> HTMLResponse:
    user = current_user(request)
    if user is None:
        return _refusal(
            request,
            "Learning requires a signed-in account. Enable PRECIS_WEB_AUTH and sign in.",
            503,
        )
    return _page(request, user)


def _save(request: Request, user: WebUser, form: Any, *, settings: bool) -> Response:
    with user_scope(user.id):
        try:
            learning = LearningStore(get_store(request))
            if settings:
                learning.set_settings(
                    timezone=_field(form, "timezone"),
                    typical_session_minutes=_optional_int(
                        _field(form, "typical_session_minutes"),
                        "Typical session minutes",
                    ),
                )
            else:
                learning.checkin(
                    study_date=_field(form, "date") or None,
                    status=_field(form, "status"),
                    minutes=_optional_int(_field(form, "minutes"), "Minutes"),
                    note=_field(form, "note"),
                    idempotency_key=_field(form, "idempotency_key"),
                )
        except (BadInput, Denied, NotFound) as exc:
            return _refusal(request, str(exc), _error_status(exc))
    saved = "settings" if settings else "checkin"
    return RedirectResponse(
        f"/learning?saved={saved}", status_code=303, headers=_NO_STORE
    )


@router.post("/answer/{card_id}", response_class=HTMLResponse)
def answer(
    request: Request, card_id: int, attempt: UUID = Form(...), response: str = Form(...)
) -> Response:
    """Record a submitted recall attempt, discarding the response text."""
    user = current_user(request)
    if user is None:
        return _refusal(request, "Sign in before answering study cards.", 503)
    if not response.strip() or len(response) > 2000:
        return _refusal(request, "Enter a study answer of 1 to 2000 characters.", 400)
    store = get_store(request)
    ref = store.get_ref(kind="anki", id=card_id)
    if ref is None or ref.owner_login != user.login:
        return _refusal(request, "Study card not found.", 404)
    meta = ref.meta or {}
    fields = meta.get("fields")
    text = fields.get("Text") if isinstance(fields, dict) else None
    if (
        meta.get("source") == "anki-foreign"
        or not isinstance(text, str)
        or not has_cloze(text)
    ):
        return _refusal(request, "Study card not found.", 404)
    with user_scope(user.id):
        try:
            LearningStore(store).record_study_answer(card_id, attempt)
        except Denied as exc:
            return _refusal(request, str(exc), _error_status(exc))
    return RedirectResponse(
        f"/learning?answered={card_id}#study-card-{card_id}",
        status_code=303,
        headers=_NO_STORE,
    )


@router.post("/checkin", response_class=HTMLResponse)
async def checkin(request: Request) -> Response:
    user = current_user(request)
    if user is None:
        return _refusal(
            request, "Sign in with PRECIS_WEB_AUTH enabled before recording study.", 503
        )
    form = await request.form()
    return await run_in_threadpool(_save, request, user, form, settings=False)


@router.post("/settings", response_class=HTMLResponse)
async def settings(request: Request) -> Response:
    user = current_user(request)
    if user is None:
        return _refusal(
            request,
            "Sign in with PRECIS_WEB_AUTH enabled before saving preferences.",
            503,
        )
    form = await request.form()
    return await run_in_threadpool(_save, request, user, form, settings=True)


@router.post("/walk/clear", response_class=HTMLResponse)
async def walk_clear(request: Request) -> Response:
    user = current_user(request)
    if user is None:
        return _refusal(
            request,
            "Sign in with PRECIS_WEB_AUTH enabled before clearing your walk.",
            503,
        )
    return await run_in_threadpool(_clear_walk, request, user)


def _clear_walk(request: Request, user: WebUser) -> Response:
    with user_scope(user.id):
        try:
            EyeVisitStore(get_store(request)).clear()
        except (BadInput, Denied, NotFound) as exc:
            return _refusal(request, str(exc), _error_status(exc))
    return RedirectResponse(
        "/learning?saved=walk-cleared", status_code=303, headers=_NO_STORE
    )


__all__ = ["LearningPrivacyMiddleware", "router"]
