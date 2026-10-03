"""``generate`` — the deterministic fill path, in two halves.

Transferred from ``precis_nm.handler`` by the nm→se merge
(docs/backlog/nm-se-merge.md). A ``generate`` op runs a pure
:mod:`precis_se.atomic.generators` builder (``params → block``, the
IC-design PCell) and then does everything that builder can't: adds the
block + its ports to the tree, mints a fresh ``structure`` design holding
the realized atoms/bonds, and binds it. No LLM guessing anywhere on this
path — a ``(n, m)`` nanotube is a formula, not a judgement call.

**Two halves, on purpose** (the "orphan on partial failure" finding):
:func:`prepare_generate` is pure/in-memory and runs in op order like every
other op; :func:`finish_generate` holds every store write and runs only
after the *whole* ops list has validated. ``structure_save`` commits on
its own (``store.tx()`` opens a fresh connection per call — it does not
nest with the caller's transaction), so a synchronous mint would let a
*later* op's failure strand a committed structure design with no block
pointing at it.

**The Å boundary lives here** (nm-se-merge.md's units trap): generators
emit genuinely ``Å``-suffixed envelope text
(:func:`precis_se.atomic.generators.fmt_length_A` — the atomistic enclave
writes its own unit into the string), while se's ``add_block`` parses
canonical/storage mode (bare metres) and would reject that text outright.
:func:`ingest_envelope` does the one Å→m conversion, at this one
boundary, before ``add_block`` ever sees the string — se's agent-facing
``add_block``/``set_envelope`` contract is untouched by the merge.
"""

from __future__ import annotations

import dataclasses
import itertools
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.errors import BadInput
from precis.structure import Atom as StructAtom
from precis.structure import Bond as StructBond
from precis.structure import Scene as StructScene
from precis.structure.cell import Cell as StructCell
from precis_se.atomic.generators import GENERATORS, GeneratorError
from precis_se.atomic.validate import A_to_m
from precis_se.chain.layout import LAYOUT_PORT_MARKER
from precis_se.ops import OpError, SeTree, apply_ops

#: ``realize_chain``'s ``relax_loops`` default — on: a realized loop is a
#: chained backbone unless the caller says ``relax_loops=false`` (the
#: rigid-template placement at the curve's own spacing, kept for
#: inspection). Default-on is Reto's ruling on gr457928: an unchained
#: loop is a 5–10 Å "bond" every downstream check has to explain away,
#: and the relax costs seconds on the loop subgraph alone.
RELAX_LOOPS_DEFAULT = True

#: Base-ring and exocyclic atoms of the Arnott templates — planar, so
#: ``sp2`` for the geometric relax's angle term; everything else (the
#: backbone, the sugar, thymine's methyl C7) is ``sp3``.
_SP2_ATOM_NAMES = frozenset(
    {
        "N1",
        "C2",
        "N3",
        "C4",
        "C5",
        "C6",
        "N7",
        "C8",
        "N9",
        "O2",
        "O4",
        "O6",
        "N2",
        "N4",
        "N6",
    }
)
#: A duplex atom this close (Å) to any loop atom joins the relax as a
#: pinned repulsion partner, so a chained loop cannot pass through the
#: helix end it caps.
_LOOP_RELAX_REACH_A = 4.5
_LOOP_RELAX_ITERS = 400
#: Largest per-step displacement (Å) at which the relax counts as settled —
#: a nominal geometry, not an energy minimum, so a hundredth of an ångström.
_LOOP_RELAX_TOL_A = 1e-2

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store


def ingest_envelope(config: str) -> str:
    """The m-boundary for a *generated* envelope (`precis/utils/units.py`'s
    ingest boundary, mirroring cad's own shipped posture —
    ``precis.cad.dsl``'s ``require_units=True``/``format_spec`` pair, never
    re-implemented here): every dimensioned token must carry an explicit
    unit (:data:`~precis.utils.units.LENGTH_UNIT_TOKEN`), which a
    generator's text always does, and the result is re-canonicalised to
    bare SI metres — exactly the shape every other stored
    ``se_blocks.envelope`` value has, whether it came from an agent's
    literal metres string or from here.

    Moved from ``precis_nm.ops._ingest_envelope`` unchanged in behaviour,
    but deliberately NOT wired into se's ``add_block``: nm required units
    on every hand-authored envelope, se's canonical-metres contract does
    not, and flipping that is an agent-facing change the merge doesn't get
    to make (nm-se-merge.md is a mechanical window). So the one caller is
    :func:`prepare_generate`, which owns the Å-text→metres crossing.

    A malformed config is wrapped as :class:`~precis_se.ops.OpError`, the
    same "bad envelope: ..." shape every other envelope-touching call site
    gives; a missing unit (:class:`~precis.utils.units.UnitRequiredError`)
    is itself already a structured, retryable error and propagates as-is,
    uncaught."""
    try:
        spec = cad_dsl.parse(config, require_units=True)
    except cad_dsl.DslError as exc:
        raise OpError(f"bad envelope: {exc}") from exc
    return cad_dsl.format_spec(spec)


def generated_cell(coords: np.ndarray) -> StructCell:
    """A non-periodic (``pbc=(F,F,F)``) cube cell sized to comfortably
    contain a generator's realized atoms — ``structure``'s molecule mode.
    Non-periodic axes never wrap (:meth:`~precis.structure.cell.Cell.wrap`),
    so the exact size only has to avoid a degenerate (zero-volume)
    lattice — it is not a physical boundary."""
    extent = float(np.max(np.abs(coords))) if coords.size else 1.0
    size = 2.0 * extent + 20.0
    return StructCell.from_lengths_angles(size, size, size, pbc=(False, False, False))


