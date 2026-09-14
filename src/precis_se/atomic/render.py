"""``view='mechanics'`` / ``view='literature'`` and the atomic
filled-fraction line — the atomic mode's read surface.

Transferred from ``precis_nm.handler`` by the nm→se merge
(docs/backlog/nm-se-merge.md), renderers unchanged in substance. They live
here rather than in :mod:`precis_se.handler` for the reason the whole
``atomic`` subpackage exists: the handler is already ~85 KB and the merge
must not produce one file.

Both views are **store-aware reads** — they hydrate the ``structure``
designs a design's blocks/ports are bound to (``mechanics``) or run the
paper corpus's own search engine (``literature``) — so they take the store
explicitly. The checkers they call stay pure: :mod:`precis_se.atomic.
mechanics` never touches a store, the same "assemble in the view path,
keep the checker pure" split :func:`precis_se.atomic.validate.
validate_atomic` uses.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.format import render_agent_table
from precis.response import Response
from precis.structure import Scene as StructScene
from precis_se.atomic import mechanics as se_mechanics
from precis_se.ops import SeTree, effective_envelope, effective_ports

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.dispatch import Hub
    from precis.store import Store


def _atomic_blocks(tree: SeTree) -> list[Any]:
    """Ordinary (non-instance) blocks that make an atomic claim — either
    ``mode='atomic'`` or a ``structure`` binding. An instance never owns a
    binding of its own (``bind_structure`` rejects it — bind via the
    template), so counting instances too would double-count the same
    underlying fill state."""
    return [
        n
        for n in tree.blocks.values()
        if n.template is None and (n.mode == "atomic" or n.bound_kind == "structure")
    ]


def atomic_fill_line(tree: SeTree) -> str | None:
    """``view='validate'``'s **atomic** filled-fraction line, or ``None``
    for a design that makes no atomic claim at all (the merge must not put
    a chemistry readout on a caster design).

    se's own header line already counts L1 fill (does a block have an
    envelope?); this counts L5 fill — is the chemistry actually there? —
    the notion nm's ``_fill_fraction_line`` carried, transferred. The
    maze.py lesson restated: a fresh, entirely unfilled scaffold has no
    bindings, so it trivially has no ``dangling_binding``/``envelope_fit``
    findings either, and a bare check-mark would misread as "this design
    is done" rather than "this design has not started"."""
    ordinary = _atomic_blocks(tree)
    if not ordinary:
        return None
    bound = [n for n in ordinary if n.bound_kind == "structure" and n.bound]
    line = (
        f"{len(bound)}/{len(ordinary)} atomic block(s) filled (bound to real chemistry)"
    )
    if not bound:
        line += (
            " — UNFILLED scaffold: zero findings below means nothing is "
            "wrong YET, not that this design is done"
        )
    return line


def hydrate_bound_scenes(
    store: Store, tree: SeTree
) -> tuple[dict[str, dict[str, str] | None], dict[str, StructScene]]:
    """Load every ``structure`` design the tree references once, up front —
    the "assemble in the view path" half of the store-free-checker split.

    Returns ``(bound_scenes, bound_full_scenes)``: the first maps every
    referenced slug to its ``{atom_label: element}`` map, or ``None`` for a
    slug that no longer resolves (which is exactly what
    ``dangling_binding`` reports on); the second holds the whole
    :class:`~precis.structure.Scene` for the checks that need real
    Cartesian positions (``envelope_fit``, ``mechanics``) and simply omits
    an unresolvable slug."""
    slugs = {
        n.bound for n in tree.blocks.values() if n.bound_kind == "structure" and n.bound
    }
    slugs |= {
        p.bound_design
        for n in tree.blocks.values()
        for p in n.ports.values()
        if p.bound_design
    }
    bound_scenes: dict[str, dict[str, str] | None] = {}
    bound_full_scenes: dict[str, StructScene] = {}
    for slug in slugs:
        ref = store.get_ref(kind="structure", id=slug)
        if ref is None:
            bound_scenes[slug] = None
            continue
        scene, _handles = store.structure_load(ref.id)
        bound_scenes[slug] = {
            label: atom.element for label, atom in scene.atoms.items()
        }
        bound_full_scenes[slug] = scene
    return bound_scenes, bound_full_scenes


