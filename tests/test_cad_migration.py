"""0041_cad_kind.sql — the cad kind + cad_nodes table land on a fresh DB."""

from __future__ import annotations

import psycopg
from psycopg.rows import dict_row

from precis.store import Migrator
from tests.conftest import MIGRATIONS_DIR


def _fetch(dsn: str, sql: str) -> list[dict]:
    with psycopg.connect(dsn, row_factory=dict_row) as conn:
        return conn.execute(sql).fetchall()


def test_cad_kind_seeded(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    kinds = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM kinds")}
    assert "cad" in kinds
    row = _fetch(fresh_db, "SELECT is_numeric, title FROM kinds WHERE slug = 'cad'")
    assert row[0]["is_numeric"] is False
    assert row[0]["title"] == "CAD"


def test_cad_nodes_table_created(fresh_db: str) -> None:
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    reg = _fetch(fresh_db, "SELECT to_regclass('public.cad_nodes') AS t")
    assert reg[0]["t"] is not None
    # the hot-read index + name-uniqueness index exist
    idx = {
        r["indexname"]
        for r in _fetch(
            fresh_db,
            "SELECT indexname FROM pg_indexes WHERE tablename = 'cad_nodes'",
        )
    }
    assert "cad_nodes_ref_ord_idx" in idx
    assert "cad_nodes_ref_name_key" in idx


def test_no_cad_chunk_kinds(fresh_db: str) -> None:
    # Amendment 1: nodes are NOT chunks — the old cad_* chunk_kinds are gone.
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    ck = {r["slug"] for r in _fetch(fresh_db, "SELECT slug FROM chunk_kinds")}
    assert not ({"cad_node", "cad_component", "cad_meta"} & ck)
    assert "card_combined" in ck  # the one search card reuses the existing kind


def test_cad_migration_idempotent(fresh_db: str) -> None:
    m = Migrator(fresh_db, MIGRATIONS_DIR)
    m.apply_all()
    assert m.apply_all() == [], "second run must be a no-op"
