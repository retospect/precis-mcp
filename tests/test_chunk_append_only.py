"""Runtime enforcement of the chunk append-only rule (migration 0065).

A body chunk (``ord >= 0``, ``content_sha IS NULL``) re-derives its
embeddings / summaries / keywords by row identity, so an in-place
``UPDATE chunks.text`` orphans those derived rows. The
``chunks_forbid_body_text_update`` trigger rejects exactly that case while
leaving the two sanctioned in-place edit paths alone:

* draft-family chunks carry a non-NULL ``content_sha`` (sha-diff cascade);
* card chunks live at ``ord < 0`` (``rewrite_cards`` drops their embeddings).

See docs/design/chunk-append-only-trigger.md.
"""

from __future__ import annotations

import psycopg
import pytest

from precis.store import Store


def _mk_ref(store: Store, title: str = "T") -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('paper', %s) RETURNING ref_id",
            (title,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _mk_body_chunk(store: Store, ref_id: int, text: str = "original") -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, text),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_body_text_update_is_rejected(store: Store) -> None:
    """In-place text UPDATE on a body row (ord>=0, content_sha NULL) raises."""
    ref_id = _mk_ref(store)
    chunk_id = _mk_body_chunk(store, ref_id, "original")
    with pytest.raises(psycopg.errors.RaiseException) as exc:
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE chunks SET text = %s WHERE chunk_id = %s",
                ("rewritten", chunk_id),
            )
    assert "append-only" in str(exc.value)
    # The old text must survive the rejected UPDATE.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None and row[0] == "original"


def test_body_non_text_update_is_allowed(store: Store) -> None:
    """A metadata-only UPDATE on a body row must still succeed."""
    ref_id = _mk_ref(store)
    chunk_id = _mk_body_chunk(store, ref_id)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET meta = '{\"k\": 1}'::jsonb WHERE chunk_id = %s",
            (chunk_id,),
        )
        row = conn.execute(
            "SELECT meta->>'k' FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert row is not None and row[0] == "1"


def test_body_same_text_update_is_allowed(store: Store) -> None:
    """Writing the identical text is a no-op change → trigger does not fire."""
    ref_id = _mk_ref(store)
    chunk_id = _mk_body_chunk(store, ref_id, "same")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE chunks SET text = %s WHERE chunk_id = %s",
            ("same", chunk_id),
        )


def test_card_text_update_is_allowed(store: Store) -> None:
    """Cards (ord < 0) are exempt — rewrite_cards edits their text in place."""
    ref_id = _mk_ref(store)
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, -1, 'card_combined', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, "old card"),
        ).fetchone()
        assert row is not None
        card_id = int(row[0])
        conn.execute(
            "UPDATE chunks SET text = %s WHERE chunk_id = %s",
            ("new card text", card_id),
        )
        got = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (card_id,)
        ).fetchone()
    assert got is not None and got[0] == "new card text"


def test_draft_text_update_with_content_sha_is_allowed(store: Store) -> None:
    """Draft-family chunks carry content_sha → edit_text's in-place UPDATE is
    the sanctioned path and must not be blocked."""
    with store.pool.connection() as conn:
        rrow = conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('draft', 'D') RETURNING ref_id"
        ).fetchone()
        assert rrow is not None
        ref_id = int(rrow[0])
        crow = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, content_sha, meta) "
            "VALUES (%s, 0, 'paragraph', %s, %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, "draft v1", "sha-v1"),
        ).fetchone()
        assert crow is not None
        chunk_id = int(crow[0])
        conn.execute(
            "UPDATE chunks SET text = %s, content_sha = %s WHERE chunk_id = %s",
            ("draft v2", "sha-v2", chunk_id),
        )
        got = conn.execute(
            "SELECT text FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
    assert got is not None and got[0] == "draft v2"


def test_body_delete_then_insert_is_allowed(store: Store) -> None:
    """The sanctioned replace path (DELETE + INSERT) never fires the trigger."""
    ref_id = _mk_ref(store)
    chunk_id = _mk_body_chunk(store, ref_id, "v1")
    with store.pool.connection() as conn:
        conn.execute("DELETE FROM chunks WHERE chunk_id = %s", (chunk_id,))
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text, meta) "
            "VALUES (%s, 0, 'paragraph', %s, '{}'::jsonb) RETURNING chunk_id",
            (ref_id, "v2"),
        ).fetchone()
    assert row is not None
