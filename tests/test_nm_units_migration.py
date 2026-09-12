"""``0007_units_nm_wipe.sql`` — clean-slate wipe of pre-cutover nm designs.

units-policy-cutover (docs/backlog/units-policy-cutover.md): every nm
design stored before the SI-metres cutover (this chain round — nm ops'
new m-boundary, `precis_nm.ops._ingest_envelope`) is dev/test data only
(Reto, 2026-09-12, the same clean-slate ruling that landed
`0159_units_cad_wipe.sql` on the core `precis` migration source) — so
this is a clean-slate wipe, not a numeric rewrite: retire every kind='nm'
ref and drop its block-tree rows (`nm_blocks`/`nm_ports`/`nm_connects`/
`nm_topology`) + card_combined search chunk. A fresh `put` after this
migration authors a new design through the new strict m-boundary from
scratch.

``nm`` is a PLUGIN migration source (namespace ``precis_nm``,
``src/precis_nm/migrations/``) — the shared test DB template only carries
core migrations (``tests/conftest.py``'s ``_initialise_test_db``), so this
module seeds the plugin's own migrations directly, same fixture shape as
``test_nm_plugin.py``'s ``handler`` fixture.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg.types.json import Jsonb

import precis_nm
from precis.dispatch import Hub
from precis.store import Store
from precis.utils.units import UnitRequiredError
from precis_nm.handler import NmHandler

_MIGRATIONS_DIR = Path(precis_nm.__file__).parent / "migrations"
_WIPE_MIGRATION = "0007_units_nm_wipe"


def _apply_nm_migrations_except_wipe(store: Store) -> None:
    with store.pool.connection() as c:
        for sql in sorted(_MIGRATIONS_DIR.glob("*.sql")):
            if sql.stem == _WIPE_MIGRATION:
                continue
            body = sql.read_text(encoding="utf-8")
            body = body.replace("BEGIN;", "").replace("COMMIT;", "")
            c.execute(body)


def _apply_wipe_migration(store: Store) -> None:
    body = (_MIGRATIONS_DIR / f"{_WIPE_MIGRATION}.sql").read_text(encoding="utf-8")
    body = body.replace("BEGIN;", "").replace("COMMIT;", "")
    with store.pool.connection() as c:
        c.execute(body)


def _seed_pre_cutover_design(store: Store) -> int:
    """A design shaped exactly like pre-cutover storage: ``pose_xyz`` and
    ``envelope`` hold bare numbers meaning Ångström, no unit suffixes
    anywhere — plus a port, a connect, and a threading row, so the wipe's
    reach across every nm table is exercised in one seed."""
    with store.pool.connection() as c:
        ref_row = c.execute(
            "INSERT INTO refs (kind, title, meta) VALUES "
            "('nm', 'pre-cutover design', %s) RETURNING ref_id",
            (Jsonb({}),),
        ).fetchone()
        assert ref_row is not None
        ref_id = int(ref_row[0])

        axle_row = c.execute(
            "INSERT INTO nm_blocks (ref_id, name, pose_xyz, pose_rot, envelope) "
            "VALUES (%s, 'axle', '{0,0,0}', '{0,0,0}', 'cyl:r2h20') "
            "RETURNING id",
            (ref_id,),
        ).fetchone()
        assert axle_row is not None
        axle_id = int(axle_row[0])
        c.execute(
            "INSERT INTO nm_ports (block_id, name, roles) "
            "VALUES (%s, 'p1', '{covalent}')",
            (axle_id,),
        )

        hub_row = c.execute(
            "INSERT INTO nm_blocks (ref_id, name, pose_xyz, pose_rot, envelope) "
            "VALUES (%s, 'hub', '{5,0,0}', '{0,0,0}', 'sphere:r3') "
            "RETURNING id",
            (ref_id,),
        ).fetchone()
        assert hub_row is not None
        hub_id = int(hub_row[0])
        c.execute(
            "INSERT INTO nm_ports (block_id, name, roles) "
            "VALUES (%s, 'p1', '{covalent}')",
            (hub_id,),
        )

        c.execute(
            "INSERT INTO nm_connects (ref_id, a_block, a_port, b_block, b_port, kind) "
            "VALUES (%s, 'axle', 'p1', 'hub', 'p1', 'bond')",
            (ref_id,),
        )
        c.execute(
            "INSERT INTO nm_topology (ref_id, kind, subject_name, object_name) "
            "VALUES (%s, 'threading', 'hub', 'axle')",
            (ref_id,),
        )
        c.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, -1, 'card_combined', 'pre-cutover nm design')",
            (ref_id,),
        )
    return ref_id


def test_nm_units_wipe_retires_seeded_pre_cutover_design(store: Store) -> None:
    _apply_nm_migrations_except_wipe(store)
    ref_id = _seed_pre_cutover_design(store)

    _apply_wipe_migration(store)

    with store.pool.connection() as c:
        ref_row = c.execute(
            "SELECT retired_at, kind FROM refs WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert ref_row is not None
        retired_at, kind = ref_row
        block_row = c.execute(
            "SELECT count(*) FROM nm_blocks WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert block_row is not None
        (block_count,) = block_row
        port_row = c.execute(
            "SELECT count(*) FROM nm_ports WHERE block_id IN "
            "(SELECT id FROM nm_blocks WHERE ref_id = %s)",
            (ref_id,),
        ).fetchone()
        assert port_row is not None
        (port_count,) = port_row
        connect_row = c.execute(
            "SELECT count(*) FROM nm_connects WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert connect_row is not None
        (connect_count,) = connect_row
        topology_row = c.execute(
            "SELECT count(*) FROM nm_topology WHERE ref_id = %s", (ref_id,)
        ).fetchone()
        assert topology_row is not None
        (topology_count,) = topology_row
        chunk_row = c.execute(
            "SELECT count(*) FROM chunks WHERE ref_id = %s AND chunk_kind = 'card_combined'",
            (ref_id,),
        ).fetchone()
        assert chunk_row is not None
        (chunk_count,) = chunk_row

    assert kind == "nm"
    assert retired_at is not None  # soft-retired, house convention
    assert block_count == 0  # hard-deleted — nothing left to numerically migrate
    assert port_count == 0
    assert connect_count == 0
    assert topology_count == 0
    assert chunk_count == 0  # search-summary chunk dropped with it


def test_nm_units_wipe_is_idempotent_with_no_designs(store: Store) -> None:
    # No nm rows at all — the migration's WHERE-scoped statements must be
    # no-ops, not errors, on an empty set of tables.
    _apply_nm_migrations_except_wipe(store)
    _apply_wipe_migration(store)


def test_nm_units_wipe_then_fresh_put_uses_strict_m_boundary(
    hub: Hub, store: Store
) -> None:
    _apply_nm_migrations_except_wipe(store)
    _seed_pre_cutover_design(store)
    _apply_wipe_migration(store)

    handler = NmHandler(hub=hub)
    # A bare, unit-less envelope token is now rejected at the boundary...
    with pytest.raises(UnitRequiredError):
        handler.put(
            id="fresh",
            text='{"ops":[{"op":"add_block","name":"axle","envelope":"cyl:r2h20"}]}',
        )
    # ...while unit-suffixed source round-trips, stored as canonical SI
    # metres (this chain round's boundary — precis_nm.ops._ingest_envelope).
    handler.put(
        id="fresh",
        text='{"ops":[{"op":"add_block","name":"axle","envelope":"cyl:r2Åh20Å"}]}',
    )
    with store.pool.connection() as c:
        row = c.execute(
            "SELECT nb.envelope FROM nm_blocks nb "
            "JOIN refs r ON r.ref_id = nb.ref_id "
            "WHERE r.kind = 'nm' AND r.retired_at IS NULL "
            "AND nb.name = 'axle'"
        ).fetchone()
    assert row is not None
    assert row[0] == "cyl:r2e-10h2e-09"
