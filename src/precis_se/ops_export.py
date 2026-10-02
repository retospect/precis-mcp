"""``get(kind='se', view='ops')`` — a design back out as the typed ops
list that would author it again (gripe 457931).

Every other ``se`` view renders the design for a *reader*: tree, ports,
topology, bom, drc. None of them round-trips, because the op vocabulary
(:data:`precis_se.ops._OPS`) is register-only — each entry is a forward
``apply`` that mutates an :class:`~precis_se.ops.SeTree`, and nothing
anywhere builds an op dict back out of a live tree. So "copy this design
into another database" meant a hand-written cross-table row transfer, and
the concrete cost was that the 3D viewer's canvas pixel-diff could only
ever run against local fixtures, never against a production design
(docs/backlog/threads/se-3d-viewer.md).

**This is a FINAL-STATE export, not a history.** It emits the ops that
reconstruct the design as it stands, so no destructive op
(``remove_block``, ``disconnect``, ``clear_*``) ever appears: a block that
was added and later removed is simply absent. Re-``put``-ing the result
yields a tree equal to this one, which is the property
``tests/test_se_ops_export.py`` asserts directly over
:func:`precis_se.persist.tree_to_json`.

What the round-trip deliberately does NOT carry, because a silent partial
copy is worse than a stated one (:data:`NOT_CARRIED` is the same list the
rendered header prints):

* **uids.** ``se_blocks.uid`` is explicitly stable, and stored
  cross-references (``design_states``, the shared state/transition tables,
  anything outside the tree that points at a block) key off it. A re-``put``
  elsewhere mints fresh ones, so those references must be re-resolved by
  label. This is a property of the identity design, not a gap in the view.
* **Derived facets.** ``SeBlock.derived`` (catalog-recomputed on every
  load), a domain's ``loop_curve`` (``relax_chain``'s settled output, which
  ``add_domain`` does not accept by contract), and composed world
  ``pose``/``rot`` — the export writes the AUTHORED ``local_pose``/
  ``local_rot``, which is what the pose ops take.
* **States and transitions.** They live in the shared ``design_states``/
  ``design_transitions`` tables keyed by ``block_uid``, not on the tree
  (:class:`~precis_se.ops.SeBlock`'s ``pending_*`` docstrings), so a
  tree-only walk cannot see them.
* **Facet origin stamps.** ``SeBlock.origins`` records ``user`` vs
  ``proposed`` per facet and no op accepts it, so a re-``put`` stamps
  everything ``user``.
* **Note timestamps.** ``NoteSpec.created_at`` is stamped by persist on
  first save and ``add_note`` does not take it, so a copied interrogation
  ledger carries the date the COPY was written.
* **Structure bindings on ports.** ``bound_design``/``bound_atom``/
  ``axis_atom``/``phase_atom`` are written by the store-aware
  ``bind_structure`` op, which the handler intercepts before ``apply_ops``
  sees it; reproducing them needs the target database to hold the same
  ``structure`` design.
* **Measurand ids.** A measure written with ``measurand=`` exports its
  taxon *slug*; replay re-resolves it, so a copy into another database
  gets that database's node (or a loud refusal when the slug is unknown or
  ambiguous there).
* **Pocket removals.** Like every ``remove_*``, ``remove_pocket`` never
  appears; a pocket's region measures are ordinary measures and replay
  as such (:mod:`precis_se.pockets` — membership is derived from
  ``datum``).
"""

from __future__ import annotations

import json
from typing import Any

from precis_se.ops import SeTree

