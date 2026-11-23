"""``/anki`` — the card-forge review page (gr548276).

The AnkiWeb credentials form on ``/account`` connects the sync; this page is
the *review* half: what the forge minted overnight, a fast "I know this" to
retire a concept's cards and mark it mastered, a manual queue of terms to
teach, and the daily mint cap for heavy mornings.

* ``GET  /anki``        — counts header + forge cards grouped by concept
                          (newest first, ``meta.mastery``/``state`` shown),
                          the queue textarea, and the cap form.
* ``POST /anki/known``  — checkbox batch: soft-delete every live card the
                          concept ``represents`` (the forge's retire path —
                          the next ``anki-sync`` removes the note from the
                          mirror) and set ``meta.mastery=1.0`` /
                          ``state='mastered'``.
* ``POST /anki/undo-known`` — restore the signed-in owner's dismissal.
* ``POST /anki/drop``   — the same retire path on one bad card.
* ``POST /anki/boost``  — stamp ``meta.anki_boost_due`` on one live card;
                          the next ``anki_sync`` tick brings the mirror
                          card's due date forward to today (never review
                          history) and clears the marker once a card moves.
* ``POST /anki/queue``  — one term per line: an existing concept/taxon named
                          it (case-insensitive) is tagged ``twin:asked``;
                          otherwise a ``twin:asked`` concept is minted. The
                          next ``card_forge`` mints asked concepts first.
* ``POST /anki/highlight`` — the base-template "Card this" chip (gr743234):
                          ``term`` + the 2–2000 char selection as ``context``
                          + the page's record handle as ``ref``. Owner-scoped
                          by login. The selection supplies forge context,
                          ``meta.source`` stamps
                          ``"highlight"``, and the page's ref gains a
                          ``related-to`` edge. JSON answers the chip's
                          fetch; a plain form post falls back to ``/anki``.
* ``POST /anki/highlight/undo`` — the toast's Undo, bound to the
                          ``submission`` token the queue POST issued for
                          this owner: consumed once, a foreign or stale
                          token undoes nothing; a queue-minted concept is
                          retired once its last ask leaves if no live card
                          represents it.
* ``POST /anki/cap``    — write ``app_settings`` key
                          ``reading.cards_per_day`` (or the dated
                          ``reading.cards_per_day:<YYYY-MM-DD>`` for
                          "today only"); the forge reads dated → setting →
                          ``PRECIS_READING_CARDS_PER_DAY``.

Cards are per-login (``refs.owner_login``, migration 0164), so everything
here is scoped to the signed-in user — same 503-when-auth-off contract as
``/favorites`` and ``/account``. Writes go straight to the store (not the
dispatch ledger), like ``/account``'s own-row writes.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from uuid import UUID

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from precis.reading.asked import queue_asked_term, unqueue_asked_term
from precis.reading.concepts import STATE_MASTERED
from precis.reading.flashcards import CARDS_PER_DAY_KEY, effective_cards_per_day
from precis.reading.known import known_undo_entries, mark_known, undo_known
from precis.users import WebUser
from precis.utils import handle_registry
from precis_web.auth import current_user
from precis_web.deps import get_store, templates

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/anki", tags=["anki"])

_FORGE_AUTHOR = "card_forge"
_FOREIGN_SOURCE = "anki-foreign"


def _require_user(request: Request) -> WebUser:
    """The signed-in user, or a 503 explaining why there isn't one.

    Same contract as ``favorites._require_user``: cards are owned rows and
    every write here keys on the login, so with auth off there is nothing
    safe to show or mutate.
    """
    user = current_user(request)
    if user is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "HTTP auth is disabled (PRECIS_WEB_AUTH), so this server "
                "has no signed-in user whose Anki cards this page could "
                "act on."
            ),
        )
    return user


def _counts(store: Store, owner: str) -> dict[str, int]:
    """The header line: this login's card totals plus the concept graph's
    mastered/active split (concepts are a shared graph, not per-user rows)."""
    out = {"cards": 0, "forge": 0, "leeches": 0, "mastered": 0, "active": 0}
    with store.pool.connection() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS total,
                   COUNT(*) FILTER (
                       WHERE meta->>'authored_by' = %s) AS forge,
                   COUNT(*) FILTER (
                       WHERE meta ? 'anki_stats'
                         AND ((meta->'anki_stats'->>'lapses_total')::int >= 4
                          OR (meta->'anki_stats'->>'ease_min')::float <= 2.0)
                   ) AS leeches
            FROM refs
            WHERE kind = 'anki' AND retired_at IS NULL AND owner_login = %s
            """,
            (_FORGE_AUTHOR, owner),
        ).fetchone()
        crow = conn.execute(
            """
            SELECT COUNT(*) FILTER (WHERE meta->>'state' = %s) AS mastered,
                   COUNT(*) FILTER (
                       WHERE COALESCE(meta->>'state', 'candidate') != %s
                   ) AS active
            FROM refs WHERE kind = 'concept' AND retired_at IS NULL
            """,
            (STATE_MASTERED, STATE_MASTERED),
        ).fetchone()
    if row:
        out.update(cards=int(row[0]), forge=int(row[1]), leeches=int(row[2]))
    if crow:
        out.update(mastered=int(crow[0]), active=int(crow[1]))
    return out