@dataclass
class PendingGenerate:
    """A ``generate`` op's deferred store write (:func:`prepare_generate`'s
    docstring, "orphan on partial failure") — everything needed to mint the
    structure design and wire the block/ports' binding directly, with no
    re-validation needed at that point: the port→atom element gate already
    ran, against the in-memory generated atoms, before this was built."""

    block_name: str
    struct_slug: str
    title: str
    scene: StructScene
    card_text: str
    provenance: str
    ports_map: dict[str, str]
    #: The generator's build record, persisted on the minted structure
    #: ref's ``meta["generated"]`` (:func:`generated_record`) so
    #: ``view='block'`` can show the report after minting -- the generate
    #: echo only digests it, and a check-mode re-run is not the same build
    #: (dogfood 2026-09-27: ``extent.snap``/``fit.propagated`` for a minted
    #: block were unreachable through every se view).
    generated: dict[str, Any] | None = None


def prepare_generate(
    store: Store, tree: SeTree, op: dict[str, Any], design_slug: str
) -> tuple[str, PendingGenerate | None]:
    """``{"op": "generate", "generator": <name>, "params": {...}, "name":
    <new block name>, "parent"?/"pose"?/"rot"?: ...}`` — the pure/in-memory
    half (module docstring): runs a pure builder (params → a
    :class:`~precis_se.atomic.generators.GeneratedBlock`), does everything
    that builder can't *except touch the store* — ``add_block`` with the
    generated envelope (converted to metres by :func:`ingest_envelope`)
    plus the provenance note as ``desc``, ``add_port`` for every generated
    port, builds the ``structure`` Scene the block will eventually own
    (atoms/bonds, in memory), and validates the port→atom element gate
    against those in-memory atoms directly (no need to read anything back —
    the same check ``bind_structure`` would run, done early so the deferred
    write in :func:`finish_generate` can never fail *validation*, only a
    raw DB error). Its store use is read-only: the slug-collision
    preflight.

    A generated bond's order/kind come straight from the generator's own
    ``(i, j, order, kind)`` quadruples — never a hardcoded aromatic order
    for every family (gripe 279306: an all-``order=1.5`` assignment
    over-sums an all-sp² atom's declared valence budget, 3 × 1.5 = 4.5 >
    carbon's max valence of 4). Each atom's ``hybridization`` comes from
    :attr:`~precis_se.atomic.generators.GeneratedBlock.hybridizations`
    when the generator set a per-atom list, else uniformly from
    :attr:`~precis_se.atomic.generators.GeneratedBlock.hybridization`.

    A generator may instead return a **dry-run** block
    (:attr:`~precis_se.atomic.generators.GeneratedBlock.dry_run` — a
    check/report-only result): its rendered report goes to the caller as
    the echo and the pending write is ``None``, so nothing enters the
    tree and nothing is minted. Returns the caller-facing echo string
    plus the deferred write."""
    gen_name = op.get("generator")
    if not gen_name or not str(gen_name).strip():
        raise BadInput("generate needs 'generator'")
    gen_name = str(gen_name).strip()
    builder = GENERATORS.get(gen_name)
    if builder is None:
        known = ", ".join(sorted(GENERATORS))
        raise BadInput(f"unknown generator {gen_name!r}; known: {known}")
    block_name = op.get("name")
    if not block_name or not str(block_name).strip():
        raise BadInput("generate needs 'name' (the new block's name)")
    block_name = str(block_name).strip()
    if block_name in tree.blocks:
        raise BadInput(
            f"duplicate block name: {block_name!r} (names are unique per design)"
        )
    params = op.get("params") or {}
    if not isinstance(params, dict):
        raise BadInput("generate 'params' must be a JSON object")
    try:
        block = builder(params)
    except GeneratorError as exc:
        raise BadInput(f"generate({gen_name!r}): {exc}") from exc

    # Dry-run (``block.dry_run``, e.g. the hexfold generator's check-only
    # mode): the block carries the rendered report as its provenance and
    # nothing else — no slug preflight, no add_block, no structure mint.
    # The caller gets the report as the echo and a ``None`` pending, so
    # the deferred-write half has nothing to finish.
    if block.dry_run:
        return f"{gen_name} check for block {block_name!r}:\n{block.provenance}", None

    # Slug-collision preflight: structure_save is create-or-replace, and
    # "axle"-style block names are exactly the shared vocabulary a
    # hand-authored structure design might already use at this slug —
    # generate must never silently retire an unrelated design's atoms.
    # Always reject on collision (no byte-identical-retry carve-out:
    # nothing here can safely tell a genuine re-generate apart from a name
    # clash against someone else's design, so don't guess).
    struct_slug = f"{design_slug}-{block_name}"
    if store.get_ref(kind="structure", id=struct_slug) is not None:
        raise BadInput(
            f"generate: a structure design already exists at "
            f"{struct_slug!r} — generate never overwrites an existing "
            "design (the minted slug is always "
            f"'{design_slug}-<block name>'); pick a different block "
            "'name', or delete(kind='structure', id="
            f"{struct_slug!r}) first if this is a genuine re-generate"
        )

    # Mint the structure Scene + every atom's label up front (ordinal i =
    # array position = labels[i]) so a port's ``annotations`` can name its
    # dangling-ring atoms by label in the add_port loop below -- unlike
    # ``finish_generate``'s deferred store write, this can't wait:
    # ``scene.next_label`` needs each atom already inserted into
    # ``scene.atoms`` before minting the next one of the same element (its
    # own docstring), so the label a port wants has to exist before that
    # port's op runs, not after every op has validated.
    scene = StructScene(cell=generated_cell(block.coords))
    labels: list[str] = []
    for i, (element, cart) in enumerate(zip(block.elements, block.coords, strict=True)):
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(cart, dtype=float)))
        scene.atoms[label] = StructAtom(
            label=label,
            element=element,
            frac=frac,
            hybridization=(
                block.hybridizations[i]
                if block.hybridizations is not None
                else block.hybridization
            ),
        )
        labels.append(label)
    for i, j, order, kind in block.bonds:
        scene.bonds.append(StructBond(i=labels[i], j=labels[j], order=order, kind=kind))

    add_op: dict[str, Any] = {
        "op": "add_block",
        "name": block_name,
        # The one Å→m crossing (module docstring): generators emit
        # Å-suffixed text and se's add_block speaks canonical metres, so
        # the multiply happens HERE, once, and add_block receives exactly
        # what a hand-authored envelope looks like.
        "envelope": ingest_envelope(block.envelope),
        "desc": block.provenance,
    }
    for passthrough in ("parent", "pose", "rot"):
        if op.get(passthrough) is not None:
            add_op[passthrough] = op[passthrough]
    try:
        apply_ops(tree, [add_op])
        # generate is the one op that knows the realization is atomistic
        # (it mints the structure and binds it below), so it states the
        # mode; leaving it unassigned made DRC ask the user to resolve a
        # mode_binding_mismatch the tool itself created (gr454488 #1).
        apply_ops(tree, [{"op": "set_mode", "block": block_name, "mode": "atomic"}])
        for port in block.ports:
            port_op: dict[str, Any] = {
                "op": "add_port",
                "block": block_name,
                "name": port.name,
                "roles": port.roles,
                "direction": port.direction,
                "expected_element": port.expected_element,
            }
            # The port *type* seam (docs/backlog/hexfold-integration.md
            # step 5's prerequisite): only when the generator actually
            # typed the port (``lattice`` set — today only hexfold rims) —
            # the pre-existing single-atom generators' ports stay
            # annotation-free, unchanged, since they carry no lattice/
            # payload/ring of their own to store. ``atoms`` is the port's
            # dangling ring (or, for a single-atom port, just its own
            # atom) by REAL label, so a later ``join`` op can read the
            # ring straight off the stored port without touching the
            # generator again.
            if port.lattice is not None:
                ring = list(port.atoms) if port.atoms is not None else [port.atom_index]
                port_op["annotations"] = {
                    "lattice": port.lattice,
                    "payload": port.payload,
                    "atoms": [labels[i] for i in ring],
                }
            # Only when the generator actually stated one — add_port reads
            # an absent 'pose' as "no stored pose", and a null rot with a
            # null pose would be refused as a rotation with no origin.
            if port.pose is not None:
                port_op["pose"] = port.pose
                if port.rot is not None:
                    port_op["rot"] = port.rot
            apply_ops(tree, [port_op])
        # Length anchors (GeneratedMeasure): the generator's discrete
        # parameters as se measures in metres, the realisable band as
        # min/max -- from here on the user's relations onto
        # ``<block>.<name>`` are ordinary stack-up rows.
        # A measure is a bare number in the measure's unit, so the Å → m
        # multiply happens here — via validate's A_to_m, the one allowed
        # structure-enclave crossing, never a local factor (the seam test).
        for gm in block.measures:
            m_op: dict[str, Any] = {
                "op": "add_measure",
                "block": block_name,
                "name": gm.name,
                "value": A_to_m(gm.value_A),
                "unit": "m",
                "strength": "gauge",
                # Declared by the generator, not authored: a later
                # set_measure is then visibly an override of a generated
                # band, not of a user's number (gr454488 #5).
                "origin": "generated",
            }
            if gm.min_A is not None:
                m_op["min"] = A_to_m(gm.min_A)
            if gm.max_A is not None:
                m_op["max"] = A_to_m(gm.max_A)
            if gm.reason:
                m_op["reason"] = gm.reason
            apply_ops(tree, [m_op])
    except OpError as exc:
        raise BadInput(str(exc)) from exc

    # Port -> atom element gate, run here against the in-memory atoms (the
    # exact check ``bind_structure`` runs, moved earlier so
    # ``finish_generate`` can never fail on *validation* — only a raw DB
    # error, the documented residual).
    ports_map: dict[str, str] = {}
    for port in block.ports:
        atom_label = labels[port.atom_index]
        element = block.elements[port.atom_index]
        if port.expected_element and port.expected_element != element:
            raise BadInput(
                f"generate({gen_name!r}): port {block_name}.{port.name} "
                f"expects element {port.expected_element!r}, but the "
                f"generated atom is {element!r} (generator bug — file "
                "a gripe)"
            )
        ports_map[port.name] = atom_label

    title = f"{block_name} ({gen_name} generator)"
    card_text = (
        f"{title} (atomistic structure). {block.provenance} "
        f"{len(scene.atoms)} atoms, {len(scene.bonds)} bonds."
    )
    pending = PendingGenerate(
        block_name=block_name,
        struct_slug=struct_slug,
        title=title,
        scene=scene,
        card_text=card_text,
        provenance=block.provenance,
        ports_map=ports_map,
        generated=generated_record(gen_name, block.topology),
    )
    topo = ", ".join(f"{k}={_topo_brief(v)}" for k, v in block.topology.items())
    echo = (
        f"generated block {block_name!r} via {gen_name!r}: "
        f"{len(scene.atoms)} atom(s), {len(scene.bonds)} bond(s), "
        f"{len(block.ports)} port(s), bound to structure {struct_slug!r} "
        f"({topo})"
    )
    return echo, pending


