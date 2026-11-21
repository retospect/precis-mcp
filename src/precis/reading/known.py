"""Atomic manual mastery and owner-scoped undo for the card review pages."""

from __future__ import annotations

from datetime import UTC
from typing import TYPE_CHECKING, Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from precis.anki.notes import ANKI_SYNC_LOCK
from precis.reading.concepts import STATE_MASTERED

if TYPE_CHECKING:
    from psycopg import Connection

    from precis.store.store import Store
    from precis.users import WebUser

_MASTERY_KEYS = ("mastery", "state", "mastery_updated_at")


def _lock_sync(conn: Connection, login: str) -> None:
    # Use the worker's lock so a sync cannot observe half a retirement or
    # remove a note after the next sync has already picked up its restoration.
    conn.execute(
        "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (ANKI_SYNC_LOCK, login)
    )


def _lock_concept(conn: Connection, concept_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT meta FROM refs WHERE ref_id = %s AND kind = 'concept' "
        "AND retired_at IS NULL FOR UPDATE",
        (concept_id,),
    ).fetchone()
    return row[0] or {} if row else None


def _concept_cards(conn: Connection, concept_id: int, owner: str) -> list[int]:
    rows = conn.execute(
        """
        SELECT a.ref_id FROM refs a
        WHERE a.kind = 'anki' AND a.retired_at IS NULL AND a.owner_login = %s
          AND EXISTS (
            SELECT 1 FROM links l
            WHERE (l.dst_ref_id = a.ref_id AND l.src_ref_id = %s AND l.relation = 'represents')
               OR (l.src_ref_id = a.ref_id AND l.dst_ref_id = %s AND l.relation = 'represented-by')
          )
        ORDER BY a.ref_id FOR UPDATE OF a
        """,
        (owner, concept_id, concept_id),
    ).fetchall()
    return [int(row[0]) for row in rows]


def mark_known(
    store: Store, user: WebUser, concept_ids: list[int], *, require_cards: bool
) -> int:
    retired = 0
    with store.tx() as conn:
        _lock_sync(conn, user.login)
        stamp = conn.execute("SELECT now()").fetchone()
        assert stamp is not None
        at = stamp[0].astimezone(UTC).isoformat()
        for cid in sorted(set(concept_ids)):
            meta = _lock_concept(conn, cid)
            if meta is None:
                continue
            cards = _concept_cards(conn, cid, user.login)
            if require_cards and not cards:
                continue
            # The first owner's snapshot is the baseline until the last
            # owner undoes their dismissal. Subsequent clicks keep it intact.
            earlier = conn.execute(
                "SELECT prior_mastery FROM anki_known_undo WHERE concept_id = %s LIMIT 1",
                (cid,),
            ).fetchone()
            baseline = (
                earlier[0]
                if earlier
                else {key: meta[key] for key in _MASTERY_KEYS if key in meta}
            )
            for card_id in cards:
                store.retire_ref(card_id, conn=conn)
            conn.execute(
                """
                INSERT INTO anki_known_undo (
                    concept_id, user_id, token, prior_mastery, card_retirements
                ) VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (user_id, concept_id) DO UPDATE SET
                    card_retirements = anki_known_undo.card_retirements || EXCLUDED.card_retirements
                """,
                (
                    cid,
                    user.id,
                    uuid4(),
                    Jsonb(baseline),
                    Jsonb(dict.fromkeys(map(str, cards), at)),
                ),
            )
            store.update_ref(
                cid,
                meta_patch={
                    "mastery": 1.0,
                    "mastery_updated_at": at,
                    "state": STATE_MASTERED,
                },
                conn=conn,
            )
            retired += len(cards)
    return retired


def undo_known(store: Store, user: WebUser, concept_id: int, token: UUID) -> bool:
    with store.tx() as conn:
        _lock_sync(conn, user.login)
        if _lock_concept(conn, concept_id) is None:
            return False
        receipt = conn.execute(
            """
            DELETE FROM anki_known_undo WHERE concept_id = %s AND user_id = %s AND token = %s
            RETURNING prior_mastery, card_retirements
            """,
            (concept_id, user.id, token),
        ).fetchone()
        if receipt is None:
            return False
        baseline, retirements = receipt
        # The retirement timestamp guards against an independently restored
        # card being dropped again. A prior drop never enters the receipt.
        rows = conn.execute(
            """
            SELECT ref_id FROM refs
            WHERE kind = 'anki' AND owner_login = %s AND ref_id = ANY(%s)
              AND retired_at = (%s::jsonb ->> ref_id::text)::timestamptz
            ORDER BY ref_id FOR UPDATE
            """,
            (user.login, list(map(int, retirements)), Jsonb(retirements)),
        ).fetchall()
        for (card_id,) in rows:
            store.restore_ref(card_id, conn=conn)
            store.update_ref(
                card_id, meta_patch={"anki_restore_pending": True}, conn=conn
            )
        remaining = conn.execute(
            "SELECT 1 FROM anki_known_undo WHERE concept_id = %s LIMIT 1",
            (concept_id,),
        ).fetchone()
        if remaining is None:
            conn.execute(
                "UPDATE refs SET meta = (meta - %s::text[]) || %s, updated_at = now() "
                "WHERE ref_id = %s",
                (list(_MASTERY_KEYS), Jsonb(baseline), concept_id),
            )
    return True


def known_undo_entries(store: Store, user: WebUser) -> list[tuple[int, str, UUID]]:
    with store.pool.connection() as conn:
        return [
            (int(cid), str(name), token)
            for cid, name, token in conn.execute(
                """
                SELECT c.ref_id, COALESCE(c.meta->>'name', 'cn' || c.ref_id), u.token
                FROM anki_known_undo u JOIN refs c ON c.ref_id = u.concept_id
                WHERE u.user_id = %s AND c.retired_at IS NULL
                ORDER BY lower(c.meta->>'name'), c.ref_id
                """,
                (user.id,),
            ).fetchall()
        ]
