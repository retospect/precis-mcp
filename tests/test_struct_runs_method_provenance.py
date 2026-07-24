"""0084_struct_runs_method_provenance.sql — provenance/method columns land on
a fresh DB, external rows are distinguishable from computed ones, and the
cache-hit index no longer matches an external row (ADR 0053 §4)."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from precis.store import Migrator
from tests.conftest import MIGRATIONS_DIR


def _fetch(dsn: str, sql: str, params: tuple = ()) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(sql, params).fetchall()


def _insert_ref(dsn: str, title: str) -> int:
    with psycopg.connect(dsn) as conn:
        return conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('structure', %s) RETURNING ref_id",
            (title,),
        ).fetchone()[0]


def test_provenance_defaults_computed_for_existing_rows(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    ref_id = _insert_ref(fresh_db, "computed design")
    with psycopg.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO struct_runs (ref_id, fidelity, on_version) "
            "VALUES (%s, 'clean', 1)",
            (ref_id,),
        )
        conn.commit()
    row = _fetch(
        fresh_db,
        "SELECT provenance, method FROM struct_runs WHERE ref_id = %s",
        (ref_id,),
    )[0]
    assert row["provenance"] == "computed"
    assert row["method"] is None


def test_external_row_carries_method_fingerprint(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    ref_id = _insert_ref(fresh_db, "external design")
    with psycopg.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO struct_runs "
            "(ref_id, fidelity, on_version, energy, provenance, method) "
            "VALUES (%s, 'dft-tight', 1, -12.34, 'external', %s::jsonb)",
            (
                ref_id,
                '{"functional": "PBE", "cutoff_eV": 520, "spin": "collinear", '
                '"dataset_doi": "10.1000/example"}',
            ),
        )
        conn.commit()
    row = _fetch(
        fresh_db,
        "SELECT provenance, method FROM struct_runs WHERE ref_id = %s",
        (ref_id,),
    )[0]
    assert row["provenance"] == "external"
    assert row["method"]["functional"] == "PBE"
    assert row["method"]["cutoff_eV"] == 520


def test_provenance_rejects_unknown_value(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    ref_id = _insert_ref(fresh_db, "bad provenance")
    with psycopg.connect(fresh_db) as conn:
        try:
            conn.execute(
                "INSERT INTO struct_runs (ref_id, fidelity, on_version, "
                "provenance) VALUES (%s, 'clean', 1, 'guessed')",
                (ref_id,),
            )
        except psycopg.errors.CheckViolation:
            conn.rollback()
        else:
            raise AssertionError("expected the provenance CHECK to reject it")


def test_cache_index_excludes_external_rows(fresh_db: str) -> None:
    # An external and a computed row sharing the same cache_key coexist as
    # distinct rows (no unique-constraint collision) — but the cache-hit
    # index must resolve to the computed one only.
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    ref_id = _insert_ref(fresh_db, "shared geometry")
    same_key = "deadbeef" * 8
    with psycopg.connect(fresh_db) as conn:
        conn.execute(
            "INSERT INTO struct_runs "
            "(ref_id, fidelity, on_version, status, cache_key, provenance) "
            "VALUES (%s, 'ml', 1, 'succeeded', %s, 'external')",
            (ref_id, same_key),
        )
        conn.execute(
            "INSERT INTO struct_runs "
            "(ref_id, fidelity, on_version, status, cache_key, provenance) "
            "VALUES (%s, 'ml', 1, 'succeeded', %s, 'computed')",
            (ref_id, same_key),
        )
        conn.commit()
    # both rows persisted — no collision at the DB level
    rows = _fetch(
        fresh_db,
        "SELECT provenance FROM struct_runs WHERE cache_key = %s ORDER BY id",
        (same_key,),
    )
    assert {r["provenance"] for r in rows} == {"external", "computed"}
    # the partial cache index only indexes the computed row
    idx_rows = _fetch(
        fresh_db,
        "SELECT provenance FROM struct_runs sr "
        "WHERE cache_key = %s "
        "AND EXISTS ("
        "  SELECT 1 FROM pg_indexes WHERE indexname = 'struct_runs_cache_idx'"
        ")",
        (same_key,),
    )
    assert len(idx_rows) == 2  # sanity: both rows exist in the table
    idxdef = _fetch(
        fresh_db,
        "SELECT indexdef FROM pg_indexes WHERE indexname = 'struct_runs_cache_idx'",
    )[0]["indexdef"]
    assert "provenance" in idxdef and "computed" in idxdef


def test_migration_is_idempotent(fresh_db: str) -> None:
    m = Migrator(fresh_db, MIGRATIONS_DIR)
    m.apply_all()
    assert m.apply_all() == [], "second run must be a no-op"
