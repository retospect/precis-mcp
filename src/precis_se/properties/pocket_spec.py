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

**Held shape (slice 3, case b).** ``pocket_from_molecule`` with
``remove=<label>`` designs for the fragment ``<molecule>!<label>`` (NH3
minus one H) frozen at the parent geometry. :func:`held_shape` computes
the GFN2 strain (frozen − freely relaxed, same charge/multiplicity,
spin polarisation an explicit parameter, default off — open question
12), the vacancy direction, the direction each atom moves when freed and
the donor H directions; :func:`held_shape_regions` turns those into
``mcone:`` regions (keep-open ``count`` max 0 — a wall atom inside is
DRC's ``pocket_keep_open`` error; hold ``count`` min 1; H-bond acceptor
count ≥ 1 per donor H) and a ``mol:<ref>`` binding-energy placeholder
(≤ −strain, until open question 11's H-addition form is settled).
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


def painted_molecule(tree: SeTree, name: str, without: str | None = None) -> Any | str:
    """The posed molecule block's painted surface (GFN2 charges,
    :data:`~precis_se.properties.surface.SURFACE_SETTINGS`), or an
    error string when it cannot load/paint — the shared source behind
    ``mpatch:`` resolution and the ``pocket_from_molecule`` op.
    ``without`` drops that atom label first (the held fragment,
    ``<block>!<label>``). Charge is the sum of the formal charges and the
    multiplicity the lowest the electrons allow, so the op and every
    later ``mpatch:`` resolve paint the same patches."""
    from precis_se.datums import posed_fragment_atoms
    from precis_se.properties import surface as se_surface

    got = posed_fragment_atoms(tree, name, without)
    if isinstance(got, str):
        return got
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
    painted = painted_molecule(tree, sel.instance or "", sel.without)
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
    tree: SeTree, molecule: str, wall: str, without: str | None = None
) -> tuple[int, int, int] | None:
    """The molecule's canonical body frame (:mod:`.body_frame`, from its
    atoms as posed — minus ``without``, for a held fragment) expressed in
    the wall block's frame, as integer-degree x-y-z Euler angles; ``None``
    when the structure is not loadable."""
    from precis_se.datums import posed_fragment_atoms
    from precis_se.properties.body_frame import (
        canonical_body_frame,
        relative_rotation,
    )

    got = posed_fragment_atoms(tree, molecule, without)
    wall_node = tree.blocks.get(wall)
    if not isinstance(got, tuple) or wall_node is None or not got[0]:
        return None
    _labels, elements, coords_A, _formal, _meta = got
    body = canonical_body_frame(list(elements), coords_A)
    wall_axes = cad_pose(as_vec3(wall_node.pose), as_vec3(wall_node.rot)).R.T
    a, b, c = relative_rotation(body, wall_axes)
    return (_norm_deg(a), _norm_deg(b), _norm_deg(c))


# ── slice 3: the held-shape pocket (case b) ────────────────────────────

#: Provisional held-shape cone geometry (slice 3, builder picks — not
#: rulings): every cone length is this plus the pocket clearance (Å).
#: Keep-open: N–H 1.01 Å plus the incoming H's 1.2 Å vdW radius, rounded
#: up. Hold: the moving atom's vdW radius plus a wall carbon's 1.7 Å,
#: about. H-bond partner: H···A runs 1.8–2.2 Å.
KEEP_OPEN_HALF_DEG = 30.0
KEEP_OPEN_LENGTH_A = 3.0
HOLD_HALF_DEG = 45.0
HOLD_LENGTH_A = 3.5
HBOND_HALF_DEG = 30.0
HBOND_LENGTH_A = 2.5
#: A fragment atom that moves less than this (Å) when relaxed free needs
#: no wall to hold it.
HOLD_MIN_DISP_A = 0.01
#: Relaxation budget for the free fragment (``_relax_xtb`` steps; its
#: force tolerance floors at 0.05 eV/Å).
RELAX_STEPS = 500


