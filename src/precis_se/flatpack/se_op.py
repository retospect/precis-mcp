"""The ``flatpack_box`` se op — handler-level beside ``realize`` and
``generate`` (:mod:`precis_se.atomic.apply`), and the only maker op that
mints a whole *set* of cad designs: one per panel.

``{"op": "flatpack_box", "params": {...}}`` where ``params`` are the
:func:`~precis_se.flatpack.generator.flatpack_box` kwargs verbatim (all
mm). The op runs the generator, then writes the se design for it:

* one **block per panel**, named after the panel id (``side-l``,
  ``shelf-1``, …), posed so the block's frame is the panel's
  :class:`~precis_se.flatpack.panels.Frame` centred on the outline's
  bounds — envelope ``box`` = the panel's slab, pose in the box's frame
  (the generator's mm pose is emitted with explicit units; the tree
  stores SI);
* one **port per mated neighbour** (``mate-<other panel id>``) and one
  ``connect`` per mated pair, joint ``{"class": "rigid"}``, so
  ``validate``'s ``undeclared_interpenetration`` treats the seam overlap
  as sanctioned;
* one **cad design per panel**, slug ``<design>-<panel>``, a
  :class:`~precis.cad.scene.SceneSpec` in the block's frame: ``add``
  boxes for the strips of the outline (exterior filled — fingers and
  tabs included), ``cut`` boxes for each slot, ``cut`` cylinders for the
  dog-bones when ``cutter_d > 0``.

Two halves, the ``realize`` precedent (:mod:`precis_se.realize`'s
docstring): :func:`prepare_flatpack_box` does the store read (prior
``meta['flatpack']`` for the retired-panel set) and every in-memory tree
write; :func:`finish_flatpack_box` holds all real store writes —
``cad_save`` per panel, ``retire_ref`` per removed panel — deferred
until the caller's whole ops list has validated.

**Re-runs are idempotent.** The same call replaces the cad designs under
the same slugs (``cad_save`` is create-or-replace) and rebinds the
blocks; a panel that no longer exists — a removed shelf — loses its
block and has its cad design retired. Nothing is suffixed ``-2`` and
nothing is orphaned. A block named like a panel but *not* bound to this
design's ``<design>-<panel>`` cad slug is a name collision, refused.

The op's ``params`` echo (``Flatpack.params``, JSON-shaped) is what the
handler merges into ``refs.meta['flatpack']`` — the params home every
export and the store-aware checks regenerate from.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np
from shapely.geometry import Polygon

from precis.cad.scene import NodeSpec, SceneSpec
from precis.cad.vec import euler_rad_from_matrix
from precis.utils.units import format_dsl_number
from precis_se.flatpack.generator import Flatpack, flatpack_box
from precis_se.flatpack.materials import material
from precis_se.flatpack.mating import Mate, mated_pairs, rect_decompose
from precis_se.flatpack.panels import FlatpackError, Panel, box_panels, inside_corners
from precis_se.ops import OpError, SeTree, apply_ops

if TYPE_CHECKING:  # pragma: no cover - typing only
    from precis.store import Store

_M_TO_MM = 1000.0


@dataclass(frozen=True)
class PendingPanel:
    """One panel's deferred cad write — the ``PendingRealize`` shape."""

    block_name: str
    cad_slug: str
    title: str
    spec: SceneSpec
    card_text: str


@dataclass(frozen=True)
class PendingFlatpack:
    """A ``flatpack_box`` op's deferred store writes (module docstring):
    one cad save per panel, plus the cad slugs of panels that no longer
    exist to retire."""

    params: dict[str, Any]
    panels: list[PendingPanel] = field(default_factory=list)
    retire_slugs: list[str] = field(default_factory=list)


def _managed(node: Any, design_slug: str, pid: str) -> bool:
    """True when the block is this op's own panel — bound to the cad
    design this op mints for it."""
    return node.bound_kind == "cad" and node.bound == f"{design_slug}-{pid}"


