"""Owner-only Ask pages over persisted conversations and live turn progress."""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from secrets import token_hex
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from precis.store.types import ChunkRow, Ref
from precis_web import chat_turns
from precis_web.auth import AuthError, check_same_origin, current_user
from precis_web.deps import get_runtime, get_store, get_web_config, templates

router = APIRouter(prefix="/chat", tags=["chat"])
_LANE = "a"
_HEADERS = {"Cache-Control": "no-store"}


async def _owner(request: Request) -> str:
    user = current_user(request)
    if user is None or user.abbrev != get_web_config(request).owner:
        raise HTTPException(403, "Ask is available to the owner only.")
    try:
        check_same_origin(request.scope, dict(request.scope["headers"]))
    except AuthError as exc:
        raise HTTPException(403, str(exc)) from exc
    return user.abbrev


Owner = Annotated[str, Depends(_owner)]


def _conversation(request: Request, sid: str, owner: str) -> Ref:
    if not re.fullmatch(r"[A-Za-z0-9_-]+", sid):
        raise HTTPException(404, "Chat not found.")
    slug = f"chat/{sid}/{_LANE}"
    ref: Ref | None = get_store(request).get_ref(kind="conv", id=slug)
    if (
        ref is None
        or ref.slug != slug
        or ref.meta.get("chat_session") != sid
        or ref.meta.get("owner_abbrev") != owner
    ):
        raise HTTPException(404, "Chat not found.")
    return ref


def _chunks(request: Request, ref: Ref) -> list[ChunkRow]:
    chunks: list[ChunkRow] = get_store(request).chunks.list_chunks_for_ref(ref.id)
    return chunks


def _refusal(request: Request, sid: str, owner: str, text: str) -> str | None:
    if problems := chat_turns.preflight():
        return " ".join(problems)
    try:
        chat_turns.start_turn(sid, _LANE, owner, text, runtime=get_runtime(request))
    except PermissionError as exc:
        raise HTTPException(404, "Chat not found.") from exc
    except (RuntimeError, ValueError) as exc:
        return str(exc)
    return None


