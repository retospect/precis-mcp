"""0159_units_cad_wipe.sql — clean-slate wipe of pre-cutover cad designs.

units-policy-cutover (docs/backlog/units-policy-cutover.md): every cad
design stored before the SI-metres cutover (chain round 4) is dev/test
data only (Reto, 2026-09-12) — no numeric rewrite, just a retire-and-drop
so a `put` after this migration authors fresh, unit-suffixed source
through the new strict boundary from scratch. Asserts (a) the migration
applies cleanly against seeded pre-cutover-style rows and actually wipes
them, and (b) a fresh design round-trips through the strict boundary
afterwards.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from precis.dispatch import Hub
from precis.handlers.cad import CadHandler
from precis.store import Migrator, Store
from precis.utils.units import UnitRequiredError
from tests.conftest import MIGRATIONS_DIR

_MIGRATION = "0159_units_cad_wipe"


def _migrate_up_to_but_not_including(dsn: str, tmp_path: Path, version: str) -> None:
    """Apply every migration strictly before ``version`` (by copying the
    real migrations dir minus that one file — same bytes, so the ledger's
    checksums for the files that DO apply match the real directory's,
    letting a later ``Migrator(dsn, MIGRATIONS_DIR)`` pick up only the
    held-back tail)."""
    pre_dir = tmp_path / "migrations_pre"
    pre_dir.mkdir()
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql.stem == version:
            continue
        shutil.copy2(sql, pre_dir / sql.name)
    Migrator(dsn, pre_dir).apply_all()


def _seed_pre_cutover_design(conn: psycopg.Connection) -> int:
    """A design shaped exactly like pre-cutover storage: bare numbers
    meaning millimetres, no unit suffixes anywhere."""
    row = conn.execute(
        "INSERT INTO refs (kind, title, meta) VALUES "
        "('cad', 'pre-cutover design', %s) RETURNING ref_id",
        (Jsonb({"units": "mm"}),),
    ).fetchone()
    assert row is not None
    ref_id = int(row[0])
    conn.execute(
        "INSERT INTO cad_nodes (ref_id, ord, name, component, op, config, loc, rot) "
        "VALUES (%s, 0, 'body', 'part', 'add', 'box:w40d20h10', '{0,0,0}', '{0,0,0}')",
        (ref_id,),
    )
    conn.execute(
        "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
        "VALUES (%s, -1, 'card_combined', 'pre-cutover design')",
        (ref_id,),
    )
    return ref_id


def test_cad_units_wipe_retires_seeded_pre_cutover_design(
    fresh_db: str, tmp_path: Path
) -> None:
    _migrate_up_to_but_not_including(fresh_db, tmp_path, _MIGRATION)

    with psycopg.connect(fresh_db, autocommit=True) as conn:
        ref_id = _seed_pre_cutover_design(conn)

    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()  # runs 0159 (+ any later tail)

    with psycopg.connect(fresh_db) as conn:
        ref_row = conn.execute(
            "SELECT retired_at, kind FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert ref_row is not None
        retired_at, kind = ref_row
        node_row = conn.execute(
            "SELECT count(*) FROM cad_nodes WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert node_row is not None
        (node_count,) = node_row
        chunk_row = conn.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref_id,),
        ).fetchone()
        assert chunk_row is not None
        (chunk_count,) = chunk_row

    assert kind == "cad"
    assert retired_at is not None  # soft-deleted, house convention
    assert node_count == 0  # hard-deleted — nothing left to numerically migrate
    assert chunk_count == 0  # search-summary chunk dropped with it


def test_cad_units_wipe_is_idempotent_with_no_designs(
    fresh_db: str, tmp_path: Path
) -> None:
    # No cad rows at all — the migration's WHERE-scoped statements must be
    # no-ops, not errors, on an empty table.
    _migrate_up_to_but_not_including(fresh_db, tmp_path, _MIGRATION)
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()


def test_cad_units_wipe_then_fresh_put_uses_strict_boundary(
    fresh_db: str, tmp_path: Path
) -> None:
    _migrate_up_to_but_not_including(fresh_db, tmp_path, _MIGRATION)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        _seed_pre_cutover_design(conn)
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()

    store = Store.connect(fresh_db)
    try:
        cad = CadHandler(hub=Hub(store=store))
        # A bare, unit-less number is now rejected at the boundary...
        with pytest.raises(UnitRequiredError):
            cad.put(id="fresh", text="component p\nbody add box:w40d20h10\n")
        # ...while unit-suffixed source round-trips, stored as canonical SI
        # metres (chain round 4's boundary re-canonicalisation).
        cad.put(id="fresh", text="component p\nbody add box:w40mmd20mmh10mm\n")
        with store.pool.connection() as conn:
            config_row = conn.execute(
                "SELECT cn.config FROM cad_nodes cn "
                "JOIN refs r ON r.ref_id = cn.ref_id "
                "WHERE r.kind = 'cad' AND r.retired_at IS NULL "
                "AND cn.name = 'body'"
            ).fetchone()
        assert config_row is not None
        assert config_row[0] == "box:w0.04d0.02h0.01"
    finally:
        store.close()