#: ``GeneratedBlock.topology`` keys kept in the persisted build record.
#: Deliberately NOT ``regions``/``ports``/``canonical_json``: those carry
#: every atom ordinal (kilobytes per block, the same reason the echo
#: digests them) and the ports already live on the se block.
_GENERATED_RECORD_KEYS = (
    "hexfold",
    "spec",
    "report",
    "rings",
    "n_atoms",
    "n_bonds",
    "seed_kind",
    "measures",
    "chiral_index",
    "radius_A",
    "surface_meridian",
    "fillet_radius_A",
    "theta_p_max_deg",
    "scene",
    "plan",
)


def generated_record(gen_name: str, topology: dict[str, Any]) -> dict[str, Any]:
    """The trimmed, JSON-ready build record ``finish_generate`` persists on
    the structure ref (``meta["generated"]``): the generator's name plus
    the :data:`_GENERATED_RECORD_KEYS` it reported. Rendered by
    ``view='block'`` (:func:`precis_se.handler._render_block`)."""
    record: dict[str, Any] = {"generator": gen_name}
    for key in _GENERATED_RECORD_KEYS:
        if key in topology:
            record[key] = topology[key]
    return record


def _topo_brief(value: object, limit: int = 80) -> str:
    """One-line digest of a topology fact for the generate echo.

    The facts themselves land on the block (``topology``); the echo only
    names them. Scalars and short strings print verbatim; a long string
    (a ``.hx`` spec, a canonical JSON) is cut with its length; a dict
    shows its keys and a list its length -- the hexfold generator's
    ``regions``/``ports`` carry every atom ordinal, which turned a
    one-line echo into kilobytes (dev-DB dogfood,
    docs/backlog/hexfold-integration.md step 4).
    """
    if isinstance(value, dict):
        keys = ",".join(str(k) for k in value)
        return f"{{{keys}}}" if len(keys) <= limit else f"{{{len(value)} keys}}"
    if isinstance(value, list | tuple):
        return f"[{len(value)} items]"
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…({len(text)} chars)"


