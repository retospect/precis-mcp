"""gr516504: v_refs picks one identifier row per kind deterministically.

Multi-row ``(ref_id, id_kind)`` state is legal — a taproot reword
mints a fresh ``pub_id`` and keeps the old; tex-import minted a second
``cite_key`` beside most ``local`` ones — so the view's scalar
subqueries must carry a deterministic ORDER BY + LIMIT 1 rather than
raise "more than one row returned by a subquery" (migration 0246):
newest-minted for ``pub_id`` (the live version, same rule
``current_pub_ids`` uses), first-minted for the content-derived
``cite_key``/``paper_id`` (the original key citations resolve).
"""

from __future__ import annotations

import psycopg

from precis.store import Migrator
from tests.conftest import MIGRATIONS_DIR


def _vref(conn: psycopg.Connection, ref_id: int) -> tuple:
    row = conn.execute(
        "SELECT pub_id, cite_key, paper_id FROM v_refs WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    assert row is not None
    return row


def _paper_ref(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO refs (kind, title) VALUES ('paper', 'T') RETURNING ref_id"
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_v_refs_deterministic_per_kind_pick(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    with psycopg.connect(fresh_db) as conn:
        rid = _paper_ref(conn)
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, created_at)"
            " VALUES "
            "('pub_id', 'old_pub', %(r)s, '2026-01-01'::timestamptz),"
            "('pub_id', 'new_pub', %(r)s, '2026-01-02'::timestamptz),"
            "('cite_key', 'tex_import_long', %(r)s, '2026-02-02'::timestamptz),"
            "('cite_key', 'local99', %(r)s, '2026-01-01'::timestamptz),"
            "('paper_id', 'pid_b', %(r)s, '2026-02-02'::timestamptz),"
            "('paper_id', 'pid_a', %(r)s, '2026-01-01'::timestamptz)",
            {"r": rid},
        )
        # Would raise "more than one row returned by a subquery" on the
        # 0149 view; the deterministic pick must not.
        assert _vref(conn, rid) == ("new_pub", "local99", "pid_a")


def test_v_refs_pick_is_stable_across_insert_order(fresh_db: str) -> None:
    """The pick can't drift with heap order: two refs carrying two
    cite_key rows each (the PK is global on ``(id_kind, id_value)``, so
    the values differ per ref) inserted in opposite order — equal
    created_at → the id_value tiebreak decides — expose the
    lexicographically smaller value."""
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    with psycopg.connect(fresh_db) as conn:
        rid_a = _paper_ref(conn)
        rid_b = _paper_ref(conn)
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, created_at)"
            " VALUES "
            "('cite_key', 'zz8', %(r)s, '2026-01-01'::timestamptz),"
            "('cite_key', 'aa8', %(r)s, '2026-01-01'::timestamptz)",
            {"r": rid_a},
        )
        conn.execute(
            "INSERT INTO ref_identifiers (id_kind, id_value, ref_id, created_at)"
            " VALUES "
            "('cite_key', 'aa9', %(r)s, '2026-01-01'::timestamptz),"
            "('cite_key', 'zz9', %(r)s, '2026-01-01'::timestamptz)",
            {"r": rid_b},
        )
        assert _vref(conn, rid_a) == (None, "aa8", None)
        assert _vref(conn, rid_b) == (None, "aa9", None)


#: The 0149 column list — CREATE OR REPLACE requires identical names,
#: order and types, so a drift here means the migration failed to
#: apply (or 0149's shape was silently widened).
_VREFS_COLUMNS_0149 = [
    "ref_id",
    "kind",
    "set_by",
    "title",
    "authors",
    "year",
    "provider",
    "human_verified_at",
    "human_verified_by",
    "human_verified_note",
    "retraction_status",
    "retracted_at",
    "retraction_reason",
    "retraction_url",
    "retraction_checked_at",
    "pdf_sha256",
    "pdf_pages",
    "pdf_role",
    "meta",
    "retired_at",
    "created_at",
    "updated_at",
    "pub_id",
    "cite_key",
    "paper_id",
]


def test_v_refs_columns_match_the_0149_list(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    with psycopg.connect(fresh_db) as conn:
        cols = [
            r[0]
            for r in conn.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = 'public' AND table_name = 'v_refs' "
                "ORDER BY ordinal_position"
            ).fetchall()
        ]
    assert cols == _VREFS_COLUMNS_0149