@dataclass
class HeldShape:
    """Slice 3's numbers for one held fragment (``<parent>!<removed>``).
    Directions are unit vectors; ``*_local`` ones are in the molecule
    structure's own frame (what ``mcone:`` selectors store), the
    vacancy is also given in the world frame. Energies in eV, GFN2 at
    ``charge``/``multiplicity``, spin-polarised only when
    ``spin_polarization`` (open question 12, default off)."""

    parent: str
    removed: str
    anchor: str
    vacancy_world: Any
    vacancy_local: Any
    e_frozen_eV: float
    e_relaxed_eV: float
    strain_eV: float
    relax_converged: bool
    relax_steps: int
    charge: int
    multiplicity: int
    spin_polarization: bool
    #: ``(label, local unit direction, displacement Å)`` per atom that
    #: moves at least :data:`HOLD_MIN_DISP_A` when the fragment relaxes.
    holds: list[tuple[str, Any, float]]
    #: ``(label, local unit X→H direction)`` per H-bond donor H.
    donors: list[tuple[str, Any]]

    @property
    def ref(self) -> str:
        return f"{self.parent}!{self.removed}"


def _kabsch_onto(mobile: Any, target: Any) -> Any:
    """``mobile`` rigidly superposed on ``target`` (least-squares
    rotation + translation), so what is left is internal motion."""
    p = np.asarray(mobile, dtype=float)
    q = np.asarray(target, dtype=float)
    pc, qc = p.mean(axis=0), q.mean(axis=0)
    h = (p - pc).T @ (q - qc)
    u, _s, vt = np.linalg.svd(h)
    d = np.sign(np.linalg.det(vt.T @ u.T)) or 1.0
    rot = vt.T @ np.diag([1.0, 1.0, d]) @ u.T
    return (rot @ (p - pc).T).T + qc


def _relax_free(
    elements: list[str],
    coords_A: Any,
    *,
    charge: int,
    multiplicity: int,
    spin_polarization: bool,
) -> tuple[Any, float, bool, int]:
    """Relax the fragment alone with ``_relax_xtb`` — ``(coords Å, energy
    eV, converged, steps)``; the coordinates come back in the input's
    frame."""
    from precis.structure.cell import Cell
    from precis.structure.relax import _relax_xtb
    from precis.structure.scene import Atom, Scene

    xyz = np.asarray(coords_A, dtype=float)
    box = float(np.ptp(xyz, axis=0).max()) + 20.0
    shift = box / 2.0 - xyz.mean(axis=0)
    cell = Cell.from_lengths_angles(box, box, box, pbc=(False, False, False))
    scene = Scene(cell=cell)
    labels = []
    for el, p in zip(elements, xyz, strict=True):
        label = scene.next_label(el)
        scene.atoms[label] = Atom(label, el, cell.cart_to_frac(p + shift))
        labels.append(label)
    result = _relax_xtb(
        scene,
        steps=RELAX_STEPS,
        tol=0.0,
        charge=charge,
        multiplicity=multiplicity,
        spin_polarization=spin_polarization,
    )
    out = np.asarray(
        [cell.frac_to_cart(scene.atoms[la].frac) for la in labels], dtype=float
    )
    return out - shift, float(result.energy or 0.0), result.converged, result.n_steps


def _round_dir(v: Any) -> tuple[float, float, float]:
    """A unit vector at 4 decimals (``+ 0.0`` folds ``-0.0``)."""
    x, y, z = (round(float(c), 4) + 0.0 for c in v)
    return (x, y, z)