def render_mechanics(store: Store, tree: SeTree) -> str:
    """``view='mechanics'`` — advisory (never-gating) L4 ceilings
    (:mod:`precis_se.atomic.mechanics`'s module docstring): per-block Euler
    buckling (tube-shaped blocks only) + harmonic strain energy, and
    per-bond-connect min-cut tensile ceiling. A block with no bound
    structure renders ``unfilled`` in every numeric column (never ``0`` —
    the filled-fraction honesty rule: an empty scaffold must never read as
    "zero strain, zero risk")."""
    _scene_map, scenes = hydrate_bound_scenes(store, tree)

    lines = ["# atomic mechanics", "", se_mechanics.HONESTY_NOTE, ""]

    lines.append("## per-block (Euler buckling + harmonic strain energy)")
    block_rows: list[dict[str, Any]] = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        # instances are never independently bound — an instance's fill state
        # is its template's.
        bd = node.bound if node.bound_kind == "structure" else None
        block_scene = scenes.get(bd) if bd else None
        if block_scene is None:
            block_rows.append(
                {
                    "block": name,
                    "buckling_ceiling_N": "unfilled",
                    "strain_energy_J": "unfilled",
                    "note": "no bound structure" if not bd else "bound design missing",
                }
            )
            continue
        env = effective_envelope(tree, node)
        geom = se_mechanics.tube_geometry_from_envelope(env)
        buckling = (
            f"{se_mechanics.euler_buckling_ceiling_N(*geom):.4g}"
            if geom
            else "n/a (not a tube envelope)"
        )
        strain_J, n_tri = se_mechanics.harmonic_strain_energy_J(block_scene)
        block_rows.append(
            {
                "block": name,
                "buckling_ceiling_N": buckling,
                "strain_energy_J": f"{strain_J:.4g} ({n_tri} angle(s))",
                "note": "",
            }
        )
    lines.append(
        render_agent_table(
            block_rows,
            schema=["block", "buckling_ceiling_N", "strain_energy_J", "note"],
        )
    )

    lines.append("")
    lines.append("## min-cut tensile ceiling (per bond connect)")
    connect_rows: list[dict[str, Any]] = []
    for c in tree.connects:
        if c.kind != "bond":
            continue
        subject_a, subject_b = f"{c.a_block}.{c.a_port}", f"{c.b_block}.{c.b_port}"
        a_node, b_node = tree.blocks.get(c.a_block), tree.blocks.get(c.b_block)
        a_port = effective_ports(tree, a_node).get(c.a_port) if a_node else None
        b_port = effective_ports(tree, b_node).get(c.b_port) if b_node else None
        row_base = {"a": subject_a, "b": subject_b}
        if (
            a_port is None
            or b_port is None
            or not a_port.bound_design
            or not a_port.bound_atom
            or not b_port.bound_design
            or not b_port.bound_atom
        ):
            connect_rows.append(
                {
                    **row_base,
                    "min_cut_bonds": "unfilled",
                    "tensile_ceiling_N": "unfilled",
                    "note": "one or both ports unbound",
                }
            )
            continue
        if a_port.bound_design != b_port.bound_design:
            # Distinct honest state, not a bare 0 (the unfilled-not-zero
            # rule): "0" reads as "measured and found to be zero tensile
            # capacity", but two ports bound to two SEPARATE structure
            # designs were never fused into one bond graph in the first
            # place — there is no min-cut to compute at all, a different
            # situation from a real, disconnected-but-shared scene (that
            # genuinely reports 0, see the branch below).
            connect_rows.append(
                {
                    **row_base,
                    "min_cut_bonds": "not fused",
                    "tensile_ceiling_N": "not fused",
                    "note": (
                        "ports bound to different structure designs — "
                        "never fused into one bond graph, not measured "
                        "as zero"
                    ),
                }
            )
            continue
        connect_scene = scenes.get(a_port.bound_design)
        if connect_scene is None:
            connect_rows.append(
                {
                    **row_base,
                    "min_cut_bonds": "unfilled",
                    "tensile_ceiling_N": "unfilled",
                    "note": (
                        f"bound design {a_port.bound_design!r} no longer resolves"
                    ),
                }
            )
            continue
        cut, ceiling = se_mechanics.min_cut(
            connect_scene, a_port.bound_atom, b_port.bound_atom
        )
        note = "" if cut else "no bond path between the two ports (disconnected)"
        connect_rows.append(
            {
                **row_base,
                "min_cut_bonds": cut,
                "tensile_ceiling_N": f"{ceiling:.4g}",
                "note": note,
            }
        )
    if connect_rows:
        lines.append(
            render_agent_table(
                connect_rows,
                schema=["a", "b", "min_cut_bonds", "tensile_ceiling_N", "note"],
            )
        )
    else:
        lines.append("(no bond connects declared)")
    return "\n".join(lines)