#: The stated gaps, in the order the rendered header prints them. Kept
#: beside the docstring that explains each rather than inlined into the
#: renderer, so the prose and the printed list cannot drift.
NOT_CARRIED: tuple[str, ...] = (
    "block uids — a re-put mints fresh ones; re-resolve stored "
    "cross-references by label",
    "states/transitions — shared design_states tables, keyed by uid, not on the tree",
    "facet origin stamps (user/proposed) — no op accepts them",
    "port structure bindings — the store-aware bind_structure op, needs the "
    "same structure design present",
    "derived values (catalog facets, settled loop curves, composed world "
    "pose) — recomputed on load",
    "layout_chain segment children — a segment has no declare op of its "
    "own; re-run layout_chain after the replay",
    "note timestamps — persist stamps created_at on first save, so a "
    "copied ledger is dated when the copy was written, not the original",
    "measurand taxon ids — a measure carries its measurand's slug, re-resolved "
    "on replay; the target database needs the same measurand nodes",
    "remove_pocket — final state only; a removed pocket's region measures "
    "stay as ordinary measures (remove_pocket never drops them)",
)


def _live(value: Any) -> bool:
    """Is this worth emitting? Drops ``None`` and empty containers, and
    nothing else — ``0``, ``0.0`` and ``False`` are all real answers in
    this vocabulary (a domain's ``start``, a ``loop_before_nt`` of zero, a
    reverse ``forward``), so the usual falsy test would eat them."""
    if value is None:
        return False
    return not (isinstance(value, list | dict | str) and len(value) == 0)


def _op(op_name: str, /, **fields: Any) -> dict[str, Any]:
    """Positional-only on purpose: ``name`` is itself an op field (every
    block op carries one), so a keyword parameter here would collide."""
    return {"op": op_name, **{k: v for k, v in fields.items() if _live(v)}}


def _vec(value: list[float] | None) -> list[float] | None:
    """An all-zero pose/rot is the op default — emit nothing rather than
    a line of zeros on every block."""
    if not value or not any(value):
        return None
    return [float(x) for x in value]


def _block_order(tree: SeTree) -> list[str]:
    """Blocks in an order ``apply_ops`` can replay: a block's ``parent``
    and ``template`` must both already exist when its op runs. A
    cross-design template (``'slug#block'``) is not a local dependency and
    is skipped."""
    out: list[str] = []
    seen: set[str] = set()

    def visit(name: str) -> None:
        if name in seen:
            return
        seen.add(name)
        node = tree.blocks.get(name)
        if node is None:
            return
        for dep in (node.parent, node.template):
            if dep is not None and dep in tree.blocks:
                visit(dep)
        out.append(name)

    for name in tree.blocks:
        visit(name)
    return out


def _block_ops(tree: SeTree) -> list[dict[str, Any]]:
    """One minting op per block. An instance/array takes only
    ``template``/``parent``/pose — ``_instance_shared`` REJECTS envelope,
    desc and use rather than dropping them, because an instance resolves
    those from its template at read time."""
    ops: list[dict[str, Any]] = []
    for name in _block_order(tree):
        node = tree.blocks[name]
        pose, rot = _vec(node.local_pose), _vec(node.local_rot)
        if node.template is not None:
            fields: dict[str, Any] = {
                "name": node.name,
                "template": node.template,
                "parent": node.parent,
                "pose": pose,
                "rot": rot,
            }
            if node.array:
                fields[str(node.array["kind"])] = {
                    k: v for k, v in node.array.items() if k != "kind"
                }
                ops.append(_op("array_block", **fields))
            else:
                ops.append(_op("instance_block", **fields))
            continue
        ops.append(
            _op(
                "add_block",
                name=node.name,
                parent=node.parent,
                pose=pose,
                rot=rot,
                envelope=node.envelope,
                desc=node.descr,
                use=node.use,
                dof=node.dof,
            )
        )
    return ops