def held_shape(
    tree: SeTree,
    molecule: str,
    removed: str,
    *,
    charge: int | None = None,
    multiplicity: int | None = None,
    spin_polarization: bool = False,
) -> HeldShape:
    """Strain, vacancy and hold directions of ``molecule`` minus atom
    ``removed``, the rest frozen at the parent geometry (slice 3).

    - strain = E(fragment frozen) − E(fragment relaxed free with
      ``_relax_xtb``), GFN2, both at ``charge``/``multiplicity``;
    - vacancy direction: the unit vector from the removed atom's nearest
      remaining atom (the anchor, N for NH3) toward the removed atom;
    - holds: each atom's relax displacement after superposing the relaxed
      fragment on the frozen one — the direction a wall must block;
    - donors: the fragment's H-bond donor H atoms and their X→H direction.

    ``charge`` defaults to (and must equal) the fragment's formal-charge
    sum — the painted patches use it; ``multiplicity`` defaults to the
    lowest the electrons allow (2 for NH2). Raises ``BadInput`` on a bad
    reference or charge, ``Unsupported`` without a GFN2 backend."""
    from precis.errors import Unsupported
    from precis.structure.relax import RelaxUnsupported
    from precis_se.datums import posed_fragment_atoms, posed_structure_atoms
    from precis_se.properties import surface as se_surface

    parent = posed_structure_atoms(tree, molecule)
    if parent is None:
        raise BadInput(
            f"held shape: block {molecule!r} binds no loadable structure",
            next="bind the molecule block to the parent structure (e.g. NH3)",
        )
    labels, _el, parent_A, _formal, _meta = parent
    frag = posed_fragment_atoms(tree, molecule, removed)
    if isinstance(frag, str):
        raise BadInput(f"held shape: {frag}", next="'remove' names an atom label")
    assert frag is not None
    f_labels, f_elements, f_coords, f_formal, _m = frag
    if not f_labels:
        raise BadInput(
            f"held shape: removing {removed!r} leaves no atoms of {molecule!r}"
        )
    parent_A = np.asarray(parent_A, dtype=float)
    gone = parent_A[labels.index(removed)]
    f_coords = np.asarray(f_coords, dtype=float)
    k = int(np.argmin(np.linalg.norm(f_coords - gone[None, :], axis=1)))
    anchor = f_labels[k]
    vac = gone - f_coords[k]
    vac = vac / float(np.linalg.norm(vac))
    node = tree.blocks[molecule]
    rot = np.asarray(cad_pose(as_vec3(node.pose), as_vec3(node.rot)).R, dtype=float)

    formal_sum = int(sum(f_formal))
    if charge is None:
        charge = formal_sum
    elif charge != formal_sum:
        raise BadInput(
            f"held shape: charge {charge:+d} differs from the fragment's "
            f"formal-charge sum {formal_sum:+d} — the painted patches use the "
            "formal charges",
            next="set the formal charge on the structure's atoms, or omit charge",
        )
    try:
        electrons = se_surface.check_charge_multiplicity(
            list(f_elements), charge, multiplicity
        )
    except ValueError as exc:
        raise BadInput(f"held shape: {exc}") from exc
    if multiplicity is None:
        multiplicity = 1 if electrons % 2 == 0 else 2
    try:
        frozen = se_surface.gfn2_single_point(
            list(f_elements),
            f_coords,
            charge=charge,
            multiplicity=multiplicity,
            spin_polarization=spin_polarization,
        )
        relaxed_A, e_relaxed, converged, steps = _relax_free(
            list(f_elements),
            f_coords,
            charge=charge,
            multiplicity=multiplicity,
            spin_polarization=spin_polarization,
        )
    except RelaxUnsupported as exc:
        raise Unsupported(
            f"held shape needs GFN2 (tblite): {exc}",
            next="run on a host with tblite (open question 13)",
        ) from exc

    aligned = _kabsch_onto(relaxed_A, f_coords)
    holds: list[tuple[str, Any, float]] = []
    for label, d in zip(f_labels, aligned - f_coords, strict=True):
        disp = float(np.linalg.norm(d))
        if disp >= HOLD_MIN_DISP_A:
            holds.append((label, rot.T @ (d / disp), disp))
    classes = se_surface.classify_atoms(list(f_elements), f_coords, list(f_formal))
    donors = [
        (label, rot.T @ c.direction)
        for label, c in zip(f_labels, classes, strict=True)
        if c.hbond == "donor" and c.direction is not None
    ]
    return HeldShape(
        parent=molecule,
        removed=removed,
        anchor=anchor,
        vacancy_world=vac,
        vacancy_local=rot.T @ vac,
        e_frozen_eV=frozen.energy_eV,
        e_relaxed_eV=e_relaxed,
        strain_eV=frozen.energy_eV - e_relaxed,
        relax_converged=converged,
        relax_steps=steps,
        charge=charge,
        multiplicity=multiplicity,
        spin_polarization=spin_polarization,
        holds=holds,
        donors=donors,
    )


def _cone(ref: str, anchor: str, direction: Any, half: float, length_A: float) -> str:
    dx, dy, dz = _round_dir(direction)
    return f"mcone:{ref}:{anchor}@{dx!r},{dy!r},{dz!r}/{half!r}+{length_A!r}"