# ── literature (the ``structure`` precedent,
# ``handlers/structure.py::_literature_query``/``_render_literature``,
# transferred through nm) ───────────────────────────────────────────────


def literature_query(
    store: Store, tree: SeTree, ref: Any, *, block_name: str | None = None
) -> str:
    """Deterministic paper-search query built from the design's own
    material — **no LLM step**, the ``structure`` precedent's own opening
    line restated for the block/port/connect domain. Assembled, in order:

    1. the design's own ``description`` (``ref.meta``), when set;
    2. the target block's ``name`` + ``desc``/``use`` text — every block's,
       with no ``block_name`` (the whole-design query, since proposals are
       per-block but a caller may still want the whole design's
       literature);
    3. the objective vocabulary on every connect touching a target block
       (``ConnectSpec.objectives``'s values — string values verbatim, any
       other value's *key* as a fallback token);
    4. when a target block is already bound to a ``structure`` design, that
       design's element composition — an UNBOUND sibling block's chemistry
       never leaks in, since proposals target one block's own gap, not the
       whole assembly's.

    Falls back to the bare target block name(s) when 1-4 together produce
    nothing (never an empty query, the ``structure`` precedent's own
    last-resort rule). Same tree + ref meta + bound compositions ⇒ same
    string (blocks/connects/objectives/composition all walked in a fixed
    sort order)."""
    targets = [block_name] if block_name is not None else sorted(tree.blocks)

    parts: list[str] = []
    desc = str((ref.meta or {}).get("description") or "").strip()
    if desc:
        parts.append(desc)

    block_bits: list[str] = []
    for name in targets:
        node = tree.blocks.get(name)
        if node is None:
            continue
        block_bits.append(name)
        if node.descr:
            block_bits.append(node.descr)
        if node.use:
            block_bits.append(node.use)
    if block_bits:
        parts.append(" ".join(block_bits))

    target_set = set(targets)
    obj_words: set[str] = set()
    for c in sorted(
        tree.connects, key=lambda c: (c.a_block, c.a_port, c.b_block, c.b_port)
    ):
        if c.a_block not in target_set and c.b_block not in target_set:
            continue
        for key, value in sorted((c.objectives or {}).items()):
            obj_words.add(str(value) if isinstance(value, str) else str(key))
    if obj_words:
        parts.append(" ".join(sorted(obj_words)))

    comp_bits: list[str] = []
    for name in targets:
        node = tree.blocks.get(name)
        if node is None or node.bound_kind != "structure" or not node.bound:
            continue
        struct_ref = store.get_ref(kind="structure", id=node.bound)
        if struct_ref is None:
            continue
        scene, _handles = store.structure_load(struct_ref.id)
        comp = scene.composition()
        if comp and " ".join(sorted(comp)) not in comp_bits:
            comp_bits.append(" ".join(sorted(comp)))
    if comp_bits:
        parts.append(" ".join(comp_bits))

    if not parts:
        parts.append(" ".join(targets) or "atomic block")
    return " — ".join(p for p in parts if p)


def render_literature(
    hub: Hub, store: Store, tree: SeTree, ref: Any, *, block_name: str | None
) -> Response:
    """``view='literature'`` — run the deterministic query above against
    the paper corpus via ``PaperHandler.search_hits``, the same fused
    block-search engine ``kind='paper'`` search uses, called in-process
    (the ``structure`` precedent, verbatim). Returns both the generated
    query (so the caller can see/refine it — proposals are per-block, small
    blast radius, so seeing exactly what ran matters) and the ranked hits.
    Pure read: no writes, no LLM, no network beyond the existing
    paper-search path."""
    from precis.handlers.paper import PaperHandler

    query = literature_query(store, tree, ref, block_name=block_name)
    subject = f"block {block_name!r} of design" if block_name else "design"
    hits = PaperHandler(hub=hub).search_hits(q=query, page_size=10)
    head = f"# literature query for {subject} {ref.slug}:\n> {query}"
    if not hits:
        return Response(
            body=f"{head}\n\nno matching papers\n\n"
            "Next: enrich the block's desc=/use= (or the design's "
            "description=) for a sharper query, or "
            f"search(kind='paper', q={query!r}) by hand for a wider net."
        )
    rows = [
        {
            "handle": hit.uhandle
            or (f"paper:{hit.slug}" if hit.slug else str(hit.ref_id)),
            "title": hit.title,
            "score": f"{hit.score:.3f}",
        }
        for hit in hits
    ]
    return Response(
        body=f"{head}\n\n"
        + render_agent_table(rows, schema=["handle", "title", "score"])
    )
