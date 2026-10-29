"""Store write-back for the se block tree — shared by ``put``/``edit``.

The :mod:`precis_nm.persist` discipline, transferred whole (see that
module's docstring for the full reasoning — the "Round-2 landmine" there is
designed out here from day one): a design's blocks live in dedicated tables
(``se_blocks``/``se_ports``/``se_connects``, migration ``0001_se_kind.sql``;
``se_measures`` from ``0002``, ``se_bom`` from ``0003``, ``se_notes``
from ``0005``, ``se_topology`` — the atomic mode's L2 threading, and from
``0015`` the nucleic-acid domain's ``kind='domain'`` route rows beside it
(one table, two kinds, one retire pass) — from
``0007``, ``se_optics`` — the design's FRET medium/pump context, ONE live
row per design rather than a ledger — from ``0010``, and ``se_pockets``
— block-row keyed like ``se_ports`` — from ``0018``, and ``se_rays``,
keyed the same way, from ``0024``)
reached over the store's public connection surface (``store.tx()`` /
``store.pool.connection()``) — a plugin never joins core's mixin list.

**Save model** (retire-all/reinsert-all): :func:`load_tree` reads a
design's live blocks into a fresh :class:`~precis_se.ops.SeTree` keyed by
name; :func:`save_tree` retires every live row for the ref and reinserts
the whole tree afresh in parent/template-respecting order. Row ids are
rebuilt on every save, which is exactly why nothing cross-referencing
(connect endpoints, measure blocks, BOM targets, threading
subject/object) is an FK to a block row id — the one exception is
``se_ports.block_id``, written **in lockstep** with the freshly minted
block ids, inside the same transaction (nm's port pattern — a port row is
always written against the block id that save just minted, never a stale
one).

**Identity is the block ``uid``** (migration ``0009_se_block_uid.sql``,
docs/backlog/design-state-core.md item 2), minted from core's
``design_block_uid_seq`` (:func:`precis.design.uids.mint_uids`) and
carried forward here across the retire/reinsert cycle as ordinary column
data. It used to be the block *name*; a name is now a **display label** —
still unique within a live design (the ``se_blocks_ref_name_key`` index
stands), still what the in-memory tree is keyed by, but resolved to a uid
at write time and rendered back from the uid at read time. Every
in-design cross-reference stores **both**: the uid is the authoritative
join, the name is the display label and the fallback for the one case a
uid cannot cover — a **dangling** reference (a measure on a removed
block, a BOM line naming a block not added yet), which is a read-time DRC
finding and never a write-time rejection, so those uid columns are
nullable.

A uid-less block adopts the uid of the live row with the same name ONLY
when no block in the incoming tree carries a uid at all — i.e. the tree
was reconstructed wholesale (a ``put``), where a label is the only
identity evidence there is. On the ``edit`` path every surviving block
arrives carrying its uid, so a uid-less node is by construction a NEW
block and always mints (:func:`_assign_uids` for the full rule and the
remove-then-re-add case that forces it).

Copying a design's rows with their uids intact is what makes a branch
diff block-by-block (design-state-core.md item 5): the se ``pin`` op
copies through :func:`tree_to_json`/:func:`tree_from_json` and
:func:`save_tree`, and ``view='diff'`` joins the two on uid.

Soft references stay name text on purpose and are NOT part of the uid
cutover: a measure ``relation.source`` (``'block.measure'``) and a note's
``re``/``about`` anchors are declared dangling-tolerant by their own
modules (:mod:`precis_se.measures`, :mod:`precis.utils.notes`) — "a
dangling anchor is the read-time honest annotation, never a write-time
error".

**``template_ref`` is name-keyed TEXT, not a row-id FK** (migration
``0004_se_template_ref.sql``, docs/backlog/blocktree-library-build-plan.md
slice 1 — the ``precis_nm.persist`` counterpart transferred verbatim): a
bare local block name, or ``<design-slug>#<block-name>`` naming a block in
ANOTHER live ``se`` design. ``node.template`` round-trips through this
column unchanged — resolution happens only at READ time, in
:mod:`precis.blocktree.ops`, never here. Both forms additionally store
``template_uid`` and are rendered back from it, so they survive a relabel
like every other cross-reference: a **local** reference (0009) by this
design's uid map, a **cross-design** one (0020) by ``(design slug, uid)``
— a uid is unique per design only, since branch copies share them — in
one batched query each way (:func:`_foreign_template_uids` at save,
:func:`_foreign_template_labels` at load), never a foreign
:func:`load_tree`. The slug half stays text: an se slug is the design's
public identity.

**Every load→mutate→save of a tree runs inside :func:`tree_mutation`**
(the invariant, 2026-09-18). Retire-all/reinsert-all means two
read-modify-write cycles on one design that overlap in time silently
lose one of them: whichever saves second retires the other's rows and
reinserts its own stale copy. ``put``/``edit`` and the ``se_simp`` job's
bind step therefore all take a per-ref ``pg_advisory_xact_lock`` before
they load, and save on the same connection before it commits — a
concurrent writer either committed before the lock was granted (so the
load sees it) or waits. The lock is transaction-scoped, so no failure
path can leak it. Long work (a solve) happens OUTSIDE the lock on a
snapshot and re-validates its inputs against a fresh load inside it.

A ``component`` binding additionally **projects onto a ``realized-by``
link** on every save (:func:`sync_realized_by`, migration 0156's realization
edge, the same one cad writes for its ``part`` lines). The plugin table stays
authoritative and the link is derived and rebuilt, so one ``links`` query
answers "what does this artifact resolve to" — and its inverse "who calls
for this component" — across both tracks.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator
from contextlib import contextmanager, nullcontext
from dataclasses import asdict, fields
from datetime import datetime
from typing import Any

import psycopg
from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.blocktree.types import TEMPLATE_SEP, parse_template_ref
from precis.design.uids import mint_uids
from precis.errors import NotFound
from precis.pcb import export as pcb_export
from precis.utils.file_id import nearest_slugs
from precis_se import catalog
from precis_se.atomic.vocab import ThreadingSpec
from precis_se.board import BoardDerived, derive_board
from precis_se.bom import BomLine
from precis_se.catalog import Derived
from precis_se.chain.vocab import DomainSpec
from precis_se.measures import MeasureSpec
from precis_se.notes import NoteSpec
from precis_se.ops import (
    BoundScene,
    ConnectSpec,
    PortSpec,
    SeBlock,
    SeTree,
    compose_world_pose,
)
from precis_se.pockets import PocketSpec
from precis_se.properties.measurand import measurand_resolver
from precis_se.rays import RaySpec

log = logging.getLogger(__name__)

#: Marks the ``realized-by`` links :func:`sync_realized_by` manages, so it
#: prunes only its own rows. Deliberately a bare flag and not a list of
#: the blocks that bind the component: ``add_link`` is idempotent on the
#: edge tuple and does not rewrite ``meta`` on conflict, so anything
#: richer here would go stale the first time a design was edited. The
#: block list lives in ``se_blocks``, which is the authority; the link
#: carries only what makes it prunable.
_SE_MANAGED = "se_binding"

_BLOCK_COLS = (
    "id, uid, parent_block_id, template_ref, template_uid, name, pose_xyz, "
    "pose_rot, envelope, array_spec, descr, use_, objectives, mode, "
    "bound_kind, bound_design, origins, dof, chromophore, process_overrides, "
    "build_frame, chain, provenance, insertion, material"
)
_PORT_COLS = (
    "block_id, name, roles, direction, annotations, expected_element, "
    "expected_hybridization, bound_design, bound_atom, axis_atom, "
    "phase_atom, pose_xyz, pose_rot, pose_source, rot_source"
)
_CONNECT_COLS = (
    "a_block, a_block_uid, a_port, b_block, b_block_uid, b_port, joint, "
    "kind, objectives, optical, contract"
)
_MEASURE_COLS = (
    "block, block_uid, name, value, relation, strength, reason, min_value, "
    "max_value, origin, unit, datum, measurand, measurand_ref_id, datum_pin, "
    "provenance"
)
#: ``se_pockets`` (migration ``0018_se_regions.sql``) — block-row keyed
#: like ``se_ports``, written in lockstep with the fresh block ids.
_POCKET_COLS = "block_id, name, shape, regions, class_hash"
#: ``se_rays`` (plugin migration ``0024_se_rays.sql``) — block-row keyed
#: like ``se_pockets``; ``definition`` + ``digests``, never events.
_RAY_COLS = "block_id, name, definition, digests"


def _has_rays_table(cur: Any) -> bool:
    """``se_rays`` exists — the pre-0024 guard: a plain catalog read, so a
    DB without the table loads and saves rayless instead of aborting the
    caller's transaction on ``UndefinedTable``."""
    cur.execute("SELECT to_regclass('se_rays') IS NOT NULL AS ok")
    row = cur.fetchone()
    if row is None:
        return False
    return bool(row["ok"] if isinstance(row, dict) else row[0])


_BOM_COLS = (
    "block, block_uid, a_block, a_block_uid, a_port, b_block, b_block_uid, "
    "b_port, item_kind, item, qty, uom, reason"
)
_NOTE_COLS = "name, kind, body, re, about, origin, created_at"
#: ``se_topology`` (migration 0007) carries its endpoints the same way
#: ``se_connects`` does — uid as the join, name as the display label
#: (0009) — never a block-row FK, which would strand on the very next save
#: (module docstring's lockstep rule).
_THREADING_COLS = "subject_name, subject_uid, object_name, object_uid"
#: ``se_topology`` ``kind='domain'`` rows (migration ``0015_se_chain.sql``)
#: — the same endpoint columns plus ``meta``, which ``_THREADING_COLS``
#: deliberately has none of: a threading row IS the fact, while a domain
#: row carries the stretch it covers (``{ord, forward, start, end, …}``).
_DOMAIN_COLS = "subject_name, subject_uid, object_name, object_uid, meta"
#: ``se_optics`` (migration 0010) is the one tree-level scalar record —
#: everything else here is a list keyed by name/pair, so it gets its own
#: short column tuple rather than folding into one of the above.
_OPTICS_COLS = "medium_index, excitation_nm, channels_available"


def _label(uid_to_name: dict[int, str], uid: int | None, stored: str | None) -> Any:
    """One cross-reference endpoint, read back as a block *label*.

    The uid is authoritative: a reference that resolves is rendered from
    the live block's current name, so a relabel can never leave a stale
    endpoint behind. The stored name text is the fallback for the one case
    the uid cannot answer — a **dangling** reference (uid NULL because the
    name never resolved at save time, or pointing at a block since
    removed), which downstream treats as a read-time DRC finding and needs
    a subject to name.
    """
    if uid is not None:
        resolved = uid_to_name.get(int(uid))
        if resolved is not None:
            return resolved
    return stored


