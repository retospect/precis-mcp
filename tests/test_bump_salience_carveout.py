"""``public.bump_salience()`` as SECURITY DEFINER (migration 0137) — the
read-only salience carve-out.

The function itself is exercised on every search by the ordinary handler
suites; these tests pin the part 0137 adds: that a role holding no UPDATE
grant on ``chunks`` (a ``write:none`` envelope, which ``envelope.py::
db_role`` resolves to ``agent_ro``) can still call it, because SECURITY
DEFINER runs the UPDATE with the *owner's* privileges. Without that,
every read verb's access accounting hard-fails under a read-only role
and search errors out instead of serving hits. Mirrors
``test_gripe_carveout.py`` (0079).
"""

from __future__ import annotations

import pytest

from precis.store import Store

pytestmark = pytest.mark.db


def _one_chunk_id(store: Store) -> int:
    ref_id = store.insert_ref(
        kind="memory", slug=None, title="salience carveout probe"
    ).id
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'memory_body', 'probe body') RETURNING chunk_id",
            (ref_id,),
        ).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def test_bump_advances_last_seen_and_accesses(store: Store) -> None:
    chunk_id = _one_chunk_id(store)
    with store.pool.connection() as conn:
        before = conn.execute(
            "SELECT accesses FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
        conn.execute("SELECT public.bump_salience(%s)", ([chunk_id],))
        after = conn.execute(
            "SELECT accesses, last_seen FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
        conn.commit()
    assert before is not None and after is not None
    assert after[0] == before[0] + 1
    assert after[1] is not None


def test_security_definer_survives_a_role_with_no_table_grants(store: Store) -> None:
    """The whole point: a connection that can't UPDATE ``chunks`` directly
    can still heat what it reads. Proven with the built-in ``pg_monitor``
    predefined role (same precedent as ``test_gripe_carveout.py``) — a role
    with no grants at all on the precis tables."""
    chunk_id = _one_chunk_id(store)
    with store.pool.connection() as conn:
        conn.execute("SET ROLE pg_monitor")
        try:
            with pytest.raises(Exception, match="permission denied"):
                conn.execute(
                    "UPDATE chunks SET accesses = accesses WHERE chunk_id = %s",
                    (chunk_id,),
                )
            conn.rollback()
            conn.execute("SET ROLE pg_monitor")
            conn.execute("SELECT public.bump_salience(%s)", ([chunk_id],))
        finally:
            conn.execute("RESET ROLE")
        row = conn.execute(
            "SELECT accesses FROM chunks WHERE chunk_id = %s", (chunk_id,)
        ).fetchone()
        conn.commit()
    assert row is not None
    assert row[0] == 1