def held_shape_regions(
    held: HeldShape, *, clearance_A: float, pocket: str
) -> list[dict[str, Any]]:
    """The cone and whole-fragment regions slice 3 adds to a held-shape
    pocket: the keep-open cone (``count`` max 0), one hold cone per
    moving atom (``count`` min 1), one H-bond partner cone per donor H
    (H-bond acceptor count ≥ 1), and the binding-energy placeholder on
    ``mol:<ref>@<clearance>`` (≤ −strain eV)."""
    ref = held.ref
    pre = f"{pocket}/" if pocket else ""
    regions: list[dict[str, Any]] = [
        {
            "selector": _cone(
                ref,
                held.anchor,
                held.vacancy_local,
                KEEP_OPEN_HALF_DEG,
                round(KEEP_OPEN_LENGTH_A + clearance_A, 4),
            ),
            "measures": [
                {
                    "name": f"{pre}open",
                    "unit": "count",
                    "max": 0,
                    "strength": "hard",
                    "reason": (
                        f"keep-open: no wall atom in the cone on {held.anchor} "
                        f"along the removed {held.removed} — the vacancy faces "
                        "the H source"
                    ),
                }
            ],
        }
    ]
    for label, direction, disp in held.holds:
        regions.append(
            {
                "selector": _cone(
                    ref,
                    label,
                    direction,
                    HOLD_HALF_DEG,
                    round(HOLD_LENGTH_A + clearance_A, 4),
                ),
                "measures": [
                    {
                        "name": f"{pre}hold_{label}",
                        "unit": "count",
                        "min": 1,
                        "strength": "hard",
                        "reason": (
                            f"steric hold: free, {label} moves {disp:.3f} Å "
                            "this way; a wall atom must block it"
                        ),
                    }
                ],
            }
        )
    for label, direction in held.donors:
        regions.append(
            {
                "selector": _cone(
                    ref,
                    label,
                    direction,
                    HBOND_HALF_DEG,
                    round(HBOND_LENGTH_A + clearance_A, 4),
                ),
                "measures": [
                    {
                        "name": f"{pre}hbond_{label}",
                        "measurand": "H-bond acceptor count",
                        "min": 1,
                        "strength": "hard",
                    }
                ],
            }
        )
    spin = "spin-polarised" if held.spin_polarization else "not spin-polarised"
    regions.append(
        {
            "selector": f"mol:{ref}@{clearance_A!r}",
            "measures": [
                {
                    "name": f"{pre}binding",
                    "measurand": "binding energy",
                    "max": round(-held.strain_eV, 4),
                    "strength": "hard",
                    "reason": (
                        "placeholder (open question 11: the requirement is on "
                        "the H-addition step, form pending literature): "
                        f"stabilisation ≥ the GFN2 strain {held.strain_eV:.4f} "
                        f"eV (frozen − relaxed, multiplicity "
                        f"{held.multiplicity}, {spin})"
                    ),
                }
            ],
        }
    )
    return regions


def _held_args(op: dict[str, Any], opname: str) -> dict[str, Any]:
    """Vet the held-shape keys: ``remove`` (label), ``charge``/
    ``multiplicity`` (ints), ``spin_polarization`` (bool)."""
    removed = op.get("remove")
    if not isinstance(removed, str) or not removed.strip():
        raise BadInput(
            f"{opname} 'remove' must be the label of the atom to remove "
            f"(e.g. 'H3'), got {removed!r}"
        )
    out: dict[str, Any] = {"removed": removed.strip()}
    for key in ("charge", "multiplicity"):
        value = op.get(key)
        if value is not None and (
            isinstance(value, bool) or not isinstance(value, int)
        ):
            raise BadInput(f"{opname} {key!r} must be an integer, got {value!r}")
        out[key] = value
    spin = op.get("spin_polarization", False)
    if not isinstance(spin, bool):
        raise BadInput(
            f"{opname} 'spin_polarization' must be true or false (default false "
            f"— open question 12), got {spin!r}"
        )
    out["spin_polarization"] = spin
    return out