#: Advisory-lock namespace for :func:`tree_mutation` — the ``(int4, int4)``
#: overload keyed ``(namespace, ref_id)``; distinct from the draft store's
#: ``hashtextextended`` keys, so an se lock can never collide with one.
TREE_LOCK_NAMESPACE = 0x5E7EE  # "se tree"


@contextmanager
def tree_mutation(store: Any, ref_id: int) -> Iterator[Connection]:
    """Open ``store.tx()`` and take the design's per-ref advisory lock,
    held to commit/rollback (module docstring's invariant). Load the tree
    *after* entering, save it on the yielded connection: ::

        with persist.tree_mutation(store, ref.id) as conn:
            tree = persist.load_tree(store, ref.id)
            ...mutate...
            persist.save_tree(store, ref_id=ref.id, tree=tree, ..., conn=conn)

    ``load_tree`` reads on its own pooled connection; that is correct
    here because every writer commits before releasing this lock, so a
    read issued after the lock is granted sees the latest committed
    tree."""
    with store.tx() as conn:
        conn.execute(
            "SELECT pg_advisory_xact_lock(%s, %s)", (TREE_LOCK_NAMESPACE, int(ref_id))
        )
        yield conn


def load_tree(store: Any, ref_id: int, *, conn: Connection | None = None) -> SeTree:
    """Load a design's live block tree, keyed by name (the display label),
    with its live ports (per owning block) and its uid-keyed
    cross-references resolved back to labels (module docstring).

    The returned tree carries its own :func:`foreign_resolver`, so a
    cross-design ``template`` resolves for every reader, not just the ones
    that remember to wire one. ``own_slug`` is NOT set here (this takes a
    ``ref_id``, not a slug): a caller that needs a foreign design's
    reference back into THIS one recognised as the same node — the
    cross-design cycle check, write-time only — sets it itself
    (:class:`precis_se.handler.SeHandler`).

    ``conn`` reads through the caller's connection — inside a save's own
    transaction that is the only way to see the rows it just wrote, which
    is how the handler snapshots a revision (:func:`tree_to_json`) as the
    rows ARE rather than as the in-memory tree was.
    """
    with nullcontext(conn) if conn is not None else store.pool.connection() as conn:
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
            pocket_rows: list[dict[str, Any]] = []
            if block_ids:
                cur.execute(
                    f"SELECT {_POCKET_COLS} FROM se_pockets "
                    "WHERE retired_at IS NULL AND block_id = ANY(%s) "
                    "ORDER BY id ASC",
                    (block_ids,),
                )
                pocket_rows = cur.fetchall()
            ray_rows: list[dict[str, Any]] = []
            if block_ids and _has_rays_table(cur):
                cur.execute(
                    f"SELECT {_RAY_COLS} FROM se_rays "
                    "WHERE retired_at IS NULL AND block_id = ANY(%s) "
                    "ORDER BY id ASC",
                    (block_ids,),
                )
                ray_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_CONNECT_COLS} FROM se_connects "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            connect_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_MEASURE_COLS} FROM se_measures "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            measure_rows = cur.fetchall()
            # Identity is the taxon ref id; the slug is a rename-able name
            # (:func:`precis_se.measures.measurand_name`). ONE batched
            # query refreshes every measure's displayed slug; a retired or
            # deleted taxon simply has no row, and the snapshot stands.
            measurand_ids = sorted(
                {
                    int(m["measurand_ref_id"])
                    for m in measure_rows
                    if m["measurand_ref_id"] is not None
                }
            )
            live_slugs: dict[int, str] = {}
            if measurand_ids:
                cur.execute(
                    "SELECT ref_id, meta->>'slug' AS slug FROM refs "
                    "WHERE kind = 'taxon' AND retired_at IS NULL "
                    "AND ref_id = ANY(%s)",
                    (measurand_ids,),
                )
                live_slugs = {
                    int(r["ref_id"]): str(r["slug"])
                    for r in cur.fetchall()
                    if r["slug"]
                }
            cur.execute(
                f"SELECT {_BOM_COLS} FROM se_bom "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            bom_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_NOTE_COLS} FROM se_notes "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY created_at ASC, id ASC",
                (ref_id,),
            )
            note_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_THREADING_COLS} FROM se_topology "
                "WHERE ref_id = %s AND retired_at IS NULL AND kind = 'threading' "
                "ORDER BY id ASC",
                (ref_id,),
            )
            threading_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_DOMAIN_COLS} FROM se_topology "
                "WHERE ref_id = %s AND retired_at IS NULL AND kind = 'domain' "
                "ORDER BY (meta->>'ord')::int ASC, id ASC",
                (ref_id,),
            )
            domain_rows = cur.fetchall()
            cur.execute(
                f"SELECT {_OPTICS_COLS} FROM se_optics "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            )
            optics_row = cur.fetchone()
            # The workspaces records (``refs.meta['workspaces'][<slug>]``,
            # :mod:`precis_se.workspace_import`) carry the one block fact
            # no ``se_*`` table stores: which blocks a manifest flagged
            # ``template: true`` (gr514241).
            cur.execute(
                "SELECT meta->'workspaces' AS ws FROM refs WHERE ref_id = %s",
                (ref_id,),
            )
            ws_row = cur.fetchone()
        foreign_labels = _foreign_template_labels(conn, rows)
    by_id = {r["id"]: r for r in rows}
    #: uid → the block's current label, the read half of the cutover.
    uid_to_name = {int(r["uid"]): r["name"] for r in rows if r["uid"] is not None}
    tree = SeTree()
    tree.from_persistence = True
    for r in rows:
        parent_row = by_id.get(r["parent_block_id"])
        tree.blocks[r["name"]] = SeBlock(
            name=r["name"],
            uid=int(r["uid"]) if r["uid"] is not None else None,
            parent=parent_row["name"] if parent_row else None,
            # A template reference comes back from its uid — local, or
            # (slug, uid) for a cross-design one — module docstring.
            template=_template_label(uid_to_name, foreign_labels, r),
            # As stored — the block origin in the PARENT's frame
            # (precis-se-help.md's Units section). ``pose``/``rot`` (below,
            # via compose_world_pose) become the WORLD-frame placement
            # every geometry consumer reads; local_pose/local_rot are what
            # save_tree writes back, byte-identical to these bytes for any
            # block this load→edit→save cycle never touches.
            local_pose=list(r["pose_xyz"] or [0.0, 0.0, 0.0]),
            local_rot=list(r["pose_rot"] or [0.0, 0.0, 0.0]),
            envelope=r["envelope"],
            descr=r["descr"],
            use=r["use_"],
            array=dict(r["array_spec"]) if r["array_spec"] is not None else None,
            objectives=dict(r["objectives"] or {}),
            mode=r["mode"],
            bound_kind=r["bound_kind"],
            bound=r["bound_design"],
            material=r["material"],
            origins=dict(r["origins"] or {}),
            provenance=dict(r["provenance"] or {}),
            insertion=(dict(r["insertion"]) if r["insertion"] is not None else None),
            dof=dict(r["dof"]) if r["dof"] is not None else None,
            chromophore=dict(r["chromophore"])
            if r["chromophore"] is not None
            else None,
            process_overrides=dict(r["process_overrides"])
            if r["process_overrides"] is not None
            else None,
            build_frame=dict(r["build_frame"])
            if r["build_frame"] is not None
            else None,
            chain=dict(r["chain"]) if r["chain"] is not None else None,
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
            expected_element=p["expected_element"],
            expected_hybridization=p["expected_hybridization"],
            bound_design=p["bound_design"],
            bound_atom=p["bound_atom"],
            axis_atom=p["axis_atom"],
            phase_atom=p["phase_atom"],
            pose=list(p["pose_xyz"]) if p["pose_xyz"] is not None else None,
            rot=list(p["pose_rot"]) if p["pose_rot"] is not None else None,
            pose_source=p["pose_source"],
            rot_source=p["rot_source"],
        )
    for k in pocket_rows:
        block_row = by_id.get(k["block_id"])
        if block_row is None:  # pragma: no cover — defensive only
            continue
        tree.blocks[block_row["name"]].pockets[k["name"]] = PocketSpec(
            name=k["name"],
            shape=k["shape"],
            regions=list(k["regions"] or []),
            class_hash=k.get("class_hash"),
        )
    for k in ray_rows:
        block_row = by_id.get(k["block_id"])
        if block_row is None:  # pragma: no cover — defensive only
            continue
        tree.blocks[block_row["name"]].rays[k["name"]] = RaySpec.from_row(
            k["name"], k["definition"], k["digests"]
        )
    for c in connect_rows:
        tree.connects.append(
            ConnectSpec(
                a_block=_label(uid_to_name, c["a_block_uid"], c["a_block"]),
                a_port=c["a_port"],
                b_block=_label(uid_to_name, c["b_block_uid"], c["b_block"]),
                b_port=c["b_port"],
                joint=dict(c["joint"]) if c["joint"] is not None else None,
                kind=c["kind"],
                objectives=dict(c["objectives"] or {}),
                optical=dict(c["optical"]) if c["optical"] is not None else None,
                contract=(dict(c["contract"]) if c["contract"] is not None else None),
            )
        )
    for m in measure_rows:
        tree.measures.append(
            MeasureSpec(
                block=_label(uid_to_name, m["block_uid"], m["block"]),
                name=m["name"],
                value=m["value"],
                relation=dict(m["relation"]) if m["relation"] is not None else None,
                strength=m["strength"],
                reason=m["reason"],
                min_value=m["min_value"],
                max_value=m["max_value"],
                origin=m["origin"],
                unit=m["unit"],
                datum=m["datum"],
                measurand=m["measurand"],
                measurand_ref=(
                    int(m["measurand_ref_id"])
                    if m["measurand_ref_id"] is not None
                    else None
                ),
                datum_pin=m["datum_pin"],
                provenance=(
                    dict(m["provenance"]) if m["provenance"] is not None else None
                ),
            )
        )
    for taxon_id, live in live_slugs.items():
        for spec in tree.measures:
            if spec.measurand_ref == taxon_id:
                spec.measurand_live = live
    for b in bom_rows:
        tree.bom.append(
            BomLine(
                item_kind=b["item_kind"],
                item=b["item"],
                qty=float(b["qty"]),
                block=_label(uid_to_name, b["block_uid"], b["block"]),
                a_block=_label(uid_to_name, b["a_block_uid"], b["a_block"]),
                a_port=b["a_port"],
                b_block=_label(uid_to_name, b["b_block_uid"], b["b_block"]),
                b_port=b["b_port"],
                uom=b["uom"],
                reason=b["reason"],
            )
        )
    for n in note_rows:
        tree.notes.append(
            NoteSpec(
                name=n["name"],
                kind=n["kind"],
                body=n["body"],
                re=n["re"],
                about=list(n["about"] or []),
                origin=n["origin"],
                created_at=n["created_at"],
            )
        )
    for t in threading_rows:
        tree.threading.append(
            ThreadingSpec(
                a=_label(uid_to_name, t["subject_uid"], t["subject_name"]),
                b=_label(uid_to_name, t["object_uid"], t["object_name"]),
            )
        )
    for d in domain_rows:
        meta = dict(d["meta"] or {})
        tree.domains.append(
            DomainSpec(
                strand=_label(uid_to_name, d["subject_uid"], d["subject_name"]),
                helix=_label(uid_to_name, d["object_uid"], d["object_name"]),
                ord=int(meta.get("ord", 0)),
                forward=bool(meta.get("forward")),
                start=int(meta.get("start", 0)),
                end=int(meta.get("end", 0)),
                geometry=meta.get("geometry"),
                overrides=dict(meta["overrides"])
                if meta.get("overrides") is not None
                else None,
                loop_before_nt=(
                    int(meta["loop_before_nt"])
                    if meta.get("loop_before_nt") is not None
                    else None
                ),
                # A placed loop's settled curve (``relax_chain``'s realizer
                # seam). Absent stays absent — the distinction between "no
                # curve" and "a curve" is the seam itself, so an empty list
                # is never reconstructed as one.
                loop_curve=(
                    [[float(v) for v in point] for point in meta["loop_curve"]]
                    if meta.get("loop_curve")
                    else None
                ),
            )
        )
    # ``template: true`` declarations from every workspace record on this
    # design — unioned because a block is the last importing workspace's
    # to describe, but a library flag it carried is design data, not one
    # run's scratch (gr514241). A name the record lists but the tree no
    # longer has is dropped state, not a marker to resurrect.
    for ws in ((ws_row or {}).get("ws") or {}).values():
        if not isinstance(ws, dict):
            continue
        for tname in ws.get("templates") or []:
            tnode = tree.blocks.get(str(tname))
            if tnode is not None:
                tnode.template_only = True
    if optics_row is not None:
        tree.optics = {
            "medium_index": optics_row["medium_index"],
            **(
                {"excitation_nm": optics_row["excitation_nm"]}
                if optics_row["excitation_nm"] is not None
                else {}
            ),
            **(
                {"channels_available": int(optics_row["channels_available"])}
                if optics_row["channels_available"] is not None
                else {}
            ),
        }
    attach_catalog(store, tree)
    attach_boards(store, tree)
    # Every block now carries its stored, parent-relative local_pose/
    # local_rot; compose them up the parent chain into WORLD-frame pose/
    # rot ONCE, here, so every reader of a loaded tree (validate/fasten/
    # toolaccess/stability/the renderers/datums/measures/printing) can go
    # on reading ``pose``/``rot`` and trust world frame, as their own
    # docstrings already (and now correctly) claim.
    compose_world_pose(tree)
    # Wired here, not at each call site: every reader of a loaded tree
    # (handler, web reader, jobs) then resolves a cross-design template the
    # same way, and a new reader cannot forget to (:func:`foreign_resolver`).
    tree.foreign = foreign_resolver(store)
    # Same reasoning for ``measurand=`` on a later measure op (the web
    # workbench's dry run applies pure ops to a loaded tree).
    tree.measurands = measurand_resolver(store)
    tree.structure_versions = structure_version_resolver(store)
    tree.structures = structure_scene_resolver(store)
    tree.cad_designs = cad_design_resolver(store)
    # The run trace a bound structure's ``generated.trace`` points at
    # (``frame:<key>`` selectors, ``view='trace'``); the design's own
    # ref_id is the chunk home for containerless (``run_job=None``)
    # imports.
    tree.traces = trace_doc_resolver(store, ref_id)
    # The bound structure's stored π perception (``pi:`` selectors, the
    # structure fisheye's π block — slice 2e).
    tree.structure_pi = structure_pi_resolver(store)
    # Same door for the store-free geometry paths that must resolve a
    # ``field:<sha>`` envelope (the flow COMPUTERS' chamber builder,
    # gr503066); a store without the cad ops (a read fake) leaves it off.
    loader_of = getattr(store, "field_loader", None)
    tree.field_loader = loader_of() if loader_of is not None else None
    tree.materials = material_resolver(store)
    return tree