def _prior_panel_ids(store: Store, design_slug: str) -> set[str]:
    """Panel ids this design's last ``flatpack_box`` produced, rebuilt
    from its stored ``meta['flatpack']`` params — the retire set's left
    half. Empty when there is no prior run; a stored param set that no
    longer builds can't be recomputed, so it yields no ids (nothing is
    retired that can't be named)."""
    ref = store.get_ref(kind="se", id=design_slug)
    old = (ref.meta or {}).get("flatpack") if ref is not None else None
    if not isinstance(old, dict):
        return set()
    try:
        t = old["t"]
        if t is None:  # a params dict where t still falls to the material
            t = material(str(old.get("material") or "corrugated-3mm")).t_actual
        return {
            p.id
            for p in box_panels(
                old["W"],
                old["H"],
                old["D"],
                t=t,
                shelves=old.get("shelves") or (),
                joint=old.get("joint") or "finger",
                fit=old.get("fit") or 0.0,
            )
        }
    except (FlatpackError, KeyError, TypeError):
        return set()


def _envelope(panel: Panel) -> str:
    """The panel's slab as an envelope ``box`` (metres, centred x/y, base
    z=0 — the block frame's convention)."""
    minx, miny, maxx, maxy = panel.polygon.bounds
    w = (maxx - minx) / _M_TO_MM
    d = (maxy - miny) / _M_TO_MM
    h = panel.thickness / _M_TO_MM
    return f"box:w{format_dsl_number(w)}d{format_dsl_number(d)}h{format_dsl_number(h)}"


def _pose_rot(panel: Panel) -> tuple[str, str]:
    """``(pose, rot)`` op strings placing the block so its local x/y/z
    are the panel's u/v/n and its origin sits at the centre of the
    outline's bounds — matching the centred envelope box."""
    minx, miny, maxx, maxy = panel.polygon.bounds
    o = panel.frame.to_world((minx + maxx) / 2, (miny + maxy) / 2, 0.0)
    f = panel.frame
    rot_mx = np.array(
        [
            [f.eu[0], f.ev[0], f.en[0]],
            [f.eu[1], f.ev[1], f.en[1]],
            [f.eu[2], f.ev[2], f.en[2]],
        ]
    )
    rx, ry, rz = euler_rad_from_matrix(rot_mx)
    pose = (
        f"[{format_dsl_number(o[0])} {format_dsl_number(o[1])} "
        f"{format_dsl_number(o[2])}] mm"
    )
    rot = (
        f"[{format_dsl_number(rx)} {format_dsl_number(ry)} {format_dsl_number(rz)}] rad"
    )
    return pose, rot


def _mate_direction(panel: Panel, region: Polygon) -> list[float]:
    """The port's direction in the block frame: the dominant (u,v) axis
    from the outline centre toward the joint region."""
    minx, miny, maxx, maxy = panel.polygon.bounds
    c = region.centroid
    du = c.x - (minx + maxx) / 2
    dv = c.y - (miny + maxy) / 2
    if abs(du) >= abs(dv):
        return [1.0 if du >= 0 else -1.0, 0.0, 0.0]
    return [0.0, 1.0 if dv >= 0 else -1.0, 0.0]


def _scene_spec(panel: Panel, base: Polygon, cutter_d: float) -> SceneSpec:
    """The panel as a cad design in its block frame (module docstring):
    ``add`` boxes for the outline's strips — the filled exterior, so
    fingers and tabs are adds — ``cut`` boxes for each slot, ``cut``
    cylinders for the dog-bones. ``base`` is the pre-dog-bone outline
    (``panel.polygon`` itself when ``cutter_d == 0``). All metres."""
    minx, miny, maxx, maxy = base.bounds
    uc, vc = (minx + maxx) / 2, (miny + maxy) / 2
    t = panel.thickness / _M_TO_MM
    h = format_dsl_number(t)
    nodes: list[NodeSpec] = []

    def loc(cx: float, cy: float) -> tuple[float, float, float]:
        return ((cx - uc) / _M_TO_MM, (cy - vc) / _M_TO_MM, 0.0)

    filled = Polygon(base.exterior) if base.interiors else base
    for k, (x0, y0, x1, y1) in enumerate(rect_decompose(filled), start=1):
        nodes.append(
            NodeSpec(
                name=f"s{k}",
                op="add",
                config=(
                    f"box:w{format_dsl_number((x1 - x0) / _M_TO_MM)}"
                    f"d{format_dsl_number((y1 - y0) / _M_TO_MM)}h{h}"
                ),
                component="part",
                loc=loc((x0 + x1) / 2, (y0 + y1) / 2),
            )
        )
    for k, ring in enumerate(base.interiors, start=1):
        for j, (x0, y0, x1, y1) in enumerate(rect_decompose(Polygon(ring)), start=1):
            nodes.append(
                NodeSpec(
                    name=f"slot{k}-{j}",
                    op="cut",
                    config=(
                        f"box:w{format_dsl_number((x1 - x0) / _M_TO_MM)}"
                        f"d{format_dsl_number((y1 - y0) / _M_TO_MM)}h{h}"
                    ),
                    component="part",
                    loc=loc((x0 + x1) / 2, (y0 + y1) / 2),
                )
            )
    if cutter_d > 0:
        r = cutter_d / 2
        for k, ((px, py), (bx, by)) in enumerate(inside_corners(base), start=1):
            nodes.append(
                NodeSpec(
                    name=f"bone{k}",
                    op="cut",
                    config=(f"cyl:r{format_dsl_number(r / _M_TO_MM)}h{h}"),
                    component="part",
                    loc=loc(px + r * bx, py + r * by),
                )
            )
    return SceneSpec(nodes=nodes, components=["part"])


