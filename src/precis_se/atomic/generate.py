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
from precis_se.ops import OpError, SeTree, apply_ops

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store


def ingest_envelope(config: str) -> str:
    """The m-boundary for a *generated* envelope (units-policy-cutover.md's
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


def prepare_generate(
    store: Store, tree: SeTree, op: dict[str, Any], design_slug: str
) -> tuple[str, PendingGenerate]:
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
    carbon's max valence of 4). Every atom's ``hybridization`` is set
    uniformly from :attr:`~precis_se.atomic.generators.GeneratedBlock.
    hybridization`. Returns the caller-facing echo string plus the deferred
    write."""
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
        for port in block.ports:
            apply_ops(
                tree,
                [
                    {
                        "op": "add_port",
                        "block": block_name,
                        "name": port.name,
                        "roles": port.roles,
                        "direction": port.direction,
                        "expected_element": port.expected_element,
                    }
                ],
            )
    except OpError as exc:
        raise BadInput(str(exc)) from exc

    scene = StructScene(cell=generated_cell(block.coords))
    labels: list[str] = []
    for element, cart in zip(block.elements, block.coords, strict=True):
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(cart, dtype=float)))
        scene.atoms[label] = StructAtom(
            label=label,
            element=element,
            frac=frac,
            hybridization=block.hybridization,
        )
        labels.append(label)
    for i, j, order, kind in block.bonds:
        scene.bonds.append(StructBond(i=labels[i], j=labels[j], order=order, kind=kind))

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
    )
    topo = ", ".join(f"{k}={v}" for k, v in block.topology.items())
    echo = (
        f"generated block {block_name!r} via {gen_name!r}: "
        f"{len(scene.atoms)} atom(s), {len(scene.bonds)} bond(s), "
        f"{len(block.ports)} port(s), bound to structure {struct_slug!r} "
        f"({topo})"
    )
    return echo, pending


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
    )
    node.bound_kind = "structure"
    node.bound = pending.struct_slug
    for port_name, atom_label in pending.ports_map.items():
        p = node.ports.get(port_name)
        if p is None:
            continue
        p.bound_design = pending.struct_slug
        p.bound_atom = atom_label
