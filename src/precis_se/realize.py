"""``realize`` — mint a block's first concrete implementation.

se-print-implementer.md: an se block as authored is the *abstract
requirements block* (L0–L2 — envelope, ports, joints, measures, loads);
its **implementation** is the L3 realization, a bound ``cad`` design plus
real joining hardware. ``realize(block, mode)`` mints the cad half,
deterministically, no LLM: a stored ``cad`` design seeded from the block's
own effective envelope as its one solid, in the block's local frame (pose
identity — the block's world pose already places it; the seed design's
own geometry is just what the envelope already says), named
``<design>-<block>`` (a collision takes a numeric suffix, reported), then
``set_binding(kind='cad', design=<slug>)`` and ``set_mode(mode)`` on the
block. The propose/interrogate lane refines the seed afterward through the
ordinary cad edit path; ``realize`` never mints fasteners — which hardware
to buy is a design decision left to the ``abstract_joint`` finding's
``suggested_fix``.

**Two halves, the ``generate`` precedent**
(:mod:`precis_se.atomic.generate`, the ``import_fragment`` pattern this
module transfers unchanged): :func:`prepare_realize` is pure/in-memory —
resolves the target block (through its template, for an array/instance
member), mints the *slug* (a read-only store preflight, retried on
collision) and applies ``set_binding``/``set_mode`` to the in-memory tree
— and :func:`finish_realize` holds the one real store write
(``store.cad_save``), deferred until the caller's *whole* ops list has
validated. The deferral matters for the same reason it does for
``generate``: ``cad_save`` commits in its own transaction, independent of
the se design's own ``persist.save_tree``, so a *later* op in the same
call failing must never leave a minted, unreferenced cad design behind —
nothing is minted until every op, including any later ones, has already
proven itself valid.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from precis.blocktree.types import parse_template_ref
from precis.cad.scene import NodeSpec, SceneSpec
from precis_se.modes import ModeError, parse_mode
from precis_se.ops import OpError, SeBlock, SeTree, apply_ops, effective_envelope

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store


@dataclass
class PendingRealize:
    """A ``realize`` op's deferred store write (module docstring) —
    everything needed to mint the cad design, with no re-validation
    needed: the block/mode/envelope checks already ran, against the
    in-memory tree, before this was built."""

    block_name: str
    cad_slug: str
    title: str
    spec: SceneSpec
    card_text: str


def _unique_cad_slug(store: Store, base: str) -> tuple[str, str | None]:
    """``base``, or ``base-2``/``base-3``… on collision (se-print-
    implementer.md: "a name collision takes a numeric suffix, reported").
    Returns ``(slug, note)`` — ``note`` is ``None`` when ``base`` itself
    was free."""
    if store.get_ref(kind="cad", id=base) is None:
        return base, None
    n = 2
    while store.get_ref(kind="cad", id=f"{base}-{n}") is not None:
        n += 1
    slug = f"{base}-{n}"
    return slug, f"{base!r} was already taken — minted {slug!r} instead"


def resolve_realize_target(tree: SeTree, op: dict[str, Any]) -> tuple[str, SeBlock]:
    """The ``(key, node)`` a ``realize`` op actually implements — the named
    block, or, for an array/template member, its template (module
    docstring). Shared by the analytic seed here and the ``simp`` strategy
    (:mod:`precis_se.simp_bridge`), so both resolve the target by one
    rule."""
    name = op.get("block")
    if not name or not str(name).strip():
        raise OpError("realize needs 'block'")
    key = tree.resolve_key(name)
    if key is None:
        raise OpError(f"realize: no such block {name!r}")
    node = tree.blocks[key]
    if node.template is not None:
        # Array/template members realize through their template — one
        # implementation per template, the same way instancing already
        # works (se-print-implementer.md). A cross-design template can't
        # be realized from here: this call has no write access to the
        # OTHER design's tree.
        design_ref, block_name = parse_template_ref(node.template)
        if design_ref is not None:
            raise OpError(
                f"realize: {key!r} instances a template in another design "
                f"({node.template!r}) — realize the template in its own "
                "design instead"
            )
        template_node = tree.blocks.get(block_name)
        if template_node is None:
            raise OpError(
                f"realize: {key!r}'s template {block_name!r} no longer "
                "exists in this design"
            )
        key, node = block_name, template_node
    return key, node


def prepare_realize(
    store: Store, tree: SeTree, op: dict[str, Any], design_slug: str
) -> tuple[str, PendingRealize]:
    """``{"op": "realize", "block": <name>, "mode": <mode key>}`` — the
    pure/in-memory half (module docstring). ``design_slug`` is the se
    design's own slug, threaded through the same way ``generate`` takes
    it: ``put`` knows it before the ref row exists. ``strategy='simp'``
    never reaches here — :func:`precis_se.atomic.apply.apply_ops_with_atomic`
    routes it to :mod:`precis_se.simp_bridge` first."""
    key, node = resolve_realize_target(tree, op)
    if node.bound_kind is not None:
        raise OpError(
            f"realize: block {key!r} is already bound (kind={node.bound_kind!r}, "
            f"design={node.bound!r}) — set_binding(clear=true) first"
        )
    mode = op.get("mode")
    if not mode or not str(mode).strip():
        raise OpError("realize needs 'mode' (a mode key, e.g. 'fdm/pla')")
    mode = str(mode).strip()
    try:
        parse_mode(mode)
    except ModeError as exc:
        raise OpError(f"realize: {exc}") from exc
    envelope = effective_envelope(tree, node)
    if not envelope:
        raise OpError(
            f"realize: block {key!r} has no envelope — set_envelope first "
            "(realize seeds the cad design from it)"
        )

    base_slug = f"{design_slug}-{key}"
    slug, note = _unique_cad_slug(store, base_slug)
    spec = SceneSpec(
        nodes=[NodeSpec(name="body", op="add", config=envelope, component="part")],
        components=["part"],
    )
    title = f"{key} ({design_slug} realize)"
    card_text = f"{title}: printed-solid seed, {envelope}"

    try:
        apply_ops(
            tree,
            [
                {"op": "set_binding", "block": key, "kind": "cad", "design": slug},
                {"op": "set_mode", "block": key, "mode": mode},
            ],
        )
    except OpError:  # pragma: no cover - defensive: both ops are pre-vetted above
        raise

    echo = f"realize({key!r}): bound to new cad design {slug!r}, mode {mode!r}"
    if note:
        echo += f" ({note})"
    pending = PendingRealize(
        block_name=key, cad_slug=slug, title=title, spec=spec, card_text=card_text
    )
    return echo, pending


def finish_realize(store: Store, tree: SeTree, pending: PendingRealize) -> None:
    """The store-touching half (module docstring) — called only after
    every op in the whole list has validated cleanly, immediately before
    the caller's own ``persist.save_tree``. Skips entirely (no mint) if a
    *later* op in the same list already unbound/removed the block this
    pending write was for — never mint something a later op already
    undid, the same rule ``finish_generate`` follows."""
    node = tree.blocks.get(pending.block_name)
    if node is None or node.bound != pending.cad_slug:
        return
    store.cad_save(
        slug=pending.cad_slug,
        title=pending.title,
        spec=pending.spec,
        card_text=pending.card_text,
    )