def _plain_panels(fp: Flatpack) -> dict[str, Panel]:
    """The panels again without dog-bones (``cutter_d = 0``) — the cad
    adds' base outlines when a CNC run is specified."""
    p = fp.params
    return {
        panel.id: panel
        for panel in box_panels(
            p["W"],
            p["H"],
            p["D"],
            t=fp.t,
            shelves=p["shelves"],
            joint=p["joint"],
            fit=fp.figures.fit,
            cutter_d=0.0,
        )
    }


def prepare_flatpack_box(
    store: Store,
    tree: SeTree,
    op: dict[str, Any],
    design_slug: str,
    carried_ids: set[str] | None = None,
) -> tuple[str, PendingFlatpack]:
    """``{"op": "flatpack_box", "params": {...}}`` — the pure/in-memory
    half (module docstring). Runs the generator, then rewrites the
    design's panel blocks/ports/connects so the result is the same
    whether the call is a first run or a re-run."""
    params = op.get("params")
    if not isinstance(params, dict) or not params:
        raise OpError(
            "flatpack_box needs 'params' — the flatpack_box kwargs "
            "(W, H, D, material, t, shelves, joint, fit, kerf, cutter_d, "
            "sheet, shelf_load_n)"
        )
    try:
        fp = flatpack_box(**params)
    except FlatpackError as exc:
        raise OpError(str(exc)) from exc
    except TypeError as exc:
        raise OpError(f"flatpack_box params: {exc}") from exc

    # The retire set: panels the last committed run produced (rebuilt from
    # meta['flatpack']) union panels an earlier flatpack_box op in this
    # same ops list minted — the second op must retire what the first
    # dropped, and the meta merge only lands at commit time.
    prior_ids = _prior_panel_ids(store, design_slug) | (carried_ids or set())
    panels = list(fp.panels)
    current_ids = {p.id for p in panels}
    managed = current_ids | prior_ids
    mates = mated_pairs(panels)
    mates_of: dict[str, list[str]] = {p.id: [] for p in panels}
    desired_pairs: set[frozenset[str]] = set()
    for m in mates:
        mates_of[m.a.id].append(m.b.id)
        mates_of[m.b.id].append(m.a.id)
        desired_pairs.add(frozenset((m.a.id, m.b.id)))

    def apply(o: dict[str, Any]) -> None:
        try:
            apply_ops(tree, [o])
        except OpError as exc:
            raise OpError(f"flatpack_box: {exc}") from exc

    # Stale internal connects first (a removed port must not still be
    # referenced), then the blocks of panels that no longer exist.
    for c in list(tree.connects):
        if (
            c.a_block in managed
            and c.b_block in managed
            and c.a_port.startswith("mate-")
            and c.b_port.startswith("mate-")
            and frozenset((c.a_block, c.b_block)) not in desired_pairs
        ):
            apply(
                {
                    "op": "disconnect",
                    "a": f"{c.a_block}.{c.a_port}",
                    "b": f"{c.b_block}.{c.b_port}",
                }
            )
    for pid in sorted(prior_ids - current_ids):
        node = tree.blocks.get(pid)
        if node is not None and _managed(node, design_slug, pid):
            apply({"op": "remove_block", "block": pid})

    pending = PendingFlatpack(params=dict(fp.params))
    cutter_d = float(fp.params["cutter_d"])
    plain = _plain_panels(fp) if cutter_d > 0 else {}
    for panel in panels:
        pid = panel.id
        slug = f"{design_slug}-{pid}"
        node = tree.blocks.get(pid)
        if node is not None and not _managed(node, design_slug, pid):
            raise OpError(
                f"flatpack_box: block {pid!r} already exists and is not "
                f"this op's panel (bound to {node.bound!r}) — rename the "
                "block or the design"
            )
        pose, rot = _pose_rot(panel)
        if node is None:
            apply(
                {
                    "op": "add_block",
                    "name": pid,
                    "envelope": _envelope(panel),
                    "pose": pose,
                    "rot": rot,
                    "desc": panel.name,
                }
            )
        else:
            apply({"op": "set_pose", "block": pid, "pose": pose, "rot": rot})
            apply(
                {
                    "op": "set_envelope",
                    "block": pid,
                    "envelope": _envelope(panel),
                }
            )
            for pname in sorted(node.ports):
                if pname.startswith("mate-") and pname[5:] not in mates_of[pid]:
                    apply({"op": "remove_port", "block": pid, "name": pname})
        region_of = _regions_of(mates, pid)
        for mate_id in sorted(mates_of[pid]):
            pname = f"mate-{mate_id}"
            if pname in tree.blocks[pid].ports:
                continue
            apply(
                {
                    "op": "add_port",
                    "block": pid,
                    "name": pname,
                    "direction": _mate_direction(panel, region_of[mate_id]),
                }
            )
        apply(
            {
                "op": "set_binding",
                "block": pid,
                "kind": "cad",
                "design": slug,
            }
        )
        pending.panels.append(
            PendingPanel(
                block_name=pid,
                cad_slug=slug,
                title=f"{panel.name} ({design_slug} flatpack)",
                spec=_scene_spec(
                    panel, plain[pid].polygon if plain else panel.polygon, cutter_d
                ),
                card_text=(
                    f"{panel.name} ({design_slug} flatpack): panel, "
                    f"t={panel.thickness} mm"
                ),
            )
        )
    existing = {
        frozenset(((c.a_block, c.a_port), (c.b_block, c.b_port))) for c in tree.connects
    }
    for m in mates:
        a_end = (m.a.id, f"mate-{m.b.id}")
        b_end = (m.b.id, f"mate-{m.a.id}")
        if frozenset((a_end, b_end)) in existing:
            continue
        apply(
            {
                "op": "connect",
                "a": f"{a_end[0]}.{a_end[1]}",
                "b": f"{b_end[0]}.{b_end[1]}",
                "joint": {"class": "rigid"},
            }
        )
    for pid in sorted(prior_ids - current_ids):
        pending.retire_slugs.append(f"{design_slug}-{pid}")
    panels_txt = ", ".join(p.id for p in panels)
    echo = (
        f"flatpack_box: {len(panels)} panel(s) ({panels_txt}), "
        f"{len(mates)} rigid connect(s)"
    )
    if pending.retire_slugs:
        echo += f"; retiring {len(pending.retire_slugs)} removed panel(s)"
    return echo, pending


