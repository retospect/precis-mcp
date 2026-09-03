"""Store write-back for the se block tree — shared by ``put``/``edit``.

The :mod:`precis_nm.persist` discipline, transferred whole (see that
module's docstring for the full reasoning — the "Round-2 landmine" there is
designed out here from day one): a design's blocks live in dedicated tables
(``se_blocks``/``se_ports``/``se_connects``, migration ``0001_se_kind.sql``)
reached over the store's public connection surface (``store.tx()`` /
``store.pool.connection()``) — a plugin never joins core's mixin list.

**Save model** (retire-all/reinsert-all, identity is the block *name*):
:func:`load_tree` reads a design's live blocks into a fresh
:class:`~precis_se.ops.SeTree` keyed by name; :func:`save_tree` retires
every live row for the ref and reinserts the whole tree afresh in
parent/template-respecting order. Row ids are rebuilt on every save, which
is exactly why everything cross-referencing (connect endpoints, later
measures/notes) is **name-keyed text, never an FK to a block row id** —
the one exception is ``se_ports.block_id``, written **in lockstep** with
the freshly minted block ids, inside the same transaction (nm's port
pattern — a port row is always written against the block id that save
just minted, never a stale one).
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis_se.ops import ConnectSpec, PortSpec, SeBlock, SeTree

_BLOCK_COLS = (
    "id, parent_block_id, template_block_id, name, pose_xyz, pose_rot, "
    "envelope, array_spec, descr, use_"
)
_PORT_COLS = "block_id, name, roles, direction, annotations"
_CONNECT_COLS = "a_block, a_port, b_block, b_port, joint, objectives"


def load_tree(store: Any, ref_id: int) -> SeTree:
    """Load a design's live block tree, keyed by name, with its live ports
    (per owning block) and connects (per ref, name-keyed)."""
    with store.pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_BLOCK_COLS} FROM se_blocks "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            rows = cur.fetchall()
            block_ids = [r["id"] for r in rows]
            port_rows: list[dict[str, Any]] = []
            if block_ids:
                cur.execute(
                    f"SELECT {_PORT_COLS} FROM se_ports "
                    "WHERE retired_at IS NULL AND block_id = ANY(%s) "
                    "ORDER BY id ASC",
                    (block_ids,),
                )
                port_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_CONNECT_COLS} FROM se_connects "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            connect_rows = cur.fetchall()
    by_id = {r["id"]: r for r in rows}
    tree = SeTree()
    for r in rows:
        parent_row = by_id.get(r["parent_block_id"])
        template_row = by_id.get(r["template_block_id"])
        tree.blocks[r["name"]] = SeBlock(
            name=r["name"],
            parent=parent_row["name"] if parent_row else None,
            template=template_row["name"] if template_row else None,
            pose=list(r["pose_xyz"] or [0.0, 0.0, 0.0]),
            rot=list(r["pose_rot"] or [0.0, 0.0, 0.0]),
            envelope=r["envelope"],
            descr=r["descr"],
            use=r["use_"],
            array=dict(r["array_spec"]) if r["array_spec"] is not None else None,
        )
    for p in port_rows:
        block_row = by_id.get(p["block_id"])
        if block_row is None:  # pragma: no cover — defensive only
            continue
        node = tree.blocks[block_row["name"]]
        node.ports[p["name"]] = PortSpec(
            name=p["name"],
            roles=list(p["roles"] or []),
            direction=list(p["direction"]) if p["direction"] is not None else None,
            annotations=dict(p["annotations"] or {}),
        )
    for c in connect_rows:
        tree.connects.append(
            ConnectSpec(
                a_block=c["a_block"],
                a_port=c["a_port"],
                b_block=c["b_block"],
                b_port=c["b_port"],
                joint=dict(c["joint"]) if c["joint"] is not None else None,
                objectives=dict(c["objectives"] or {}),
            )
        )
    return tree


def _topo_order(tree: SeTree) -> list[str]:
    """A block-name order where every ``parent`` and every ``template``
    precedes its dependents — the FK-safe INSERT sequence. Acyclic by
    construction (``ops.py`` only lets a block reference an
    already-existing block), so a plain fixed-point pass suffices."""
    placed: set[str] = set()
    order: list[str] = []
    remaining = dict(tree.blocks)
    while remaining:
        progressed = False
        for name, node in list(remaining.items()):
            deps = [d for d in (node.parent, node.template) if d is not None]
            if all(d in placed for d in deps):
                order.append(name)
                placed.add(name)
                del remaining[name]
                progressed = True
        if not progressed:  # pragma: no cover — defensive only, see docstring
            raise RuntimeError(
                f"se block tree has an unresolvable parent/template chain: "
                f"{sorted(remaining)}"
            )
    return order


def save_tree(
    store: Any,
    *,
    ref_id: int,
    tree: SeTree,
    card_text: str,
    conn: Connection | None = None,
) -> None:
    """Retire every live row for ``ref_id`` then reinsert the whole tree,
    and re-emit the ``card_combined`` search chunk — one transaction (joins
    an outer one when ``conn`` is given, e.g. ``put``'s ref-upsert)."""

    def _do(c: Connection) -> None:
        # Ports have no ref_id of their own — reach them through their
        # blocks. Retiring by the blocks' ref (not only the blocks just
        # retired below) also mops up any port left live by an interrupted
        # prior save (nm persist's mop-up rule).
        c.execute(
            "UPDATE se_ports SET retired_at = now() "
            "WHERE retired_at IS NULL AND block_id IN "
            "(SELECT id FROM se_blocks WHERE ref_id = %s)",
            (ref_id,),
        )
        c.execute(
            "UPDATE se_connects SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        c.execute(
            "UPDATE se_blocks SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        name_to_id: dict[str, int] = {}
        for name in _topo_order(tree):
            node = tree.blocks[name]
            parent_id = name_to_id.get(node.parent) if node.parent else None
            template_id = name_to_id.get(node.template) if node.template else None
            row = c.execute(
                "INSERT INTO se_blocks "
                "(ref_id, parent_block_id, template_block_id, name, "
                " pose_xyz, pose_rot, envelope, array_spec, descr, use_) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    ref_id,
                    parent_id,
                    template_id,
                    name,
                    node.pose,
                    node.rot,
                    node.envelope,
                    Jsonb(node.array) if node.array is not None else None,
                    node.descr,
                    node.use,
                ),
            ).fetchone()
            assert row is not None
            name_to_id[name] = int(row[0])
            # Ports in lockstep, right here — the block id this row just
            # got from Postgres is the only one that will ever be valid
            # for this save (module docstring).
            for port in node.ports.values():
                c.execute(
                    "INSERT INTO se_ports "
                    "(block_id, name, roles, direction, annotations) "
                    "VALUES (%s,%s,%s,%s,%s)",
                    (
                        name_to_id[name],
                        port.name,
                        port.roles,
                        port.direction,
                        Jsonb(port.annotations) if port.annotations else None,
                    ),
                )
        for conn_spec in tree.connects:
            # Canonicalize the endpoint order so the unordered-pair
            # uniqueness ops.py promises is exactly what the stored tuple
            # reflects (nm persist's connect canonicalization).
            a, b = sorted(
                (
                    (conn_spec.a_block, conn_spec.a_port),
                    (conn_spec.b_block, conn_spec.b_port),
                )
            )
            c.execute(
                "INSERT INTO se_connects "
                "(ref_id, a_block, a_port, b_block, b_port, joint, objectives) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    ref_id,
                    a[0],
                    a[1],
                    b[0],
                    b[1],
                    Jsonb(conn_spec.joint) if conn_spec.joint is not None else None,
                    Jsonb(conn_spec.objectives) if conn_spec.objectives else None,
                ),
            )
        store.chunks.upsert_card_combined(ref_id, card_text, conn=c)

    if conn is not None:
        _do(conn)
        return
    with store.tx() as c:
        _do(c)


def retire_design(store: Any, ref_id: int) -> int:
    """Soft-retire the ref and every live block (+ ports/connects) under
    it. Returns the number of blocks retired."""
    with store.tx() as conn:
        store.retire_ref(ref_id, conn=conn)
        conn.execute(
            "UPDATE se_ports SET retired_at = now() "
            "WHERE retired_at IS NULL AND block_id IN "
            "(SELECT id FROM se_blocks WHERE ref_id = %s)",
            (ref_id,),
        )
        conn.execute(
            "UPDATE se_connects SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        rows = conn.execute(
            "UPDATE se_blocks SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "RETURNING id",
            (ref_id,),
        ).fetchall()
    return len(rows)