#: The snapshot format :func:`tree_to_json` writes; bumped only if the
#: shape changes incompatibly (an added dataclass field is not that — the
#: reader takes what it knows and defaults the rest).
TREE_JSON_FORMAT = "se-tree/1"

#: :class:`SeBlock` fields that are NOT design data and never snapshot:
#: ``derived`` is recomputed from the catalog on every load
#: (:func:`attach_catalog`); the ``pending_*`` quartet is one call's
#: not-yet-written state-op payload (the class docstring — cleared by
#: discarding the tree); ``posed_by_state`` is a read-time label for
#: whose pose a block is currently carrying; and ``template_only`` is
#: surfaced from the workspaces record at load time, not stored on the
#: block row (gr514241). Grep the dataclass when
#: adding a field here — the count in this comment has drifted before
#: (gr458297).
_BLOCK_TRANSIENT = frozenset(
    {
        "derived",
        "pending_states",
        "pending_transitions",
        "pending_current_state",
        "pending_state_poses",
        "posed_by_state",
        "template_only",
    }
)


def tree_to_json(tree: SeTree) -> dict[str, Any]:
    """Serialise a tree to a JSON-clean dict — the ``payload`` of a
    ``design_checkpoints`` row (:func:`precis.design.history.
    save_checkpoint`; design-workbench build, slice 2).

    Design data only: blocks (uid included — that is the identity a
    restore must carry back), ports, connects, measures, BOM, notes,
    threading, optics. Transient fields (:data:`_BLOCK_TRANSIENT`,
    ``own_slug``, ``foreign``, ``from_persistence``) are not design data
    and are left out. ``created_at`` on a note is ISO 8601 text — the one
    non-JSON scalar in the tree. Inverse: :func:`tree_from_json`.
    """
    blocks = [
        {k: v for k, v in asdict(node).items() if k not in _BLOCK_TRANSIENT}
        for node in tree.blocks.values()
    ]
    for b in blocks:
        # an unset bundle is absent, not null — ray-bearing snapshots from
        # before bundles existed keep their bytes (and their sha)
        for ray in (b.get("rays") or {}).values():
            if ray.get("bundle") is None:
                ray.pop("bundle", None)
    notes = []
    for note in tree.notes:
        d = asdict(note)
        d["created_at"] = (
            note.created_at.isoformat() if note.created_at is not None else None
        )
        notes.append(d)
    return {
        "format": TREE_JSON_FORMAT,
        "blocks": blocks,
        "connects": [asdict(c) for c in tree.connects],
        # ``measurand_live`` is derived (refreshed on load), not design data.
        "measures": [
            {k: v for k, v in asdict(m).items() if k != "measurand_live"}
            for m in tree.measures
        ],
        "bom": [asdict(b) for b in tree.bom],
        "notes": notes,
        "threading": [asdict(t) for t in tree.threading],
        "domains": [asdict(d) for d in tree.domains],
        "optics": dict(tree.optics) if tree.optics is not None else None,
    }


def tree_from_json(payload: dict[str, Any], *, store: Any = None) -> SeTree:
    """Rebuild a tree from :func:`tree_to_json`'s output.

    The result equals :func:`load_tree`'s for the same rows — the
    round-trip a revert relies on (``load_checkpoint`` + :func:`save_tree`).
    ``from_persistence`` is set, since the blocks arrive carrying uids the
    same way loaded rows do (a uid-less block in a restored tree is, by
    construction, one that was added since). With ``store`` given the
    catalog-derived facets and the foreign resolver are wired exactly as
    :func:`load_tree` wires them; without one the tree is bare design
    data.

    Unknown keys are dropped and missing ones take the dataclass default,
    so a snapshot written before a field existed (or after one was
    removed) still restores.
    """
    fmt = payload.get("format")
    if fmt != TREE_JSON_FORMAT:
        raise ValueError(f"not an se tree snapshot: format={fmt!r}")
    tree = SeTree()
    tree.from_persistence = True
    for d in payload.get("blocks") or []:
        ports = {
            name: PortSpec(**_known(PortSpec, p))
            for name, p in (d.get("ports") or {}).items()
        }
        pockets = {
            name: PocketSpec(**_known(PocketSpec, k))
            for name, k in (d.get("pockets") or {}).items()
        }
        rays = {
            name: RaySpec.from_row(
                name,
                {k: v for k, v in r.items() if k not in ("name", "digests")},
                r.get("digests"),
            )
            for name, r in (d.get("rays") or {}).items()
        }
        node = SeBlock(
            **{
                **_known(SeBlock, d),
                "ports": ports,
                "pockets": pockets,
                "rays": rays,
            }
        )
        tree.blocks[node.name] = node
    # "equals load_tree's for the same rows" (docstring) includes pose:
    # a payload from BEFORE this engine fix has no local_pose/local_rot,
    # only the ``pose``/``rot`` that, pre-fix, WAS the stored relative
    # value — SeBlock.__post_init__'s empty-list sentinel already falls
    # local_pose/local_rot back to exactly that, so composing here (same
    # as load_tree) is correct for a pre- or post-fix snapshot alike.
    compose_world_pose(tree)
    tree.connects = [
        ConnectSpec(**_known(ConnectSpec, c)) for c in payload.get("connects") or []
    ]
    tree.measures = [
        MeasureSpec(**_known(MeasureSpec, m)) for m in payload.get("measures") or []
    ]
    tree.bom = [BomLine(**_known(BomLine, b)) for b in payload.get("bom") or []]
    for n in payload.get("notes") or []:
        d = _known(NoteSpec, n)
        raw = d.get("created_at")
        d["created_at"] = datetime.fromisoformat(raw) if isinstance(raw, str) else None
        tree.notes.append(NoteSpec(**d))
    tree.threading = [
        ThreadingSpec(**_known(ThreadingSpec, t))
        for t in payload.get("threading") or []
    ]
    tree.domains = [
        DomainSpec(**_known(DomainSpec, d)) for d in payload.get("domains") or []
    ]
    optics = payload.get("optics")
    tree.optics = dict(optics) if optics is not None else None
    if store is not None:
        attach_catalog(store, tree)
        attach_boards(store, tree)
        tree.foreign = foreign_resolver(store)
        tree.measurands = measurand_resolver(store)
        tree.structure_versions = structure_version_resolver(store)
        tree.structures = structure_scene_resolver(store)
        tree.cad_designs = cad_design_resolver(store)
        tree.materials = material_resolver(store)
        tree.traces = trace_doc_resolver(store, None)
        tree.structure_pi = structure_pi_resolver(store)
    return tree