def _port_ops(tree: SeTree) -> list[dict[str, Any]]:
    """Ports, after every block exists. Only an ordinary block ever holds
    its own — an instance's resolve from its template
    (:func:`precis_se.ops.effective_ports`).

    ``pose_source``/``rot_source`` are NOT emitted: ``add_port`` does not
    accept them, it derives each from whether the matching field was given
    (the provenance invariant in :class:`~precis.blocktree.types.Port`'s
    docstring), so a replay re-stamps them rather than copying them.
    """
    ops: list[dict[str, Any]] = []
    for name in _block_order(tree):
        node = tree.blocks[name]
        if node.template is not None:
            continue
        for port in node.ports.values():
            ops.append(
                _op(
                    "add_port",
                    block=node.name,
                    name=port.name,
                    roles=list(port.roles),
                    direction=port.direction,
                    annotations=dict(port.annotations),
                    pose=port.pose,
                    rot=port.rot,
                    expected_element=port.expected_element,
                    expected_hybridization=port.expected_hybridization,
                )
            )
    return ops


def _connect_ops(tree: SeTree) -> list[dict[str, Any]]:
    """``connect`` carries the structural claim (``joint``) and the atomic
    one (``kind``) because both are fields on the same row; the optical
    claim is a second op on the same pair — unlike the other two it does
    not exclude them (:class:`~precis_se.ops.ConnectSpec`)."""
    ops: list[dict[str, Any]] = []
    for c in tree.connects:
        a, b = f"{c.a_block}.{c.a_port}", f"{c.b_block}.{c.b_port}"
        ops.append(
            _op(
                "connect",
                a=a,
                b=b,
                joint=c.joint,
                kind=c.kind,
                objectives=dict(c.objectives),
            )
        )
        if c.optical:
            ops.append(_op("set_optical_link", a=a, b=b, **c.optical))
    return ops


def _facet_ops(tree: SeTree) -> list[dict[str, Any]]:
    """Per-block facets that no minting op takes. Ordered mode → binding →
    overrides because :func:`precis_se.ops._op_set_process_override`
    rejects a field the block's mode family does not define, so the mode
    has to be in place first."""
    ops: list[dict[str, Any]] = []
    for name in _block_order(tree):
        node = tree.blocks[name]
        intent = (node.build_frame or {}).get("intent")
        if node.mode is not None or intent is not None:
            ops.append(_op("set_mode", block=name, mode=node.mode, intent=intent))
        if node.bound_kind is not None:
            ops.append(
                _op("set_binding", block=name, kind=node.bound_kind, design=node.bound)
            )
        for field_name, value in (node.process_overrides or {}).items():
            ops.append(
                _op(
                    "set_process_override",
                    block=name,
                    field=field_name,
                    value=value,
                )
            )
        down = (node.build_frame or {}).get("down")
        if down is not None:
            ops.append(_op("set_build_frame", block=name, down=down))
        if node.objectives:
            ops.append(_op("set_load", block=name, **node.objectives))
        if node.chromophore:
            ops.append(_op("set_chromophore", block=name, **node.chromophore))
        chain_op = _chain_op(name, node.chain)
        if chain_op is not None:
            ops.append(chain_op)
    return ops


def _chain_op(name: str, chain: dict[str, Any] | None) -> dict[str, Any] | None:
    """A block's chain record back into its ``declare_*`` op.

    The record is NOT the op payload, so this cannot be a spread: the
    builders normalise every quantity to a bare SI number
    (``phase0`` radians, ``min_bend_radius_m``), while the ops reject a
    bare number for a dimensioned argument outright — the units-policy
    exponent-slip guard. So each one is re-quoted with its unit on the way
    out. ``anchor`` likewise stores ``{'block', 'port', 'nt'}`` and the op
    takes the dotted token plus ``tether_nt``.

    A ``segment`` is a ``layout_chain`` child and has no declare op of its
    own; it returns ``None`` (:data:`NOT_CARRIED`).
    """
    if not chain:
        return None
    rec = dict(chain)
    role = rec.pop("role", None)
    if role == "helix":
        return _op(
            "declare_helix",
            block=name,
            n_units=rec.get("n_units"),
            nucleic=rec.get("nucleic"),
            motif=rec.get("motif"),
            path=rec.get("path"),
            register=rec.get("register"),
            phase0=f"{rec['phase0']} rad" if rec.get("phase0") else None,
            min_bend_radius=(
                f"{rec['min_bend_radius_m']} m" if "min_bend_radius_m" in rec else None
            ),
            min_gap=f"{rec['min_gap_m']} m" if "min_gap_m" in rec else None,
        )
    if role == "strand":
        anchor = rec.get("anchor") or {}
        return _op(
            "declare_strand",
            block=name,
            nucleic=rec.get("nucleic"),
            sequence=rec.get("sequence"),
            anchor=f"{anchor['block']}.{anchor['port']}" if anchor else None,
            tether_nt=anchor.get("nt") if anchor else None,
        )
    return None


