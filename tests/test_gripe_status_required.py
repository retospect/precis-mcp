"""Gripe STATUS invariant: migration 0176 deferred triggers.

Every gripe carries exactly one STATUS tag from the gripe vocabulary
(open, triaged, fixing, ready_for_fix, in_review, verify, done, wontfix —
``verify`` joined under migration 0247, ``fixing`` under 0267).``refuted`` is a finding status
and must not land on a gripe.
"""

from __future__ import annotations

from importlib import resources
from typing import Any

import pytest

from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
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


def test_verify_status_commits(store: Store) -> None:
    """0247 widened the trigger vocabulary: STATUS:verify is a valid sole
    STATUS on a gripe."""
    with store.tx() as conn:
        rid = _insert_gripe(store, conn, status="verify")
    assert any("STATUS:verify" in str(t) for t in store.tags_for(rid))


def test_fixing_status_commits(store: Store) -> None:
    """0267 widened the trigger vocabulary: STATUS:fixing (gr541800, a
    held ``claimed-by:`` lease) is a valid sole STATUS on a gripe."""
    with store.tx() as conn:
        rid = _insert_gripe(store, conn, status="fixing")
    assert any("STATUS:fixing" in str(t) for t in store.tags_for(rid))


def test_newest_trigger_vocab_matches_kind_status_values() -> None:
    """The newest migration that redefines ``gripe_status_check()`` lists
    exactly ``_KIND_STATUS_VALUES['gripe']`` — the app check and the
    commit-time trigger cannot drift apart."""
    import re

    from precis.store.types import _KIND_STATUS_VALUES

    root = resources.files("precis.migrations")
    newest = max(
        (
            p
            for p in root.iterdir()
            if p.name.endswith(".sql")
            and "FUNCTION public.gripe_status_check()" in p.read_text(encoding="utf-8")
        ),
        key=lambda p: p.name,
    )
    sql = newest.read_text(encoding="utf-8")
    m = re.search(r"NOT IN\s*\(([^)]*)\)", sql)
    assert m is not None, newest.name
    values = frozenset(re.findall(r"'([^']+)'", m.group(1)))
    assert values == _KIND_STATUS_VALUES["gripe"], newest.name


def _migration_function_sql(migration: str) -> str:
    """The ``CREATE OR REPLACE FUNCTION`` block sliced out of a migration
    file (the same slicing the backfill test uses for its (a2) block)."""
    sql = (
        resources.files("precis.migrations")
        .joinpath(migration)
        .read_text(encoding="utf-8")
    )
    return sql[sql.index("CREATE OR REPLACE FUNCTION") : sql.index("$$;") + 3]


def test_status_vocab_drift_refuses_typed(hub: Any) -> None:
    """gr544215: the app STATUS vocabulary ahead of the DB trigger's.

    land_notify's resolve put — ``resolved: <sha9> (main, release
    pending) — landed: <subject>`` with ``STATUS:auto_verify`` — ran
    under code that knew migration 0268's vocabulary while prod's
    ``gripe_status_check()`` still held 0267's (the migration landed in
    the same ship, undeployed). ``Tag.parse_strict`` accepted; the
    deferred trigger raised ``CheckViolation`` at COMMIT and the call
    surfaced as ``[error:Internal] … file a gripe … server bug``
    (exit 3). The handler translates that trigger refusal into a typed
    error naming the database's own vocabulary — never an unhandled
    raise. Simulated by swapping the 0267-era function in, then the
    exact incident call shape.
    """
    store: Store = hub.store
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
            "JOIN pg_namespace n ON n.oid = p.pronamespace "
            "WHERE p.proname = 'gripe_status_check' AND n.nspname = 'public'"
        ).fetchone()
        assert row is not None
        conn.execute(_migration_function_sql("0267_gripe_status_fixing.sql"))
        conn.commit()
    try:
        h = GripeHandler(hub=hub)
        h.put(text="drift probe gripe")
        gid = int(store.list_refs(kind="gripe", limit=1, order_by="id_desc")[0].id)
        # The exact resolve put land_notify sent and prod refused (gr544215).
        with pytest.raises(BadInput) as ei:
            h.put(
                id=gid,
                text="resolved: 9ab88a7b5 (main, release pending) — landed: "
                "fix(ssh_node): live re-adopt is not a crash attempt (gr541147)",
                tags=["STATUS:auto_verify"],
            )
        assert "must have exactly one STATUS" in str(ei.value)
        # options= the *database's* vocabulary (0267), not the build's.
        assert ei.value.options == [
            "open",
            "triaged",
            "fixing",
            "ready_for_fix",
            "in_review",
            "verify",
            "done",
            "wontfix",
        ]
        assert ei.value.next is not None
        # The whole unit rolled back — no half-landed comment or tag.
        assert not [
            c
            for c in store.chunks.list_chunks_for_ref(gid)
            if c.chunk_kind == "gripe_comment"
        ]
        assert "STATUS:auto_verify" not in {str(t) for t in store.tags_for(gid)}
        # The bare tag verb refuses the same way.
        with pytest.raises(BadInput):
            h.tag(id=gid, add=["STATUS:auto_verify"])
    finally:
        with store.pool.connection() as conn:
            conn.execute(row[0])
            conn.commit()


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