def finish_generate(store: Store, tree: SeTree, pending: PendingGenerate) -> None:
    """The store-touching half (:func:`prepare_generate`'s docstring) —
    called by :func:`precis_se.atomic.apply.apply_ops_with_atomic` only
    after every op in the whole list has validated cleanly, immediately
    before the caller's own ``persist.save_tree``. Deferring past the whole
    ops list closes the "orphan on partial failure" window: a later op
    failing can no longer leave a minted structure design with no block
    pointing at it, because nothing is minted until every op — including
    any later ones — has already proven itself valid.

    The one residual: a hard crash between this function's
    ``structure_save`` and the caller's ``persist.save_tree`` (two separate
    transactions — ``store.tx()`` opens a fresh connection per call, so
    ``structure_save`` can't join the tree's own transaction without
    changing its signature) would leave a real, valid structure design that
    this call's tree edit never got to reference — not a dangling pointer
    (nothing points at it yet), just a design an operator would need to
    notice and clean up by hand. Skips entirely (no mint at all) if a
    *later* op in the same list already removed the block this pending
    write was for — never mint something the caller's own later op just
    undid."""
    node = tree.blocks.get(pending.block_name)
    if node is None:
        return
    store.structure_save(
        slug=pending.struct_slug,
        title=pending.title,
        scene=pending.scene,
        version=1,
        card_text=pending.card_text,
        description=pending.provenance,
        meta_extra=(
            {"generated": pending.generated} if pending.generated is not None else None
        ),
    )
    node.bound_kind = "structure"
    node.bound = pending.struct_slug
    for port_name, atom_label in pending.ports_map.items():
        p = node.ports.get(port_name)
        if p is None:
            continue
        p.bound_design = pending.struct_slug
        p.bound_atom = atom_label


# ── realize_chain — atoms for a region of a helix ───────────────────────


@dataclass
class PendingRealizeChain:
    """A ``realize_chain`` op's deferred store write — the minted structure
    plus the bind that follows it (:func:`finish_realize_chain`), built by
    :func:`prepare_realize_chain` after every check has run against the
    in-memory atoms."""

    block_name: str
    struct_slug: str
    title: str
    scene: StructScene
    card_text: str
    provenance: str
    #: ``bind_structure``'s object-form ``ports=`` payload: each port's
    #: atom plus the ``axis_atom``/``phase_atom`` pair that makes the bind
    #: measure a frame into ``rot``.
    ports_map: dict[str, dict[str, str]]
    #: PDB naming per atom, in scene atom order — persisted on the
    #: structure ref's ``meta['chain_atoms']`` so ``view='pdb'`` and the
    #: se ``view='export'`` PDB can name residues (a ``structure`` scene
    #: carries elements and labels, not residues).
    chain_atoms: dict[str, Any]


def _loop_atoms(region: Any) -> list[int]:
    """Indices of the atoms of every loop nucleotide (a residue row with no
    helix offset) and every inserted base (a non-zero insertion index) —
    the residues off the duplex the relax may move — in atom order."""
    loop_keys = {
        (c, r)
        for c, r, _s, _o, offset, _l, ins in region.residues
        if offset is None or ins
    }
    if not loop_keys:
        return []
    return [
        i
        for i, key in enumerate(zip(region.chain_ids, region.resseq, strict=True))
        if key in loop_keys
    ]


def _worst_loop_step(region: Any, coords_A: np.ndarray, loop_set: set[int]) -> float:
    """The longest inter-residue O3'–P bond touching a loop nucleotide (Å) —
    the number that says whether a loop's backbone is chained (a bond
    length) or merely connected (the curve's own spacing)."""
    worst = 0.0
    for i, j in region.bonds:
        if (i in loop_set or j in loop_set) and region.resseq[i] != region.resseq[j]:
            if {region.names[i], region.names[j]} == {"O3'", "P"}:
                worst = max(worst, float(np.linalg.norm(coords_A[j] - coords_A[i])))
    return worst


