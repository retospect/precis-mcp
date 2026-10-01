"""``join`` — the block-joiner op (SPEC §22.2, docs/backlog/
hexfold-integration.md step 5 slice 1): compose two already-resolved
blocks over a matched port pair into a new composite se block. A thin
store-aware wrapper around ``hexfold.join``'s pure numpy half
(``src/hexfold/join.py``'s module docstring names this module as its
intended caller): rebuild each side's :class:`~hexfold.join.Block`
topology from its own generator record, call :func:`~hexfold.join.compose`,
and mint the composite — the same prepare/finish two-halves shape
:mod:`precis_se.atomic.generate` uses, for the same "orphan on partial
failure" reason (:func:`prepare_join`'s docstring).

``{"op": "join", "name": "<composite>", "a": "<block>.<port>", "b":
"<block>.<port>", "seam"?: "auto"|"fuse"|"adapter", "k"?: 0|"fit",
"seam_radius"?: {"a": 8, "b": 2}, "rung"?: "auto"|"stick"|"geo",
"parent"?: "..."}`` — dispatch is by the two ports' **lattice** annotation
pair (:data:`JOINERS`, keyed on the canonical sorted 2-tuple of both
sides' ``GeneratedPort.lattice``, the way
:mod:`precis_se.atomic.generators.hexfold_spec` tags a port's
``GeneratedPort.lattice``): today only ``("sp2-hex", "sp2-hex")`` (two
hexfold rims) is wired; a missing annotation or an unregistered pair is
``join.lattice``, before anything else runs. Before even that: an
endpoint that names a block already claimed as a **part** of another
composite is ``join.part_addressed`` — the fix is an addressing one
(join through the owning composite's own exposed port), not a lattice or
ownership one (:func:`_addressed_part_redirect`'s docstring).

**Rung gate** (slice 2, :func:`_select_relaxer`): each side's resolved
rung comes off its own bound structure's ``meta['last_relax']['rung']``
(:func:`_rung_of`, absent → ``"stick"``, the generator's untouched
preview geometry). ``rung="auto"`` (default) picks the shared rung when
both sides agree, or raises ``join.rung`` (:class:`~precis.errors.
BadInput`) when they don't — relaxing one side's rest lengths against the
other's frozen boundary is a false-leak hazard, not a join `compose` can
paper over. An explicit ``"stick"``/``"geo"`` forces that rung regardless
of what either side is actually relaxed to; forcing ``"geo"`` over a
stick-rung block is allowed but records ``join.rung`` WARN (rest lengths
1.42 vs 1.52 A strain the frozen boundary). The chosen rung's own
:data:`~hexfold.join.Relaxer` and leak thresholds
(:data:`~hexfold.join.LEAK_THRESH_GEO` on ``"geo"``, the stick defaults
otherwise) both follow from this one choice, recorded verbatim as
``meta['generated']['relaxer']``.

**Rebuilding a block's topology, generically and recursively**
(:func:`_rebuild_block`, design call 3 — "join-time rings/zones from spec
rebuild", generalised to a *chain* of joins rather than only a single
hexfold spec): a block bound to a ``hexfold``-generated structure rebuilds
its :class:`~hexfold.build.Net` from ``meta['generated']['spec']`` exactly
as the generator did; a block bound to an EARLIER join's composite has no
spec of its own, so it recursively rebuilds its two recorded ``parts``
and replays :func:`~hexfold.join.compose` with the recorded ``k``/
``seam_radius`` — deterministic, so the replay's topology (bonds/rings/
ports) agrees with what the original join actually minted. Either way,
**coordinates always come from the structure ref's own stored atoms**,
never recomputed — only topology is rebuilt/replayed; a rebuild whose
element sequence disagrees with what is actually stored is ``join.stale``
(regenerate advised), never silently patched over.

**Ordinal↔label mapping** is the same contract :mod:`precis_se.atomic.
generate` uses: ``structure_load`` returns atoms in ``id ASC`` order
(store contract, never edited in place for a generated/composite ref —
the append-only body-chunk rule's structure-design analogue), so ordinal
``i`` of the rebuilt topology is atom ``i`` of the loaded scene by
construction; validated by count and per-ordinal element equality before
anything is trusted.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import numpy as np

from hexfold.build import Port as HxPort
from hexfold.build import _fit_alternatives_finding, build
from hexfold.join import LEAK_THRESH_GEO, Block, block_from_net, compose, rank_k
from hexfold.report import Finding as HxFinding
from hexfold.report import Report as HxReport
from hexfold.report import Severity as HxSeverity
from precis.cad.vec import euler_rad_from_matrix
from precis.errors import BadInput, NotFound
from precis.structure import Atom as StructAtom
from precis.structure import Bond as StructBond
from precis.structure import Scene as StructScene
from precis.structure.georelax import relax_graph
from precis_se.atomic.catalogue import for_store
from precis_se.atomic.generate import generated_cell, ingest_envelope
from precis_se.atomic.generators._types import fmt_length_A
from precis_se.atomic.generators.hexfold_spec import _se_port_name
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A
from precis_se.atomic.validate import A_to_m
from precis_se.ops import OpError, PortSpec, SeTree, apply_ops, compose_world_pose
from precis_se.ops import effective_ports as _effective_ports

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

#: The sp2-sheet Pauling bond order every composite scene atom gets —
#: :mod:`precis_se.atomic.generators.hexfold_spec`'s own convention, reused
#: rather than re-derived. A :class:`~hexfold.join.Block`/``Composite``
#: carries no per-atom hybridization of its own (its own module docstring:
#: "a composed block's ring interiors are always sp2 in this slice; sp3
#: seam vertices are a later slice's scope"), so every composite atom is
#: stamped ``"sp2"`` and every composite bond gets this uniform order —
#: honest for this slice's fuse/adapter motifs (plain sp2 tubes/sheets/
#: caps), and a documented simplification for a later slice that joins a
#: block with sp3 attachment sites.
_SP2_BOND_ORDER = 4.0 / 3.0

#: geo rung sub-graph relax iteration budget (slice 2) -- deliberately
#: smaller than `tests/test_hexfold_seam_decay.py`'s 4000 (a *whole*
#: tube's free relax): the seam sub-graph :func:`~hexfold.join.compose`
#: hands :func:`geo_relax_pinned` is the movable radius plus a two-shell
#: guard band only, not a whole part, so it converges well inside this
#: budget in practice -- `relax_graph`'s own ``tol=1e-4`` early-stop
#: (:data:`_GEO_TOL`) still cuts it short whenever it does.
_GEO_RELAX_ITERS = 2000
_GEO_TOL = 1e-4


def geo_relax_pinned(
    elements: list[str],
    coords: np.ndarray,
    bonds: list[tuple[int, int, int]],
    rings: list[tuple[int, ...]],
    pinned_mask: np.ndarray,
) -> np.ndarray:
    """The geo-rung :data:`~hexfold.join.Relaxer` (slice 2): plain
    :func:`~precis.structure.georelax.relax_graph` over the seam sub-graph
    :func:`~hexfold.join.compose` hands it -- bond springs at the
    covalent-radius sum + a VSEPR angle term at every vertex, the SAME
    physics `tests/test_hexfold_seam_decay.py` measures the geo leak
    thresholds against, rather than :mod:`hexfold.stick`'s ring-chord
    springs (``rings`` is accepted only to match the shared
    :data:`~hexfold.join.Relaxer` signature and is otherwise unused here --
    `relax_graph` derives its own angle triples from the bond graph
    directly). Every composite vertex is stamped uniform ``"sp2"``
    (:data:`_SP2_BOND_ORDER`'s own docstring: "a composed block's ring
    interiors are always sp2 in this slice"). ``pinned_mask``'s 1-meaning
    is :data:`~hexfold.join.Relaxer`'s own convention, translated to
    `relax_graph`'s pinned-INDEX-SET convention exactly as that type's
    docstring spells out."""
    del rings
    pinned = {i for i, p in enumerate(pinned_mask) if p}
    out = np.asarray(coords, dtype=np.float64).copy()
    relax_graph(
        list(elements),
        out,
        [(i, j) for i, j, *_rest in bonds],
        pinned,
        hybridizations="sp2",
        iters=_GEO_RELAX_ITERS,
        tol=_GEO_TOL,
    )
    return out


def _rung_of(ref: Any) -> str:
    """``ref.meta['last_relax']['rung']``, defaulting to ``"stick"`` when
    absent (never relaxed past its generator's own preview geometry) --
    the plan's own contract for rung detection, read straight off the
    :func:`_rebuild_block`-loaded :class:`~precis.store.types.Ref`, the
    same object :func:`~precis.store._structure_ops.StructureMixin.
    structure_save` stamps ``last_relax`` onto (`relax.py`'s
    ``_relax_summary``)."""
    return str(((ref.meta or {}).get("last_relax") or {}).get("rung") or "stick")


def _select_relaxer(
    rung_a: str, rung_b: str, forced: str, sigma: float, a_block: str, b_block: str
) -> tuple[Callable[..., np.ndarray] | None, str, HxFinding | None]:
    """The rung gate (plan "Re-relax only the seam radii"): both parts'
    detected rung (:func:`_rung_of`) picks the relaxer, unless ``forced``
    (the op's own ``"rung"`` key, default ``"auto"``) overrides it.
    ``auto`` with a rung mismatch is ``join.rung`` -- raised here as
    :class:`~precis.errors.BadInput` immediately (like `join.lattice`,
    `port.mismatch`, `seam.mismatch` above it), never deferred into a
    finding list, because there is no sane geometry to hand back: relaxing
    one side's rest lengths against the other's frozen boundary is
    exactly the false-leak hazard the plan's "Risks" section names.
    Forcing ``geo`` onto a stick-rung block is allowed (an explicit ask,
    not a mismatch) but is not free -- ``join.rung`` WARN, rest lengths
    1.42 vs 1.52 A will strain the frozen boundary. Returns
    ``(relaxer, name, finding)`` -- ``relaxer=None`` for ``"stick"`` so
    the caller can pass it straight through to
    :func:`~hexfold.join.compose`'s own ``relax=None`` default (its
    :func:`~hexfold.join._stick_relaxer` construction stays private to
    that module, needing only ``a.sigma`` it already has -- no reason for
    this module to duplicate it)."""
    if forced == "geo":
        chosen = "geo"
    elif forced == "stick":
        chosen = "stick"
    elif rung_a == "geo" and rung_b == "geo":
        chosen = "geo"
    elif rung_a != "geo" and rung_b != "geo":
        chosen = "stick"
    else:
        raise BadInput(
            f"join.rung: {a_block} is on rung {rung_a!r} but {b_block} is on "
            f"rung {rung_b!r} -- relax both blocks on the same rung before "
            "joining (edit(kind='structure', id=..., ops=[{'op':'relax', "
            "'fidelity':'geo'}]) on whichever is behind), or pass an "
            "explicit 'rung': 'stick'|'geo' to force one"
        )
    finding: HxFinding | None = None
    if chosen == "geo" and (rung_a != "geo" or rung_b != "geo"):
        finding = HxFinding(
            "join.rung",
            HxSeverity.WARN,
            f"forcing the geo rung over a stick-rung block ({a_block}="
            f"{rung_a!r}, {b_block}={rung_b!r}) -- rest lengths 1.42 vs "
            "1.52 A will strain the frozen boundary",
            data=(("a_rung", rung_a), ("b_rung", rung_b), ("forced", chosen)),
        )
    if chosen == "geo":
        return geo_relax_pinned, "geo", finding
    return None, "stick", finding


class _JoinStale(Exception):
    """Internal: a rebuilt topology disagrees with what is actually
    stored (module docstring) — caught by :func:`_hexfold_join` and turned
    into a ``join.stale`` :class:`~precis.errors.BadInput`, never left to
    propagate as a raw exception."""


@dataclass
class PendingJoin:
    """A ``join`` op's deferred store write — :class:`~precis_se.atomic.
    generate.PendingGenerate`'s shape, for the same reason (that module's
    docstring's "orphan on partial failure")."""

    block_name: str
    struct_slug: str
    title: str
    scene: StructScene
    card_text: str
    provenance: str
    ports_map: dict[str, str]
    generated: dict[str, Any]


def _resolve_join_endpoint(
    tree: SeTree, raw: Any, *, side: str
) -> tuple[str, str, PortSpec]:
    """``'block.port'`` → ``(block key, port name, port)`` — join's own
    tiny endpoint resolver (rather than reaching into ``ops.py``'s
    module-private ``_resolve_endpoint``, which is wired for ``connect``'s
    ConnectSpec bookkeeping this op has no use for): split on the last
    dot (a block name may itself contain one; ``add_port`` already
    reserves the dot so a port name never does), resolve the block token
    through the tree, then look the port up through
    :func:`~precis_se.ops.effective_ports` so an instance's template ports
    resolve the same way ``connect`` sees them."""
    s = str(raw or "").strip()
    block_token, sep, port_name = s.rpartition(".")
    block_token, port_name = block_token.strip(), port_name.strip()
    if not sep or not block_token or not port_name:
        raise BadInput(f"join {side} must be 'block.port', got {raw!r}")
    key = tree.resolve_key(block_token)
    if key is None:
        roster = ", ".join(sorted(tree.blocks)) or "(none)"
        raise NotFound(
            f"no such block: {block_token!r}. Blocks in this design: {roster}"
        )
    node = tree.blocks[key]
    ports = _effective_ports(tree, node)
    port = ports.get(port_name)
    if port is None:
        roster = ", ".join(sorted(ports)) if ports else "(none)"
        raise NotFound(
            f"no such port on block {key!r}: {port_name!r}. Available ports: {roster}"
        )
    return key, port_name, port


def _join_generated_record(store: Store, node: Any) -> dict[str, Any] | None:
    """``node``'s bound structure's ``meta['generated']`` record, iff
    ``node`` is itself a join composite (``generator == 'join'``) — else
    ``None``. The change-2 discriminator lives here: "is a part of a
    composite" tests THIS, never merely "has a parent" — an ordinary
    assembly block used as a layout parent has no such record, and a
    hexfold block sitting under one stays joinable (module docstring)."""
    if node.bound_kind != "structure" or not node.bound:
        return None
    ref = store.get_ref(kind="structure", id=node.bound)
    if ref is None:
        return None
    generated = (ref.meta or {}).get("generated") or {}
    return generated if generated.get("generator") == "join" else None


def _addressed_part_redirect(
    store: Store, tree: SeTree, block_key: str, port_name: str
) -> tuple[str, str] | None:
    """gr456213 (2026-09-29 prod dogfood): the fix for the case
    :func:`_hexfold_join`'s old, differently-scoped ``join.reparented``
    WARN used to merely flag (that code still exists, narrowed to an INFO
    on the ordinary-layout-parent path only -- see :func:`_hexfold_join`)
    — a block already claimed as a **part** of a composite (per
    :func:`_join_generated_record`) has its remaining free rim ALREADY
    exposed as that composite's own port; addressing the part directly is
    the same physical rim under a second name, not a second, independent
    endpoint. Returns ``(owning composite, corrected port name)`` when
    ``block_key`` is a part (possibly several joins deep — a part two
    levels down needs the FULL accumulated prefix), ``None`` when it
    names no composite's part at all.

    Walks the parent chain one join composite at a time, applying the
    same ``<part>_<port>`` prefixing :func:`_hexfold_join`'s own
    ``side_of``/``compose`` convention stamps onto every exposed
    composite port (never reimplemented here — this is composed forward
    with the identical ``f"{prefix}_{name}"`` shape :func:`~hexfold.join.
    compose` itself uses), so the corrected name is exactly the port the
    owning composite already carries."""
    current, accumulated, owner = block_key, port_name, None
    while True:
        node = tree.blocks.get(current)
        if node is None or node.parent is None:
            break
        parent_key = node.parent
        parent_node = tree.blocks.get(parent_key)
        if parent_node is None:
            break
        generated = _join_generated_record(store, parent_node)
        if generated is None:
            break
        parts = generated.get("parts") or []
        if not any(isinstance(p, dict) and p.get("block") == current for p in parts):
            break
        accumulated = _se_port_name(f"{current}_{accumulated}")
        current, owner = parent_key, parent_key
    return (owner, accumulated) if owner is not None else None


def _hx_port(block: Block, se_name: str) -> HxPort:
    """The rebuilt :class:`~hexfold.join.Block`'s :class:`~hexfold.build.
    Port` matching a stored SE port name — hexfold names ports with a
    dotted path (multi-instance) or a compound composite prefix
    (``<block>_<port>``, ``hexfold.join.compose``'s ``prefix_a``/
    ``prefix_b``); :func:`~precis_se.atomic.generators.hexfold_spec.
    _se_port_name`'s dot→underscore map is the one-way function that
    produced the stored SE name from it, so this inverts by search rather
    than assuming the map is injective (its own docstring: it is not, in
    general — collisions are loud, not silent, at generate time)."""
    for name, p in block.ports.items():
        if _se_port_name(name) == se_name:
            return p
    known = ", ".join(sorted(_se_port_name(n) for n in block.ports)) or "(none)"
    raise BadInput(f"join: no rebuildable port {se_name!r} (available: {known})")


def _rebuild_block(store: Store, struct_slug: str) -> tuple[Block, list[str], Any]:
    """A resolved :class:`~hexfold.join.Block` for ``struct_slug`` —
    topology (bonds/rings/ports) rebuilt from its own generator record,
    coordinates always the STORED atoms (module docstring). Returns
    ``(block, labels, ref)`` — ``labels`` the ordinal-ordered atom labels
    (structure_load's ``id ASC`` contract), ``ref`` the loaded
    :class:`~precis.store.types.Ref` (its ``meta['version']`` feeds the
    join record's ``parts`` provenance)."""
    ref = store.get_ref(kind="structure", id=struct_slug)
    if ref is None:
        raise _JoinStale(f"structure design {struct_slug!r} does not exist")
    scene, _handles = store.structure_load(ref.id)
    labels = list(scene.atoms)
    elements = [scene.atoms[lbl].element for lbl in labels]
    coords = np.array(
        [scene.cell.frac_to_cart(scene.atoms[lbl].frac) for lbl in labels],
        dtype=np.float64,
    )
    generated = (ref.meta or {}).get("generated") or {}
    gen_name = generated.get("generator")
    if gen_name == "hexfold":
        spec = generated.get("spec")
        if not isinstance(spec, str) or not spec.strip():
            raise _JoinStale(f"{struct_slug!r} carries no regeneration spec")
        try:
            net = build(spec, strict=True)
        except Exception as exc:  # BuildError/HexfoldError and friends
            raise _JoinStale(
                f"{struct_slug!r}'s spec no longer builds cleanly: {exc}"
            ) from exc
        net_elements = [a.element for a in net.atoms]
        if net_elements != elements:
            raise _JoinStale(
                f"{struct_slug!r}: stored {len(elements)} atom(s) but its "
                f"spec rebuilds {len(net_elements)} (or a different element "
                "sequence) — the block was edited since it was generated"
            )
        block = block_from_net(net, coords)
    elif gen_name == "join":
        parts = generated.get("parts")
        seam = generated.get("seam")
        if not isinstance(parts, list) or len(parts) != 2 or not isinstance(seam, dict):
            raise _JoinStale(f"{struct_slug!r}: malformed join record")
        blk_a, _labels_a, _ref_a = _rebuild_block(store, parts[0]["structure"])
        blk_b, _labels_b, _ref_b = _rebuild_block(store, parts[1]["structure"])
        try:
            pa = blk_a.ports[parts[0]["port"]]
            pb = blk_b.ports[parts[1]["port"]]
        except KeyError as exc:
            raise _JoinStale(
                f"{struct_slug!r}: its recorded seam port {exc} is gone from "
                "the rebuilt part"
            ) from exc
        seam_radius = generated.get("seam_radius") or seam.get("radius")
        # No `catalogue=` here, deliberately: this is the REPLAY of a join
        # that already happened, and its job is to reproduce the recorded
        # geometry exactly (the element-count check below raises
        # `join.stale` when it does not). The catalogue is mutable shared
        # state, so consulting it would make a replay's result depend on
        # what someone measured since — turning every warm-up into a
        # spurious `join.stale` across unrelated designs. The recorded
        # `seam_radius` above is the authority for a replay.
        composite = compose(
            blk_a,
            pa,
            blk_b,
            pb,
            int(seam["k"]),
            seam_radius=seam_radius,
            prefix_a=str(parts[0]["block"]),
            prefix_b=str(parts[1]["block"]),
        )
        if any(f.severity == HxSeverity.ERROR for f in composite.findings):
            raise _JoinStale(f"{struct_slug!r}: its recorded join no longer composes")
        if list(composite.elements) != elements:
            raise _JoinStale(
                f"{struct_slug!r}: stored {len(elements)} atom(s) but "
                f"replaying its recorded join recomposes "
                f"{len(composite.elements)} — the block was edited since "
                "it was joined"
            )
        block = Block(
            elements=tuple(elements),
            coords=coords,
            bonds=composite.bonds,
            rings=composite.rings,
            ports=composite.ports,
            sigma=blk_a.sigma,
        )
    else:
        raise _JoinStale(
            f"{struct_slug!r}: generator {gen_name!r} is not rebuildable by "
            "join (only hexfold- and join-origin blocks are)"
        )
    return block, labels, ref


def _is_identity_pose(vec: list[float]) -> bool:
    """``True`` for an empty (never-authored, :class:`~precis_se.ops.
    SeBlock.local_pose`'s own default) or all-zero pose/rot triple."""
    return not vec or all(x == 0.0 for x in vec)


def _composite_envelope(coords: np.ndarray) -> str:
    """Bounding cylinder about the composite frame's own z axis (design
    call 1: the composite frame IS ``a``'s frame, unmodified) — the same
    ``cyl:r<>h<>`` shape :mod:`~precis_se.atomic.generators.hexfold_spec`'s
    ``_canonical_frame`` emits, but never re-framing the atoms: only the
    bound grows to cover whatever ``b`` added. ``h``'s lower bound stays 0
    (``a``'s own frame convention: z in ``[0, h]``) even if a stray atom
    sits slightly below it — ``envelope_fit`` never blocks on a
    protrusion, only warns, and every slice-1 join fuses onto the
    outward-facing end, so z never actually goes negative in practice."""
    if coords.size == 0:
        return f"cyl:r{fmt_length_A(VDW_MARGIN_A)}h{fmt_length_A(VDW_MARGIN_A)}"
    radial = np.linalg.norm(coords[:, :2], axis=1)
    r = float(radial.max()) + VDW_MARGIN_A
    h = float(max(float(coords[:, 2].max()), 0.0)) + VDW_MARGIN_A
    return f"cyl:r{fmt_length_A(r)}h{fmt_length_A(h)}"


def _hexfold_join(
    store: Store,
    tree: SeTree,
    op: dict[str, Any],
    design_slug: str,
    a_lattice: str,
    b_lattice: str,
    a_block: str,
    a_port_name: str,
    b_block: str,
    b_port_name: str,
) -> tuple[str, PendingJoin | None]:
    """The ``("sp2-hex", "sp2-hex")`` :data:`JOINERS` entry — everything
    past lattice-pair routing (:func:`prepare_join`'s docstring). Both
    lattices are handed through (rather than the one shared value the old
    single-lattice keying implied) so a future heterojunction entry can
    tell its two sides apart without this function changing shape again;
    this slice's only registered pair still has ``a_lattice == b_lattice``
    by construction."""
    block_name = op.get("name")
    if not block_name or not str(block_name).strip():
        raise BadInput("join needs 'name' (the new composite block's name)")
    block_name = str(block_name).strip()
    if block_name in tree.blocks:
        raise BadInput(
            f"duplicate block name: {block_name!r} (names are unique per design)"
        )

    node_a, node_b = tree.blocks[a_block], tree.blocks[b_block]

    def _unbound(blk: str) -> BadInput:
        """gr459057: the overwhelmingly common way to reach this is a
        single ops list holding both the ``generate`` and the ``join`` —
        :func:`~precis_se.atomic.generate.finish_generate` mints the
        structure only after the whole list validates (so a partial
        failure cannot orphan one), so no join in that same list can ever
        see its endpoints bound. The bare state sentence sent readers to
        inspect the generate, which is correct and succeeded. Detecting
        the case exactly would mean threading the pending-generate set in
        here; naming it as the likely cause costs nothing and is what the
        reader needs either way."""
        return BadInput(
            f"join: block {blk!r} is not bound to a structure design. "
            "If you generated it in this same ops list, that is why: the "
            "structure is not minted until the whole list validates, so "
            "put the generate and the join in separate calls. Otherwise "
            "the block was never generated or bound — a join needs both "
            "endpoints bound to real chemistry."
        )

    # Two explicit checks rather than a loop: each one narrows its own
    # `node.bound` to `str` for the rebuild below.
    if node_a.bound_kind != "structure" or not node_a.bound:
        raise _unbound(a_block)
    if node_b.bound_kind != "structure" or not node_b.bound:
        raise _unbound(b_block)

    # Design call 2 (composite takes over a's pre-join placement, net
    # effect: a's world placement is bit-for-bit unchanged by a join):
    # captured before anything mutates the tree. b's own pre-join pose is
    # captured too, only to know whether it is about to be silently
    # dropped (b is placed by the seam transform below, never by
    # whatever pose it happened to carry beforehand).
    old_a_parent = node_a.parent
    old_a_local_pose = list(node_a.local_pose) if node_a.local_pose else [0.0, 0.0, 0.0]
    old_a_local_rot = list(node_a.local_rot) if node_a.local_rot else [0.0, 0.0, 0.0]
    # b's own pre-join parent, captured for the same reason as its pose:
    # only to know whether it is about to be silently discarded. a's
    # parent survives a join (the composite inherits it, `add_op` below);
    # b's does not, and nothing else records it.
    old_b_parent = node_b.parent
    b_pose_dropped = not _is_identity_pose(
        list(node_b.local_pose)
    ) or not _is_identity_pose(list(node_b.local_rot))

    try:
        blk_a, labels_a, ref_a = _rebuild_block(store, node_a.bound)
        blk_b, labels_b, ref_b = _rebuild_block(store, node_b.bound)
    except _JoinStale as exc:
        raise BadInput(f"join.stale: {exc} — regenerate the block(s) first") from exc

    pa = _hx_port(blk_a, a_port_name)
    pb = _hx_port(blk_b, b_port_name)

    seam_raw = str(op.get("seam", "auto")).strip().lower()
    if seam_raw not in ("auto", "fuse", "adapter"):
        raise BadInput(
            f"join: 'seam' must be auto|fuse|adapter, got {op.get('seam')!r}"
        )
    ta, tb = pa.rim_type, pb.rim_type
    motif = (
        "adapter" if (ta is not None and tb is not None and ta[0] != tb[0]) else "fuse"
    )
    if seam_raw != "auto" and seam_raw != motif:
        raise BadInput(
            f"seam.mismatch: seam={seam_raw!r} requested but "
            f"{a_block}.{a_port_name} ({ta}) onto {b_block}.{b_port_name} "
            f"({tb}) is a {motif!r} join"
        )

    extra_findings: list[HxFinding] = []
    k_raw = op.get("k", 0)
    if k_raw == "fit":
        ranked = rank_k(blk_a, pa, blk_b, pb)
        if not ranked:
            raise BadInput("join: k='fit' has no candidates (an empty port)")
        k = int(ranked[0][0])
        extra_findings.append(_fit_alternatives_finding("k", "", None, k, ranked[1:]))
    else:
        try:
            k = int(k_raw)
        except (TypeError, ValueError) as exc:
            raise BadInput(f"join: 'k' must be an int or 'fit', got {k_raw!r}") from exc

    seam_radius_raw = op.get("seam_radius")
    seam_radius: dict[str, int] | None = None
    if seam_radius_raw is not None:
        if not isinstance(seam_radius_raw, dict):
            raise BadInput("join: 'seam_radius' must be a JSON object {'a'|'b': int}")
        seam_radius = {}
        for side, value in seam_radius_raw.items():
            if side not in ("a", "b"):
                raise BadInput(
                    f"join: 'seam_radius' keys must be 'a'/'b', got {side!r}"
                )
            try:
                seam_radius[side] = int(value)
            except (TypeError, ValueError) as exc:
                raise BadInput(f"join: seam_radius[{side!r}] must be an int") from exc

    rung_raw = str(op.get("rung", "auto")).strip().lower()
    if rung_raw not in ("auto", "stick", "geo"):
        raise BadInput(f"join: 'rung' must be auto|stick|geo, got {op.get('rung')!r}")
    rung_a, rung_b = _rung_of(ref_a), _rung_of(ref_b)
    relaxer, relaxer_name, rung_finding = _select_relaxer(
        rung_a, rung_b, rung_raw, blk_a.sigma, a_block, b_block
    )
    if rung_finding is not None:
        extra_findings.append(rung_finding)
    leak_thresholds = LEAK_THRESH_GEO if relaxer_name == "geo" else None

    # The catalogue read (step 6 slice 2). `for_store` seeds the pinned
    # wildcards if they are not already in `se_hexfold_catalogue` and is
    # idempotent, so it is safe per join; `compose` is read-only on it.
    # Today this is behaviour-neutral by construction: `seed_rows`
    # restates `join.SEAM_RADIUS`/`_LEAK_THRESH` (a hexfold test pins the
    # two equal), and the store withholds `source="measured"` rows from
    # `resolve_edge` under the step 6 slice 1 ruling, so every lookup
    # returns the same number the module constant would have. It becomes
    # load-bearing only when a row is trusted to differ.
    # `rung` must go with it: the catalogue is keyed by rung, so a geo
    # join looking up stick rows would read the wrong decay length.
    catalogue = for_store(store)

    composite = compose(
        blk_a,
        pa,
        blk_b,
        pb,
        k,
        seam_radius=seam_radius,
        leak_thresholds=leak_thresholds,
        relax=relaxer,
        prefix_a=a_block,
        prefix_b=b_block,
        rung=relaxer_name,
        catalogue=catalogue,
    )
    all_findings = list(extra_findings) + list(composite.findings)
    if any(f.severity == HxSeverity.ERROR for f in all_findings):
        report_text = HxReport(tuple(all_findings)).sorted().render(verbose=True)
        raise BadInput(f"join: {report_text}")
    if b_pose_dropped:
        extra_findings.append(
            HxFinding(
                "join.pose_dropped",
                HxSeverity.INFO,
                f"{b_block}'s pre-join pose/rot is dropped — a join places "
                "b by the seam transform, never by whatever pose it "
                "carried before",
                data=(("block", b_block),),
            )
        )
    if old_b_parent is not None:
        extra_findings.append(
            HxFinding(
                "join.reparented",
                HxSeverity.INFO,
                f"{b_block} was authored under {old_b_parent!r} and moves "
                f"into the composite — a join owns both endpoints, so b's "
                f"authored parent is discarded (a's is inherited by the "
                f"composite instead)",
                data=(("block", b_block), ("old_parent", old_b_parent)),
            )
        )

    struct_slug = f"{design_slug}-{block_name}"
    if store.get_ref(kind="structure", id=struct_slug) is not None:
        raise BadInput(
            f"join: a structure design already exists at {struct_slug!r} — "
            f"pick a different 'name', or delete(kind='structure', "
            f"id={struct_slug!r}) first"
        )

    R, t = composite.transform
    n_a = len(blk_a.elements)

    scene = StructScene(cell=generated_cell(composite.coords))
    new_labels: list[str] = []
    for element, cart in zip(composite.elements, composite.coords, strict=True):
        label = scene.next_label(element)
        frac = scene.cell.wrap(scene.cell.cart_to_frac(np.asarray(cart, dtype=float)))
        scene.atoms[label] = StructAtom(
            label=label, element=element, frac=frac, hybridization="sp2"
        )
        new_labels.append(label)
    for i, j, _order in composite.bonds:
        scene.bonds.append(
            StructBond(
                i=new_labels[i], j=new_labels[j], order=_SP2_BOND_ORDER, kind="aromatic"
            )
        )

    ordinal_a = {lbl: i for i, lbl in enumerate(labels_a)}
    ordinal_b = {lbl: i for i, lbl in enumerate(labels_b)}

    def _relabel(side: str, old_labels: list[Any]) -> list[str]:
        table = ordinal_a if side == "a" else ordinal_b
        offset = 0 if side == "a" else n_a
        return [new_labels[table[old] + offset] for old in old_labels]

    # composite.ports' keys are already prefixed by the REAL block names
    # (``compose``'s ``prefix_a``/``prefix_b``) — an independent lookup off
    # blk_a/blk_b's own port tables (rather than re-parsing the composite
    # key's prefix, which cannot generally be split back out unambiguously)
    # tells which side and original hx name each one is.
    side_of: dict[str, tuple[str, str]] = {}
    for name in blk_a.ports:
        if name != pa.name:
            side_of[f"{a_block}_{name}"] = ("a", name)
    for name in blk_b.ports:
        if name != pb.name:
            side_of[f"{b_block}_{name}"] = ("b", name)

    port_ops: list[dict[str, Any]] = []
    ports_map: dict[str, str] = {}
    for hx_key, p in composite.ports.items():
        side, hx_bare = side_of[hx_key]
        se_bare = _se_port_name(hx_bare)
        se_name = _se_port_name(hx_key)
        orig_node = node_a if side == "a" else node_b
        orig_spec = orig_node.ports.get(se_bare)
        direction = (
            [float(x) for x in orig_spec.direction]
            if orig_spec is not None and orig_spec.direction is not None
            else None
        )
        if side == "b" and direction is not None:
            direction = [float(x) for x in (R @ np.array(direction))]
        roles = (
            list(orig_spec.roles)
            if orig_spec is not None and orig_spec.roles
            else ["covalent"]
        )
        annotations: dict[str, Any] | None = None
        if orig_spec is not None and orig_spec.annotations:
            annotations = dict(orig_spec.annotations)
            old_atoms = annotations.get("atoms")
            if isinstance(old_atoms, list) and old_atoms:
                annotations["atoms"] = _relabel(side, old_atoms)
        # ``p.atoms``/``p.dangling`` on a composite port are already
        # COMPOSITE ordinals -- ``compose`` offsets side "b"'s by ``n_a``
        # when it builds ``composite.ports`` (side "a"'s are untouched,
        # already 0..n_a-1) -- so this indexes ``new_labels`` directly,
        # unlike ``annotations["atoms"]`` below (real labels from the
        # ORIGINAL block's own ref, which do need ``_relabel``).
        ring = list(p.dangling) if p.dangling else list(p.atoms)
        rep_label = new_labels[ring[0]] if ring else None
        port_op: dict[str, Any] = {
            "op": "add_port",
            "block": block_name,
            "name": se_name,
            "roles": roles,
            "direction": direction,
            "expected_element": orig_spec.expected_element
            if orig_spec is not None
            else None,
        }
        if annotations is not None:
            port_op["annotations"] = annotations
        port_ops.append(port_op)
        if rep_label is not None:
            ports_map[se_name] = rep_label

    envelope_A = _composite_envelope(composite.coords)
    # Design call 2: the composite takes over a's pre-join pose AND parent
    # frame — an explicit op 'parent' overrides only the parent, keeping
    # a's authored pose numbers as the composite's own (a caller that asks
    # to re-home the composite gets exactly that, not a world-preserving
    # transform on top of it; see the module docstring's deferred-item
    # note this closes). a itself then goes to identity under the
    # composite (below) — net effect: a's world placement is bit-for-bit
    # unchanged by a join.
    add_op: dict[str, Any] = {
        "op": "add_block",
        "name": block_name,
        "envelope": ingest_envelope(envelope_A),
        "desc": f"join: {a_block}.{a_port_name} --{motif} k={k}--> {b_block}.{b_port_name}",
        "parent": op["parent"] if op.get("parent") is not None else old_a_parent,
        "pose": old_a_local_pose,
        "rot": old_a_local_rot,
    }

    try:
        apply_ops(tree, [add_op])
        apply_ops(tree, [{"op": "set_mode", "block": block_name, "mode": "atomic"}])
        for port_op in port_ops:
            apply_ops(tree, [port_op])
        t_m = [A_to_m(float(x)) for x in t]
        rot = [float(x) for x in euler_rad_from_matrix(R)]
        apply_ops(tree, [{"op": "set_pose", "block": b_block, "pose": t_m, "rot": rot}])
        apply_ops(
            tree,
            [
                {
                    "op": "set_pose",
                    "block": a_block,
                    "pose": [0.0, 0.0, 0.0],
                    "rot": [0.0, 0.0, 0.0],
                }
            ],
        )
        apply_ops(
            tree,
            [
                {
                    "op": "connect",
                    "a": f"{a_block}.{a_port_name}",
                    "b": f"{b_block}.{b_port_name}",
                    "kind": "bond",
                }
            ],
        )
    except OpError as exc:
        raise BadInput(str(exc)) from exc

    # a/b re-parented under the composite (Contract "Composite
    # materialisation"): no dedicated reparent op exists (nothing else in
    # se moves a block between parents post-hoc), so this sets the field
    # directly and recomposes every block's world pose in one pass —
    # local_pose/local_rot (the byte-exact save target) are untouched by a
    # parent change, only what they compose AGAINST changes, exactly what
    # compose_world_pose recomputes.
    # gr456213 (2026-09-29 prod dogfood) originally WARNed here
    # (``join.reparented``) when an endpoint was already parented under a
    # DIFFERENT composite, rather than refusing — reparenting it would
    # silently pull it out of that composite's block tree while its own
    # build record (``generated.parts``) and the port it still exposes
    # (e.g. ``<part>_<port>``) kept naming/claiming the very same physical
    # rim. The user has since ruled that a part may NOT belong to two
    # composites at once — :func:`prepare_join` now refuses this earlier,
    # as ``join.part_addressed``, before either side is even rebuilt, and
    # redirects the caller to the owning composite's own already-exposed
    # port instead of accepting the silent reparent. Every endpoint this
    # function still sees has therefore already cleared that gate — its
    # own pre-join parent is either ``None`` (the ordinary chained-join
    # case: a fresh, never-parented block on every join) or an ORDINARY,
    # non-composite parent (a layout assembly block), which carries no
    # build record to strand. That ordinary case is not a defect, but it
    # does discard authored intent for ``b`` (``a``'s parent is inherited
    # by the composite above; ``b``'s is dropped outright), so it is
    # reported as a ``join.reparented`` INFO — user ruling 2026-09-29,
    # the same "say what was silently discarded" the neighbouring
    # ``join.pose_dropped`` INFO exists for.
    node_a.parent = block_name
    node_b.parent = block_name
    compose_world_pose(tree)

    parts = [
        {
            "block": a_block,
            "structure": node_a.bound,
            "version": int((ref_a.meta or {}).get("version", 1)),
            "port": pa.name,
            "n_atoms": len(blk_a.elements),
            "frame": [np.eye(3).tolist(), [0.0, 0.0, 0.0]],
        },
        {
            "block": b_block,
            "structure": node_b.bound,
            "version": int((ref_b.meta or {}).get("version", 1)),
            "port": pb.name,
            "n_atoms": len(blk_b.elements),
            "frame": [R.tolist(), t.tolist()],
        },
    ]
    report = (
        HxReport(tuple(extra_findings) + tuple(composite.findings)).sorted().to_dict()
    )
    generated_record: dict[str, Any] = {
        "generator": "join",
        "lattice": a_lattice,
        "parts": parts,
        "seam": composite.seam,
        "seam_radius": composite.seam.get("radius"),
        "relaxer": relaxer_name,
        "report": report,
        "n_atoms": len(composite.elements),
        "n_bonds": len(composite.bonds),
    }

    title = f"{block_name} (join generator)"
    card_text = (
        f"{title} (atomistic structure). join: {a_block}.{a_port_name} "
        f"--{motif} k={k}--> {b_block}.{b_port_name}. "
        f"{len(composite.elements)} atoms, {len(composite.bonds)} bonds."
    )
    echo = (
        f"joined block {block_name!r} ({motif}, k={k}): "
        f"{len(composite.elements)} atom(s), {len(composite.bonds)} bond(s), "
        f"{len(port_ops)} port(s), bound to structure {struct_slug!r}"
    )
    pending = PendingJoin(
        block_name=block_name,
        struct_slug=struct_slug,
        title=title,
        scene=scene,
        card_text=card_text,
        provenance=card_text,
        ports_map=ports_map,
        generated=generated_record,
    )
    return echo, pending


#: Dispatch by the two ports' ``lattice`` annotations, keyed on the
#: canonical **sorted 2-tuple** of both sides (module docstring) — never a
#: single lattice string: that would hard-code the assumption that a join
#: only ever happens within one lattice, making a heterojunction (sp2
#: carbon onto sp3 diamondoid, or a future DNA joiner) inexpressible by
#: construction. Today only ``("sp2-hex", "sp2-hex")`` (hexfold's own sp2
#: rims, joined to themselves) is wired; a future heterojunction entry
#: registers its own sorted pair here without touching
#: :func:`prepare_join` — this dict stays a plain dict, no plugin
#: discovery or capability negotiation.
JOINERS: dict[
    tuple[str, str],
    Callable[
        [Store, SeTree, dict[str, Any], str, str, str, str, str, str, str],
        tuple[str, PendingJoin | None],
    ],
] = {("sp2-hex", "sp2-hex"): _hexfold_join}


def prepare_join(
    store: Store, tree: SeTree, op: dict[str, Any], design_slug: str
) -> tuple[str, PendingJoin | None]:
    """``{"op": "join", ...}`` (module docstring) — the pure/in-memory half
    of the prepare/finish pair (:mod:`precis_se.atomic.generate`'s module
    docstring's "orphan on partial failure" reasoning applies identically:
    a fresh composite ``structure`` design commits on its own, so its mint
    is deferred to :func:`finish_join`, run only after the whole ops list
    has validated). Resolves both endpoints, refuses an endpoint that
    names a block already claimed as a **part** of another composite
    (``join.part_addressed``, :func:`_addressed_part_redirect` — before
    anything lattice-related even runs, since this is an addressing
    defect, not a lattice one), gates on their ``lattice`` port
    annotations (``join.lattice`` — a distinct message for a missing
    annotation, pointing at regenerating the block, vs. no joiner
    registered for the pair, which also covers a genuine mismatch between
    two present ones), and dispatches to the matching :data:`JOINERS`
    entry."""
    a_raw, b_raw = op.get("a"), op.get("b")
    if not a_raw or not b_raw:
        raise BadInput("join needs 'a' and 'b' (each 'block.port')")
    a_block, a_port_name, a_spec = _resolve_join_endpoint(tree, a_raw, side="'a'")
    b_block, b_port_name, b_spec = _resolve_join_endpoint(tree, b_raw, side="'b'")
    for blk, port_name in ((a_block, a_port_name), (b_block, b_port_name)):
        redirect = _addressed_part_redirect(store, tree, blk, port_name)
        if redirect is not None:
            owner, corrected = redirect
            raise BadInput(
                f"join.part_addressed: {blk!r} is already a part of "
                f"composite {owner!r} -- a part may not belong to two "
                f"composites; its free rim is {owner!r}'s own port now, "
                f"join '{owner}.{corrected}' instead of '{blk}.{port_name}'"
            )
    a_lattice = a_spec.annotations.get("lattice") if a_spec.annotations else None
    b_lattice = b_spec.annotations.get("lattice") if b_spec.annotations else None
    if not a_lattice or not b_lattice:
        # gr456201 (2026-09-29 prod dogfood): "don't share a joinable
        # lattice" told the user nothing about what to do -- the actual,
        # overwhelmingly common cause is a block generated before
        # generate.py started minting the `lattice` annotation (design
        # call: `port.lattice is not None` gate, generate.py:265); every
        # one of prod's 77 active ports had an empty annotation. Name the
        # missing side(s) and point at the one fix that exists:
        # regenerate the block through its own `generate` op. This never
        # writes an annotation itself -- minting one any other way is a
        # write-path decision the user hasn't made.
        missing = [
            f"{blk}.{port}"
            for blk, port, lattice in (
                (a_block, a_port_name, a_lattice),
                (b_block, b_port_name, b_lattice),
            )
            if not lattice
        ]
        verb = "has" if len(missing) == 1 else "have"
        raise BadInput(
            f"join.lattice: {' and '.join(missing)} {verb} no lattice "
            "annotation — annotations are minted only when a block is "
            "generated (precis_se.atomic.generate), so a block generated "
            "before that landed, or a hand-added port, carries none and "
            "can never be joined as-is. Regenerate the block through its "
            "own 'generate' op to mint the annotation, then join again."
        )
    pair = tuple(sorted((a_lattice, b_lattice)))
    joiner = JOINERS.get(pair)
    if joiner is None:
        known = ", ".join(f"{x!r}+{y!r}" for x, y in sorted(JOINERS)) or "(none)"
        raise BadInput(
            f"join.lattice: no joiner registered for {a_block}.{a_port_name} "
            f"({a_lattice!r}) onto {b_block}.{b_port_name} ({b_lattice!r}) "
            f"(known pairs: {known})"
        )
    return joiner(
        store,
        tree,
        op,
        design_slug,
        a_lattice,
        b_lattice,
        a_block,
        a_port_name,
        b_block,
        b_port_name,
    )


def finish_join(store: Store, tree: SeTree, pending: PendingJoin) -> None:
    """The store-touching half — :func:`~precis_se.atomic.generate.
    finish_generate`'s shape exactly (its docstring's residual applies
    unchanged here). Skips entirely if a later op in the same list already
    removed the just-minted composite block."""
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
        meta_extra={"generated": pending.generated},
    )
    node.bound_kind = "structure"
    node.bound = pending.struct_slug
    for port_name, atom_label in pending.ports_map.items():
        p = node.ports.get(port_name)
        if p is None:
            continue
        p.bound_design = pending.struct_slug
        p.bound_atom = atom_label
