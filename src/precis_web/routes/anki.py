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
* ``POST /anki/drop``   — the same retire path on one bad card.
* ``POST /anki/queue``  — one term per line: an existing concept/taxon named
                          it (case-insensitive) is tagged ``twin:asked``;
                          otherwise a ``twin:asked`` concept is minted. The
                          next ``card_forge`` mints asked concepts first.
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

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.responses import Response

from precis.reading.asked import queue_asked_term
from precis.reading.concepts import STATE_MASTERED
from precis.reading.flashcards import CARDS_PER_DAY_KEY, effective_cards_per_day
from precis.users import WebUser
from precis_web.auth import current_user
from precis_web.deps import get_store, templates

if TYPE_CHECKING:
    from precis.store.store import Store

log = logging.getLogger(__name__)

router = APIRouter(prefix="/anki", tags=["anki"])

_FORGE_AUTHOR = "card_forge"


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


def _forge_groups(store: Store, owner: str) -> list[dict[str, Any]]:
    """Forge-authored live cards, grouped by their ``represents`` concept —
    newest card first inside each group, groups ordered by their newest
    card. ``(concept, mastery, state, [(card_id, cloze_text), …])``."""
    sql = """
        SELECT c.ref_id,
               c.meta->>'name',
               c.meta->>'mastery',
               c.meta->>'state',
               a.ref_id,
               a.meta->'fields'->>'Text'
        FROM refs a
        JOIN links l ON (
                 (l.dst_ref_id = a.ref_id AND l.relation = 'represents')
              OR (l.src_ref_id = a.ref_id AND l.relation = 'represented-by'))
        JOIN refs c ON c.kind = 'concept' AND c.retired_at IS NULL
                   AND c.ref_id = CASE WHEN l.src_ref_id = a.ref_id
                                       THEN l.dst_ref_id ELSE l.src_ref_id END
        WHERE a.kind = 'anki' AND a.retired_at IS NULL
          AND a.owner_login = %s
          AND a.meta->>'authored_by' = %s
        ORDER BY a.created_at DESC, a.ref_id DESC
    """
    groups: dict[int, dict[str, Any]] = {}
    with store.pool.connection() as conn:
        for cid, name, mastery, state, card_id, cloze in conn.execute(
            sql, (owner, _FORGE_AUTHOR)
        ).fetchall():
            g = groups.setdefault(
                int(cid),
                {
                    "concept_id": int(cid),
                    "name": name or f"cn{int(cid)}",
                    "mastery": float(mastery) if mastery is not None else 0.0,
                    "state": state or "candidate",
                    "cards": [],
                },
            )
            g["cards"].append({"id": int(card_id), "text": cloze or ""})
    return list(groups.values())


def _concept_cards(store: Store, concept_id: int, owner: str) -> list[int]:
    """Live ``anki`` ref ids ``owner`` has that render ``concept_id`` — the
    retire set for "I know this"."""
    sql = """
        SELECT a.ref_id FROM refs a
        JOIN links l ON (
                 (l.dst_ref_id = a.ref_id AND l.relation = 'represents')
              OR (l.src_ref_id = a.ref_id AND l.relation = 'represented-by'))
        WHERE a.kind = 'anki' AND a.retired_at IS NULL AND a.owner_login = %s
          AND CASE WHEN l.src_ref_id = a.ref_id
                   THEN l.dst_ref_id ELSE l.src_ref_id END = %s
    """
    with store.pool.connection() as conn:
        return [int(r[0]) for r in conn.execute(sql, (owner, concept_id)).fetchall()]


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
            "groups": _forge_groups(store, user.login),
            "cap": effective_cards_per_day(store),
            "cap_key": CARDS_PER_DAY_KEY,
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
    elif saved:
        notice = "Saved."
    return _render(request, user=user, notice=notice)


@router.post("/known", response_class=HTMLResponse)
async def known(request: Request) -> Response:
    """The "I know this" batch — retire the selected concepts' cards and mark the
    concepts mastered. Concept ids without cards still get the mastery
    stamp: the human said they know it, which is exactly what the field
    means."""
    user = _require_user(request)
    store = get_store(request)
    form = await request.form()
    ids = [
        int(v)
        for v in form.getlist("concept")
        if isinstance(v, str) and v.strip().isdigit()
    ]
    if not ids:
        return _render(request, user=user, error="Nothing selected.")
    retired = 0
    for cid in ids:
        for card_id in _concept_cards(store, cid, user.login):
            store.retire_ref(card_id)
            retired += 1
        store.update_ref(
            cid,
            meta_patch={
                "mastery": 1.0,
                "mastery_updated_at": datetime.now(UTC).isoformat(),
                "state": STATE_MASTERED,
            },
        )
    return RedirectResponse(f"/anki?retired={retired}", status_code=303)


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
    return RedirectResponse("/anki?retired=1", status_code=303)


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
        return _render(request, user=user, error="concepts per morning must be 0–100.")
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
