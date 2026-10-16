"""0219_pcb_locks_regions.sql — lock/region columns and the leash backfill.

A leash authored before the migration lives in ``pcb_instances.meta``
(``max_travel_mm``); after it, the column is the only copy. Asserts the
backfill (value kept, meta key gone), that a part with no leash stays NULL,
that the CHECK refuses a negative, that the new defaults hold, and that
``region_hint`` is carried into ``pcb_planes.region``.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import psycopg
import pytest
from psycopg.types.json import Jsonb

from precis.store import Migrator
from tests.conftest import MIGRATIONS_DIR

_MIGRATION = "0219_pcb_locks_regions"


def _apply_before(dsn: str, tmp_path: Path) -> None:
    pre_dir = tmp_path / "migrations_pre"
    pre_dir.mkdir()
    for sql in sorted(MIGRATIONS_DIR.glob("*.sql")):
        if sql.stem >= _MIGRATION:
            continue
        shutil.copy2(sql, pre_dir / sql.name)
    Migrator(dsn, pre_dir).apply_all()


def _seed(conn: psycopg.Connection) -> int:
    row = conn.execute(
        "INSERT INTO refs (kind, title) VALUES ('pcb', 'locks') RETURNING ref_id"
    ).fetchone()
    assert row is not None
    ref_id = int(row[0])
    board = conn.execute(
        "INSERT INTO pcb_boards (ref_id, name, stackup) VALUES (%s, 'b', '[]') "
        "RETURNING board_id",
        (ref_id,),
    ).fetchone()
    comp = conn.execute(
        "INSERT INTO pcb_components (ref_id, label) VALUES (%s, 'ic') "
        "RETURNING component_id",
        (ref_id,),
    ).fetchone()
    assert comp is not None and board is not None
    for refdes, meta in (
        ("A", {"max_travel_mm": 0.5, "group": "g1"}),
        ("B", {"group": "g1"}),
        ("C", {"max_travel_mm": 0}),
        ("D", {"max_travel_mm": "1.5"}),
        ("E", {"max_travel_mm": "abc"}),
        ("F", {"max_travel_mm": -2}),
    ):
        conn.execute(
            "INSERT INTO pcb_instances "
            "(ref_id, board_id, component_id, refdes, meta) "
            "VALUES (%s, %s, %s, %s, %s)",
            (ref_id, board[0], comp[0], refdes, Jsonb(meta)),
        )
    net = conn.execute(
        "INSERT INTO pcb_nets (ref_id, name) VALUES (%s, 'GND') RETURNING net_id",
        (ref_id,),
    ).fetchone()
    assert board is not None and net is not None
    conn.execute(
        "INSERT INTO pcb_planes (board_id, layer, net_id, region_hint) "
        "VALUES (%s, 'In1.Cu', %s, %s)",
        (board[0], net[0], Jsonb([[0, 0], [1, 0], [1, 1]])),
    )
    return ref_id


def test_leash_backfills_to_column_and_leaves_meta(
    fresh_db: str, tmp_path: Path
) -> None:
    _apply_before(fresh_db, tmp_path)
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        ref_id = _seed(conn)

    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()

    with psycopg.connect(fresh_db) as conn:
        rows = {
            r[0]: r[1:]
            for r in conn.execute(
                "SELECT refdes, max_travel_mm, meta, fixed_side, group_fixed, "
                "       room_id FROM pcb_instances WHERE ref_id = %s",
                (ref_id,),
            ).fetchall()
        }
        plane = conn.execute(
            "SELECT region, priority FROM pcb_planes pl "
            "JOIN pcb_boards b ON b.board_id = pl.board_id WHERE b.ref_id = %s",
            (ref_id,),
        ).fetchone()
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE pcb_instances SET max_travel_mm = -1 WHERE ref_id = %s",
                (ref_id,),
            )

    # leash survives as the column value; the meta key is gone, others stay
    assert rows["A"] == (0.5, {"group": "g1"}, False, False, None)
    assert rows["B"] == (None, {"group": "g1"}, False, False, None)
    assert rows["C"][0] == 0.0 and rows["C"][1] == {}
    # a numeric string is carried too; what cannot be carried stays in meta
    assert rows["D"][0] == 1.5 and rows["D"][1] == {}
    assert rows["E"][0] is None and rows["E"][1] == {"max_travel_mm": "abc"}
    assert rows["F"][0] is None and rows["F"][1] == {"max_travel_mm": -2}
    # region_hint carried into region; priority defaults 0
    assert plane == ([[0, 0], [1, 0], [1, 1]], 0)