def _ledger_ops(tree: SeTree) -> list[dict[str, Any]]:
    """The tree-level collections. ``add_note`` takes the body under
    ``text`` while :class:`~precis.utils.notes.NoteSpec` stores it as
    ``body`` — the one name that differs between an op and its record."""
    ops: list[dict[str, Any]] = []
    for m in tree.measures:
        ops.append(
            _op(
                "add_measure",
                block=m.block,
                name=m.name,
                value=m.value,
                relation=m.relation,
                strength=m.strength,
                reason=m.reason,
                # The op's key names are the SHORT ones; the record's are
                # ``min_value``/``max_value``. Same pair-of-names trap as
                # ``add_note``'s ``text`` → ``NoteSpec.body`` below.
                min=m.min_value,
                max=m.max_value,
                origin=m.origin,
                # A measurand measure's unit is the measurand's snapshot,
                # which add_measure re-derives (and would refuse as unit=
                # whenever it is outside the m|count|ratio|deg registry).
                # The slug is the portable name; the ref id is re-resolved.
                unit=m.unit if m.measurand is None else None,
                measurand=m.measurand,
                datum=m.datum,
            )
        )
    for b in tree.bom:
        ops.append(
            _op(
                "add_bom",
                item_kind=b.item_kind,
                item=b.item,
                qty=b.qty,
                # A connect line stores four endpoint columns but the op
                # takes the two dotted tokens; ``block`` and the pair are
                # mutually exclusive, which ``_bom_target`` enforces.
                block=b.block,
                a=None if b.block is not None else f"{b.a_block}.{b.a_port}",
                b=None if b.block is not None else f"{b.b_block}.{b.b_port}",
                uom=b.uom,
                reason=b.reason,
            )
        )
    for n in tree.notes:
        ops.append(
            _op(
                "add_note",
                name=n.name,
                kind=n.kind,
                text=n.body,
                re=n.re,
                about=list(n.about),
                origin=n.origin,
            )
        )
    for t in tree.threading:
        ops.append(_op("declare_threading", a=t.a, b=t.b))
    for d in tree.domains:
        ops.append(
            _op(
                "add_domain",
                strand=d.strand,
                helix=d.helix,
                ord=d.ord,
                forward=d.forward,
                start=d.start,
                end=d.end,
                geometry=d.geometry,
                overrides=d.overrides,
                loop_before_nt=d.loop_before_nt,
            )
        )
    if tree.optics:
        ops.append(_op("set_optics", **tree.optics))
    return ops


def _pocket_ops(tree: SeTree) -> list[dict[str, Any]]:
    """One ``add_pocket`` per pocket, regions as bare selectors: a
    region's measures are ordinary measures (already emitted by
    :func:`_ledger_ops` with the region's selector as ``datum``), and
    membership is derived from that datum on replay — emitting them
    inline as well would mint each twice."""
    ops: list[dict[str, Any]] = []
    for name in _block_order(tree):
        node = tree.blocks[name]
        for pocket in node.pockets.values():
            ops.append(
                _op(
                    "add_pocket",
                    block=name,
                    name=pocket.name,
                    shape=pocket.shape,
                    regions=[{"selector": s} for s in pocket.regions],
                )
            )
    return ops