def _known(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """``d`` narrowed to ``cls``'s dataclass fields — :func:`tree_from_json`'s
    forward/backward tolerance."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


def material_resolver(store: Any) -> Callable[[str], dict[str, float] | None]:
    """A ``kind='material'`` slug → ``{property_id: value_num}`` (canonical
    SI units — the kind is canonical-only), or ``None`` when the slug is
    gone. Memoized per returned closure. Backs the flow adapter's
    ``fluid=`` (se-multiphysics.md slice 1): μ/ρ/D are read off the row —
    a missing property is a reason, never a silent default."""
    cache: dict[str, dict[str, float] | None] = {}

    def props_of(slug: str) -> dict[str, float] | None:
        if slug not in cache:
            ref = store.get_ref(kind="material", id=slug)
            out: dict[str, float] | None = None
            if ref is not None:
                out = {
                    str(v["property_id"]): float(v["value_num"])
                    for v in store.material_values_for_ref(ref.id)
                    if v.get("property_id") and v.get("value_num") is not None
                }
            cache[slug] = out
        return cache[slug]

    return props_of


def structure_version_resolver(store: Any) -> Callable[[str], int | None]:
    """A structure design's slug → its CURRENT version
    (``refs.meta['version']``, bumped by every save in
    :mod:`precis.handlers.structure`), or ``None`` when no live design has
    that slug. Memoized per returned closure — one lookup per slug per
    call. Backs the ``datum_pin`` stamp at write and the stale-pin check at
    read (:func:`precis_se.datums.stale_pin_note`)."""
    cache: dict[str, int | None] = {}

    def version_of(slug: str) -> int | None:
        if slug not in cache:
            ref = store.get_ref(kind="structure", id=slug)
            meta = (ref.meta or {}) if ref is not None else {}
            cache[slug] = int(meta.get("version", 0)) if ref is not None else None
        return cache[slug]

    return version_of


def structure_scene_resolver(store: Any) -> Callable[[str], BoundScene | None]:
    """A structure design's slug → its loaded :class:`BoundScene` (scene
    plus ref meta — ``meta['generated']`` carries the seam-site records
    ``sites:`` selectors index into, ``meta['version']`` the pin's
    currency), or ``None`` when no live design has the slug or its load
    fails (a resolver answers "cannot load", never raises — the lenient
    posture :mod:`precis_se.datums` resolves region selectors under).
    Memoized per returned closure — one load per slug per call; backs
    ``atoms:``/``sites:``/``mpatch:`` resolution and the wall-side region
    computers."""
    cache: dict[str, BoundScene | None] = {}

    def scene_of(slug: str) -> BoundScene | None:
        if slug not in cache:
            out: BoundScene | None = None
            try:
                ref = store.get_ref(kind="structure", id=slug)
                if ref is not None:
                    scene, _handles = store.structure_load(ref.id)
                    out = BoundScene(
                        scene=scene, meta=dict(ref.meta or {}), ref_id=int(ref.id)
                    )
            except Exception:
                out = None
            cache[slug] = out
        return cache[slug]

    return scene_of


def cad_design_resolver(
    store: Any,
) -> Callable[[str], tuple[Any, dict[str, Any]] | None]:
    """A cad design's slug → ``(built Design, ref meta)`` — the bound solid
    a cad-bound block's observation rays probe (structure-workspaces 2r.2).
    ``None`` when no live cad ref has the slug or it fails to build (a
    resolver answers "cannot load", never raises). Memoized per closure."""
    from precis.cad.scene import build_design

    cache: dict[str, tuple[Any, dict[str, Any]] | None] = {}

    def design_of(slug: str) -> tuple[Any, dict[str, Any]] | None:
        if slug not in cache:
            out = None
            try:
                ref = store.get_ref(kind="cad", id=slug)
                if ref is not None:
                    spec, _handles = store.cad_load(ref.id)
                    out = (build_design(spec), dict(ref.meta or {}))
            except Exception:
                out = None
            cache[slug] = out
        return cache[slug]

    return design_of


def structure_pi_resolver(store: Any) -> Callable[[str], dict[str, Any] | None]:
    """A structure design's slug → its ``structure_pi`` row for the
    CURRENT scene's ``pi_key`` (structure-workspaces 2e): the perceived
    rings / conjugated systems / per-atom membership JSONB, or ``None``
    when the design is gone, unbound-loadable or predates the write path.
    The key is recomputed from the loaded scene so a reverted version
    reads its own row, never a stale latest. Memoized per returned
    closure — one load + one lookup per slug per call."""
    scenes = structure_scene_resolver(store)
    data_cache: dict[str, dict[str, Any] | None] = {}

    def pi_of(slug: str) -> dict[str, Any] | None:
        if slug not in data_cache:
            out: dict[str, Any] | None = None
            bs = scenes(slug)
            if bs is not None and bs.ref_id is not None:
                from precis.structure.cache import pi_key

                key = pi_key(bs.scene)
                try:
                    with store.pool.connection() as conn:
                        row = conn.execute(
                            "SELECT data FROM structure_pi "
                            "WHERE ref_id = %s AND pi_key = %s",
                            (bs.ref_id, key),
                        ).fetchone()
                except psycopg.errors.UndefinedTable:
                    # Pre-0238 DB — the row cannot exist.
                    out = None
                else:
                    if row is not None:
                        out = dict(row[0])
            data_cache[slug] = out
        return data_cache[slug]

    return pi_of


def _read_ws_trace(store: Any, home: int, root: Any) -> dict[str, Any] | None:
    """Newest ``ws_trace`` chunk on ``home`` whose root frame's key is
    ``root`` (``root=None`` → the latest chunk). A chunk that fails the
    schema parse is skipped, never raised — the same lenient posture as
    the other resolvers."""
    from precis_geom import trace_schema

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT text FROM chunks WHERE ref_id = %s AND "
            "chunk_kind = 'ws_trace' ORDER BY ord DESC",
            (home,),
        ).fetchall()
    for (text,) in rows:
        try:
            doc = trace_schema.loads(text)
        except Exception:
            continue
        if root is None:
            return doc
        # scan every chunk — the newest ws_trace may belong to a
        # different root (the design's op-trace beside an import's run
        # trace on the same home)
        if any(
            f.get("parent") is None and f.get("key") == root
            for f in doc.get("frames") or []
        ):
            return doc
    return None


def trace_doc_resolver(
    store: Any, design_ref_id: int | None
) -> Callable[[dict[str, Any] | None], dict[str, Any] | None]:
    """A bound ref's ``meta['generated']['trace']`` record
    (``{'run_job', 'root'}``) → the run's stored trace.json parsed
    through :mod:`precis_geom.trace_schema`.

    The ``ws_trace`` chunk sits on the run job's ref — or on the
    design's own ref when a containerless import ran without a job
    (:func:`precis_se.workspace_import.import_workspace_run`), so a
    ``run_job=None`` record reads ``design_ref_id``'s chunks, and
    ``root`` picks the matching run when several imports share a home.
    Memoized per (home, root) — one lookup per distinct run per call."""
    cache: dict[tuple[Any, Any], dict[str, Any] | None] = {}

    def doc_of(trace_ref: dict[str, Any] | None) -> dict[str, Any] | None:
        if not isinstance(trace_ref, dict):
            return None
        home = trace_ref.get("run_job")
        if home is None:
            home = design_ref_id
        if home is None:
            return None
        root = trace_ref.get("root")
        key = (int(home), root)
        if key not in cache:
            cache[key] = _read_ws_trace(store, int(home), root)
        return cache[key]

    return doc_of


def foreign_resolver(store: Any) -> Callable[[str], SeTree | None]:
    """Build the cross-design ``template`` resolver a tree hands to
    :attr:`precis.blocktree.types.Tree.foreign` — ``'<design-slug>#<block-
    name>'`` → that design's live tree (docs/backlog/blocktree-library-
    build-plan.md slice 1).

    Store-aware, so it lives here and not in :mod:`precis_se.ops` /
    :mod:`precis.blocktree` (both stay store-free by design). Memoized in a
    plain dict scoped to the returned closure — one ``get_ref`` +
    :func:`load_tree` per distinct slug, however many ports, blocks or
    cycle-check hops read through it ("cache per request", the plan's
    resolver read-path decision). A slug that doesn't resolve to a live
    ``se`` design (never existed, or soft-retired — ``store.get_ref``'s
    "retired reads as absent" default) caches as ``None`` too, so a
    dangling reference is not re-queried either.

    Lazy: nothing is fetched unless a template is actually
    cross-design-qualified, so the purely local designs that are the
    overwhelming majority pay one closure allocation and no query. Depth is
    1 by construction — only the tree a read STARTED from is asked to
    resolve foreign slugs (:func:`precis.blocktree.ops._foreign_tree`), so
    the resolver a foreign tree carries in turn is never walked and A→B→A
    cannot recurse here; refusing an actual cross-design instance CYCLE is
    :func:`precis.blocktree.ops._find_instance_cycle`'s job, at write time.
    """
    cache: dict[str, SeTree | None] = {}

    def resolve(slug: str) -> SeTree | None:
        if slug not in cache:
            ref = store.get_ref(kind="se", id=slug)
            cache[slug] = load_tree(store, ref.id) if ref is not None else None
        return cache[slug]

    return resolve


def attach_catalog(store: Any, tree: SeTree) -> None:
    """Fill every `component`-bound block's ``derived`` facets from the
    catalog (:mod:`precis_se.catalog`) — rung 2b of
    ``se-off-the-shelf-fabrication.md``.

    Runs on **load**, so the derivation is recomputed from the
    component's live spec rows on every read and never persisted: the
    same sketch-canonical / copper-derived rule the rest of the tree
    follows. Re-pricing or re-dimensioning a component therefore reaches
    every design bound to it without a migration or a rebuild.

    Batched per distinct slug, not per block — a frame with twenty
    identical bolts binds one component — and **total**: an unresolvable
    slug, a missing category or a thin spec set leaves ``derived``
    carrying only its ``why_not``, never raises. A load that failed
    because a catalog row was deleted would take the whole design with
    it, which is exactly the fragility name-keyed identity exists to
    avoid.

    ``store`` may be a fake without the component ops (plugin tests that
    never touch a catalog); the whole pass no-ops in that case rather
    than demanding the surface.
    """
    slugs = {
        node.bound
        for node in tree.blocks.values()
        if node.bound_kind == "component" and node.bound
    }
    if not slugs or not hasattr(store, "component_current_spec_values"):
        return
    by_slug: dict[str, Derived] = {}
    for slug in sorted(slugs):
        by_slug[slug] = _derive_one(store, slug)
    for node in tree.blocks.values():
        if node.bound_kind == "component" and node.bound:
            node.derived = by_slug.get(node.bound)


def attach_boards(store: Any, tree: SeTree) -> None:
    """Fill every ``pcb``-bound block's ``derived`` from the board's
    ``mechanical_profile`` (:mod:`precis_se.board`) — the consumer half
    of the 0041 bridge, rough box (pcb-se-binding v0).

    Same contract as :func:`attach_catalog` above: run on **load** (the
    derivation is recomputed from the live pcb rows on every read and
    never persisted — moving a part or re-outlining the board reaches
    every design bound to it), batched per distinct slug, and **total**:
    an unresolvable slug leaves ``derived`` carrying only its
    ``why_not``, never raises. ``pcb_graph``/thickness are deliberately
    not read yet — v0 derives the profile from ``pcb_load`` +
    ``pcb_features_list`` and ``DEFAULT_THICKNESS_MM``; the board-column
    half lands with pass A.

    ``store`` may be a fake without the pcb ops; the pass no-ops then,
    same courtesy ``attach_catalog`` extends."""
    slugs = {
        node.bound
        for node in tree.blocks.values()
        if node.bound_kind == "pcb" and node.bound
    }
    if not slugs or not hasattr(store, "pcb_load"):
        return
    by_slug: dict[str, BoardDerived] = {}
    for slug in sorted(slugs):
        by_slug[slug] = _derive_board_one(store, slug)
    for node in tree.blocks.values():
        if node.bound_kind == "pcb" and node.bound:
            node.derived = by_slug.get(node.bound)


def _derive_board_one(store: Any, slug: str) -> BoardDerived:
    """One pcb slug → its board derivation, in metres."""
    ref = store.get_ref(kind="pcb", id=slug)
    if ref is None:
        return BoardDerived(why_not=f"pcb {slug!r} not found")
    try:
        load = store.pcb_load(ref.id)
        features = (
            store.pcb_features_list(ref.id)
            if hasattr(store, "pcb_features_list")
            else []
        )
        profile = pcb_export.mechanical_profile(
            {"instances": load.get("instances") or []}, features
        )
    except Exception as exc:  # a load must never die on one binding
        return BoardDerived(why_not=f"pcb {slug!r}: {type(exc).__name__}: {exc}")
    return derive_board(profile, load.get("instances"))


def provenance_rows(
    store: Any, *, ref_id: int | None = None, margin_only: bool = False
) -> list[dict[str, Any]]:
    """Every stored provenance claim over live se rows — block facets
    (``se_blocks.provenance``) and measures (``se_measures.provenance``),
    migration ``0019`` — as ``{ref_id, block, param, entry}``; ``param`` is
    the facet name, or ``'measure:<name>'`` for a measure.

    ``margin_only`` is the **margin audit** (design-state-core.md item 4):
    every number a margin rule touched, i.e. whose entry carries
    ``margin_origin``. A query over the store, not a feature — across all
    designs when ``ref_id`` is None. Ordered by design, block, param."""
    where_ref = "AND ref_id = %s " if ref_id is not None else ""
    margin = "AND f.value ? 'margin_origin' " if margin_only else ""
    params: tuple[Any, ...] = (ref_id,) if ref_id is not None else ()
    with store.pool.connection() as c:
        with c.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT ref_id, block, param, entry FROM ("
                "  SELECT b.ref_id, b.name AS block, f.key AS param, "
                "         f.value AS entry "
                "  FROM se_blocks b, jsonb_each(b.provenance) f "
                "  WHERE b.retired_at IS NULL AND b.provenance IS NOT NULL "
                f"  {where_ref.replace('ref_id', 'b.ref_id')}{margin}"
                "  UNION ALL "
                "  SELECT m.ref_id, m.block, 'measure:' || m.name AS param, "
                "         m.provenance AS entry "
                "  FROM se_measures m, LATERAL (SELECT m.provenance AS value) f "
                "  WHERE m.retired_at IS NULL AND m.provenance IS NOT NULL "
                f"  {where_ref.replace('ref_id', 'm.ref_id')}{margin}"
                ") rows ORDER BY ref_id, block, param",
                params * 2,
            )
            return [
                {
                    "ref_id": int(r["ref_id"]),
                    "block": str(r["block"]),
                    "param": str(r["param"]),
                    "entry": dict(r["entry"] or {}),
                }
                for r in cur.fetchall()
            ]


def sync_realized_by(store: Any, ref_id: int, tree: SeTree) -> None:
    """Mirror the design's `component` bindings as ``realized-by`` links
    (se design → the procurable ``component`` ref) — migration 0156's
    realization edge, the same one ``CadHandler._sync_realized_by``
    writes for ``part`` lines.

    **Why se emits a link at all**, given that se relations are otherwise
    plugin-local: because two subsystems had grown two spellings of "this
    design is that component" (``docs/backlog/``, decided 2026-09-06), and
    a consumer asking "everything this artifact resolves to" would have
    had to know both. The reconciliation keeps each layer doing what it is
    good at — ``se_blocks.bound_kind``/``bound_design`` stays
    **authoritative** (name-keyed, plugin-local, where the block tree
    lives), and the link is a **derived projection**, rebuilt on every
    save, so one `links` query now answers that question across both
    tracks. It is the sketch-canonical / copper-derived rule applied to a
    cross-kind edge, not a second home for the binding.

    Only rows carrying ``meta.se_binding`` are pruned here, so cad's
    catalog-managed rows and any hand-authored candidate realization
    survive untouched — the same courtesy cad's sync extends.

    ``part`` bindings are deliberately **not** linked: ``realized-by``
    targets a procurable `component` ref (``CadHandler.link`` rejects
    anything else), and an LCSC C-number is a `part`. That is a gap in
    the *relation's* scope, not something to paper over here.

    Best-effort and total: a fake store without the link surface, a
    binding whose component ref does not exist, or a transient failure
    all leave the design saved and the projection incomplete. A design
    must never fail to save because a derived index could not be
    updated."""
    if not all(
        hasattr(store, name) for name in ("links_for", "add_link", "remove_link")
    ):
        return
    try:
        want: set[int] = set()
        for slug in sorted(
            {
                node.bound
                for node in tree.blocks.values()
                if node.bound_kind == "component" and node.bound
            }
        ):
            comp = store.get_ref(kind="component", id=slug)
            if comp is not None:
                want.add(int(comp.id))
        have = {
            int(lk.dst_ref_id): lk
            for lk in store.links_for(ref_id, direction="out", relation="realized-by")
        }
        for dst in sorted(want - set(have)):
            store.add_link(
                src_ref_id=ref_id,
                dst_ref_id=dst,
                relation="realized-by",
                meta={_SE_MANAGED: True},
            )
        for dst, lk in sorted(have.items()):
            if dst not in want and (lk.meta or {}).get(_SE_MANAGED):
                store.remove_link(
                    src_ref_id=ref_id, dst_ref_id=dst, relation="realized-by"
                )
    except Exception:  # pragma: no cover — defensive, mirrors cad's sync
        log.warning("se: realized-by link sync failed for ref %s", ref_id)


def _derive_one(store: Any, slug: str) -> Derived:
    """One component slug → its catalog derivation, in metres."""
    ref = store.get_ref(kind="component", id=slug)
    if ref is None:
        return Derived(why_not=f"component {slug!r} not found")
    category = (ref.meta or {}).get("category")
    specs: dict[str, Any] = {}
    for spec_id, row in store.component_current_spec_values(ref.id).items():
        spec_row = store.component_spec_get(spec_id)
        if spec_row is None:  # pragma: no cover — FK makes this unreachable
            continue
        if row.get("value_num") is None:
            # categorical/text/boolean — carried through unconverted for
            # the generators that read them (thread_size, drive_type).
            specs[spec_id] = (
                row.get("value_text")
                if row.get("value_text") is not None
                else row.get("value_bool")
            )
            continue
        metres = catalog.to_metres(
            float(row["value_num"]), spec_row.get("canonical_unit")
        )
        # A numeric spec in a unit this bridge doesn't know (USD, kg, N)
        # is not a length and simply isn't geometry input — dropping it
        # is correct, and `to_metres` returning None is how it says so.
        if metres is not None:
            specs[spec_id] = metres
    return catalog.derive(category, specs)


def _template_uid(
    uid_of: dict[str, int],
    foreign_uids: dict[tuple[str, str], int],
    template: str | None,
) -> int | None:
    """The uid half of a ``template_ref``: the local block's uid for a
    bare reference, the FOREIGN block's uid for ``'slug#block'``
    (:func:`_foreign_template_uids`). ``None`` when it does not resolve —
    a dangling reference keeps working through its name text, the same
    fallback every other cross-reference has (module docstring)."""
    if template is None:
        return None
    design_slug, block_name = parse_template_ref(template)
    if design_slug is None:
        return uid_of.get(block_name)
    return foreign_uids.get((design_slug, block_name))


#: Live se blocks by (design slug, label) — the save half of a cross-design
#: ``template_ref``'s uid. A uid is unique only within one design (branch
#: copies share them), so the design slug stays part of the key.
_FOREIGN_BY_NAME_SQL = (
    "SELECT i.id_value, b.name, b.uid FROM se_blocks b "
    "JOIN refs r ON r.ref_id = b.ref_id AND r.kind = 'se' AND r.retired_at IS NULL "
    "JOIN ref_identifiers i ON i.ref_id = r.ref_id AND i.id_kind = 'cite_key' "
    "WHERE b.retired_at IS NULL AND b.uid IS NOT NULL "
    "AND (i.id_value, b.name) IN (SELECT * FROM unnest(%s::text[], %s::text[]))"
)

#: The load half: (design slug, uid) → that block's CURRENT label.
_FOREIGN_BY_UID_SQL = (
    "SELECT i.id_value, b.uid, b.name FROM se_blocks b "
    "JOIN refs r ON r.ref_id = b.ref_id AND r.kind = 'se' AND r.retired_at IS NULL "
    "JOIN ref_identifiers i ON i.ref_id = r.ref_id AND i.id_kind = 'cite_key' "
    "WHERE b.retired_at IS NULL "
    "AND (i.id_value, b.uid) IN (SELECT * FROM unnest(%s::text[], %s::bigint[]))"
)


def _foreign_template_uids(c: Connection, tree: SeTree) -> dict[tuple[str, str], int]:
    """``{(slug, label): uid}`` for every cross-design ``template`` in
    ``tree`` that resolves to a live block — ONE query however many
    instances reference other designs, and none when no template is
    cross-design (the overwhelming majority).

    A (slug, label) that matches more than one live block resolves to
    NOTHING (the dangling name-text fallback), never to an arbitrary pick:
    today ``se_blocks_ref_name_key`` and the cite_key primary key make the
    match single-valued, but a label is a helper, not an identity, and a
    relaxed name index must not silently bind an instance to the wrong
    block."""
    keys = sorted(
        {
            ref
            for node in tree.blocks.values()
            if node.template is not None
            for ref in [parse_template_ref(node.template)]
            if ref[0] is not None
        }
    )
    if not keys:
        return {}
    rows = c.execute(
        _FOREIGN_BY_NAME_SQL, ([k[0] for k in keys], [k[1] for k in keys])
    ).fetchall()
    found: dict[tuple[str, str], set[int]] = {}
    for r in rows:
        found.setdefault((str(r[0]), str(r[1])), set()).add(int(r[2]))
    return {key: next(iter(uids)) for key, uids in found.items() if len(uids) == 1}


def _foreign_template_labels(
    c: Connection, rows: list[dict[str, Any]]
) -> dict[tuple[str, int], str]:
    """``{(slug, uid): current label}`` for every stored cross-design
    ``template_ref`` carrying a uid — the read half, so an instance follows
    its foreign template across a relabel. One query, or none."""
    keys = sorted(
        {
            (design, int(r["template_uid"]))
            for r in rows
            if r["template_uid"] is not None and r["template_ref"]
            for design in [parse_template_ref(r["template_ref"])[0]]
            if design is not None
        }
    )
    if not keys:
        return {}
    found = c.execute(
        _FOREIGN_BY_UID_SQL, ([k[0] for k in keys], [k[1] for k in keys])
    ).fetchall()
    return {(str(f[0]), int(f[1])): str(f[2]) for f in found}


def _template_label(
    uid_to_name: dict[int, str],
    foreign_labels: dict[tuple[str, int], str],
    row: dict[str, Any],
) -> str | None:
    """A stored ``template_ref`` read back: from its uid when that still
    resolves (local or foreign — a relabel never strands an instance),
    else the stored text (the dangling fallback)."""
    stored = row["template_ref"]
    if stored is None:
        return None
    design, _block = parse_template_ref(stored)
    if design is None:
        return _label(uid_to_name, row["template_uid"], stored)
    uid = row["template_uid"]
    current = foreign_labels.get((design, int(uid))) if uid is not None else None
    return f"{design}{TEMPLATE_SEP}{current}" if current is not None else stored


def _topo_order(tree: SeTree) -> list[str]:
    """A block-name order where every ``parent`` precedes its dependents —
    the FK-safe INSERT sequence for ``parent_block_id``, the one remaining
    row-id self-FK on ``se_blocks`` (``template`` used to constrain this
    order too, back when it was itself a row-id FK — migration
    ``0004_se_template_ref.sql`` made it name-keyed TEXT, which needs no
    INSERT ordering at all — :mod:`precis_nm.persist`'s ``_topo_order``,
    transferred verbatim). Acyclic by construction (``ops.py`` only lets a
    block reference an already-existing block as its parent), so a plain
    fixed-point pass suffices."""
    placed: set[str] = set()
    order: list[str] = []
    remaining = dict(tree.blocks)
    while remaining:
        progressed = False
        for name, node in list(remaining.items()):
            deps = [node.parent] if node.parent is not None else []
            if all(d in placed for d in deps):
                order.append(name)
                placed.add(name)
                del remaining[name]
                progressed = True
        if not progressed:  # pragma: no cover — defensive only, see docstring
            raise RuntimeError(
                f"se block tree has an unresolvable parent chain: {sorted(remaining)}"
            )
    return order


def _assign_uids(
    store: Any, c: Connection, ref_id: int, tree: SeTree
) -> dict[str, int]:
    """Give every block in ``tree`` its uid and return the ``name → uid``
    map the cross-reference writes below are keyed by.

    A node that already carries a uid keeps it — it was loaded from this
    design (an ``edit``), or copied from another one (uids are carried
    through a copy on purpose, so a copy diffs block-by-block). A uid-less
    node is either a **new** block or an old one the caller rebuilt from
    scratch, and which of the two it is depends on where the tree came
    from — so the rule is decided per *tree*, not per node:

    * **a caller-built tree** (``from_persistence`` False — a full
      ``put``: it replaces the design and never sees the stored rows).
      Here a label is the only identity evidence there is, so a uid-less
      node **adopts** the live row with the same label — which is what
      makes a re-``put`` identity-preserving rather than
      identity-churning. It is a best-effort match, and it is ambiguous by
      construction: a ``put`` that renames ``'wheel'`` to ``'rim'`` reads
      as "removed wheel, added rim", two fresh uids. Accepted — the caller
      that cares about identity across a rename edits.
    * **a persisted-origin tree** (``from_persistence`` True — it came
      from :func:`load_tree`, the ``edit`` path): a uid-less node can only
      be one the ops just added. Always mint. Adopting by label here would
      be wrong in the one case that matters:
      ``remove_block('wheel')`` + ``add_block('wheel')`` in a single edit
      — the in-memory removal never retires the DB row, so the *new*
      block would inherit the *dead* block's identity (gr339743). The
      flag, not tree shape, decides: an edit that removes every
      uid-carrying block before re-adding same-named ones must still
      mint (shape-sniffing ``all(uid is None)`` would flip it back to
      adoption exactly there).

    ONE mint round trip whichever branch runs. Call it before the retire
    pass, while "the live rows" still means the design as it was.
    """
    # The edit path never reads the live rows at all — the query is the
    # full-put branch's evidence, and only that branch pays for it.
    adopt_by_label = not tree.from_persistence
    live: dict[str, int] = {}
    if adopt_by_label:
        live = {
            str(row[0]): int(row[1])
            for row in c.execute(
                "SELECT name, uid FROM se_blocks "
                "WHERE ref_id = %s AND retired_at IS NULL",
                (ref_id,),
            ).fetchall()
        }
    fresh: list[SeBlock] = []
    for name, node in tree.blocks.items():
        if node.uid is None:
            inherited = live.get(name)
            if inherited is None:
                fresh.append(node)
            else:
                node.uid = inherited
    for node, uid in zip(fresh, mint_uids(store, len(fresh), conn=c), strict=True):
        node.uid = uid
    return {name: int(node.uid) for name, node in tree.blocks.items() if node.uid}


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
    an outer one when ``conn`` is given, e.g. ``put``'s ref-upsert).

    Mutates ``tree`` in one way: every block comes out carrying the ``uid``
    it was saved under (:func:`_assign_uids`), so the caller's post-save
    render speaks the same identity the rows do."""

    def _do(c: Connection) -> None:
        # Before anything is retired: the uid a block keeps is decided
        # against the design's CURRENT live rows.
        uid_of = _assign_uids(store, c, ref_id, tree)
        foreign_uids = _foreign_template_uids(c, tree)
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
            "UPDATE se_pockets SET retired_at = now() "
            "WHERE retired_at IS NULL AND block_id IN "
            "(SELECT id FROM se_blocks WHERE ref_id = %s)",
            (ref_id,),
        )
        with c.cursor() as rcur:
            has_rays = _has_rays_table(rcur)
        if has_rays:
            c.execute(
                "UPDATE se_rays SET retired_at = now() "
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
            "UPDATE se_measures SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        c.execute(
            "UPDATE se_bom SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        c.execute(
            "UPDATE se_notes SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        c.execute(
            "UPDATE se_topology SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        c.execute(
            "UPDATE se_optics SET retired_at = now() "
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
            row = c.execute(
                "INSERT INTO se_blocks "
                "(ref_id, uid, parent_block_id, template_ref, template_uid, "
                " name, pose_xyz, pose_rot, envelope, array_spec, descr, "
                " use_, objectives, mode, bound_kind, bound_design, origins, "
                " dof, chromophore, process_overrides, build_frame, chain, "
                " provenance, insertion, material) "
                "VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (
                    ref_id,
                    node.uid,
                    parent_id,
                    # The label text is written for both reference forms;
                    # only a LOCAL one also gets a uid — module docstring.
                    node.template,
                    _template_uid(uid_of, foreign_uids, node.template),
                    name,
                    # Parent-relative, as stored (module docstring's engine
                    # fix): NEVER node.pose/node.rot, which by this point
                    # hold the WORLD-frame placement compose_world_pose
                    # derived from these — writing THOSE would strand a
                    # relative-to-parent design as an absolute one on the
                    # very next load.
                    node.local_pose,
                    node.local_rot,
                    node.envelope,
                    Jsonb(node.array) if node.array is not None else None,
                    node.descr,
                    node.use,
                    Jsonb(node.objectives) if node.objectives else None,
                    node.mode,
                    node.bound_kind,
                    node.bound,
                    Jsonb(node.origins) if node.origins else None,
                    Jsonb(node.dof) if node.dof is not None else None,
                    Jsonb(node.chromophore) if node.chromophore is not None else None,
                    Jsonb(node.process_overrides)
                    if node.process_overrides is not None
                    else None,
                    Jsonb(node.build_frame) if node.build_frame is not None else None,
                    Jsonb(node.chain) if node.chain is not None else None,
                    Jsonb(node.provenance) if node.provenance else None,
                    Jsonb(node.insertion) if node.insertion is not None else None,
                    node.material,
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
                    "(block_id, name, roles, direction, annotations, "
                    " expected_element, expected_hybridization, "
                    " bound_design, bound_atom, axis_atom, phase_atom, "
                    " pose_xyz, pose_rot, pose_source, rot_source) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                    (
                        name_to_id[name],
                        port.name,
                        port.roles,
                        port.direction,
                        Jsonb(port.annotations) if port.annotations else None,
                        port.expected_element,
                        port.expected_hybridization,
                        port.bound_design,
                        port.bound_atom,
                        port.axis_atom,
                        port.phase_atom,
                        port.pose,
                        port.rot,
                        port.pose_source,
                        port.rot_source,
                    ),
                )
            for pocket in node.pockets.values():
                c.execute(
                    "INSERT INTO se_pockets (block_id, name, shape, regions, "
                    "class_hash) VALUES (%s,%s,%s,%s,%s)",
                    (
                        name_to_id[name],
                        pocket.name,
                        pocket.shape,
                        list(pocket.regions),
                        pocket.class_hash,
                    ),
                )
            if node.rays and not has_rays:
                log.warning(
                    "se_rays table absent (pre-0024 DB) — %d ray(s) on %r not saved",
                    len(node.rays),
                    name,
                )
            for ray in node.rays.values() if has_rays else ():
                c.execute(
                    "INSERT INTO se_rays (block_id, name, definition, digests) "
                    "VALUES (%s,%s,%s,%s)",
                    (
                        name_to_id[name],
                        ray.name,
                        Jsonb(ray.definition()),
                        Jsonb(list(ray.digests)),
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
                "(ref_id, a_block, a_block_uid, a_port, b_block, b_block_uid, "
                " b_port, joint, kind, objectives, optical, contract) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    ref_id,
                    a[0],
                    uid_of.get(a[0]),
                    a[1],
                    b[0],
                    uid_of.get(b[0]),
                    b[1],
                    Jsonb(conn_spec.joint) if conn_spec.joint is not None else None,
                    conn_spec.kind,
                    Jsonb(conn_spec.objectives) if conn_spec.objectives else None,
                    Jsonb(conn_spec.optical) if conn_spec.optical is not None else None,
                    (
                        Jsonb(conn_spec.contract)
                        if conn_spec.contract is not None
                        else None
                    ),
                ),
            )
        for m in tree.measures:
            c.execute(
                "INSERT INTO se_measures "
                "(ref_id, block, block_uid, name, value, relation, strength, "
                " reason, min_value, max_value, origin, unit, datum, "
                " measurand, measurand_ref_id, datum_pin, provenance) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    ref_id,
                    m.block,
                    uid_of.get(m.block),
                    m.name,
                    m.value,
                    Jsonb(m.relation) if m.relation is not None else None,
                    m.strength,
                    m.reason,
                    m.min_value,
                    m.max_value,
                    m.origin,
                    m.unit,
                    m.datum,
                    m.measurand,
                    m.measurand_ref,
                    m.datum_pin,
                    Jsonb(m.provenance) if m.provenance else None,
                ),
            )
        for note in tree.notes:
            # ``created_at`` is CARRIED across the retire/reinsert cycle
            # (COALESCE stamps only a note minted this batch) so the
            # interview timeline stays truthful — see 0005's header.
            c.execute(
                "INSERT INTO se_notes "
                "(ref_id, name, kind, body, re, about, origin, created_at) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,COALESCE(%s, now()))",
                (
                    ref_id,
                    note.name,
                    note.kind,
                    note.body,
                    note.re,
                    Jsonb(note.about),
                    note.origin,
                    note.created_at,
                ),
            )
        for line in tree.bom:
            # Canonicalize a connect line's endpoints exactly as the
            # connect itself is canonicalized above, so the two always
            # agree on which tuple names the edge.
            a_end, b_end = (
                sorted(
                    (
                        (line.a_block, line.a_port),
                        (line.b_block, line.b_port),
                    )
                )
                if line.is_connect
                else ((None, None), (None, None))
            )
            c.execute(
                "INSERT INTO se_bom "
                "(ref_id, block, block_uid, a_block, a_block_uid, a_port, "
                " b_block, b_block_uid, b_port, item_kind, item, qty, uom, "
                " reason) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    ref_id,
                    line.block,
                    uid_of.get(line.block) if line.block else None,
                    a_end[0],
                    uid_of.get(a_end[0]) if a_end[0] else None,
                    a_end[1],
                    b_end[0],
                    uid_of.get(b_end[0]) if b_end[0] else None,
                    b_end[1],
                    line.item_kind,
                    line.item,
                    line.qty,
                    line.uom,
                    line.reason,
                ),
            )
        for thread in tree.threading:
            # Directional — ``a`` threaded through ``b`` — so the endpoint
            # pair is NOT canonicalized the way a connect's is: the two
            # orders mean different (and mutually impossible) things.
            c.execute(
                "INSERT INTO se_topology "
                "(ref_id, kind, subject_name, subject_uid, object_name, "
                " object_uid) "
                "VALUES (%s,'threading',%s,%s,%s,%s)",
                (
                    ref_id,
                    thread.a,
                    uid_of.get(thread.a),
                    thread.b,
                    uid_of.get(thread.b),
                ),
            )
        # Domain rows share ``se_topology`` with threading (migration
        # 0015) and are ordered WITHIN a strand, so — unlike threading's
        # unordered pair — ``meta.ord`` is half the row's identity and the
        # partial unique index keys on it.
        for domain in sorted(tree.domains, key=lambda d: (d.strand, d.ord)):
            c.execute(
                "INSERT INTO se_topology "
                "(ref_id, kind, subject_name, subject_uid, object_name, "
                " object_uid, meta) "
                "VALUES (%s,'domain',%s,%s,%s,%s,%s)",
                (
                    ref_id,
                    domain.strand,
                    uid_of.get(domain.strand),
                    domain.helix,
                    uid_of.get(domain.helix),
                    Jsonb(domain.meta()),
                ),
            )
        # ``se_optics`` is a 0-or-1-row table, not a ledger — a fresh row
        # is inserted only when the tree actually declares an optical
        # context, so an undeclared design leaves no stray row behind for
        # the retire pass above to ever have to find again.
        if tree.optics is not None:
            c.execute(
                "INSERT INTO se_optics "
                "(ref_id, medium_index, excitation_nm, channels_available) "
                "VALUES (%s,%s,%s,%s)",
                (
                    ref_id,
                    tree.optics["medium_index"],
                    tree.optics.get("excitation_nm"),
                    tree.optics.get("channels_available"),
                ),
            )
        store.chunks.upsert_card_combined(ref_id, card_text, conn=c)
        _flush_op_trace(store, c, ref_id, tree)

    if conn is not None:
        _do(conn)
        return
    with store.tx() as c:
        _do(c)