def _memory_link(match: Any) -> dict[str, Any] | None:
    """The ``meta.memory_match`` stamp as a template link, from meta alone
    (gr743229): ``None`` for a miss or an unstamped concept."""
    if not isinstance(match, dict) or not isinstance(match.get("ref_id"), int):
        return None
    kind = "me" if match.get("kind") != "finding" else "fi"
    return {
        "href": f"/eye/{kind}{match['ref_id']}",
        "label": f"{kind}{match['ref_id']} {match.get('title') or ''}".strip(),
    }


def _card_groups(
    store: Store, owner: str, *, forge_only: bool, ungrouped: bool = False
) -> list[dict[str, Any]]:
    """Live precis-authored cards, grouped by their live ``represents``
    concept — newest card first inside each group, groups ordered by their
    newest card. ``(concept, mastery, state, memory, [(card_id, cloze_text, boosted), …])``.

    ``forge_only=True`` keeps the forge cohort (``meta.authored_by =
    'card_forge'``) for ``/anki``. ``forge_only=False`` — ``/learning``'s
    Current context (gr742681) — lists every card authored in precis:
    forge, agent-put, or hand-made. The authored bar is the sync worker's
    own (``precis.anki.notes.spec_from_ref``): neither an ``anki-foreign``
    source nor ``meta.readonly`` — a card the sync will never push must
    not sit here offering drop/boost as if it mirrored.

    ``ungrouped=True`` appends a trailing pseudo-group (``ungrouped=True``,
    ``concept_id=None``) holding the listed cards that have no live
    concept at all (gr742735) — /learning's orphan shelf. The caller's
    template must not offer "I know this" on it: there is no concept to
    mark mastered.

    One query covers the grouped and ungrouped halves on a single
    snapshot — two reads could each miss a card that gained a live
    concept link between them. A card is shelved only when every join to
    a live concept came back NULL: a ``represents``/``represented-by``
    link to a *retired* concept counts as no live concept.

    ``memory`` is the stamped nearest research memory from
    ``concept.meta.memory_match`` (gr743229) — a link dict or ``None``;
    it is rendered from meta alone, never searched at page load. A stamped
    ``{"ref_id": None}`` miss is never revisited: rework only rewrites card
    refs, so a miss stays a miss until the concept is re-forged; the 0.45
    similarity bar is unmeasured (see the forge mint) with an env override.

    ``boosted`` mirrors ``meta.anki_boost_due``: a queued bring-forward the
    next ``anki_sync`` tick applies, so a list row can show "boost queued"
    instead of offering the same boost again."""
    sql = """
        SELECT a.ref_id,
               a.meta->'fields'->>'Text',
               a.meta->>'anki_boost_due',
               c.ref_id,
               c.meta->>'name',
               c.meta->>'mastery',
               c.meta->>'state',
               c.meta->'memory_match'
        FROM refs a
        LEFT JOIN links l ON (
                 (l.dst_ref_id = a.ref_id AND l.relation = 'represents')
              OR (l.src_ref_id = a.ref_id AND l.relation = 'represented-by'))
        LEFT JOIN refs c ON c.kind = 'concept' AND c.retired_at IS NULL
                   AND c.ref_id = CASE WHEN l.src_ref_id = a.ref_id
                                       THEN l.dst_ref_id ELSE l.src_ref_id END
        WHERE a.kind = 'anki' AND a.retired_at IS NULL
          AND a.owner_login = %s
          AND COALESCE(a.meta->>'source', '') != %s
          AND NOT COALESCE((a.meta->>'readonly')::boolean, FALSE)
          AND (a.meta->>'authored_by' = %s OR NOT %s)
        ORDER BY a.created_at DESC, a.ref_id DESC, c.ref_id
    """
    cards: dict[int, dict[str, Any]] = {}
    with store.pool.connection() as conn:
        for (
            card_id,
            cloze,
            boost_due,
            cid,
            name,
            mastery,
            state,
            match,
        ) in conn.execute(
            sql, (owner, _FOREIGN_SOURCE, _FORGE_AUTHOR, forge_only)
        ).fetchall():
            card = cards.setdefault(
                int(card_id),
                {
                    "id": int(card_id),
                    "text": cloze or "",
                    "boosted": bool(boost_due),
                    "concepts": {},
                },
            )
            if cid is not None:
                card["concepts"][int(cid)] = (name, mastery, state, match)

    groups: dict[int, dict[str, Any]] = {}
    shelf: list[dict[str, Any]] = []
    for card in cards.values():
        concepts = card.pop("concepts")
        if not concepts:
            shelf.append(card)
            continue
        for cid, (name, mastery, state, match) in concepts.items():
            g = groups.setdefault(
                cid,
                {
                    "concept_id": cid,
                    "name": name or f"cn{cid}",
                    "mastery": float(mastery) if mastery is not None else 0.0,
                    "state": state or "candidate",
                    "memory": _memory_link(match),
                    "ungrouped": False,
                    "cards": [],
                },
            )
            g["cards"].append(card)
    out = list(groups.values())
    if ungrouped and shelf:
        out.append(
            {
                "concept_id": None,
                "name": "No live concept",
                "mastery": 0.0,
                "state": "ungrouped",
                "memory": None,
                "ungrouped": True,
                "cards": shelf,
            }
        )
    return out