def _chain_loops(
    region: Any, coords_A: np.ndarray
) -> tuple[np.ndarray, dict[str, Any] | None]:
    """Chain the loop nucleotides (gr457928): a geometric relax
    (:func:`precis.structure.georelax.relax_graph`) over the loop
    residues' atoms with every duplex atom pinned — the bond springs pull
    each loop residue's P onto its predecessor's O3' (the templates were
    placed rigid at the curve's own spacing, so those steps start at
    5–10 Å), non-bond repulsion keeps a residue off its neighbours and off
    the duplex atoms within :data:`_LOOP_RELAX_REACH_A`, and the VSEPR
    angle term keeps rings planar and sugars tetrahedral. **Geometry, not
    thermodynamics**: no pairing, no stacking energy, no sampling, and the
    duplex never moves. Only the loop atoms and their nearby duplex atoms
    enter the relax (the engine is O(n²) per step), so a scaffold-length
    region pays for its loops, not its duplex.

    Returns the coordinates (a copy, Å) and a report — the worst
    inter-residue O3'–P step touching a loop residue before and after,
    the loop atom count, and the engine's convergence — or the input and
    ``None`` when the region has no loop residue to chain.
    """
    from precis.structure.georelax import relax_graph

    loop_atoms = _loop_atoms(region)
    if not loop_atoms:
        return coords_A, None
    loop_set = set(loop_atoms)

    def max_step(xyz: np.ndarray) -> float:
        return _worst_loop_step(region, xyz, loop_set)

    before = max_step(coords_A)
    gaps = np.linalg.norm(
        coords_A[:, None, :] - coords_A[loop_atoms][None, :, :], axis=2
    ).min(axis=1)
    # The duplex atoms the loop is bonded to (its two anchors) always join,
    # whatever their distance — they are what the springs pull the loop
    # ends onto; the rest join by proximity as repulsion partners.
    anchors = {
        j if i in loop_set else i
        for i, j in region.bonds
        if (i in loop_set) != (j in loop_set)
    }
    near = [
        i
        for i in range(int(coords_A.shape[0]))
        if i not in loop_set and (i in anchors or gaps[i] <= _LOOP_RELAX_REACH_A)
    ]
    members = loop_atoms + near
    index = {atom: k for k, atom in enumerate(members)}
    sub = coords_A[members].copy()
    trace = relax_graph(
        [region.elements[i] for i in members],
        sub,
        [(index[i], index[j]) for i, j in region.bonds if i in index and j in index],
        frozenset(index[i] for i in near),
        hybridizations=[
            "sp2" if region.names[i] in _SP2_ATOM_NAMES else "sp3" for i in members
        ],
        iters=_LOOP_RELAX_ITERS,
        tol=_LOOP_RELAX_TOL_A,
    )
    out = coords_A.copy()
    out[members] = sub
    return out, {
        "max_step_before_A": before,
        "max_step_after_A": max_step(out),
        "n_loop_atoms": len(loop_atoms),
        "n_pinned_atoms": len(near),
        "converged": bool(trace.converged),
        "n_steps": int(trace.n_steps),
    }


def _region_int(op: dict[str, Any], key: str) -> int:
    raw = op.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise BadInput(f"realize_chain needs an integer {key!r} (a helix offset)")
    return int(raw)


def _loop_letters(
    sequence: str | None, route: list[Any], after: Any
) -> tuple[str | None, ...]:
    """The letters of the loop that precedes domain ``after`` — the
    strand's sequence read in route order (domains and the loops between
    them), ``None`` per nucleotide when the strand has no sequence."""
    n_loop = int(after.loop_before_nt or 0)
    if sequence is None:
        return (None,) * n_loop
    pos = 0
    for d in route:
        n_before = int(d.loop_before_nt or 0)
        if d.ord == after.ord:
            chunk = sequence[pos : pos + n_before]
            return tuple(chunk) + (None,) * (n_before - len(chunk))
        pos += n_before + d.n_units
    return (None,) * n_loop


