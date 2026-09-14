"""Store write-back for the se block tree — shared by ``put``/``edit``.

The :mod:`precis_nm.persist` discipline, transferred whole (see that
module's docstring for the full reasoning — the "Round-2 landmine" there is
designed out here from day one): a design's blocks live in dedicated tables
(``se_blocks``/``se_ports``/``se_connects``, migration ``0001_se_kind.sql``;
``se_measures`` from ``0002``, ``se_bom`` from ``0003``, ``se_notes``
from ``0005``, ``se_topology`` — the atomic mode's L2 threading — from
``0007``)
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

Copying a design's rows with their uids intact is what would make a
branch diff block-by-block (design-state-core.md item 5); the mechanism
is verified by test here, and the ``pin``→``branch`` verb itself lands
with the design-history wiring.

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
:mod:`precis.blocktree.ops`, never here. A **local** reference additionally
stores ``template_uid`` (0009) and is rendered back from it, so it survives
a relabel like every other cross-reference; a **cross-design** one cannot
yet, because :func:`precis.blocktree.ops.resolve_template` walks foreign
trees by slug and there is no uid→design index to walk instead — that
conversion is its own slice.
"""

from __future__ import annotations

import logging
from typing import Any

from psycopg import Connection
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from precis.blocktree.types import parse_template_ref
from precis.design.uids import mint_uids
from precis_se import catalog
from precis_se.atomic.vocab import ThreadingSpec
from precis_se.bom import BomLine
from precis_se.catalog import Derived
from precis_se.measures import MeasureSpec
from precis_se.notes import NoteSpec
from precis_se.ops import ConnectSpec, PortSpec, SeBlock, SeTree

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
    "bound_kind, bound_design, origins, dof"
)
_PORT_COLS = (
    "block_id, name, roles, direction, annotations, expected_element, "
    "expected_hybridization, bound_design, bound_atom"
)
_CONNECT_COLS = (
    "a_block, a_block_uid, a_port, b_block, b_block_uid, b_port, joint, "
    "kind, objectives"
)
_MEASURE_COLS = (
    "block, block_uid, name, value, relation, strength, reason, min_value, "
    "max_value, origin, unit"
)
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


def load_tree(store: Any, ref_id: int) -> SeTree:
    """Load a design's live block tree, keyed by name (the display label),
    with its live ports (per owning block) and its uid-keyed
    cross-references resolved back to labels (module docstring)."""
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
            cur.execute(
                f"SELECT {_MEASURE_COLS} FROM se_measures "
                "WHERE ref_id = %s AND retired_at IS NULL "
                "ORDER BY id ASC",
                (ref_id,),
            )
            measure_rows = cur.fetchall()
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
            # A LOCAL template reference comes back from its uid; a
            # cross-design one ('slug#block') round-trips as text —
            # module docstring.
            template=_label(uid_to_name, r["template_uid"], r["template_ref"]),
            pose=list(r["pose_xyz"] or [0.0, 0.0, 0.0]),
            rot=list(r["pose_rot"] or [0.0, 0.0, 0.0]),
            envelope=r["envelope"],
            descr=r["descr"],
            use=r["use_"],
            array=dict(r["array_spec"]) if r["array_spec"] is not None else None,
            objectives=dict(r["objectives"] or {}),
            mode=r["mode"],
            bound_kind=r["bound_kind"],
            bound=r["bound_design"],
            origins=dict(r["origins"] or {}),
            dof=dict(r["dof"]) if r["dof"] is not None else None,
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
            )
        )
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
    attach_catalog(store, tree)
    return tree


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


def _local_template_uid(uid_of: dict[str, int], template: str | None) -> int | None:
    """The uid half of a ``template_ref``, for a LOCAL reference only.

    A cross-design reference (``'slug#block'``) gets ``None``: its uid is
    perfectly well defined — uids are global — but resolving it would mean
    loading the foreign design here, and reading it back would mean a
    uid→design index that does not exist (module docstring). The name text
    keeps that case working exactly as it did.
    """
    if template is None:
        return None
    design_slug, block_name = parse_template_ref(template)
    return None if design_slug is not None else uid_of.get(block_name)


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
                " dof) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                "RETURNING id",
                (
                    ref_id,
                    node.uid,
                    parent_id,
                    # The label text is written for both reference forms;
                    # only a LOCAL one also gets a uid — module docstring.
                    node.template,
                    _local_template_uid(uid_of, node.template),
                    name,
                    node.pose,
                    node.rot,
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
                    " bound_design, bound_atom) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
                " b_port, joint, kind, objectives) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
                ),
            )
        for m in tree.measures:
            c.execute(
                "INSERT INTO se_measures "
                "(ref_id, block, block_uid, name, value, relation, strength, "
                " reason, min_value, max_value, origin, unit) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
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
        rows = conn.execute(
            "UPDATE se_blocks SET retired_at = now() "
            "WHERE ref_id = %s AND retired_at IS NULL "
            "RETURNING id",
            (ref_id,),
        ).fetchall()
    return len(rows)
