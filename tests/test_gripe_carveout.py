"""``public.file_gripe_readonly()`` (migration 0079) — the agent_ro gripe
carve-out.

``GripeHandler._create`` (``handlers/gripe.py``) now routes its base insert
through this SECURITY DEFINER SQL function instead of hand-rolled insert_ref/
insert_blocks/add_tag calls, so the function is exercised by the ordinary
``TestGripe`` suite in ``test_state_kinds.py`` on every gripe creation. These
tests pin the function's own contract directly against the DB: exactly one
gripe (ref + gripe_body chunk + STATUS:open tag) and nothing else, and that a
SECURITY DEFINER function keeps working from a role that holds no direct
INSERT grant on refs/chunks/tags/ref_tags — the whole point (an
``agent_ro``/``write:none`` connection, ``envelope.py::db_role``).
"""

from __future__ import annotations

import pytest

from precis.store import Store

pytestmark = pytest.mark.db


def _call(store: Store, text: str) -> int:
    with store.pool.connection() as conn:
        row = conn.execute("SELECT public.file_gripe_readonly(%s)", (text,)).fetchone()
        assert row is not None
        conn.commit()
        return int(row[0])


def test_inserts_ref_body_chunk_and_status_open_tag(store: Store) -> None:
    ref_id = _call(store, "the search page 500s on a percent sign")

    ref = next(r for r in store.list_refs(kind="gripe", limit=50) if r.id == ref_id)
    assert ref.kind == "gripe"
    assert ref.title == "the search page 500s on a percent sign"

    blocks = store.list_blocks_for_ref(ref_id)
    assert len(blocks) == 1
    assert blocks[0].chunk_kind == "gripe_body"
    assert blocks[0].text == "the search page 500s on a percent sign"

    tags = store.tags_for(ref_id)
    assert any("STATUS:open" in str(t) for t in tags)


def test_rejects_empty_text(store: Store) -> None:
    with (
        store.pool.connection() as conn,
        pytest.raises(Exception, match="must not be empty"),
    ):
        conn.execute("SELECT public.file_gripe_readonly('')")


def test_two_calls_create_two_independent_gripes(store: Store) -> None:
    first = _call(store, "first friction report")
    second = _call(store, "second friction report")
    assert first != second
    assert len(store.list_blocks_for_ref(first)) == 1
    assert len(store.list_blocks_for_ref(second)) == 1


def test_security_definer_survives_a_role_with_no_table_grants(store: Store) -> None:
    """The whole point: a connection that can't INSERT into refs/chunks/tags
    directly can still call the function, because SECURITY DEFINER runs it
    with the *owner's* privileges. Proven with the built-in ``pg_monitor``
    predefined role (mirrors ``test_pool_db_role.py``'s precedent for
    exercising a real role switch without creating a new role) — a role
    with no grants at all on the precis tables."""
    with store.pool.connection() as conn:
        conn.execute("SET ROLE pg_monitor")
        try:
            row = conn.execute(
                "SELECT public.file_gripe_readonly(%s)",
                ("filed while running as pg_monitor",),
            ).fetchone()
        finally:
            conn.execute("RESET ROLE")
        conn.commit()
    assert row is not None
    ref_id = int(row[0])
    blocks = store.list_blocks_for_ref(ref_id)
    assert len(blocks) == 1
    assert blocks[0].chunk_kind == "gripe_body"
