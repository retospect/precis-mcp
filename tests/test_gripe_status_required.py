"""Gripe STATUS invariant: migration 0176 deferred triggers.

Every gripe carries exactly one STATUS tag from the gripe vocabulary
(open, triaged, ready_for_fix, in_review, done, wontfix). ``refuted`` is a
finding status and must not land on a gripe.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import pytest

from precis.store import Store
from precis.store.types import Tag

pytestmark = pytest.mark.db


def _insert_gripe(store: Store, conn: Any, *, status: str | None) -> int:
    ref = store.insert_ref(kind="gripe", slug=None, title="x", meta={}, conn=conn)
    if status is not None:
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    return int(ref.id)


def test_untagged_gripe_fails_at_commit(store: Store) -> None:
    with pytest.raises(Exception, match="exactly one STATUS"):
        with store.tx() as conn:
            _insert_gripe(store, conn, status=None)


def test_ref_then_tag_in_one_tx_commits(store: Store) -> None:
    with store.tx() as conn:
        rid = _insert_gripe(store, conn, status="open")
    assert any("STATUS:open" in str(t) for t in store.tags_for(rid))


def test_replace_prefix_status_change_succeeds(store: Store) -> None:
    with store.tx() as conn:
        rid = _insert_gripe(store, conn, status="open")
    store.add_tag(
        rid, Tag.closed("STATUS", "triaged"), set_by="agent", replace_prefix=True
    )
    statuses = [str(t) for t in store.tags_for(rid) if "STATUS:" in str(t)]
    assert statuses == ["STATUS:triaged"]


def test_off_vocab_status_fails_at_commit(store: Store) -> None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO tags (namespace, value) VALUES ('STATUS', 'refuted') "
            "ON CONFLICT (namespace, value) DO UPDATE SET value = EXCLUDED.value "
            "RETURNING tag_id"
        ).fetchone()
        assert row is not None
        tid = row[0]
        conn.commit()
    with store.tx() as conn:
        rid = _insert_gripe(store, conn, status="open")
    # Swap the tag in place (UPDATE) — covered by the ref_tags trigger.
    with pytest.raises(Exception, match="exactly one STATUS"):
        with store.tx() as conn:
            conn.execute(
                "UPDATE ref_tags SET tag_id = %s WHERE ref_id = %s", (tid, rid)
            )
    # Re-insert path.
    with pytest.raises(Exception, match="exactly one STATUS"):
        with store.tx() as conn:
            conn.execute("DELETE FROM ref_tags WHERE ref_id = %s", (rid,))
            conn.execute(
                "INSERT INTO ref_tags (ref_id, tag_id, set_by) VALUES (%s, %s, 'agent')",
                (rid, tid),
            )


def test_deleting_gripe_cascade_is_allowed(store: Store) -> None:
    with store.tx() as conn:
        rid = _insert_gripe(store, conn, status="open")
    with store.tx() as conn:
        conn.execute("DELETE FROM refs WHERE ref_id = %s", (rid,))


def test_backfill_two_off_vocab_statuses_collapse_to_one_wontfix(store: Store) -> None:
    """Migration step (a2) on a gripe with two off-vocab STATUS and no valid one.

    An UPDATE-based remap would hit the ``(ref_id, tag_id)`` primary key and
    abort the whole migration. Runs the real (a2) SQL inside one rolled-back
    transaction (the constraint triggers are deferred, so the bad seed state
    is legal until COMMIT, which never comes).
    """
    sql = (
        resources.files("precis.migrations")
        .joinpath("0176_gripe_status_required.sql")
        .read_text(encoding="utf-8")
    )
    a2 = sql[sql.index("-- (a2)") : sql.index("-- (a3)")]
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO tags (namespace, value) VALUES "
            "('STATUS', 'wontfix'), ('STATUS', 'refuted'), ('STATUS', 'bogus') "
            "ON CONFLICT (namespace, value) DO NOTHING"
        )
        ref = store.insert_ref(kind="gripe", slug=None, title="x", meta={}, conn=conn)
        conn.execute(
            "INSERT INTO ref_tags (ref_id, tag_id, set_by) "
            "SELECT %s, tag_id, 'agent' FROM tags "
            "WHERE namespace = 'STATUS' AND value IN ('refuted', 'bogus')",
            (ref.id,),
        )
        conn.execute(a2)
        conn.execute(a2)  # idempotent: a second pass changes nothing
        rows = conn.execute(
            "SELECT t.value FROM ref_tags rt JOIN tags t USING (tag_id) "
            "WHERE rt.ref_id = %s AND t.namespace = 'STATUS'",
            (ref.id,),
        ).fetchall()
        conn.rollback()
    assert [r[0] for r in rows] == ["wontfix"]