def _render(
    request: Request,
    *,
    user: WebUser,
    error: str = "",
    notice: str = "",
) -> HTMLResponse:
    store = get_store(request)
    today = datetime.now(UTC).date()
    return templates.TemplateResponse(
        request,
        "anki/index.html.j2",
        {
            "active_tab": "account",
            "user": user,
            "error": error,
            "notice": notice,
            "counts": _counts(store, user.login),
            "groups": _card_groups(store, user.login, forge_only=True),
            "undo_entries": known_undo_entries(store, user),
            "return_to": "anki",
            "cap": effective_cards_per_day(store),
            "dated_key": f"{CARDS_PER_DAY_KEY}:{today.isoformat()}",
            "today": today.isoformat(),
        },
        headers={"Cache-Control": "no-store"},
    )


@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request, queued: int = 0, retired: int = 0, saved: str = ""
) -> HTMLResponse:
    user = _require_user(request)
    notice = ""
    if queued:
        notice = f"Queued {queued} term(s) — the forge mints asked concepts first."
    elif retired:
        notice = f"Retired {retired} card(s); the next sync removes them."
    elif saved == "undo-known":
        notice = "Dismissal undone. Restored cards will sync back to Anki."
    elif saved:
        notice = "Saved."
    return _render(request, user=user, notice=notice)


def _review_redirect(request: Request, path: str) -> Response:
    if request.headers.get("HX-Request") == "true":
        return Response(headers={"HX-Redirect": path, "Cache-Control": "no-store"})
    return RedirectResponse(
        path, status_code=303, headers={"Cache-Control": "no-store"}
    )


@router.post("/known", response_class=HTMLResponse)
async def known(request: Request) -> Response:
    """Retire selected cards and save an owner-bound undo in one transaction."""
    user = _require_user(request)
    form = await request.form()
    ids = [
        int(v)
        for v in form.getlist("concept")
        if (
            isinstance(v, str)
            and v.isascii()
            and v.isdecimal()
            and len(v) <= 19
            and 0 < int(v) < 2**63
        )
    ]
    if not ids:
        return await run_in_threadpool(
            _render, request, user=user, error="Nothing selected."
        )
    learning = form.get("return_to") == "learning"
    retired = await run_in_threadpool(
        mark_known, get_store(request), user, ids, require_cards=learning
    )
    path = "/learning" if learning else f"/anki?retired={retired}"
    return _review_redirect(request, path)


