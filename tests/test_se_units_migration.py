"""``0006_units_se_pose_rot_rad.sql`` — lossless deg->rad rewrite of
live ``se_blocks.pose_rot`` data.

units-policy-cutover angle ruling (docs/backlog/units-policy-cutover.md
decisions log, "se pose_rot deg->rad migration", round 8): unlike cad/nm's
clean-slate wipes, se's stored designs (``unicycle-printed-v1``,
``boxel-3nm``) are live dogfood — this is a LOSSLESS numeric rewrite,
proven exact against seeded degree rows (0, negative, >360) rather than
wiped.

``se`` is a PLUGIN migration source (namespace ``precis_se``,
``src/precis_se/migrations/``) — the shared test DB template only carries
core migrations (``tests/conftest.py``'s ``_initialise_test_db``), so this
module seeds the plugin's own migrations directly, same fixture shape as
``test_se_plugin.py``'s ``handler`` fixture.
"""

from __future__ import annotations

import math
from pathlib import Path

from psycopg.types.json import Jsonb

import precis_se
from precis.store import Store

_MIGRATIONS_DIR = Path(precis_se.__file__).parent / "migrations"
_REWRITE_MIGRATION = "0006_units_se_pose_rot_rad"


def _apply_se_migrations_except_rewrite(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            if sql.stem == _REWRITE_MIGRATION:
                continue
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


def _apply_rewrite_migration(store: Store) -> None:
    body = (_MIGRATIONS_DIR / f"{_REWRITE_MIGRATION}.sql").read_text(encoding="utf-8")
    body = body.replace("BEGIN;", "").replace("COMMIT;", "")
    with store.pool.connection() as c:
        c.execute(body)


def _seed_degree_block(store: Store, *, name: str, pose_rot: list[float]) -> int:
    with store.pool.connection() as c:
        ref_row = c.execute(
            "INSERT INTO refs (kind, title, meta) VALUES "
            "('se', 'pre-cutover se design', %s) RETURNING ref_id",
            (Jsonb({}),),
        ).fetchone()
        assert ref_row is not None
        ref_id = int(ref_row[0])
        block_row = c.execute(
            "INSERT INTO se_blocks (ref_id, name, pose_xyz, pose_rot) "
            "VALUES (%s, %s, '{0,0,0}', %s) RETURNING id",
            (ref_id, name, pose_rot),
        ).fetchone()
        assert block_row is not None
        return int(block_row[0])


def test_pose_rot_rewrite_converts_degrees_to_radians_exactly(store: Store) -> None:
    _apply_se_migrations_except_rewrite(store)
    # 0, negative, and >360 — the decisions log's explicit seed set, so
    # the exact-radians() claim isn't just proven at the easy angles.
    b1 = _seed_degree_block(store, name="zero", pose_rot=[0.0, -45.0, 405.0])
    b2 = _seed_degree_block(store, name="neg", pose_rot=[-180.0, 90.0, -720.5])

    _apply_rewrite_migration(store)

    with store.pool.connection() as c:
        row1 = c.execute(
            "SELECT pose_rot FROM se_blocks WHERE id = %s", (b1,)
        ).fetchone()
        row2 = c.execute(
            "SELECT pose_rot FROM se_blocks WHERE id = %s", (b2,)
        ).fetchone()
    assert row1 is not None and row2 is not None
    assert row1[0] == [math.radians(x) for x in [0.0, -45.0, 405.0]]
    assert row2[0] == [math.radians(x) for x in [-180.0, 90.0, -720.5]]


def test_pose_rot_rewrite_is_a_noop_with_no_blocks(store: Store) -> None:
    # No se_blocks rows at all — the migration's WHERE-scoped UPDATE must
    # be a no-op, not an error, on an empty table.
    _apply_se_migrations_except_rewrite(store)
    _apply_rewrite_migration(store)


def test_pose_rot_rewrite_preserves_default_zero_pose(store: Store) -> None:
    # The column default '{0,0,0}' — radians(0) == 0, so an untouched
    # block's rotation stays exactly zero (no sign-flip / NaN surprise).
    _apply_se_migrations_except_rewrite(store)
    with store.pool.connection() as c:
        ref_row = c.execute(
            "INSERT INTO refs (kind, title, meta) VALUES ('se', 'd', %s) "
            "RETURNING ref_id",
            (Jsonb({}),),
        ).fetchone()
        assert ref_row is not None
        ref_id = int(ref_row[0])
        block_row = c.execute(
            "INSERT INTO se_blocks (ref_id, name) VALUES (%s, 'body') RETURNING id",
            (ref_id,),
        ).fetchone()
        assert block_row is not None
        block_id = int(block_row[0])

    _apply_rewrite_migration(store)

    with store.pool.connection() as c:
        row = c.execute(
            "SELECT pose_rot FROM se_blocks WHERE id = %s", (block_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == [0.0, 0.0, 0.0]
