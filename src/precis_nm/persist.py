"""Store write-back for the nm block tree — shared by ``put``/``edit``.

``precis_nm`` owns dedicated tables (``nm_blocks``/``nm_ports``/
``nm_topology``, migration ``0001_nm_kind.sql``) rather than folding
everything into ``refs.meta`` the way the lighter plugins (``route``,
``protein``, ``pathway``) do — a block tree is genuinely structured graph
data, the ``cad``/``structure``/``pcb`` shape. Core's ``Store`` only mixes
in *core* domain tables (``store/store.py``'s curated mixin list), so a
plugin can't add itself as a mixin; instead this module talks to its own
tables directly over the store's public connection surface
(``store.tx()`` / ``store.pool.connection()``), the same seam
``precis_chem``/``precis_estimate`` use for their own reads/writes.

**Save model** (the ``structure``/``cad`` pattern, not incremental diffing):
:func:`load_tree` reads a design's live blocks into a fresh
:class:`~precis_nm.ops.BlockTree` keyed by **name** (row ids are never
exposed outside this module); :func:`save_tree` retires every live row for
the ref and reinserts the whole tree afresh, in an order that respects both
the parent edge and the template (instance) edge — the same "retire-all,
reinsert-all, identity is the label" discipline
``store._structure_ops.py::structure_save`` uses for atoms/bonds. Cheap for
a design-sized tree; a version-stamped incremental save is a later
refinement if trees ever get large enough to matter.

**Round-2 landmine**: ``save_tree`` rebuilds *every* ``nm_blocks.id`` on
every save (retire the old rows, INSERT fresh ones with new ids) — so a
future ``nm_ports`` row keyed by ``block_id`` (a raw row id) would silently
strand on the very next save of that design, pointing at a retired block
forever. Round 2 must persist ports **in lockstep** with the block tree,
keyed by ``(block name, port name)`` — never by ``nm_ports.block_id``
alone across saves — mirroring how this module already treats
``nm_blocks.name`` (not ``id``) as the stable identity.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis_nm.ops import BlockNode, BlockTree

_BLOCK_COLS = (
    "id, parent_block_id, template_block_id, name, pose_xyz, pose_rot, "
    "envelope, descr, use_, dof"
)


def load_tree(store: Any, ref_id: int) -> BlockTree:
    """Load a design's live block tree, keyed by name."""
    with store.pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_BLOCK_COLS} FROM nm_blocks "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            )
            rows = cur.fetchall()
    by_id = {r["id"]: r for r in rows}
    tree = BlockTree()
    for r in rows:
        parent_row = by_id.get(r["parent_block_id"])
        template_row = by_id.get(r["template_block_id"])
        tree.blocks[r["name"]] = BlockNode(
            name=r["name"],
            parent=parent_row["name"] if parent_row else None,
            template=template_row["name"] if template_row else None,
            pose=list(r["pose_xyz"] or [0.0, 0.0, 0.0]),
            rot=list(r["pose_rot"] or [0.0, 0.0, 0.0]),
            envelope=r["envelope"],
            descr=r["descr"],
            use=r["use_"],
            dof=r["dof"],
        )
    return tree


def _topo_order(tree: BlockTree) -> list[str]:
    """A block-name order where every ``parent`` and every ``template``
    precedes its dependents — the FK-safe INSERT sequence.

    ``ops.py`` only ever lets a block reference an *already-existing* block
    as its parent or template (``add_block``/``instance_block`` both
    require the reference to pre-exist in the tree), so the combined
    parent+template graph is acyclic by construction; this is a plain
    fixed-point pass, not a general topo-sort, because nm trees are small.
    """
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
                f"nm block tree has an unresolvable parent/template chain: "
                f"{sorted(remaining)}"
            )
    return order


def save_tree(
    store: Any,
    *,
    ref_id: int,
    tree: BlockTree,
    card_text: str,
    conn: Connection | None = None,
) -> None:
    """Retire every live block for ``ref_id`` then reinsert the whole tree,
    and re-emit the ``card_combined`` search chunk — one transaction (joins
    an outer one when ``conn`` is given, e.g. ``put``'s ref-upsert)."""

    def _do(c: Connection) -> None:
        c.execute(
            "UPDATE nm_blocks SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        name_to_id: dict[str, int] = {}
        for name in _topo_order(tree):
            node = tree.blocks[name]
            parent_id = name_to_id.get(node.parent) if node.parent else None
            template_id = name_to_id.get(node.template) if node.template else None
            row = c.execute(
                "INSERT INTO nm_blocks "
                "(ref_id, parent_block_id, template_block_id, name, "
                " pose_xyz, pose_rot, envelope, descr, use_, dof) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
                (
                    ref_id,
                    parent_id,
                    template_id,
                    name,
                    node.pose,
                    node.rot,
                    node.envelope,
                    node.descr,
                    node.use,
                    Jsonb(node.dof) if node.dof is not None else None,
                ),
            ).fetchone()
            assert row is not None
            name_to_id[name] = int(row[0])
        store.chunks.upsert_card_combined(ref_id, card_text, conn=c)

    if conn is not None:
        _do(conn)
        return
    with store.tx() as c:
        _do(c)


def retire_design(store: Any, ref_id: int) -> int:
    """Soft-retire the ref and every live block under it. Returns the
    number of blocks retired."""
    with store.tx() as conn:
        store.retire_ref(ref_id, conn=conn)
        rows = conn.execute(
            "UPDATE nm_blocks SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "RETURNING id",
            (ref_id,),
        ).fetchall()
    return len(rows)