@router.post("/undo-known", response_class=HTMLResponse)
def restore_known(
    request: Request,
    concept: int = Form(..., gt=0, lt=2**63),
    token: UUID = Form(...),
    return_to: str = Form("anki"),
) -> Response:
    user = _require_user(request)
    restored = undo_known(get_store(request), user, concept, token)
    path = "/learning" if return_to == "learning" else "/anki"
    if restored:
        path += "?saved=undo-known"
    return _review_redirect(request, path)


@router.post("/drop", response_class=HTMLResponse)
async def drop(request: Request, card: int = Form(...)) -> Response:
    """Retire a single card. Only this login's live cards — a posted id
    naming someone else's (or a non-anki) ref is a 404, never a write."""
    user = _require_user(request)
    store = get_store(request)
    ref = store.get_ref(kind="anki", id=card)
    if ref is None or getattr(ref, "owner_login", None) != user.login:
        raise HTTPException(status_code=404, detail=f"anki id={card} not found")
    store.retire_ref(card)
    if request.headers.get("HX-Request") == "true":
        return Response(status_code=200)
    return RedirectResponse("/anki?retired=1", status_code=303)


@router.post("/boost", response_class=HTMLResponse)
async def boost(request: Request, card: int = Form(...)) -> Response:
    """Queue one live card to be shown sooner: stamp ``meta.anki_boost_due``
    with today's date; the next ``anki_sync`` tick brings the mirror card's
    due date forward to that day (review history untouched) and clears the
    marker. Same owner guard as ``drop`` — a foreign id is a 404, never a
    write. Re-boosting an already-queued card just refreshes the date."""
    user = _require_user(request)
    store = get_store(request)
    ref = store.get_ref(kind="anki", id=card)
    if ref is None or getattr(ref, "owner_login", None) != user.login:
        raise HTTPException(status_code=404, detail=f"anki id={card} not found")
    store.update_ref(
        card,
        meta_patch={"anki_boost_due": datetime.now(UTC).date().isoformat()},
    )
    if request.headers.get("HX-Request") == "true":
        # Swap the button for the queued chip (``hx-swap="outerHTML"``).
        return templates.TemplateResponse(request, "anki/_boosted.html.j2", {})
    return RedirectResponse("/anki?saved=1", status_code=303)


@router.post("/queue", response_class=HTMLResponse)
async def queue(request: Request, terms: str = Form("")) -> Response:
    """Manual queue — one term per line into the ``twin:asked`` cohort."""
    user = _require_user(request)
    store = get_store(request)
    lines = [t.strip() for t in terms.splitlines() if t.strip()]
    if not lines:
        return _render(request, user=user, error="Enter at least one term.")
    queued = 0
    for term in lines:
        try:
            queue_asked_term(store, term, owner_login=user.login)
            queued += 1
        except Exception:
            log.warning("anki queue: failed on term %r", term, exc_info=True)
    if not queued:
        return _render(request, user=user, error="No term could be queued.")
    return RedirectResponse(f"/anki?queued={queued}", status_code=303)


#: Provenance stamp a highlight-queue mint carries in ``meta.source`` —
#: distinguishes the selection chip's cards from the textarea queue's.
_HIGHLIGHT_SOURCE = "highlight"

#: Bounds the chip already enforces client-side; the POST re-checks them
#: because a form body is not the chip.
_HIGHLIGHT_CONTEXT_MAX = 2000
_HIGHLIGHT_TERM_MAX = 200


def _form_str(form: Any, key: str) -> str:
    """One form field as a stripped string (``""`` when absent or a file)."""
    value = form.get(key)
    return value.strip() if isinstance(value, str) else ""


def _wants_json(request: Request) -> bool:
    """The chip's fetch asks for JSON; a plain form post gets the 303."""
    return "application/json" in request.headers.get("accept", "")


