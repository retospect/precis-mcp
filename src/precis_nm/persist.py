"""Store write-back for the nm block tree — shared by ``put``/``edit``.

**Round-3 addition** — threading (``nm_topology``) rides the same
retire-all/reinsert-all pass as blocks/ports/connects, in the same
transaction, purely to keep its ``retired_at`` bookkeeping in step with the
rest of the design (mirrors ``nm_connects``'s reasoning in this module's
own docstring below). It is written NAME-keyed
(``subject_name``/``object_name``, migration ``0003_nm_bindings.sql``) —
never the id-keyed ``subject_block``/``object_block`` columns 0001
originally gave it, which would strand on the very next save exactly like
an id-keyed port would (this module's "Round-2 landmine" note); those two
columns are simply never populated by this module. ``bound_design``
(blocks) and ``bound_design``/``bound_atom`` (ports, already declared by
0001) round-trip as plain scalar columns — no lockstep concern, they carry
no id reference of their own.

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

**Round-2 landmine** (now resolved, kept as the lockstep discipline going
forward): ``save_tree`` rebuilds *every* ``nm_blocks.id`` on every save
(retire the old rows, INSERT fresh ones with new ids) — so a ``nm_ports``
row keyed by ``block_id`` (a raw row id, per 0001's already-sealed schema)
would silently strand on the very next save of that design, pointing at a
retired block forever. This module persists ports **in lockstep** with the
block tree, in the *same* pass that builds ``name_to_id`` from the fresh
INSERTs, so a port row is always written against the block id that save
just minted — never a stale one. ``nm_connects`` (0002) sidesteps the
problem entirely by not having a ``block_id`` at all — its endpoints are
``ref_id`` + block/port *names* (0002's header) — but this module still
retires and reinserts every live connect on each save, in the same
transaction as the blocks/ports, purely to keep its bookkeeping in step
with the rest of the design.
"""

from __future__ import annotations

from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis_nm.ops import BlockNode, BlockTree, ConnectSpec, PortSpec, ThreadingSpec

_BLOCK_COLS = (
    "id, parent_block_id, template_block_id, name, pose_xyz, pose_rot, "
    "envelope, descr, use_, dof, bound_design"
)
_PORT_COLS = (
    "block_id, name, roles, direction, expected_element, "
    "expected_hybridization, bound_design, bound_atom"
)
_CONNECT_COLS = "a_block, a_port, b_block, b_port, kind, objectives"
_THREADING_COLS = "subject_name, object_name"


def load_tree(store: Any, ref_id: int) -> BlockTree:
    """Load a design's live block tree, keyed by name, with its live ports
    (per owning block) and connects (per ref, name-keyed — see this
    module's docstring)."""
    with store.pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                f"SELECT {_BLOCK_COLS} FROM nm_blocks "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            rows = cur.fetchall()
            block_ids = [r["id"] for r in rows]
            port_rows: list[dict[str, Any]] = []
            if block_ids:
                cur.execute(
                    f"SELECT {_PORT_COLS} FROM nm_ports "
                    "WHERE retired_at IS NULL AND block_id = ANY(%s) "
                    "ORDER BY id ASC",
                    (block_ids,),
                )
                port_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_CONNECT_COLS} FROM nm_connects "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            connect_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_THREADING_COLS} FROM nm_topology "
                "WHERE ref_id = %s AND retired_at IS NULL AND kind = 'threading' "
                "ORDER BY id ASC",
                (ref_id,),
            )
            threading_rows = cur.fetchall()
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
            bound_design=r["bound_design"],
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
            expected_element=p["expected_element"],
            expected_hybridization=p["expected_hybridization"],
            bound_design=p["bound_design"],
            bound_atom=p["bound_atom"],
        )
    for c in connect_rows:
        tree.connects.append(
            ConnectSpec(
                a_block=c["a_block"],
                a_port=c["a_port"],
                b_block=c["b_block"],
                b_port=c["b_port"],
                kind=c["kind"],
                objectives=dict(c["objectives"] or {}),
            )
        )
    for t in threading_rows:
        tree.threading.append(ThreadingSpec(a=t["subject_name"], b=t["object_name"]))
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
        # Ports have no ref_id of their own (0001's schema) — reach them
        # through the blocks they belong to. Retiring by block_id here
        # (rather than only the blocks just retired above) also mops up
        # any port left live by an interrupted prior save.
        c.execute(
            "UPDATE nm_ports SET retired_at = now() "
            "WHERE retired_at IS NULL AND block_id IN "
            "(SELECT id FROM nm_blocks WHERE ref_id = %s)",
            (ref_id,),
        )
        c.execute(
            "UPDATE nm_connects SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        c.execute(
            "UPDATE nm_topology SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL AND kind = 'threading'",
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
                " pose_xyz, pose_rot, envelope, descr, use_, dof, "
                " bound_design) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id",
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
                    node.bound_design,
                ),
            ).fetchone()
            assert row is not None
            name_to_id[name] = int(row[0])
            # Ports in lockstep, right here — the block id this row just
            # got from Postgres is the only one that will ever be valid
            # for this save (module docstring's "Round-2 landmine").
            for port in node.ports.values():
                c.execute(
                    "INSERT INTO nm_ports "
                    "(block_id, name, roles, direction, expected_element, "
                    " expected_hybridization, bound_design, bound_atom) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        name_to_id[name],
                        port.name,
                        port.roles,
                        port.direction,
                        port.expected_element,
                        port.expected_hybridization,
                        port.bound_design,
                        port.bound_atom,
                    ),
                )
        for conn_spec in tree.connects:
            # Canonicalize the endpoint order so the unordered-pair
            # uniqueness ops.py promises (`_connects_endpoint_pair`) is
            # exactly what 0002's ordered-tuple unique index enforces (see
            # 0002_nm_connects.sql's index comment).
            a, b = sorted(
                (
                    (conn_spec.a_block, conn_spec.a_port),
                    (conn_spec.b_block, conn_spec.b_port),
                )
            )
            c.execute(
                "INSERT INTO nm_connects "
                "(ref_id, a_block, a_port, b_block, b_port, kind, objectives) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                (
                    ref_id,
                    a[0],
                    a[1],
                    b[0],
                    b[1],
                    conn_spec.kind,
                    Jsonb(conn_spec.objectives) if conn_spec.objectives else None,
                ),
            )
        for t in tree.threading:
            # NAME-keyed only (this module's docstring / 0003's header) —
            # subject_block/object_block are left NULL.
            c.execute(
                "INSERT INTO nm_topology (ref_id, kind, subject_name, object_name) "
                "VALUES (%s, 'threading', %s, %s)",
                (ref_id, t.a, t.b),
            )
        store.chunks.upsert_card_combined(ref_id, card_text, conn=c)

    if conn is not None:
        _do(conn)
        return
    with store.tx() as c:
        _do(c)


def retire_design(store: Any, ref_id: int) -> int:
    """Soft-retire the ref and every live block (+ its ports) and connect
    under it. Returns the number of blocks retired."""
    with store.tx() as conn:
        store.retire_ref(ref_id, conn=conn)
        conn.execute(
            "UPDATE nm_ports SET retired_at = now() "
            "WHERE retired_at IS NULL AND block_id IN "
            "(SELECT id FROM nm_blocks WHERE ref_id = %s)",
            (ref_id,),
        )
        conn.execute(
            "UPDATE nm_connects SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        conn.execute(
            "UPDATE nm_topology SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        rows = conn.execute(
            "UPDATE nm_blocks SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "RETURNING id",
            (ref_id,),
        ).fetchall()
    return len(rows)
