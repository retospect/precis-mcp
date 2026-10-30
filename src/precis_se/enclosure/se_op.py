"""The ``enclosure_box`` SE op: two assembled parts, deferred CAD writes.

Preparation validates the generator inputs and both destination names before
editing the tree. Each part binds ``<design>-box`` or ``<design>-lid``;
reruns replace those owned parts in place. The generator's SI scenes already
share the assembled frame, so both blocks have zero pose and rotation. Their
origin-based envelopes conservatively include each scene's additive bounds
(the lid's envelope includes the empty space underneath it).

The params home is each CAD scene's ``meta['enclosure']``; no SE metadata
copy is needed. Finishing saves only surviving CAD bindings, after the whole
op batch validates, so rejected batches mint no enclosure designs.
"""

from __future__ import annotations

from dataclasses import dataclass
from inspect import signature
from typing import TYPE_CHECKING, Any

from precis.cad.scene import SceneSpec
from precis.cad.tessellate import design_aabb
from precis.errors import BadInput
from precis.utils.units import format_dsl_number
from precis_se.enclosure.generator import enclosure_box
from precis_se.ops import OpError, SeTree, apply_ops

if TYPE_CHECKING:
    from precis.store import Store

_PARAMS_NEXT = (
    "edit(kind='se', id='<design>', args={'ops': "
    "[{'op': 'enclosure_box', 'params': {'x': 80, 'y': 50, 'z': 25}}]})"
)


@dataclass(frozen=True)
class PendingPart:
    """One generated part's deferred CAD save."""

    block_name: str
    cad_slug: str
    spec: SceneSpec


@dataclass(frozen=True)
class PendingEnclosure:
    """The generator params and the two deferred part writes."""

    params: dict[str, Any]
    parts: tuple[PendingPart, ...]


def _envelope(spec: SceneSpec) -> str:
    """Enclose assembled coordinates without translating the CAD scene."""
    lo, hi = design_aabb(spec)
    width = 2 * max(abs(float(lo[0])), abs(float(hi[0])))
    depth = 2 * max(abs(float(lo[1])), abs(float(hi[1])))
    height = float(hi[2])
    return (
        f"box:w{format_dsl_number(width)}d{format_dsl_number(depth)}"
        f"h{format_dsl_number(height)}"
    )


def prepare_enclosure_box(
    store: Store, tree: SeTree, op: dict[str, Any], design_slug: str
) -> tuple[str, PendingEnclosure]:
    """Validate and bind both parts in memory, without writing CAD refs."""
    params_next = _PARAMS_NEXT.replace("<design>", design_slug)
    params = op.get("params")
    if not isinstance(params, dict) or not params:
        raise BadInput(
            "enclosure_box needs params with x, y and z in millimetres",
            next=params_next,
        )
    try:
        signature(enclosure_box).bind(**params)
    except TypeError as exc:
        raise BadInput(f"enclosure_box params: {exc}", next=params_next) from exc
    # Keep the generator's structured BadInput.next recovery action intact.
    enclosure = enclosure_box(**params)
    pending = PendingEnclosure(
        params=dict(enclosure.params),
        parts=tuple(
            PendingPart(name, f"{design_slug}-{name}", enclosure.parts[name])
            for name in ("box", "lid")
        ),
    )
    envelopes = {part.block_name: _envelope(part.spec) for part in pending.parts}

    # Preflight both names before any tree edit: an unrelated lid must not
    # leave an already-rewritten box behind even for direct prepare callers.
    for part in pending.parts:
        node = tree.blocks.get(part.block_name)
        if node is not None and (
            node.bound_kind != "cad"
            or node.bound != part.cad_slug
            or node.parent is not None
            or node.template is not None
        ):
            raise BadInput(
                f"enclosure_box: block {part.block_name!r} already exists and "
                "is not this op's owned root part",
                next="put(kind='se', title='New enclosure', args={'ops': "
                "[{'op': 'enclosure_box', 'params': {'x': 80, 'y': 50, 'z': 25}}]})",
            )
        if node is None and store.get_ref(kind="cad", id=part.cad_slug) is not None:
            raise BadInput(
                f"enclosure_box: CAD slug {part.cad_slug!r} already exists "
                "without this op's owning block",
                next="put(kind='se', title='New enclosure', args={'ops': "
                "[{'op': 'enclosure_box', 'params': {'x': 80, 'y': 50, 'z': 25}}]})",
            )

    ops: list[dict[str, Any]] = []
    for part in pending.parts:
        name = part.block_name
        if name not in tree.blocks:
            ops.append(
                {
                    "op": "add_block",
                    "name": name,
                    "envelope": envelopes[name],
                    "pose": [0, 0, 0],
                    "rot": [0, 0, 0],
                    "desc": f"Enclosure {name}",
                }
            )
        else:
            ops.extend(
                [
                    {
                        "op": "set_pose",
                        "block": name,
                        "pose": [0, 0, 0],
                        "rot": [0, 0, 0],
                    },
                    {"op": "set_envelope", "block": name, "envelope": envelopes[name]},
                ]
            )
        ops.append(
            {"op": "set_binding", "block": name, "kind": "cad", "design": part.cad_slug}
        )
    try:
        apply_ops(tree, ops)
    except OpError as exc:
        raise BadInput(f"enclosure_box: {exc}", next=params_next) from exc
    return "enclosure_box: bound 2 parts (box, lid) in assembled coordinates", pending


def finish_enclosure_box(store: Store, tree: SeTree, pending: PendingEnclosure) -> None:
    """Save only parts still bound to their generated CAD design."""
    for part in pending.parts:
        node = tree.blocks.get(part.block_name)
        if node is None or node.bound_kind != "cad" or node.bound != part.cad_slug:
            continue
        title = f"Enclosure {part.block_name} ({part.cad_slug})"
        store.cad_save(
            slug=part.cad_slug,
            title=title,
            spec=part.spec,
            card_text=f"{title}: generated enclosure part, dimensions in millimetres",
        )