def prepare_realize_chain(
    store: Store, tree: SeTree, op: dict[str, Any], design_slug: str
) -> tuple[str, PendingRealizeChain]:
    """``{"op": "realize_chain", "block": <helix>, "start": <offset>, "end":
    <offset, exclusive>, "fidelity"?: "allatom"|"backbone", "sites"?:
    [<offset>, …], "loops"?: bool, "relax_loops"?: bool}`` — the pure/
    in-memory half.

    Every tree read happens here: the helix's per-unit frames
    (:func:`precis_se.chain.layout.helix_geometry`), the occupancy at each
    offset (:func:`precis_se.chain.pairing.derive_pairing`), the placed
    loop curves on the domain rows (``meta.loop_curve``, written by
    ``relax_chain``), and the segment child whose ``chain`` range covers
    the region — then one pure call to
    :func:`precis_se.chain.atoms.build_region`.

    The atoms come out in the **segment child's own local frame** (its
    nominal capsule pose, :func:`precis_chain.envelope.capsule_pose`), which
    is the frame ``envelope_fit`` checks a bound scene in, so the segment's
    own ``cyl`` envelope is what the atoms are held against; the helix
    parent carries none. One region per segment: a range that straddles
    two segments is refused naming both, so the caller cuts it at the
    tiling.

    ``loops=True`` also realizes every loop of the region's strands whose
    two ends are both in the region. A loop with no placed curve is
    ``Unsupported`` naming it — nothing here guesses a loop's geometry;
    ``relax_chain`` places it. The default ``loops=False`` realizes the
    duplex alone, which is how a hairpin's stem realizes before its loop is
    settled. ``relax_loops`` (default
    :data:`RELAX_LOOPS_DEFAULT`) then chains the placed loop nucleotides'
    backbone (:func:`_chain_loops`); without it they sit at the curve's own
    spacing, connectivity right and geometry not.

    Store use is read-only (the slug-collision preflight); the mint and
    the bind are :func:`finish_realize_chain`'s.
    """
    from precis.cad.vec import as_vec3, pose
    from precis.errors import Unsupported
    from precis_chain.envelope import capsule_pose
    from precis_se.chain.atoms import (
        FIDELITIES,
        LoopNts,
        PlacedUnit,
        UnitOccupant,
        build_region,
    )
    from precis_se.chain.layout import helix_geometry, segment_capsule
    from precis_se.chain.pairing import HelixIndel, derive_pairing, inserted_letters
    from precis_se.chain.vocab import (
        HELIX_ROLE,
        SEGMENT_ROLE,
        ChainError,
        chain_role,
        group_domains,
    )

    block = op.get("block")
    if not block or not str(block).strip():
        raise BadInput("realize_chain needs 'block' (a helix block name)")
    key = tree.resolve_key(block)
    if key is None:
        raise BadInput(f"realize_chain: no such block {block!r}")
    helix_node = tree.blocks[key]
    if chain_role(helix_node) != HELIX_ROLE:
        raise BadInput(
            f"realize_chain: block {key!r} is not a helix (declare_helix "
            "declares one; a segment child realizes through its helix)"
        )
    record = helix_node.chain or {}
    nucleic_name = str(record.get("nucleic") or "DNA")
    if nucleic_name != "DNA":
        raise Unsupported(
            f"realize_chain: helix {key!r} is {nucleic_name} — only the B-DNA "
            "fibre templates are built (an A-RNA helix is not approximated "
            "with B-DNA atoms)",
            next="declare the helix as DNA, or file the A-RNA template as a gripe",
        )
    n_units = int(record.get("n_units") or 0)
    start = _region_int(op, "start")
    end = _region_int(op, "end")
    if not 0 <= start < end <= n_units:
        raise BadInput(
            f"realize_chain: region [{start}, {end}) is not inside helix "
            f"{key!r}'s {n_units} units (start < end, end exclusive)"
        )
    fidelity = str(op.get("fidelity") or "allatom").strip().lower()
    if fidelity not in FIDELITIES:
        raise BadInput(
            f"realize_chain: fidelity {fidelity!r} — one of {', '.join(FIDELITIES)}"
        )
    raw_sites = op.get("sites") or []
    if not isinstance(raw_sites, list) or not all(
        isinstance(s, int) and not isinstance(s, bool) for s in raw_sites
    ):
        raise BadInput("realize_chain 'sites' must be a list of helix offsets")
    sites = tuple(sorted({int(s) for s in raw_sites}))
    for s in sites:
        if not start <= s < end:
            raise BadInput(
                f"realize_chain: site {s} is outside the region [{start}, {end})"
            )
    want_loops = bool(op.get("loops", False))
    want_relax = bool(op.get("relax_loops", RELAX_LOOPS_DEFAULT))

    # The segment child the region lives in.
    segments = sorted(
        (int((n.chain or {})["start"]), int((n.chain or {})["end"]), name)
        for name, n in tree.blocks.items()
        if chain_role(n) == SEGMENT_ROLE and (n.chain or {}).get("helix") == key
    )
    if not segments:
        raise BadInput(
            f"realize_chain: helix {key!r} has no segment children — run "
            "layout_chain first (the atoms bind to the segment covering the region)"
        )
    covering = [s for s in segments if s[0] <= start and end - 1 <= s[1]]
    if not covering:
        touched = [
            f"{name} {s}–{e}" for s, e, name in segments if s <= end - 1 and e >= start
        ]
        raise BadInput(
            f"realize_chain: region [{start}, {end}) straddles segments "
            f"{', '.join(touched)} — one region per segment; cut it at the tiling"
        )
    seg_start, seg_end, seg_name = covering[0]
    seg_node = tree.blocks[seg_name]
    if seg_node.bound_kind is not None:
        raise BadInput(
            f"realize_chain: segment {seg_name!r} is already bound "
            f"(kind={seg_node.bound_kind!r}, design={seg_node.bound!r}) — "
            "set_binding(clear=true) first"
        )
    struct_slug = f"{design_slug}-{seg_name}"
    if (
        store is not None
        and store.get_ref(kind="structure", id=struct_slug) is not None
    ):
        raise BadInput(
            f"realize_chain: a structure design already exists at "
            f"{struct_slug!r} — never overwritten; delete(kind='structure', "
            f"id={struct_slug!r}) first if this is a genuine re-realize"
        )

    try:
        geom = helix_geometry(helix_node)
    except ChainError as exc:
        raise BadInput(f"realize_chain: {exc}") from exc
    nominal_origin, nominal_euler, _length = capsule_pose(
        segment_capsule(geom, seg_start, seg_end)
    )
    nominal = pose(
        as_vec3([float(v) for v in nominal_origin]), as_vec3(list(nominal_euler))
    )
    actual = pose(
        as_vec3([float(v) for v in seg_node.pose]),
        as_vec3([float(v) for v in seg_node.rot]),
    )
    m_to_A = 1.0 / A_to_m(1.0)

    pairing = derive_pairing(tree)
    indel = pairing.indels.get(key, HelixIndel())
    # An inserted offset's 1 + k letters per strand: the first on the unit,
    # the k extras bulged after it (pairing gives such an offset no letter).
    inserted: dict[tuple[str, int, int], tuple[str | None, ...]] = {}
    if any(start <= o < end for o in indel.insertions):
        tables_ = group_domains(list(tree.domains))
        for strand, route in tables_.by_strand.items():
            node = tree.blocks.get(strand)
            sequence = (node.chain or {}).get("sequence") if node is not None else None
            for (ord_, offset), letters in inserted_letters(
                sequence, route, pairing.indels
            ).items():
                inserted[(strand, ord_, offset)] = letters
    units: list[PlacedUnit] = []
    strands_here: set[str] = set()
    for offset in range(start, end):
        occ = pairing.at(key, offset)
        if occ is None:
            continue
        occupants = tuple(
            UnitOccupant(
                strand=o.strand,
                ord=o.ord,
                forward=o.forward,
                letter=letters[0] if letters else o.letter,
                extra=letters[1:] if letters else (),
            )
            for o in occ.occupants
            for letters in [inserted.get((o.strand, o.ord, offset))]
        )
        strands_here.update(o.strand for o in occ.occupants)
        units.append(
            PlacedUnit(
                offset=offset,
                origin_A=np.asarray(geom.units.origins[offset], dtype=float) * m_to_A,
                frame=np.asarray(geom.units.frames[offset], dtype=float),
                occupants=occupants,
            )
        )
    if not units:
        raise BadInput(
            f"realize_chain: no strand occupies helix {key!r} offsets "
            f"[{start}, {end}) — add_domain routes a strand through it first"
        )

    loops: list[LoopNts] = []
    if want_loops:
        tables = group_domains(list(tree.domains))
        for strand, route in tables.by_strand.items():
            if strand not in strands_here:
                continue
            node = tree.blocks.get(strand)
            sequence = (node.chain or {}).get("sequence") if node is not None else None
            for before, after in itertools.pairwise(route):
                if before.helix != key or after.helix != key:
                    continue
                if not (
                    start <= before.exit_offset < end
                    and start <= after.entry_offset < end
                ):
                    continue
                n_nt = int(after.loop_before_nt or 0)
                if after.loop_curve is None:
                    if n_nt == 0:
                        loops.append(
                            LoopNts(
                                strand=strand,
                                exit_ord=before.ord,
                                exit_offset=before.exit_offset,
                                entry_ord=after.ord,
                                entry_offset=after.entry_offset,
                                letters=(),
                                points_A=np.zeros((2, 3)),
                            )
                        )
                        continue
                    raise Unsupported(
                        f"realize_chain: the {n_nt}-nt loop of strand {strand!r} "
                        f"between domains {before.ord} and {after.ord} "
                        f"({key}[{before.exit_offset}] → {key}[{after.entry_offset}]) "
                        "has no placed curve — nothing here guesses a loop's "
                        "geometry",
                        next="run relax_chain (it writes each loop's curve), then realize again",
                    )
                curve_world = np.asarray(after.loop_curve, dtype=float)
                curve_nominal = np.array(
                    [
                        np.asarray(
                            nominal.to_world_point(
                                actual.to_local_point(as_vec3(list(p)))
                            ),
                            dtype=float,
                        )
                        for p in curve_world
                    ]
                )
                loops.append(
                    LoopNts(
                        strand=strand,
                        exit_ord=before.ord,
                        exit_offset=before.exit_offset,
                        entry_ord=after.ord,
                        entry_offset=after.entry_offset,
                        letters=_loop_letters(sequence, route, after),
                        points_A=curve_nominal * m_to_A,
                    )
                )

    try:
        region = build_region(
            units,
            fidelity=fidelity,
            sites=sites,
            loops=tuple(loops),
            deletions=frozenset(indel.deletions),
        )
    except ValueError as exc:
        raise BadInput(f"realize_chain: {exc}") from exc

    # World (nominal) → the segment's local frame, once, for every atom.
    local = (
        np.array(
            [
                np.asarray(
                    nominal.to_local_point(as_vec3(list(p * A_to_m(1.0)))), dtype=float
                )
                for p in region.coords_A
            ]
        )
        * m_to_A
    )

    relax_report: dict[str, Any] | None = None
    if want_relax:
        local, relax_report = _chain_loops(region, np.asarray(local, dtype=float))

    scene = StructScene(cell=generated_cell(local))
    labels: list[str] = []
    for element, cart in zip(region.elements, local, strict=True):
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(cart, dtype=float)))
        scene.atoms[label] = StructAtom(label=label, element=element, frac=frac)
        labels.append(label)
    for i, j in region.bonds:
        # Connectivity only: a fibre model carries no bond orders, and an
        # all-1.5 ring assignment over-sums sp² valence budgets (gripe
        # 279306), so every bond is a single, declared, pairwise bond.
        scene.bonds.append(
            StructBond(i=labels[i], j=labels[j], order=1.0, kind="pairwise")
        )

    ports_map: dict[str, dict[str, str]] = {}
    port_ops: list[dict[str, Any]] = []
    for pname, pa in region.ports.items():
        annotations = {**pa.annotations, "atoms": [labels[pa.atom]]}
        existing = seg_node.ports.get(pname)
        if existing is None:
            port_ops.append(
                {
                    "op": "add_port",
                    "block": seg_name,
                    "name": pname,
                    "expected_element": pa.expected_element,
                    "annotations": annotations,
                }
            )
        else:
            # ``layout_chain`` pre-mints ``5p``/``3p`` as backbone anchors.
            # Keep that slot (its roles, and any pose the USER set) but give
            # it the same expected chemistry and annotations the freshly
            # minted ports get, so the element gate runs for every port
            # ``bind_structure`` measures (gripe 457930). The pose layout
            # itself put there (marker ``LAYOUT_PORT_MARKER``, gr458316) is
            # a backbone exit, not a designer's target: drop it, and its
            # direction, so the bind's measurement fills the slot instead
            # of being refused by the declared-pose rule.
            merged = {**existing.annotations, **annotations}
            marker_key, marker_value = LAYOUT_PORT_MARKER
            if merged.get(marker_key) == marker_value:
                merged.pop(marker_key)
                seg_node.ports[pname] = dataclasses.replace(
                    existing,
                    expected_element=pa.expected_element,
                    annotations=merged,
                    pose=None,
                    pose_source=None,
                    direction=None,
                )
            else:
                seg_node.ports[pname] = dataclasses.replace(
                    existing,
                    expected_element=pa.expected_element,
                    annotations=merged,
                )
        ports_map[pname] = {
            "atom": labels[pa.atom],
            "axis_atom": labels[pa.axis_atom],
            "phase_atom": labels[pa.phase_atom],
        }
    try:
        apply_ops(tree, port_ops)
        apply_ops(tree, [{"op": "set_mode", "block": seg_name, "mode": "atomic"}])
    except OpError as exc:
        raise BadInput(str(exc)) from exc

    motif = geom.motif
    provenance = (
        f"realize_chain over {key}[{start}, {end}) at fidelity {fidelity!r}: "
        f"{region.n_residues} nucleotide(s) on {len(units)} unit(s)"
        f"{f' + {len(loops)} placed loop(s)' if loops else ''}; Arnott B-DNA "
        f"fibre templates (Arnott & Hukins 1972; NAB fd_helix 'abdna') placed "
        f"in the {motif.name} unit frames (rise {motif.rise * 1e9:.3f} nm, "
        f"{2 * np.pi / motif.twist:.2f} bp/turn); atoms in segment "
        f"{seg_name!r}'s own frame."
    )
    title = f"{seg_name} ({design_slug} realize_chain)"
    card_text = (
        f"{title} (atomistic structure). {provenance} {len(scene.atoms)} atoms, "
        f"{len(scene.bonds)} bonds."
    )
    chain_atoms = {
        "names": list(region.names),
        "resnames": list(region.resnames),
        "resseq": [int(v) for v in region.resseq],
        "chain_ids": list(region.chain_ids),
        "chains": dict(region.chains),
        "fidelity": fidelity,
        "helix": key,
        "start": start,
        "end": end,
        "nucleic": nucleic_name,
        "motif": motif.name,
        "rise_m": float(motif.rise),
        "twist_rad": float(motif.twist),
        "units": [u.offset for u in units],
        # Per residue in atom order: ``[chain id, resseq, strand, ord,
        # offset or None for a loop nucleotide, letter, insertion index
        # (0 on the unit, i for the i-th inserted base)]`` — the rows
        # ``envelope_fit`` reads to skip loop atoms and a pick reads to
        # name "O3' of DA 8 (stem@3)" (gr457928; se-pick-hierarchy's chain
        # instance).
        "residues": [list(row) for row in region.residues],
    }
    if relax_report is not None:
        chain_atoms["loop_relax"] = dict(relax_report)
        provenance += (
            " Loop nucleotides chained by a geometric relax with the duplex "
            "pinned (precis.structure.georelax; bond springs, repulsion, VSEPR "
            f"angles — geometry, no energy): worst O3'–P step "
            f"{relax_report['max_step_before_A']:.2f} → "
            f"{relax_report['max_step_after_A']:.2f} Å over "
            f"{relax_report['n_loop_atoms']} loop atom(s), "
            f"{'converged' if relax_report['converged'] else 'NOT converged'} in "
            f"{relax_report['n_steps']} step(s)."
        )
    pending = PendingRealizeChain(
        block_name=seg_name,
        struct_slug=struct_slug,
        title=title,
        scene=scene,
        card_text=card_text,
        provenance=provenance,
        ports_map=ports_map,
        chain_atoms=chain_atoms,
    )
    chains = ", ".join(f"{cid}={strand}" for cid, strand in region.chains.items())
    echo = (
        f"realize_chain({key!r}[{start}, {end})): {len(scene.atoms)} atom(s), "
        f"{len(scene.bonds)} bond(s), {region.n_residues} nucleotide(s) at "
        f"fidelity {fidelity!r} on segment {seg_name!r} → structure "
        f"{struct_slug!r} (chains {chains}; ports "
        f"{', '.join(sorted(ports_map))})"
    )
    # The relax/spacing clause belongs to the headline, so it goes on before
    # the per-strand notes — appended after, it read as part of the last note.
    if relax_report is not None:
        echo += (
            "; loop backbone chained by a geometric relax (duplex pinned): worst "
            f"O3'–P step {relax_report['max_step_before_A']:.2f} → "
            f"{relax_report['max_step_after_A']:.2f} Å, "
            f"{'converged' if relax_report['converged'] else 'NOT converged'} in "
            f"{relax_report['n_steps']} step(s)"
        )
    elif loop_atoms := _loop_atoms(region):
        worst = _worst_loop_step(
            region, np.asarray(local, dtype=float), set(loop_atoms)
        )
        echo += (
            "; loop nucleotides sit at the curve's own spacing (worst O3'–P step "
            f"{worst:.2f} Å — connectivity right, geometry not chained): pass "
            "relax_loops=true to chain them"
        )
    for note in region.notes:
        echo += f"\n· {note}"
    return echo, pending


def finish_realize_chain(
    store: Store, tree: SeTree, pending: PendingRealizeChain
) -> None:
    """The store-touching half — ``structure_save`` then the bind, after
    every op in the list has validated, immediately before the caller's
    own ``save_tree`` (the same deferral :func:`finish_generate` explains).
    The bind goes through :func:`precis_se.atomic.bind.bind_structure`'s
    object form, so the ports' poses and rots are *measured* off the atoms
    the same way a hand bind measures them, and the ``envelope_fit``
    preflight runs. Skips entirely when a later op removed the segment."""
    from precis_se.atomic.bind import bind_structure

    node = tree.blocks.get(pending.block_name)
    if node is None:
        return
    store.structure_save(
        slug=pending.struct_slug,
        title=pending.title,
        scene=pending.scene,
        version=1,
        card_text=pending.card_text,
        description=pending.provenance,
        meta_extra={"chain_atoms": pending.chain_atoms},
    )
    bind_structure(
        store,
        tree,
        {
            "op": "bind_structure",
            "block": pending.block_name,
            "design": pending.struct_slug,
            "ports": pending.ports_map,
        },
    )