def external_refs(tree: SeTree) -> list[str]:
    """Refs in OTHER kinds this design resolves at load time, as
    ``'kind:slug'`` — component/part/cad/structure bindings, and the
    designs behind any cross-design template.

    These are the export's prerequisites, and leaving them implicit is
    the trap this function exists to close: a binding whose target is
    absent does not error, it silently resolves to nothing. The catalog
    supplies a bound block's derived envelope and ports
    (:func:`precis_se.persist.attach_catalog`), so a copied design with
    its bindings missing renders with FEWER ports than the original and
    says nothing about it — which would quietly invalidate any
    geometry comparison between the two.

    Dogfooding this on a real design is what surfaced it: `unicycle-c1`'s
    fastener blocks author one port each and derive three more from an
    ISO-fastener component, so the copy rendered `[1 port]` where prod
    renders `[4 ports]`.
    """
    refs: set[str] = set()
    for node in tree.blocks.values():
        if node.bound_kind and node.bound:
            refs.add(f"{node.bound_kind}:{node.bound}")
        template = node.template or ""
        # 'slug#block' is a cross-design reference; '#41' is a local uid
        if "#" in template and not template.startswith("#"):
            refs.add(f"se:{template.split('#', 1)[0]}")
    return sorted(refs)


def design_ops(tree: SeTree) -> list[dict[str, Any]]:
    """The whole design as one replayable ops list.

    Phase order is a dependency order, not a taste: every block must exist
    before a port names one, every port before a ``connect`` addresses
    ``block.port``, and both before the ledger rows that hang off them.
    """
    return [
        *_block_ops(tree),
        *_port_ops(tree),
        *_connect_ops(tree),
        *_facet_ops(tree),
        *_ledger_ops(tree),
        *_pocket_ops(tree),
    ]


def render_ops(tree: SeTree, title: str) -> str:
    """The view body: the caveats first, then one fenced JSON object in
    exactly the shape ``put(kind='se', text=...)`` accepts, so the block
    can be copied straight into a call without reshaping."""
    ops = design_ops(tree)
    lines = [
        f"# {title} — ops export",
        "",
        f"{len(ops)} op(s) reconstructing this design's FINAL STATE. Paste the "
        "block below into `put(kind='se', id='<new-slug>', text=...)` to "
        "rebuild it in another database.",
        "",
        "Not carried by the round-trip:",
        "",
    ]
    lines += [f"- {gap}" for gap in NOT_CARRIED]
    needed = external_refs(tree)
    if needed:
        lines += [
            "",
            f"REQUIRED IN THE TARGET DATABASE — {len(needed)} ref(s) this "
            "design resolves at load time. A missing one does NOT error: the "
            "binding resolves to nothing, and the block loses the envelope "
            "and ports the catalog would have derived for it. Check these "
            "before comparing a copy against the original.",
            "",
        ]
        lines += [f"- {ref}" for ref in needed]
    lines += ["", "```json", _ops_json(ops), "```"]
    return "\n".join(lines)


def _ops_json(ops: list[dict[str, Any]]) -> str:
    """``{"ops": [...]}`` with ONE op per line, each op compact.

    Not a style choice — a size one. Pretty-printing at ``indent=2`` puts
    every scalar on its own line, which for a real design is most of the
    payload: `unicycle-c1`, only 20 blocks, rendered to just under the
    24 KB response frame that way, so adding a few lines of header
    truncated it. A truncated export is worse than a small one, because in
    a one-shot process (``precis tools …``) there is no pagination cursor
    to redeem — the tail is simply gone, and what remains still looks like
    a valid fenced block.

    One op per line keeps the thing diffable and greppable, which is half
    of why a text form of a design is useful at all.
    """
    if not ops:
        return '{"ops": []}'
    body = ",\n  ".join(json.dumps(op, separators=(",", ":")) for op in ops)
    return '{"ops": [\n  ' + body + "\n]}"
