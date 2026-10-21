"""Pocket spec inversion — pocket-from-molecule slice 2b
(docs/backlog/pocket-from-molecule.md): a posed molecule's *painted*
surface becomes the property spec for the pocket the wall must offer.

:func:`cluster_patches` groups the slice-1 painter's surface points into
patches: connected components of points sharing one class key — the
owner atom's ``hbond`` class, its ``polarity``, and the ESP sign —
where two points are adjacent when closer than ``2/sqrt(density_per_A2)``
Å (the mean surface-point spacing doubled). Patch ids are the rank by
``(z, θ)`` of the patch centroid in the wall frame — stable under
re-sampling with the same seed, so ``mpatch:<mol>:<id>`` names the same
physical patch across runs.

:func:`pocket_from_surface` turns each patch into a pocket region with
the measures the spec lists: the required wall ESP band is the patch's
``[p10, p90]`` ``esp_V`` band *mirrored in sign* (the wall presents the
opposite potential); each acceptor patch requires an H-bond donor count
≥ 1 on the wall opposite it; each nonpolar patch requires hydrophobicity
index ``hydrophobic`` (migration 0182's allowed value — ``nonpolar`` is
never stored, ruling 4).

``pocket_from_molecule`` is a *handler-level* op (ruling 7): it needs
GFN2 and a bound structure, so it lives in
:mod:`precis_se.atomic.apply`'s ``HANDLER_LEVEL_OPS`` dispatch and emits
``add_pocket`` + inline ``add_measure`` ops through the existing pure
paths. ``mpatch:`` region resolution
(:func:`precis_se.datums.resolve`) calls
:func:`molecule_patch_points` — the wall atoms opposite the patch —
which shares this module's painting and clustering, so the region a
computer sees is the same partition the pocket was written from.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.cad.vec import as_vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput
from precis_se.ops import OpError, SeTree, apply_ops

#: Patches ranked by their centroid's (z, θ) in the wall frame; a point
#: pair is the same patch when closer than
#: ``_ADJ_FACTOR / sqrt(density_per_A2)`` Å (the mean spacing doubled).
_ADJ_FACTOR = 2.0


@dataclass
class Patch:
    """One connected surface class of a painted molecule. ``id`` is the
    rank by ``(z, θ)`` of ``centroid_wall`` — the ordinal ``mpatch:``
    selectors name."""

    id: int
    hbond: str
    polarity: str
    esp_sign: int
    point_indices: tuple[int, ...]
    centroid: Any  # world-frame centroid, Å
    centroid_wall: Any  # wall-frame centroid, Å
    esp_band: tuple[float, float]  # [p10, p90] of esp_V, volts
    owner_atoms: tuple[int, ...]


def _components(
    groups: dict[Any, list[int]], pos: Any, radius: float
) -> list[list[int]]:
    """Connected components within each class group under an Å radius —
    cell-hash union-find (a 200-atom paint is ~10⁴ points; pairwise would
    be ~10⁸)."""
    parent = list(range(len(pos)))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    components: list[list[int]] = []
    for idxs in groups.values():
        cell = radius
        grid: dict[tuple[int, int, int], list[int]] = {}
        for i in idxs:
            key = tuple(np.floor(pos[i] / cell).astype(int))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    for dz in (-1, 0, 1):
                        for j in grid.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                            if np.linalg.norm(pos[i] - pos[j]) <= radius:
                                union(i, j)
            grid.setdefault(key, []).append(i)
        seen: dict[int, list[int]] = {}
        for i in idxs:
            seen.setdefault(find(i), []).append(i)
        components.extend(sorted(g) for g in seen.values())
    return components


def cluster_patches(painted: Any, wall_inv: Any = None) -> list[Patch]:
    """Connected-class patches of a :class:`PaintedSurface`, ranked by
    ``(z, θ)`` of the centroid in the wall frame (``wall_inv``: the wall
    block's inverse pose, metres in/metres out; ``None`` = world frame).
    """
    pos = np.asarray(painted.points, dtype=float)
    owner = np.asarray(painted.owner, dtype=np.int64)
    esp = np.asarray(painted.esp_V, dtype=float)
    cls = painted.classes

    def key(i: int) -> tuple[str, str, int]:
        c = cls[int(owner[i])]
        sign = 1 if esp[i] > 0 else (-1 if esp[i] < 0 else 0)
        return (c.hbond, c.polarity, sign)

    groups: dict[tuple[str, str, int], list[int]] = {}
    for i in range(len(pos)):
        groups.setdefault(key(i), []).append(i)
    radius = _ADJ_FACTOR / math.sqrt(float(painted.settings.density_per_A2))
    components = _components(groups, pos, radius)

    patches = []
    for comp in components:
        idx = np.asarray(comp, dtype=np.int64)
        centroid = pos[idx].mean(axis=0)
        if wall_inv is not None:
            w = (
                np.asarray(
                    wall_inv.apply(as_vec3([float(c) * 1e-10 for c in centroid])),
                    dtype=float,
                )
                * 1e10
            )
        else:
            w = centroid
        p10, p90 = (float(v) for v in np.percentile(esp[idx], (10, 90)))
        c0 = cls[int(owner[idx[0]])]
        sign = 1 if esp[idx[0]] > 0 else (-1 if esp[idx[0]] < 0 else 0)
        patches.append(
            Patch(
                id=-1,
                hbond=c0.hbond,
                polarity=c0.polarity,
                esp_sign=sign,
                point_indices=tuple(comp),
                centroid=centroid,
                centroid_wall=w,
                esp_band=(p10, p90),
                owner_atoms=tuple(sorted(set(int(o) for o in owner[idx]))),
            )
        )
    patches.sort(
        key=lambda p: (
            p.centroid_wall[2],
            math.atan2(p.centroid_wall[1], p.centroid_wall[0]),
            p.point_indices[0],
        )
    )
    return [_replace_id(p, i) for i, p in enumerate(patches)]


def _replace_id(p: Patch, i: int) -> Patch:
    return Patch(
        id=i,
        hbond=p.hbond,
        polarity=p.polarity,
        esp_sign=p.esp_sign,
        point_indices=p.point_indices,
        centroid=p.centroid,
        centroid_wall=p.centroid_wall,
        esp_band=p.esp_band,
        owner_atoms=p.owner_atoms,
    )


def painted_molecule(tree: SeTree, name: str) -> Any | str:
    """The posed molecule block's painted surface (GFN2 charges,
    :data:`~precis_se.properties.surface.PROVISIONAL_SETTINGS`), or an
    error string when it cannot load/paint — the shared source behind
    ``mpatch:`` resolution and the ``pocket_from_molecule`` op."""
    from precis_se.datums import posed_structure_atoms
    from precis_se.properties import surface as se_surface

    got = posed_structure_atoms(tree, name)
    if got is None:
        return f"block {name!r} binds no loadable structure — nothing to paint"
    labels, elements, coords_A, formal, _meta = got
    if not labels:
        return f"the bound structure of {name!r} has no atoms"
    if len(labels) > se_surface.SURFACE_MAX_ATOMS:
        return (
            f"{len(labels)} atoms is over the {se_surface.SURFACE_MAX_ATOMS}"
            "-atom paint limit"
        )
    try:
        electrons = se_surface.check_charge_multiplicity(elements, sum(formal), None)
    except Exception as exc:
        return f"charge/multiplicity check failed: {exc}"
    multiplicity = 1 if electrons % 2 == 0 else 2
    try:
        sp = se_surface.gfn2_single_point(
            elements, coords_A, charge=sum(formal), multiplicity=multiplicity
        )
    except Exception as exc:
        return f"GFN2 single point failed: {exc}"
    return se_surface.paint_surface(labels, elements, coords_A, sp.charges, formal)


def _wall_inv(tree: SeTree, wall_name: str) -> Any | None:
    node = tree.blocks.get(wall_name)
    if node is None:
        return None
    return cad_pose(as_vec3(node.pose), as_vec3(node.rot)).inverse()


def molecule_patch_points(tree: SeTree, wall_node: Any, sel: Any) -> Any | str:
    """``mpatch:<mol>:<id>`` → the patch's surface points (world Å) — or
    an error string, which :func:`precis_se.datums._resolve_mpatch`
    returns as a lenient note."""
    painted = painted_molecule(tree, sel.instance or "")
    if isinstance(painted, str):
        return painted
    patches = cluster_patches(painted, _wall_inv(tree, wall_node.name))
    pid = sel.patch_id
    if pid is None or pid >= len(patches):
        return f"mpatch: {sel.instance!r} has {len(patches)} patch(es), no patch {pid}"
    return np.asarray(painted.points, dtype=float)[
        np.asarray(patches[pid].point_indices, dtype=np.int64)
    ]


def pocket_from_surface(
    painted: Any,
    *,
    molecule: str,
    wall_inv: Any,
    z_window_A: tuple[float, float],
    clearance_A: float,
    pocket: str = "",
) -> list[dict[str, Any]]:
    """The pocket's ``regions`` op payload: one ``mpatch:<molecule>:<id>``
    region per patch whose wall-frame centroid z sits inside
    ``z_window_A`` (Å), carrying the spec's measures — mirrored ESP band
    (electrostatic potential, the patch's [p10, p90] ``esp_V`` negated),
    an H-bond donor count ≥ 1 opposite each acceptor patch, and
    hydrophobicity index ``hydrophobic`` opposite each nonpolar patch.
    Measure names are ``<pocket>/<kind>_p<id>`` (measure names are unique
    per block, so two pockets on one wall must not share them); no
    ``pocket`` leaves them bare."""
    pre = f"{pocket}/" if pocket else ""
    patches = cluster_patches(painted, wall_inv)
    z0, z1 = z_window_A
    regions: list[dict[str, Any]] = []
    for p in patches:
        z = float(p.centroid_wall[2])
        if not (z0 <= z <= z1):
            continue
        p10, p90 = p.esp_band
        measures: list[dict[str, Any]] = [
            {
                "name": f"{pre}esp_p{p.id}",
                "measurand": "electrostatic potential",
                "min": -p90,
                "max": -p10,
                "strength": "hard",
            }
        ]
        if p.hbond == "acceptor":
            measures.append(
                {
                    "name": f"{pre}don_p{p.id}",
                    "measurand": "H-bond donor count",
                    "min": 1,
                    "strength": "hard",
                }
            )
        if p.polarity == "nonpolar":
            measures.append(
                {
                    "name": f"{pre}phob_p{p.id}",
                    "measurand": "hydrophobicity index",
                    "reason": "hydrophobic",
                    "strength": "hard",
                }
            )
        regions.append({"selector": f"mpatch:{molecule}:{p.id}", "measures": measures})
    return regions


def _angle_token(deg: int) -> str:
    return f"m{-deg}" if deg < 0 else str(deg)


def pocket_name(
    wall: str,
    molecule: str,
    z0_A: int,
    z1_A: int,
    angles: tuple[int, int, int] | None = None,
) -> str:
    """``<wall>-<molecule>@z<z0>-<z1>[r<a>-<b>-<c>]``. The window is in
    integer Å (pocket names ban dots, so no fractional nm). ``angles`` is
    the molecule's canonical body frame → wall frame rotation in integer
    degrees (x-y-z Euler, ``precis.cad.vec.pose`` convention), each in
    (-180, 180]; a negative angle is written ``m<abs>`` (``r0-m90-0``)
    because '-' is the separator. The suffix is omitted when all three
    are 0 (or ``angles`` is None)."""
    name = f"{wall}-{molecule}@z{z0_A}-{z1_A}"
    if angles is not None and any(angles):
        name += "r" + "-".join(_angle_token(a) for a in angles)
    return name


def _norm_deg(x: float) -> int:
    """Round half-up to an integer and wrap to (-180, 180]."""
    d = math.floor(x + 0.5)
    d = (d + 180) % 360 - 180
    return 180 if d == -180 else int(d)


def molecule_rotation_deg(
    tree: SeTree, molecule: str, wall: str
) -> tuple[int, int, int] | None:
    """The molecule's canonical body frame (:mod:`.body_frame`, from its
    atoms as posed) expressed in the wall block's frame, as integer-degree
    x-y-z Euler angles; ``None`` when the structure is not loadable."""
    from precis_se.datums import posed_structure_atoms
    from precis_se.properties.body_frame import (
        canonical_body_frame,
        relative_rotation,
    )

    got = posed_structure_atoms(tree, molecule)
    wall_node = tree.blocks.get(wall)
    if got is None or wall_node is None or not got[0]:
        return None
    _labels, elements, coords_A, _formal, _meta = got
    body = canonical_body_frame(list(elements), coords_A)
    wall_axes = cad_pose(as_vec3(wall_node.pose), as_vec3(wall_node.rot)).R.T
    a, b, c = relative_rotation(body, wall_axes)
    return (_norm_deg(a), _norm_deg(b), _norm_deg(c))


def op_pocket_from_molecule(store: Any, tree: SeTree, op: dict[str, Any]) -> str:
    """``pocket_from_molecule`` (handler-level, ruling 7): paint the
    molecule's surface, invert it to pocket regions on the wall block,
    and write them through ``add_pocket`` + inline ``add_measure``.

    Args: ``molecule`` (block), ``wall`` (block), ``z_window`` ``[z0,
    z1]`` in nm (the station window the pocket serves; Q1 keeps stations
    themselves open), ``clearance_A`` (Å, provisional 0 — Q4).

    The pocket is named by :func:`pocket_name`:
    ``<wall>-<molecule>@z<z0>-<z1>[r<a>-<b>-<c>]`` — window in integer Å
    ([2.0, 4.5] nm → ``@z20-45``), plus the molecule's canonical body
    frame → wall frame rotation in integer degrees (``m`` = minus, omitted
    when all zero). The wall frame is the wall block's own pose frame. A
    second placement with the same name (same window and rotation, only
    translated) refuses with a next= to move the window or rotate."""
    opname = "pocket_from_molecule"
    allowed = {"op", "molecule", "wall", "z_window", "clearance_A"}
    strays = sorted(set(op) - allowed)
    if strays:
        raise BadInput(
            f"{opname}: unknown key(s) {', '.join(strays)} — args are "
            "molecule, wall, z_window, clearance_A"
        )
    molecule = op.get("molecule")
    wall = op.get("wall")
    if not isinstance(molecule, str) or not molecule.strip():
        raise BadInput(f"{opname} needs 'molecule' (the posed molecule block)")
    if not isinstance(wall, str) or not wall.strip():
        raise BadInput(f"{opname} needs 'wall' (the block the pocket writes on)")
    molecule = molecule.strip()
    wall = wall.strip()
    mol_node = tree.blocks.get(molecule)
    if mol_node is None:
        raise BadInput(f"{opname}: no block {molecule!r} — molecule names a block")
    wall_node = tree.blocks.get(wall)
    if wall_node is None:
        raise BadInput(f"{opname}: no block {wall!r} — wall names a block")
    z_window = op.get("z_window")
    if (
        not isinstance(z_window, list | tuple)
        or len(z_window) != 2
        or not all(isinstance(z, int | float) for z in z_window)
    ):
        raise BadInput(
            f"{opname} 'z_window' must be [z0, z1] in nm (the station "
            f"window), got {z_window!r}"
        )
    z0, z1 = (float(z) for z in z_window)
    if not (math.isfinite(z0) and math.isfinite(z1)) or z0 > z1:
        raise BadInput(
            f"{opname} 'z_window' must be finite with z0 ≤ z1, got {z_window!r}"
        )
    clearance_A = op.get("clearance_A", 0.0)
    if not isinstance(clearance_A, int | float) or not math.isfinite(
        float(clearance_A)
    ):
        raise BadInput(
            f"{opname} 'clearance_A' must be a number in Å, got {clearance_A!r}"
        )
    clearance_A = float(clearance_A)
    if clearance_A < 0.0:
        raise BadInput(
            f"{opname} 'clearance_A' must be ≥ 0 Å (the pocket surface "
            f"offsets outward), got {clearance_A:g}"
        )
    painted = painted_molecule(tree, molecule)
    if isinstance(painted, str):
        raise BadInput(
            f"{opname}: cannot paint {molecule!r}: {painted}",
            next="bind the molecule block to a structure it can paint",
        )
    # half-up (not banker's) so 0.45 nm → 5 Å
    name = pocket_name(
        wall,
        molecule,
        math.floor(z0 * 10.0 + 0.5),
        math.floor(z1 * 10.0 + 0.5),
        molecule_rotation_deg(tree, molecule, wall),
    )
    regions = pocket_from_surface(
        painted,
        molecule=molecule,
        wall_inv=_wall_inv(tree, wall),
        z_window_A=(z0 * 10.0, z1 * 10.0),
        clearance_A=clearance_A,
        pocket=name,
    )
    if name in wall_node.pockets:
        raise BadInput(
            f"{opname}: pocket {name!r} already exists on {wall!r} (the name "
            "carries wall, molecule and the z window in Å, not the pose)",
            next="move the z window (or rotate the molecule); pocket names "
            "are unique per block",
        )
    try:
        apply_ops(
            tree,
            [
                {
                    "op": "add_pocket",
                    "block": wall,
                    "name": name,
                    "shape": f"mol:{molecule}@{clearance_A!r}",
                    "regions": regions,
                }
            ],
        )
    except OpError as exc:
        raise BadInput(str(exc)) from exc
    return (
        f"pocket_from_molecule: pocket {name!r} on {wall!r} — "
        f"{len(regions)} region(s) from {len(painted.points)} painted "
        f"points, shape mol:{molecule}@{clearance_A!r}"
    )