def _flush_op_trace(store: Any, c: Any, ref_id: int, tree: SeTree) -> None:
    """Fold ``tree.op_frames`` (minted by :func:`precis_se.ops.apply_ops`
    since the last save) into the design's op-trace — a ``ws_trace``
    chunk on the design ref whose root frame key is
    :data:`precis_se.ops.OP_TRACE_ROOT`, appended to across saves
    (delete+insert — chunks are append-only). The ops' *bound* targets
    get ``meta['frames'][name] = <fid>`` plus ``generated.trace``
    pointing at this doc, so ``made-by`` answers ``op <op> (call
    <seq>)`` the way it does for a workspace run's frames."""
    frames = getattr(tree, "op_frames", None) or []
    if not frames:
        return
    from precis_geom import trace_schema as _ts
    from precis_se.ops import OP_TRACE_ROOT

    # The existing op-trace chunk on this ref (root frame key == 'op')
    # — ids and frames continue from it.
    rows = c.execute(
        "SELECT chunk_id, text FROM chunks WHERE ref_id = %s AND "
        "chunk_kind = 'ws_trace' ORDER BY ord",
        (ref_id,),
    ).fetchall()
    prior_frames: list[dict[str, Any]] = []
    prior_chunk: int | None = None
    for chunk_id, text in rows:
        try:
            doc = _ts.loads(text)
        except Exception:
            continue
        roots = [f for f in doc.get("frames") or [] if f.get("parent") is None]
        if roots and roots[0].get("key") == OP_TRACE_ROOT:
            prior_chunk = int(chunk_id)
            prior_frames = list(doc.get("frames") or [])
            break
    if prior_frames:
        merged = prior_frames
        start = 1 + max(int(f.get("id") or 0) for f in prior_frames)
    else:
        merged = []
        start = 1
    # Session-scoped keys can still collide for same-(sid,seq,qual)
    # repeats outside a session (line=0) — bump occ until the key is
    # fresh within this doc.
    existing_keys = {f.get("key") for f in merged}
    new_frames: list[dict[str, Any]] = []
    for i, f in enumerate(frames):
        f = dict(f)
        pkey = f.pop("_pkey", OP_TRACE_ROOT)
        while f["key"] in existing_keys:
            f["occ"] = int(f.get("occ") or 0) + 1
            f["key"] = _ts.frame_key(pkey, str(f["qual"]), f["occ"])
        existing_keys.add(f["key"])
        new_frames.append({**f, "id": start + i})
    if not merged:
        merged = [
            {
                "id": 0,
                "key": OP_TRACE_ROOT,
                "parent": None,
                "qual": "ops",
                "file": "op",
                "line": 0,
                "line_in_def": 0,
                "src": None,
                "occ": 0,
                "kind": "op",
                "args": {},
                "made": {},
                "touched": {},
            }
        ]
    merged = merged + new_frames
    if prior_chunk is not None:
        c.execute("DELETE FROM chunks WHERE chunk_id = %s", (prior_chunk,))
    row = c.execute(
        "SELECT COALESCE(MAX(ord), -1) + 1 FROM chunks WHERE ref_id = %s",
        (ref_id,),
    ).fetchone()
    ord_next = int(row[0]) if row else 0
    c.execute(
        "INSERT INTO chunks (ref_id, ord, text, meta, chunk_kind) "
        "VALUES (%s, %s, %s, %s, 'ws_trace')",
        (
            ref_id,
            ord_next,
            _ts.dumps(
                {
                    "schema": _ts.SCHEMA_VERSION,
                    "run": {"op": True},
                    "frames": merged,
                }
            ),
            Jsonb({"chunk_kind": "ws_trace", "op_trace": True}),
        ),
    )
    # Bound targets: ``set_pose``/``connect`` ops that touched a bound
    # block record which op frame owns the latest write —
    # ``meta['frames'][block] = fid`` + ``generated.trace`` → this doc
    # (``run_job`` names the doc's home ref — the design's own).
    trace_ref = {"run_job": ref_id, "root": OP_TRACE_ROOT}
    targets: dict[str, int] = {}
    for f in new_frames:
        args = f.get("args") or {}
        names: list[str] = []
        if f.get("qual") == "set_pose":
            names.append(str(args.get("block") or ""))
        elif f.get("qual") == "connect":
            for side in ("a", "b"):
                raw = str(args.get(side) or "")
                if "." in raw:
                    names.append(raw.split(".", 1)[0])
        for raw in names:
            if raw:
                targets.setdefault(raw, int(f["id"]))
    for raw, fid in targets.items():
        node = tree.blocks.get(raw)
        if node is None:
            try:
                key = tree.resolve_key(raw)
            except Exception:
                key = None
            node = tree.blocks.get(key) if key else None
        if node is None:
            continue
        # An instance's made-by resolves to its template's block — but an
        # op that moved/connected the INSTANCE didn't build the template.
        # Never rewrite the template ref's provenance for it; the op
        # frame stays on the design's own op-trace doc.
        if node.template is not None:
            continue
        if node.bound_kind not in ("structure", "cad") or not node.bound:
            continue
        try:
            bref = store.get_ref(kind=node.bound_kind, id=node.bound)
        except Exception:
            continue
        if bref is None:
            continue
        meta = dict(bref.meta or {})
        frames_map = dict(meta.get("frames") or {})
        frames_map[node.name] = fid
        gen = dict(meta.get("generated") or {})
        gen["trace"] = dict(trace_ref)
        store.update_ref(
            int(bref.id),
            meta_patch={"frames": frames_map, "generated": gen},
            conn=c,
        )
    tree.op_frames.clear()