def op_pocket_from_molecule(store: Any, tree: SeTree, op: dict[str, Any]) -> str:
    """``pocket_from_molecule`` (handler-level, ruling 7): paint the
    molecule's surface, invert it to pocket regions on the wall block,
    and write them through ``add_pocket`` + inline ``add_measure``.

    Args: ``molecule`` (block), ``wall`` (block), ``z_window`` ``[z0,
    z1]`` in nm (the station window the pocket serves; Q1 keeps stations
    themselves open), ``clearance_A`` (Å, default 0 — Q4).

    The pocket is named by :func:`pocket_name`:
    ``<wall>-<molecule>@z<z0>-<z1>[r<a>-<b>-<c>]`` — window in integer Å
    ([2.0, 4.5] nm → ``@z20-45``), plus the molecule's canonical body
    frame → wall frame rotation in integer degrees (``m`` = minus, omitted
    when all zero). The wall frame is the wall block's own pose frame. A
    second placement with the same name (same window and rotation, only
    translated) refuses with a next= to move the window or rotate.

    **Held-shape mode (slice 3, case b)** — ``remove`` (an atom label of
    the molecule's structure) turns it on: the pocket is for the
    fragment ``<molecule>!<remove>`` frozen at the parent geometry
    (:func:`held_shape`). Optional ``charge`` (must equal the fragment's
    formal-charge sum), ``multiplicity`` (default: the lowest that fits)
    and ``spin_polarization`` (default ``false`` — open question 12)
    set the strain energies. The pocket is named for the fragment
    (``<wall>-<molecule>!<remove>@z…``), its shape is
    ``mol:<molecule>!<remove>@<clearance>``, and it carries slice 2's
    patch regions for the fragment plus :func:`held_shape_regions`."""
    opname = "pocket_from_molecule"
    held_keys = {"remove", "charge", "multiplicity", "spin_polarization"}
    allowed = {"op", "molecule", "wall", "z_window", "clearance_A"} | held_keys
    strays = sorted(set(op) - allowed)
    if strays:
        raise BadInput(
            f"{opname}: unknown key(s) {', '.join(strays)} — args are "
            "molecule, wall, z_window, clearance_A, and for a held shape "
            "remove, charge, multiplicity, spin_polarization"
        )
    held_args = _held_args(op, opname) if "remove" in op else None
    if held_args is None and set(op) & held_keys:
        raise BadInput(
            f"{opname}: {', '.join(sorted(set(op) & held_keys))} only apply to "
            "a held shape",
            next="add 'remove' (the atom label to take off), or drop them",
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
    removed = held_args["removed"] if held_args else None
    ref = f"{molecule}!{removed}" if removed else molecule
    painted = painted_molecule(tree, molecule, removed)
    if isinstance(painted, str):
        raise BadInput(
            f"{opname}: cannot paint {ref!r}: {painted}",
            next="bind the molecule block to a structure it can paint",
        )
    # half-up (not banker's) so 0.45 nm → 5 Å
    name = pocket_name(
        wall,
        ref,
        math.floor(z0 * 10.0 + 0.5),
        math.floor(z1 * 10.0 + 0.5),
        molecule_rotation_deg(tree, molecule, wall, removed),
    )
    if name in wall_node.pockets:
        raise BadInput(
            f"{opname}: pocket {name!r} already exists on {wall!r} (the name "
            "carries wall, molecule and the z window in Å, not the pose)",
            next="move the z window (or rotate the molecule); pocket names "
            "are unique per block",
        )
    regions = pocket_from_surface(
        painted,
        molecule=ref,
        wall_inv=_wall_inv(tree, wall),
        z_window_A=(z0 * 10.0, z1 * 10.0),
        clearance_A=clearance_A,
        pocket=name,
    )
    held: HeldShape | None = None
    if held_args is not None:
        held = held_shape(
            tree,
            molecule,
            held_args["removed"],
            charge=held_args["charge"],
            multiplicity=held_args["multiplicity"],
            spin_polarization=held_args["spin_polarization"],
        )
        regions += held_shape_regions(held, clearance_A=clearance_A, pocket=name)
    try:
        apply_ops(
            tree,
            [
                {
                    "op": "add_pocket",
                    "block": wall,
                    "name": name,
                    "shape": f"mol:{ref}@{clearance_A!r}",
                    "regions": regions,
                }
            ],
        )
    except OpError as exc:
        raise BadInput(str(exc)) from exc
    echo = (
        f"pocket_from_molecule: pocket {name!r} on {wall!r} — "
        f"{len(regions)} region(s) from {len(painted.points)} painted "
        f"points, shape mol:{ref}@{clearance_A!r}"
    )
    if held is not None:
        echo += "; " + _held_echo(held)
    return echo


def _held_echo(held: HeldShape) -> str:
    """The op's held-shape line: strain with its parts, the vacancy
    direction (world frame) and the cone counts."""
    vx, vy, vz = _round_dir(held.vacancy_world)
    spin = "on" if held.spin_polarization else "off (open question 12)"
    unconverged = (
        ""
        if held.relax_converged
        else f", free relax NOT converged in {held.relax_steps} steps"
    )
    return (
        f"held shape {held.ref}: GFN2 strain {held.strain_eV:.4f} eV (frozen "
        f"{held.e_frozen_eV:.4f} − relaxed {held.e_relaxed_eV:.4f}, charge "
        f"{held.charge:+d}, multiplicity {held.multiplicity}, spin "
        f"polarisation {spin}{unconverged}); vacancy {held.anchor}→"
        f"{held.removed} world [{vx:g}, {vy:g}, {vz:g}]; {len(held.holds)} "
        f"hold cone(s), {len(held.donors)} H-bond partner cone(s)"
    )