def _regions_of(mates: list[Mate], pid: str) -> dict[str, Polygon]:
    """``{mate panel id: joint region in this panel's uv}`` — for the
    port directions."""
    out: dict[str, Polygon] = {}
    for m in mates:
        if m.a.id == pid:
            out[m.b.id] = m.region_a
        elif m.b.id == pid:
            out[m.a.id] = m.region_b
    return out


def finish_flatpack_box(store: Store, tree: SeTree, pending: PendingFlatpack) -> None:
    """The store-touching half (module docstring) — one ``cad_save`` per
    panel, one ``retire_ref`` per removed panel's cad design. Runs only
    after the whole ops list validated. A panel block a *later* op
    unbound or removed skips its save (the ``finish_realize`` rule); a
    retired slug still bound by a surviving block is left alone."""
    for panel in pending.panels:
        node = tree.blocks.get(panel.block_name)
        if node is None or node.bound != panel.cad_slug:
            continue
        store.cad_save(
            slug=panel.cad_slug,
            title=panel.title,
            spec=panel.spec,
            card_text=panel.card_text,
        )
    for slug in pending.retire_slugs:
        if any(b.bound_kind == "cad" and b.bound == slug for b in tree.blocks.values()):
            continue
        ref = store.get_ref(kind="cad", id=slug)
        if ref is not None:
            store.retire_ref(int(ref.id))