def _is_protected_structure(store: Any, ref_id: int) -> bool:
    """True when a structure must survive a design-retire cascade.

    Hook for the promoted "building block" structures (gr459058): Reto's
    ruling is that they outlive the designs that minted them, but the
    promotion mechanism (tag, folder placement, or component row) is still
    undecided — see td458221.  Returns False until that lands; the later
    decision is a change to this one function.
    """
    return False


def reobserve_rays(
    store: Any, structure_slug: str, *, service: dict[str, Any] | None = None
) -> list[tuple[str, str]]:
    """Re-observe every ray on every live se block bound to
    ``structure_slug`` and store the new digest beside ``service`` (the
    relax write-back's ``{job, version, rung}`` — structure-workspaces
    2r: "what did the relax change along the pore" is then one
    ``against='last'`` diff). Observation only: the digest is written to
    the live ``se_rays`` row in place under the tree lock — no tree save,
    no new block rows. Returns ``[(design slug, ray name)]``; a DB
    without ``se_rays`` returns ``[]``."""
    from precis_se import rays as se_rays

    done: list[tuple[str, str]] = []
    with store.pool.connection() as c:
        with c.cursor() as cur:
            if not _has_rays_table(cur):
                return done
        rows = c.execute(
            "SELECT DISTINCT b.ref_id FROM se_blocks b "
            "WHERE b.bound_kind = 'structure' AND b.bound_design = %s "
            "AND b.retired_at IS NULL",
            (structure_slug,),
        ).fetchall()
    for (ref_id,) in rows:
        ref = store.get_ref(kind="se", id=int(ref_id))
        if ref is None:
            continue
        with tree_mutation(store, int(ref_id)) as conn:
            tree = load_tree(store, int(ref_id), conn=conn)
            # A ray lives on a template block; it is touched when that block
            # (or one of its instances) binds the relaxed structure.
            touched = {
                node.template or name
                for name, node in tree.blocks.items()
                if (owner := tree.blocks.get(node.template or "") or node).bound
                == structure_slug
                and owner.bound_kind == "structure"
            }
            for block, spec in se_rays.all_rays(tree):
                if block not in touched:
                    continue
                try:
                    obs = se_rays.observe(tree, block, spec, slug=str(ref.slug))
                except se_rays.RayError:
                    continue
                se_rays.push_digest(spec, se_rays.digest(obs, service=service))
                conn.execute(
                    "UPDATE se_rays SET digests = %s WHERE name = %s "
                    "AND retired_at IS NULL AND block_id = (SELECT id FROM "
                    "se_blocks WHERE ref_id = %s AND name = %s AND retired_at IS NULL)",
                    (Jsonb(list(spec.digests)), spec.name, int(ref_id), block),
                )
                done.append((str(ref.slug), spec.name))
    return done