def _index(
    request: Request, owner: str, message: str | None = None, draft: str = ""
) -> Response:
    store = get_store(request)
    now = datetime.now(UTC)
    sessions: dict[str, tuple[str, float]] = {}
    for ref in store.list_refs(
        kind="conv", meta_has="chat_session", order_by="created_desc", limit=None
    ):
        sid = ref.meta.get("chat_session")
        if (
            not isinstance(sid, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", sid)
            or not (ref.slug or "").startswith(f"chat/{sid}/")
            or ref.meta.get("owner_abbrev") != owner
        ):
            continue
        title, cost = sessions.get(sid, (ref.title or sid, 0.0))
        sessions[sid] = (
            title,
            cost + chat_turns.day_cost(_chunks(request, ref), now.date()),
        )
    return templates.TemplateResponse(
        request,
        "chat/index.html.j2",
        {
            "active_tab": "chat",
            "sessions": sessions,
            "problems": chat_turns.preflight(),
            "message": message,
            "draft": draft,
        },
        headers=_HEADERS,
    )


@router.get("", response_class=HTMLResponse)
async def index(request: Request, owner: Owner) -> Response:
    return _index(request, owner)


@router.post("/new", response_class=HTMLResponse)
async def new_chat(
    request: Request, owner: Owner, text: Annotated[str, Form()] = ""
) -> Response:
    sid = f"{datetime.now(UTC):%Y%m%d-%H%M}-{token_hex(2)}"
    while get_store(request).get_ref(
        kind="conv", id=f"chat/{sid}/{_LANE}", include_deleted=True
    ):
        sid = f"{datetime.now(UTC):%Y%m%d-%H%M}-{token_hex(2)}"
    if message := _refusal(request, sid, owner, text):
        return _index(request, owner, message, text)
    return RedirectResponse(f"/chat/{sid}", status_code=303, headers=_HEADERS)


def _deletes(
    items: Iterable[dict[str, object]], turn: int
) -> Iterable[dict[str, str | int]]:
    for item in items:
        handle = item.get("id")
        kind = item.get("kind")
        if not isinstance(handle, (str, int)) or not handle:
            yield {
                "handle": "Unknown target",
                "href": "",
                "reason": str(item.get("reason") or "No reason given."),
                "turn": turn,
            }
            continue
        ident = quote(str(handle), safe="")
        href = (
            f"/refs/{quote(kind, safe='')}/{ident}"
            if isinstance(kind, str) and kind
            else f"/eye/{ident}"
        )
        yield {
            "handle": str(handle),
            "href": href,
            "reason": str(item.get("reason") or "No reason given."),
            "turn": turn,
        }


def _page(
    request: Request,
    sid: str,
    owner: str,
    ref: Ref,
    *,
    full: bool = False,
    message: str | None = None,
    draft: str = "",
    finished: chat_turns.ChatTurn | None = None,
) -> Response:
    chunks = _chunks(request, ref)
    pending = chat_turns.active_turn(f"chat/{sid}/{_LANE}", owner)
    tail = chunks[-1] if chunks else None
    # A completed turn normally appears in the transcript already. Keep the
    # registry result visible when the answer could not be saved.
    unsaved = (
        finished
        if finished
        and not any(chunk.meta.get("msg_id") == finished.token for chunk in chunks)
        else None
    )
    deletes = [
        item
        for chunk in chunks
        if chunk.meta.get("author") == "asa"
        for item in _deletes(chunk.meta.get("desired_deletes") or [], chunk.ord + 1)
    ]
    if live := pending or unsaved:
        deletes.extend(_deletes(live.desired_deletes, tail.ord + 2 if tail else 1))
    return templates.TemplateResponse(
        request,
        "chat/detail.html.j2" if full else "chat/content.html.j2",
        {
            "active_tab": "chat",
            "sid": sid,
            "ref": ref,
            "chunks": chunks,
            "tail": tail,
            "pending": pending,
            "unsaved": unsaved,
            "deletes": deletes,
            "spent": chat_turns.day_cost(chunks, datetime.now(UTC).date()),
            "cap": chat_turns.day_cap(),
            "problems": chat_turns.preflight(),
            "message": message,
            "draft": draft,
        },
        headers=_HEADERS,
    )


@router.get("/{sid}", response_class=HTMLResponse)
async def detail(request: Request, sid: str, owner: Owner) -> Response:
    return _page(request, sid, owner, _conversation(request, sid, owner), full=True)


def _submit(
    request: Request,
    sid: str,
    owner: str,
    ref: Ref,
    text: str,
    *,
    preserve_draft: bool = False,
) -> Response:
    message = _refusal(request, sid, owner, text)
    fragment = request.headers.get("HX-Request") == "true"
    if not fragment and message is None:
        return RedirectResponse(f"/chat/{sid}", status_code=303, headers=_HEADERS)
    return _page(
        request,
        sid,
        owner,
        ref,
        full=not fragment,
        message=message,
        draft=text if message and preserve_draft else "",
    )


@router.post("/{sid}/send", response_class=HTMLResponse)
async def send(
    request: Request, sid: str, owner: Owner, text: Annotated[str, Form()] = ""
) -> Response:
    return _submit(
        request,
        sid,
        owner,
        _conversation(request, sid, owner),
        text,
        preserve_draft=True,
    )


@router.post("/{sid}/continue", response_class=HTMLResponse)
async def continue_chat(request: Request, sid: str, owner: Owner) -> Response:
    ref = _conversation(request, sid, owner)
    chunks = _chunks(request, ref)
    if not chunks or chunks[-1].meta.get("status") != "capped":
        return _page(
            request,
            sid,
            owner,
            ref,
            full=request.headers.get("HX-Request") != "true",
            message="The latest turn is no longer capped.",
        )
    return _submit(request, sid, owner, ref, "Continue.")


@router.post("/{sid}/retry", response_class=HTMLResponse)
async def retry(request: Request, sid: str, owner: Owner) -> Response:
    ref = _conversation(request, sid, owner)
    chunks = _chunks(request, ref)
    last_human = next(
        (chunk for chunk in reversed(chunks) if chunk.meta.get("author") == owner), None
    )
    if last_human and (
        last_human is chunks[-1] or chunks[-1].meta.get("status") == "paused"
    ):
        return _submit(request, sid, owner, ref, last_human.text)
    return _page(
        request,
        sid,
        owner,
        ref,
        full=request.headers.get("HX-Request") != "true",
        message="There is no interrupted or paused turn to retry.",
    )


@router.get("/{sid}/turn/{token}", response_class=HTMLResponse)
async def poll(request: Request, sid: str, token: str, owner: Owner) -> Response:
    ref = _conversation(request, sid, owner)
    turn = chat_turns.get_turn(token, owner)
    if turn is None:
        if not any(
            chunk.meta.get("msg_id") == token for chunk in _chunks(request, ref)
        ):
            raise HTTPException(404, "Turn not found.")
    elif turn.conv_id != ref.id:
        raise HTTPException(404, "Turn not found.")
    return _page(
        request,
        sid,
        owner,
        ref,
        finished=turn if turn and turn.status != "running" else None,
    )


@router.post("/{sid}/turn/{token}/stop", response_class=HTMLResponse)
async def stop(request: Request, sid: str, token: str, owner: Owner) -> Response:
    ref = _conversation(request, sid, owner)
    turn = chat_turns.get_turn(token, owner)
    if turn is None or turn.conv_id != ref.id:
        raise HTTPException(404, "Turn not found.")
    chat_turns.stop_turn(token)
    if request.headers.get("HX-Request") != "true":
        return RedirectResponse(f"/chat/{sid}", status_code=303, headers=_HEADERS)
    return _page(
        request, sid, owner, ref, finished=turn if turn.status != "running" else None
    )