def _page_ref_id(store: Store, handle: str, *, owner: str) -> int | None:
    """ref_id for the record handle the chip posted, or ``None``.

    A chunk handle resolves to its containing record. The ``related-to``
    edge is decoration, so a missing or malformed handle skips it and the
    queue itself must never fail on it. The visibility check mirrors the
    detail readers (gr490266): a ``profile`` is owner-private, and a
    per-login ref another login owns is not this user's page either, so a
    forged post naming one links nothing.
    """
    parsed = handle_registry.parse(handle.strip())
    if parsed is None or parsed[0] == "profile":
        return None
    kind, is_chunk, pk = parsed
    try:
        if is_chunk:
            resolved = store.resolve_handle(handle)
            if resolved is None or resolved.kind == "profile":
                return None
            kind, pk = resolved.kind, resolved.ref_id
        ref = store.get_ref(kind=kind, id=pk)
    except Exception:
        log.debug("anki highlight: page ref lookup failed", exc_info=True)
        return None
    if ref is None:
        return None
    ref_owner = getattr(ref, "owner_login", None)
    if ref_owner is not None and ref_owner != owner:
        return None
    return int(ref.id)


@router.post("/highlight")
async def highlight(request: Request) -> Response:
    """Queue a term captured from page text (gr743234)."""
    user = _require_user(request)
    form = await request.form()
    term = " ".join(_form_str(form, "term").split())
    context = _form_str(form, "context")
    error = ""
    if not term:
        error = "Enter a term to card."
    elif len(term) > _HIGHLIGHT_TERM_MAX:
        error = f"terms are at most {_HIGHLIGHT_TERM_MAX} characters."
    elif not 2 <= len(context) <= _HIGHLIGHT_CONTEXT_MAX:
        error = f"context must be 2–{_HIGHLIGHT_CONTEXT_MAX} characters."
    if error:
        if _wants_json(request):
            return JSONResponse({"ok": False, "error": error}, status_code=400)
        return _render(request, user=user, error=error)
    store = get_store(request)
    try:
        out = await run_in_threadpool(
            queue_asked_term,
            store,
            term,
            owner_login=user.login,
            conv_ref_id=_page_ref_id(store, _form_str(form, "ref"), owner=user.login),
            definition=context,
            source=_HIGHLIGHT_SOURCE,
        )
    except Exception:
        log.warning("anki highlight: queue failed", exc_info=True)
        if _wants_json(request):
            return JSONResponse(
                {"ok": False, "error": "queue failed — try again"},
                status_code=500,
            )
        return _render(request, user=user, error="The term could not be queued.")
    if _wants_json(request):
        return JSONResponse({"ok": True, **out})
    return RedirectResponse("/anki?queued=1", status_code=303)


@router.post("/highlight/undo")
async def highlight_undo(
    request: Request,
    ref: int = Form(..., gt=0, lt=2**63),
    submission: str = Form("", max_length=64),
) -> Response:
    """Toast Undo for one highlight submission (gr743234). The token the
    queue POST issued binds the reversal to that one ask and this login —
    consumed once, so a replay or a foreign token undoes nothing."""
    user = _require_user(request)
    out = await run_in_threadpool(
        unqueue_asked_term,
        get_store(request),
        ref,
        submission=submission,
        owner_login=user.login,
    )
    if out is None:
        raise HTTPException(status_code=404, detail=f"ref id={ref} not found")
    return JSONResponse({"ok": True, **out})


@router.post("/cap", response_class=HTMLResponse)
async def cap(
    request: Request,
    per_day: str = Form(...),
    today_only: str = Form(""),
) -> Response:
    """Write the mint cap. ``today_only`` scopes the row to the dated key so
    tomorrow's pass falls back to the durable setting untouched."""
    from precis.budget import settings as app_settings

    user = _require_user(request)
    store = get_store(request)
    try:
        value = int(str(per_day).strip())
    except ValueError:
        return _render(request, user=user, error=f"{per_day!r} is not a whole number.")
    if value < 0 or value > 100:
        return _render(request, user=user, error="new cards per day must be 0–100.")
    key = CARDS_PER_DAY_KEY
    if today_only:
        key = f"{CARDS_PER_DAY_KEY}:{datetime.now(UTC).date().isoformat()}"
    try:
        app_settings.set_setting(store, key, str(value))
    except Exception as exc:
        log.warning("anki cap: app_settings write failed", exc_info=True)
        return _render(request, user=user, error=str(exc))
    return RedirectResponse("/anki?saved=1", status_code=303)


__all__ = ["router"]