def observe_all_rays(store: Any, ref_id: int) -> dict[str, Any]:
    """``{ray name: Observation}`` for every ray of a design as it stands
    — the "before" a rebuild/import compares against (read-only)."""
    from precis_se import rays as se_rays

    tree = load_tree(store, int(ref_id))
    ref = store.get_ref(kind="se", id=int(ref_id))
    slug = str(ref.slug) if ref is not None else ""
    out: dict[str, Any] = {}
    for block, spec in se_rays.all_rays(tree):
        try:
            out[spec.name] = se_rays.observe(tree, block, spec, slug=slug)
        except se_rays.RayError:
            continue
    return out


def reobserve_after_rebuild(
    store: Any,
    ref_id: int,
    before: dict[str, Any],
    *,
    service: dict[str, Any] | None = None,
) -> list[str]:
    """Re-observe every ray after a rebuild/import and store the digest;
    a ray whose events differ from ``before`` (the design version the
    rebuild replaced, observed before it) gets ``changed`` on its digest —
    the ``ray.changed`` finding :func:`precis_se.validate.validate` raises
    — and the returned echo lines show both lists. Rays new in this
    version have no "before" and never read as changed."""
    from precis_se import rays as se_rays

    lines: list[str] = []
    with store.pool.connection() as c:
        with c.cursor() as cur:
            if not _has_rays_table(cur):
                return lines
    with tree_mutation(store, int(ref_id)) as conn:
        tree = load_tree(store, int(ref_id), conn=conn)
        ref = store.get_ref(kind="se", id=int(ref_id), conn=conn)
        slug = str(ref.slug) if ref is not None else ""
        for block, spec in se_rays.all_rays(tree):
            try:
                obs = se_rays.observe(tree, block, spec, slug=slug)
            except se_rays.RayError:
                continue
            dg = se_rays.digest(obs, service=service)
            old = before.get(spec.name)
            if old is not None and se_rays.event_hash(old) != dg["hash"]:
                diff = se_rays.diff_lines(old, obs)
                dg["changed"] = {
                    "summary": diff[0],
                    "prev_hash": se_rays.event_hash(old),
                }
                lines.append(f"ray.changed: {spec.name} on {block} — {diff[0]}")
                lines.append("  before:")
                lines.extend(f"  {ln}" for ln in se_rays.hit_lines(old))
                lines.append("  after:")
                lines.extend(f"  {ln}" for ln in se_rays.hit_lines(obs))
            se_rays.push_digest(spec, dg)
            conn.execute(
                "UPDATE se_rays SET digests = %s WHERE name = %s "
                "AND retired_at IS NULL AND block_id = (SELECT id FROM "
                "se_blocks WHERE ref_id = %s AND name = %s AND retired_at IS NULL)",
                (Jsonb(list(spec.digests)), spec.name, int(ref_id), block),
            )
    return lines


def _bound_structures(conn: Any, ref_id: int) -> dict[str, int]:
    """``{slug: ref_id}`` of the live structure refs this design's blocks
    and ports are bound to (``bound_kind='structure'``)."""
    rows = conn.execute(
        "SELECT DISTINCT i.id_value, r.ref_id FROM refs r "
        "JOIN ref_identifiers i ON i.ref_id = r.ref_id AND i.id_kind = 'cite_key' "
        "WHERE r.kind = 'structure' AND r.retired_at IS NULL AND i.id_value IN ("
        " SELECT b.bound_design FROM se_blocks b WHERE b.ref_id = %s"
        "  AND b.bound_kind = 'structure' AND b.retired_at IS NULL"
        " UNION SELECT p.bound_design FROM se_ports p"
        "  JOIN se_blocks b ON b.id = p.block_id WHERE b.ref_id = %s"
        "  AND p.retired_at IS NULL)",
        (ref_id, ref_id),
    ).fetchall()
    return {r[0]: r[1] for r in rows}


def retire_design(store: Any, ref_id: int) -> tuple[int, list[str], dict[str, str]]:
    """Soft-retire the ref and every live block (+ ports/connects) under
    it, then cascade to the ``structure`` refs its blocks/ports were bound
    to (gr459058).

    Returns ``(blocks_retired, retired_structure_slugs, kept)`` where
    ``kept`` maps each structure left live to why (``"bound by <design>"``
    or ``"protected"``).
    """
    with store.tx() as conn:
        bound = _bound_structures(conn, ref_id)
        kept: dict[str, str] = {}
        for slug, sid in sorted(bound.items()):
            others = conn.execute(
                "SELECT DISTINCT i.id_value FROM refs d JOIN se_blocks b "
                "ON b.ref_id = d.ref_id JOIN ref_identifiers i ON "
                "i.ref_id = d.ref_id AND i.id_kind = 'cite_key' WHERE d.kind = 'se' "
                "AND d.retired_at IS NULL AND d.ref_id <> %s "
                "AND b.retired_at IS NULL AND b.bound_kind = 'structure' "
                "AND b.bound_design = %s "
                "UNION SELECT DISTINCT i.id_value FROM refs d JOIN se_blocks b "
                "ON b.ref_id = d.ref_id JOIN se_ports p ON p.block_id = b.id "
                "JOIN ref_identifiers i ON i.ref_id = d.ref_id "
                "AND i.id_kind = 'cite_key' "
                "WHERE d.kind = 'se' AND d.retired_at IS NULL "
                "AND d.ref_id <> %s AND b.retired_at IS NULL "
                "AND p.retired_at IS NULL "
                "AND p.bound_design = %s ORDER BY 1",
                (ref_id, slug, ref_id, slug),
            ).fetchall()
            names = [o[0] for o in others]
            if names:
                kept[slug] = "bound by " + ", ".join(names)
            elif _is_protected_structure(store, sid):
                kept[slug] = "protected"
        to_retire = {s: i for s, i in bound.items() if s not in kept}
        store.retire_ref(ref_id, conn=conn)
        conn.execute(
            "UPDATE se_ports SET retired_at = now() "
            "WHERE retired_at IS NULL AND block_id IN "
            "(SELECT id FROM se_blocks WHERE ref_id = %s)",
            (ref_id,),
        )
        conn.execute(
            "UPDATE se_pockets SET retired_at = now() "
            "WHERE retired_at IS NULL AND block_id IN "
            "(SELECT id FROM se_blocks WHERE ref_id = %s)",
            (ref_id,),
        )
        with conn.cursor() as rcur:
            has_rays = _has_rays_table(rcur)
        if has_rays:
            conn.execute(
                "UPDATE se_rays SET retired_at = now() "
                "WHERE retired_at IS NULL AND block_id IN "
                "(SELECT id FROM se_blocks WHERE ref_id = %s)",
                (ref_id,),
            )
        conn.execute(
            "UPDATE se_connects SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        conn.execute(
            "UPDATE se_measures SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        conn.execute(
            "UPDATE se_bom SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        conn.execute(
            "UPDATE se_notes SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        conn.execute(
            "UPDATE se_topology SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        conn.execute(
            "UPDATE se_optics SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL",
            (ref_id,),
        )
        rows = conn.execute(
            "UPDATE se_blocks SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "RETURNING id",
            (ref_id,),
        ).fetchall()
    for sid in to_retire.values():
        store.structure_delete(sid)
    return len(rows), sorted(to_retire), kept


#: ``store.list_refs`` page size while collecting every live se design
#: slug for a not-found suggestion — a guessed slug must not cap the
#: candidate set at the 50-row default page.
_SLUG_PAGE = 200

#: ``next=`` on a design-not-found names the listing call — the door that
#: answers "which designs DO exist" without another guessed slug.
_DESIGN_LIST_NEXT = "get(kind='se') — lists every live se design"


def design_not_found(store: Any, slug: Any, *, next: Any = None) -> NotFound:
    """``NotFound`` for an unknown ``se`` design id, carrying the up-to-5
    closest live design slugs as ``options=`` and the design listing as
    ``next=`` when the caller didn't supply a more specific one.

    A bare "not found" dead-ends the caller into retrying the same
    lookup (gr528152: agents guessed ``boxmake-molding``/``boxmake-fdm``
    with no way to discover the real ``box3-fdm``); the close matches
    turn the refusal into "did you mean one of these".
    """
    slugs: list[str] = []
    offset = 0
    while True:
        refs = store.list_refs(
            kind="se", order_by="id_desc", limit=_SLUG_PAGE, offset=offset
        )
        if not refs:
            break
        slugs.extend(str(r.slug) for r in refs)
        offset += _SLUG_PAGE
        if len(refs) < _SLUG_PAGE:
            break
    return NotFound(
        f"se design {slug!r} not found",
        options=nearest_slugs(str(slug), slugs) or None,
        next=next if next is not None else _DESIGN_LIST_NEXT,
    )
