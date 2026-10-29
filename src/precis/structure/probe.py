"""The read surface — exact, in-memory probes over the Scene.

These are *reads* (the Read category of §6): idempotent queries that return
numbers for the LLM, computed against the hydrated Scene with no DB round-trip.
v1 floor: toc · atom config · neighborhood · coordination · distance/angle ·
find · auto bond detection. Field/ensemble probes are vision (§6.7/§18).

Unit enclave (package docstring): Å-native; ``distance``/``neighborhood``/
``radius``/``reach`` arguments and returns are all Å, angles are degrees —
never SI.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from . import elements
from .cell import ImageOffset
from .scene import Bond, Scene


@dataclass
class NeighborHit:
    """One neighbour in a coordination shell."""

    label: str
    element: str
    distance: float
    image: ImageOffset


def coordination(scene: Scene, label: str, tolerance: float = 1.2) -> int:
    """Number of atoms within the covalent bond cutoff of ``label`` (MIC)."""
    return _row_count(scene, label, tolerance, covalent_only=False)


def coordination_all(scene: Scene, tolerance: float = 1.2) -> dict[str, int]:
    """:func:`coordination` for every atom at once (one O(N²) kernel pass)."""
    from . import _pair_kernel

    labels, frac, lat, pbc = _pair_kernel.pack_scene(scene)
    radii = _pair_kernel.radii_of(scene, labels)
    ok = np.ones(len(labels), dtype=np.bool_)
    counts = _pair_kernel.coordination_counts(frac, lat, pbc, radii, tolerance, ok)
    return {la: int(c) for la, c in zip(labels, counts, strict=True)}


def covalent_coordination_all(scene: Scene, tolerance: float = 1.2) -> dict[str, int]:
    """:func:`covalent_coordination` for every atom at once."""
    from . import _pair_kernel

    labels, frac, lat, pbc = _pair_kernel.pack_scene(scene)
    radii = _pair_kernel.radii_of(scene, labels)
    ok = np.array(
        [elements.max_valence(scene.atoms[la].element) is not None for la in labels],
        dtype=np.bool_,
    )
    counts = _pair_kernel.coordination_counts(frac, lat, pbc, radii, tolerance, ok)
    return {la: int(c) for la, c in zip(labels, counts, strict=True)}


def _row_count(
    scene: Scene, label: str, tolerance: float, *, covalent_only: bool
) -> int:
    """Neighbours of ``label`` within bond cutoff — one O(N) kernel row."""
    from . import _pair_kernel

    labels, frac, lat, pbc = _pair_kernel.pack_scene(scene)
    radii = _pair_kernel.radii_of(scene, labels)
    if label not in scene.atoms:
        raise KeyError(label)
    idx = labels.index(label)
    d, _img = _pair_kernel.row_mic(frac, idx, lat, pbc)
    hit = d <= (radii[idx] + radii) * tolerance
    hit[idx] = False
    if covalent_only:
        for k, la in enumerate(labels):
            if hit[k] and elements.max_valence(scene.atoms[la].element) is None:
                hit[k] = False
    return int(hit.sum())


def covalent_coordination(scene: Scene, label: str, tolerance: float = 1.2) -> int:
    """Covalent-only neighbour count within bond cutoff of ``label`` (MIC).

    Like :func:`coordination`, but skips neighbours whose element is not
    valence-bounded (metals — ``elements.max_valence`` is ``None``). Surface
    coordination to a metal slab isn't covalent bonding, so it shouldn't
    consume an adsorbate's covalent valence budget; this is what the
    over-valence gate (validate.py rule 2) wants.
    """
    return _row_count(scene, label, tolerance, covalent_only=True)


def neighborhood(scene: Scene, label: str, radius: float) -> list[NeighborHit]:
    """The coordination shell of ``label`` within ``radius`` Å, nearest first."""
    hits: list[NeighborHit] = []
    for other_label, image, dist in scene.neighbors(label, radius):
        hits.append(
            NeighborHit(
                label=other_label,
                element=scene.atoms[other_label].element,
                distance=dist,
                image=image,
            )
        )
    return hits


def distance(scene: Scene, a: str, b: str) -> float:
    """MIC distance (Å) between two atoms."""
    dist, _ = scene.cell.mic(scene.atoms[a].frac, scene.atoms[b].frac)
    return dist


def angle(scene: Scene, a: str, b: str, c: str) -> float:
    """Angle a–b–c in degrees, MIC-aware (b is the vertex)."""
    cell = scene.cell
    fb = scene.atoms[b].frac
    # nearest images of a and c relative to the vertex b
    _, ia = cell.mic(fb, scene.atoms[a].frac)
    _, ic = cell.mic(fb, scene.atoms[c].frac)
    va = cell.frac_to_cart(scene.atoms[a].frac + np.array(ia) - fb)
    vc = cell.frac_to_cart(scene.atoms[c].frac + np.array(ic) - fb)
    cosang = float(va @ vc / (np.linalg.norm(va) * np.linalg.norm(vc)))
    return float(np.degrees(np.arccos(np.clip(cosang, -1.0, 1.0))))


def detect_bonds(scene: Scene, tolerance: float = 1.2) -> list[Bond]:
    """Auto-detect bonds from geometry (covalent cutoff), marked ``inferred``.

    The structure atomistic IR Open-Q2: the LLM always sees the best image of reality — bonds are
    auto-detected, never withheld, and tagged ``inferred`` so they're marked, not
    hidden. Each unordered pair is emitted once with its MIC image offset.
    """
    from . import _pair_kernel

    labels, frac, lat, pbc = _pair_kernel.pack_scene(scene)
    radii = _pair_kernel.radii_of(scene, labels)
    ii, jj, _d, img = _pair_kernel.pairs_within(frac, lat, pbc, radii, tolerance, True)
    return [
        Bond(
            i=scene.atoms[labels[i]].label,
            j=scene.atoms[labels[j]].label,
            provenance="inferred",
            image=(int(g[0]), int(g[1]), int(g[2])),
        )
        for i, j, g in zip(ii.tolist(), jj.tolist(), img, strict=True)
    ]


def find(
    scene: Scene,
    *,
    element: str | None = None,
    undercoordinated: bool = False,
    tolerance: float = 1.2,
) -> list[str]:
    """Select atom labels by predicate (element and/or under-coordination)."""
    out: list[str] = []
    for label, atom in scene.atoms.items():
        if element is not None and atom.element != element:
            continue
        if undercoordinated:
            mv, _known = elements.effective_valence(atom.element, atom.charge)
            if mv is None or coordination(scene, label, tolerance) >= mv:
                continue
        out.append(label)
    return out


def dihedral(scene: Scene, a: str, b: str, c: str, d: str) -> float:
    """Dihedral angle a–b–c–d in degrees, MIC-aware (the torsion about b–c)."""
    cell = scene.cell
    fb = scene.atoms[b].frac
    fc = scene.atoms[c].frac
    # place every atom in the image nearest the b–c axis midpoint
    _, ia = cell.mic(fb, scene.atoms[a].frac)
    _, ic = cell.mic(fb, fc)
    _, id_ = cell.mic(fc, scene.atoms[d].frac)
    pa = cell.frac_to_cart(scene.atoms[a].frac + np.array(ia))
    pb = cell.frac_to_cart(fb)
    pc = cell.frac_to_cart(fc + np.array(ic))
    pd = cell.frac_to_cart(scene.atoms[d].frac + np.array(id_) + np.array(ic))
    b1, b2, b3 = pb - pa, pc - pb, pd - pc
    n1 = np.cross(b1, b2)
    n2 = np.cross(b2, b3)
    m1 = np.cross(n1, b2 / np.linalg.norm(b2))
    x = float(n1 @ n2)
    y = float(m1 @ n2)
    return float(np.degrees(np.arctan2(y, x)))


# -- spatial probes (the CAD ray / plane, retargeted to atoms, §6.2) ---------


@dataclass
class LineHit:
    """An atom near a probe ray, with its position along and offset from it."""

    label: str
    element: str
    along: float  # Å projected onto the ray direction
    offset: float  # Å perpendicular distance from the ray


def _cart(scene: Scene, label: str) -> np.ndarray:
    return scene.cell.frac_to_cart(scene.atoms[label].frac)


def line(
    scene: Scene,
    origin: np.ndarray,
    direction: np.ndarray,
    radius: float,
) -> list[LineHit]:
    """Atoms within ``radius`` Å of a Cartesian ray, ordered along it (§6.2).

    The instrument for channels / atom columns. v1 reads in-cell positions (no
    periodic image expansion) — adequate for an adsorbate column or a pore; a
    PBC-tiled ray is a later refinement.
    """
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    dn = d / np.linalg.norm(d)
    hits: list[LineHit] = []
    for label, atom in scene.atoms.items():
        p = scene.cell.frac_to_cart(atom.frac) - o
        along = float(p @ dn)
        offset = float(np.linalg.norm(p - along * dn))
        if offset <= radius:
            hits.append(LineHit(label, atom.element, along, offset))
    hits.sort(key=lambda h: h.along)
    return hits


def local_normal(scene: Scene, label: str) -> np.ndarray | None:
    """An atom's local axis for the ray's ``∠`` (structure-workspaces 2r):
    the POAV normal for a 3-coordinate centre — the normal of the plane
    through its three unit bond-vector tips, the vector
    :func:`precis.structure.geometry_stats.poav_theta` measures against —
    the bond axis for a linear 2-coordinate one (> 170°), else ``None``."""
    if label not in scene.atoms:
        return None
    vecs: list[np.ndarray] = []
    fi = scene.atoms[label].frac
    for b in scene.bonds:
        if label not in (b.i, b.j) or b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        other = b.j if b.i == label else b.i
        sign = 1.0 if b.i == label else -1.0
        fj = scene.atoms[other].frac + sign * np.asarray(b.image, dtype=float)
        v = np.asarray(scene.cell.frac_to_cart(fj - fi), dtype=float)
        n = float(np.linalg.norm(v))
        if n > 1e-9:
            vecs.append(v / n)
    if len(vecs) == 3:
        nv = np.cross(vecs[1] - vecs[0], vecs[2] - vecs[0])
        norm = float(np.linalg.norm(nv))
        return nv / norm if norm > 1e-9 else None
    if len(vecs) == 2 and float(vecs[0] @ vecs[1]) < np.cos(np.radians(170.0)):
        return vecs[0]
    return None


def normal_angle(scene: Scene, label: str, direction: np.ndarray) -> float | None:
    """Acute angle (degrees, 0–90) between a ray direction and the atom's
    :func:`local_normal`; ``None`` when the atom has none."""
    n = local_normal(scene, label)
    if n is None:
        return None
    d = np.asarray(direction, dtype=float)
    d = d / np.linalg.norm(d)
    return float(np.degrees(np.arccos(min(1.0, abs(float(n @ d))))))


# -- observation rays (structure-workspaces 2r, §Observation rays) -----------

#: Every atomic field :func:`ray_hits` answers, in header order.
RAY_FIELDS = ("pass", "charge", "pi", "frame", "relax", "region")
#: An aromatic ring's π cylinder: half-height off the ring plane (Å) and
#: the radius pad over the ring's circumradius (Å) — the §4 isosurface at
#: template fidelity.
PI_HALF_HEIGHT_A = 1.7
PI_RADIUS_PAD_A = 0.5
#: A non-aromatic conjugated system's region: the union of per-atom
#: spheres, Å (the carbon vdW radius when the caller passes no table).
PI_SYSTEM_RADIUS_A = 1.7
#: Coulomb constant in V·Å/e — ESP of a point charge q (e) at r (Å).
_COULOMB_V_A = 14.399645
#: An ``esp`` hit fires when the sampled ESP moves this far (V) from the
#: last emitted value, as well as on a sign change or an extremum.
ESP_STEP_V = 0.05
#: Transition refinement: bisection steps between two samples.
_BISECT = 14


@dataclass
class RayHit:
    """One event along an atomic observation ray. ``s`` is Å from the
    ray origin; ``field`` one of :data:`RAY_FIELDS`; ``event`` ``pass`` |
    ``esp`` | ``enter`` | ``leave`` | ``change``; ``subject`` the
    coalescing key (atom label, ``ring:<i>`` / ``system:<i>``, frame key,
    region name); ``data`` the field's numbers (``d`` and ``angle`` for a
    pass, ``q`` with charges, ``V`` for esp, ring/system facts for pi)."""

    s: float
    field: str
    event: str
    subject: str
    data: dict[str, Any]


@dataclass
class RayTrace:
    """:func:`ray_hits`'s result: the hits in ``s`` order, ``end`` (the
    last ``s`` with any atom within ``reach``, ``None`` when none is) and
    ``fields`` — the requested fields that had data (a field with none is
    absent, never a per-hit gap — multiscale §5.4)."""

    hits: list[RayHit]
    end: float | None
    fields: list[str]


def _transitions(
    inside: Any, s_grid: np.ndarray, o: np.ndarray, dn: np.ndarray
) -> list[tuple[float, str]]:
    """``(s, 'enter'|'leave')`` where the boolean field ``inside(points)``
    flips along the samples, each refined by bisection. A ray starting
    inside enters at ``s=0``."""
    pts = o[None, :] + s_grid[:, None] * dn[None, :]
    state = np.asarray(inside(pts), dtype=bool)
    out: list[tuple[float, str]] = []
    if state.size and state[0]:
        out.append((0.0, "enter"))
    for k in np.nonzero(state[1:] != state[:-1])[0]:
        lo, hi = float(s_grid[k]), float(s_grid[k + 1])
        want = bool(state[k + 1])
        for _ in range(_BISECT):
            mid = 0.5 * (lo + hi)
            if bool(np.asarray(inside((o + mid * dn)[None, :]))[0]) == want:
                hi = mid
            else:
                lo = mid
        out.append((hi, "enter" if want else "leave"))
    return out


def _ring_inside(carts: np.ndarray) -> Any:
    c = carts.mean(axis=0)
    _u, _s, vt = np.linalg.svd(carts - c)
    n = vt[2]
    radius = float(np.max(np.linalg.norm(carts - c, axis=1))) + PI_RADIUS_PAD_A

    def inside(pts: np.ndarray) -> np.ndarray:
        rel = pts - c
        h = rel @ n
        radial = np.linalg.norm(rel - h[:, None] * n[None, :], axis=1)
        return (np.abs(h) <= PI_HALF_HEIGHT_A) & (radial <= radius)

    return inside


def _spheres_inside(carts: np.ndarray, radii: np.ndarray) -> Any:
    def inside(pts: np.ndarray) -> np.ndarray:
        d = np.linalg.norm(pts[:, None, :] - carts[None, :, :], axis=2)
        return np.any(d <= radii[None, :], axis=1)

    return inside


#: Bondi van der Waals radii (Å) — the valve doc's gap definition, one
#: table for the clearance gap, the pocket radius and :func:`vdw_field`
#: (``precis_se.atomic.clearance`` re-exports it). Elements absent from it
#: use :data:`VDW_DEFAULT_A`.
VDW_RADII_A: dict[str, float] = {
    "H": 1.20,
    "C": 1.70,
    "N": 1.55,
    "O": 1.52,
    "F": 1.47,
    "P": 1.80,
    "S": 1.80,
    "Cl": 1.75,
}
VDW_DEFAULT_A = 1.80
#: Distance-matrix entries evaluated per block in :func:`vdw_sdf`.
_VDW_BLOCK = 2_000_000


def _vdw_arrays(
    scene: Scene,
    labels: Sequence[str] | None,
    radii: dict[str, float] | None,
) -> tuple[list[str], np.ndarray, np.ndarray]:
    """``(labels, carts, radii)`` of the atoms a vdW field is built from —
    ``labels`` restricts to a subset (a frame's atoms, a region)."""
    names = (
        list(scene.atoms)
        if labels is None
        else [la for la in labels if la in scene.atoms]
    )
    table = {**VDW_RADII_A, **(radii or {})}
    carts = np.asarray([_cart(scene, la) for la in names], dtype=float).reshape(-1, 3)
    rad = np.asarray(
        [table.get(scene.atoms[la].element, VDW_DEFAULT_A) for la in names],
        dtype=float,
    )
    return names, carts, rad


def _nearest_sphere(
    carts: np.ndarray, rad: np.ndarray, pts: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """``(d, idx)`` per point: the signed distance to the union of spheres
    ``min_i(|p − c_i| − r_i)`` and the index of the sphere that attains it
    (blocked, so a many-atom field stays inside a fixed temporary)."""
    pts = np.asarray(pts, dtype=float).reshape(-1, 3)
    d = np.empty(len(pts))
    idx = np.empty(len(pts), dtype=np.intp)
    rows = max(1, _VDW_BLOCK // max(len(carts), 1))
    for k in range(0, len(pts), rows):
        dd = (
            np.linalg.norm(pts[k : k + rows, None, :] - carts[None, :, :], axis=2)
            - rad[None, :]
        )
        j = np.argmin(dd, axis=1)
        idx[k : k + rows] = j
        d[k : k + rows] = dd[np.arange(len(j)), j]
    return d, idx


def vdw_sdf(
    scene: Scene,
    pts: np.ndarray,
    *,
    labels: Sequence[str] | None = None,
    radii: dict[str, float] | None = None,
) -> np.ndarray:
    """Signed distance (Å, negative inside) from ``pts`` to the union of
    the atoms' van der Waals spheres — the field form of the boolean
    :func:`_spheres_inside`. On a channel axis it is the free radius to
    the nearest wall. ``labels`` restricts to a subset; ``radii`` overrides
    :data:`VDW_RADII_A` per element. In-cell positions only (no periodic
    image expansion, as :func:`line`)."""
    _names, carts, rad = _vdw_arrays(scene, labels, radii)
    if not len(carts):
        return np.full(len(np.asarray(pts).reshape(-1, 3)), np.inf)
    return _nearest_sphere(carts, rad, pts)[0]


def vdw_field(
    scene: Scene,
    *,
    labels: Sequence[str] | None = None,
    radii: dict[str, float] | None = None,
    frame_key: Callable[[int | None], str | None] | None = None,
) -> Any:
    """The atomic inspect provider (:class:`precis.cad.inspect.Field`, Å):
    :func:`vdw_sdf` plus attribution — the nearest atom's construction
    frame, ``FeatureRef(label=<frame key>, role='owner')``, where
    ``frame_key(maker)`` maps the atom's ``maker`` stamp to the trace
    frame's key; an atom with no ``maker`` (or no resolver) is labelled by
    its own atom label. Raises ``ValueError`` on an empty atom set."""
    from precis.cad.inspect import FeatureRef, Field

    names, carts, rad = _vdw_arrays(scene, labels, radii)
    if not len(carts):
        raise ValueError("no atoms to build a van der Waals field from")
    pad = float(rad.max())
    aabb = (carts.min(axis=0) - pad, carts.max(axis=0) + pad)

    def label_of(i: int) -> str:
        atom = scene.atoms[names[i]]
        if frame_key is not None and atom.maker is not None:
            key = frame_key(int(atom.maker))
            if key:
                return str(key)
        return names[i]

    def features_at(p: np.ndarray, _n: np.ndarray, _eps: float) -> Any:
        _d, idx = _nearest_sphere(carts, rad, np.asarray(p, dtype=float)[None, :])
        return (FeatureRef(label=label_of(int(idx[0])), role="owner"),)

    return Field(
        sdf=lambda pts: _nearest_sphere(carts, rad, pts)[0],
        features_at=features_at,
        aabb=aabb,
    )


def ray_hits(
    scene: Scene,
    origin: np.ndarray,
    direction: np.ndarray,
    *,
    length: float,
    fields: list[str] | tuple[str, ...] | None = None,
    step: float = 0.5,
    reach: float = 4.0,
    pi_data: dict[str, Any] | None = None,
    charges: dict[str, float] | None = None,
    frame_of: Any = None,
    relax_of: Any = None,
    regions: list[tuple[str, Any]] | None = None,
    vdw_radii: dict[str, float] | None = None,
) -> RayTrace:
    """The atomic observation ray (structure-workspaces 2r) — the §5.4
    ``cast_ray`` at template fidelity over one scene, Å throughout.

    ``pass`` is :func:`line` within ``reach`` clipped to ``[0, length]``:
    the perpendicular foot's ``s``, distance ``d`` and the acute angle to
    the atom's :func:`local_normal`. Every other field samples the ray at
    ``step`` and refines each transition by bisection:

    - ``charge`` (``charges`` = stored partial charges by label; ``None``
      → the field is absent): ``q`` on each pass, and ``esp`` hits where
      the point-charge ESP changes sign, passes an extremum or moves
      :data:`ESP_STEP_V` from the last emitted value.
    - ``pi`` (``pi_data`` = the ``structure_pi`` payload): enter/leave an
      aromatic ring's cylinder, or a ring-less conjugated system's sphere
      union (``vdw_radii`` by element, default :data:`PI_SYSTEM_RADIUS_A`).
    - ``frame`` / ``relax`` (``frame_of(label)`` / ``relax_of(label)`` →
      a hashable description or ``None``): a hit where the nearest atom
      within ``reach`` changes its value; ``None`` atoms carry nothing.
    - ``region`` (``regions`` = ``[(name, inside(points) -> bool[])]``):
      enter/leave each.

    Hits coalesce to one per ``(field, subject, event)`` — the first.
    """
    want = list(fields) if fields else list(RAY_FIELDS)
    o = np.asarray(origin, dtype=float)
    d = np.asarray(direction, dtype=float)
    dn = d / np.linalg.norm(d)
    length = float(length)
    near = [h for h in line(scene, o, dn, reach) if 0.0 <= h.along <= length]
    hits: list[RayHit] = []
    have: list[str] = []
    end = max((h.along for h in near), default=None)

    if "pass" in want:
        have.append("pass")
        for h in near:
            data: dict[str, Any] = {
                "d": h.offset,
                "angle": normal_angle(scene, h.label, dn),
                "element": h.element,
            }
            if charges is not None and h.label in charges:
                data["q"] = float(charges[h.label])
            hits.append(RayHit(h.along, "pass", "pass", h.label, data))

    n_samples = max(2, int(np.ceil(length / max(step, 1e-6))) + 1)
    s_grid = np.linspace(0.0, length, n_samples)
    labels = list(scene.atoms)
    carts = (
        np.asarray([_cart(scene, la) for la in labels], dtype=float)
        if labels
        else np.zeros((0, 3))
    )

    if "charge" in want and charges is not None:
        have.append("charge")
        q_labels = [la for la in labels if la in charges]
        if q_labels:
            qs = np.asarray([charges[la] for la in q_labels], dtype=float)
            qc = np.asarray([_cart(scene, la) for la in q_labels], dtype=float)
            pts = o[None, :] + s_grid[:, None] * dn[None, :]
            r = np.maximum(
                np.linalg.norm(pts[:, None, :] - qc[None, :, :], axis=2), 0.3
            )
            v = _COULOMB_V_A * (qs[None, :] / r).sum(axis=1)
            last: float | None = None
            for k, s in enumerate(s_grid):
                tag = None
                if 0 < k < len(v) - 1 and (v[k] - v[k - 1]) * (v[k + 1] - v[k]) < 0:
                    tag = "min" if v[k] < v[k - 1] else "max"
                elif k > 0 and v[k] * v[k - 1] < 0:
                    tag = "sign"
                elif last is not None and abs(v[k] - last) >= ESP_STEP_V:
                    tag = "step"
                if tag is None and last is not None:
                    continue
                nearest = q_labels[int(np.argmin(r[k]))]
                hits.append(
                    RayHit(
                        float(s),
                        "charge",
                        "esp",
                        f"esp@{k}",
                        {
                            "V": float(v[k]),
                            "tag": tag or "start",
                            "near": nearest,
                            "q": float(charges[nearest]),
                        },
                    )
                )
                last = float(v[k])

    if "pi" in want and pi_data and not pi_data.get("skipped") and labels:
        have.append("pi")
        rings = pi_data.get("rings") or []
        for sys_ in pi_data.get("systems") or []:
            arom = [k for k in sys_.get("aromatic_rings") or [] if k < len(rings)]
            regions_pi: list[tuple[str, Any, dict[str, Any]]] = []
            if arom:
                for k in arom:
                    ords = [i for i in rings[k]["atoms"] if i < len(labels)]
                    if len(ords) < 3:
                        continue
                    regions_pi.append(
                        (
                            f"ring:{k}",
                            _ring_inside(carts[ords]),
                            {
                                "ring": k,
                                "size": len(ords),
                                "aromatic": True,
                                "system": sys_["id"],
                            },
                        )
                    )
            else:
                ords = [i for i in sys_.get("atoms") or [] if i < len(labels)]
                if ords:
                    radii = np.asarray(
                        [
                            (vdw_radii or {}).get(
                                scene.atoms[labels[i]].element, PI_SYSTEM_RADIUS_A
                            )
                            for i in ords
                        ],
                        dtype=float,
                    )
                    regions_pi.append(
                        (
                            f"system:{sys_['id']}",
                            _spheres_inside(carts[ords], radii),
                            {
                                "system": sys_["id"],
                                "size": len(ords),
                                "aromatic": False,
                            },
                        )
                    )
            for subject, inside, facts in regions_pi:
                for s_t, ev in _transitions(inside, s_grid, o, dn):
                    hits.append(RayHit(s_t, "pi", ev, subject, dict(facts)))

    for fname, of in (("frame", frame_of), ("relax", relax_of)):
        if fname not in want or of is None or not near:
            continue
        near_l = [h.label for h in near]
        vals = {la: of(la) for la in near_l}
        if all(v is None for v in vals.values()):
            continue
        have.append(fname)
        nc = np.asarray([_cart(scene, la) for la in near_l], dtype=float)
        pts = o[None, :] + s_grid[:, None] * dn[None, :]
        dist = np.linalg.norm(pts[:, None, :] - nc[None, :, :], axis=2)
        prev: Any = None
        for k, s in enumerate(s_grid):
            j = int(np.argmin(dist[k]))
            if dist[k, j] > reach:
                continue
            la = near_l[j]
            val = vals[la]
            if val is None or val == prev:
                continue
            prev = val
            hits.append(
                RayHit(float(s), fname, "change", str(val), {"value": val, "atom": la})
            )

    if "region" in want and regions:
        have.append("region")
        for name, inside in regions:
            for s_t, ev in _transitions(inside, s_grid, o, dn):
                hits.append(RayHit(s_t, "region", ev, name, {}))

    seen: set[tuple[str, str, str]] = set()
    out: list[RayHit] = []
    for hit in sorted(hits, key=lambda x: (x.s, RAY_FIELDS.index(x.field))):
        key = (hit.field, hit.subject, hit.event)
        if key in seen:
            continue
        seen.add(key)
        out.append(hit)
    return RayTrace(out, end, [f for f in RAY_FIELDS if f in have])


@dataclass
class PlaneHit:
    """An atom within a planar slab, with its in-plane 2D coordinates."""

    label: str
    element: str
    signed: float  # Å off the plane (sign = which side)
    u: float  # Å along the first in-plane axis
    v: float  # Å along the second in-plane axis


def _plane_basis(normal: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = np.asarray(normal, dtype=float)
    n = n / np.linalg.norm(n)
    seed = np.array([1.0, 0.0, 0.0]) if abs(n[0]) < 0.9 else np.array([0.0, 1.0, 0.0])
    u = seed - (seed @ n) * n
    u = u / np.linalg.norm(u)
    v = np.cross(n, u)
    return n, u, v


def plane(
    scene: Scene,
    point: np.ndarray,
    normal: np.ndarray,
    thickness: float,
) -> list[PlaneHit]:
    """Atoms within ``thickness`` Å of a plane, as a labelled 2D map (§6.2).

    The honest, token-cheap form of a layer slice: real in-plane coordinates +
    labels, never a raster. Returned sorted by (u, v).
    """
    p0 = np.asarray(point, dtype=float)
    n, u, v = _plane_basis(normal)
    hits: list[PlaneHit] = []
    for label, atom in scene.atoms.items():
        rel = scene.cell.frac_to_cart(atom.frac) - p0
        signed = float(rel @ n)
        if abs(signed) <= thickness:
            hits.append(
                PlaneHit(label, atom.element, signed, float(rel @ u), float(rel @ v))
            )
    hits.sort(key=lambda h: (round(h.u, 3), round(h.v, 3)))
    return hits


@dataclass
class CrossingBond:
    """A bond whose segment crosses a plane (§6.2 bonds_through_plane)."""

    i: str
    j: str
    order: float
    length: float
    angle_to_normal: float  # degrees


def bonds_through_plane(
    scene: Scene,
    point: np.ndarray,
    normal: np.ndarray,
) -> list[CrossingBond]:
    """Bonds whose segment crosses the plane (§6.2) — what stitches two layers.

    Image-aware: atom ``j`` is taken in the bond's declared periodic image, so a
    bond crossing *via* a cell wall is measured at its real geometry.
    """
    p0 = np.asarray(point, dtype=float)
    n, _, _ = _plane_basis(normal)
    out: list[CrossingBond] = []
    for b in scene.bonds:
        if b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        pi = scene.cell.frac_to_cart(scene.atoms[b.i].frac)
        pj = scene.cell.frac_to_cart(scene.atoms[b.j].frac + np.array(b.image))
        si = float((pi - p0) @ n)
        sj = float((pj - p0) @ n)
        if si * sj < 0:  # endpoints on opposite sides
            vec = pj - pi
            length = float(np.linalg.norm(vec))
            cos = abs(float(vec @ n) / length) if length else 0.0
            ang = float(np.degrees(np.arccos(np.clip(cos, 0.0, 1.0))))
            out.append(CrossingBond(b.i, b.j, b.order, length, ang))
    return out


def bonds_in_sphere(
    scene: Scene,
    center: np.ndarray,
    radius: float,
) -> list[CrossingBond]:
    """Bonds inside or crossing a sphere (§6.2) — the local bonding environment.

    A bond counts if either endpoint is within ``radius`` or the segment passes
    within it. ``angle_to_normal`` is repurposed as the angle of the bond to the
    centre→midpoint direction (a cheap orientation cue)."""
    c = np.asarray(center, dtype=float)
    out: list[CrossingBond] = []
    for b in scene.bonds:
        if b.i not in scene.atoms or b.j not in scene.atoms:
            continue
        pi = scene.cell.frac_to_cart(scene.atoms[b.i].frac)
        pj = scene.cell.frac_to_cart(scene.atoms[b.j].frac + np.array(b.image))
        seg = pj - pi
        length = float(np.linalg.norm(seg))
        # closest point on the segment to the sphere centre
        t = 0.0 if length == 0 else float(np.clip((c - pi) @ seg / (length**2), 0, 1))
        closest = pi + t * seg
        if float(np.linalg.norm(closest - c)) <= radius:
            mid = (pi + pj) / 2 - c
            cos = (
                abs(float(seg @ mid) / (length * np.linalg.norm(mid)))
                if length and np.linalg.norm(mid)
                else 0.0
            )
            ang = float(np.degrees(np.arccos(np.clip(cos, 0.0, 1.0))))
            out.append(CrossingBond(b.i, b.j, b.order, length, ang))
    return out


# -- graph topology (path · rings · fragments, §6.1/§6.5) --------------------


def _adjacency(scene: Scene) -> dict[str, set[str]]:
    adj: dict[str, set[str]] = {label: set() for label in scene.atoms}
    for b in scene.bonds:
        if b.i in adj and b.j in adj:
            adj[b.i].add(b.j)
            adj[b.j].add(b.i)
    return adj


def path(scene: Scene, a: str, b: str) -> list[str] | None:
    """Shortest bond path a→b (BFS over the graph), or None if disconnected."""
    if a not in scene.atoms or b not in scene.atoms:
        return None
    adj = _adjacency(scene)
    prev: dict[str, str | None] = {a: None}
    queue = [a]
    while queue:
        cur = queue.pop(0)
        if cur == b:
            chain = [cur]
            while True:
                # indexed lookup, not the loop var, so mypy can't narrow the
                # `while` guard's None-check into the body — capture once.
                nxt = prev[chain[-1]]
                if nxt is None:
                    break
                chain.append(nxt)
            return list(reversed(chain))
        for nxt in adj[cur]:
            if nxt not in prev:
                prev[nxt] = cur
                queue.append(nxt)
    return None


def fragments(scene: Scene) -> list[list[str]]:
    """Connected components over the bond graph — the chemical fragments (§6.5).

    Returned largest-first; an isolated atom is its own one-atom fragment. The
    instrument for "did this edit fragment the structure?" and fragment-centric
    navigation.
    """
    adj = _adjacency(scene)
    seen: set[str] = set()
    comps: list[list[str]] = []
    for start in scene.atoms:
        if start in seen:
            continue
        comp: list[str] = []
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in adj[cur]:
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        comps.append(sorted(comp))
    comps.sort(key=len, reverse=True)
    return comps


def rings(scene: Scene, max_size: int = 8) -> list[list[str]]:
    """Smallest bond cycles up to ``max_size`` atoms (§6.1 — find sp² 6-rings).

    For each bonded pair, drop the edge and find the shortest alternate path;
    its length+1 is the smallest ring through that bond. Rings are deduplicated
    by their atom set, returned smallest-first.
    """
    adj = _adjacency(scene)
    found: dict[frozenset[str], list[str]] = {}
    for b in scene.bonds:
        i, j = b.i, b.j
        if i not in adj or j not in adj:
            continue
        # BFS i→j without using the direct i–j edge
        prev: dict[str, str | None] = {i: None}
        queue = [i]
        hit = False
        while queue and not hit:
            cur = queue.pop(0)
            for nxt in adj[cur]:
                if cur == i and nxt == j:
                    continue  # the edge we're testing
                if nxt == j:
                    prev[j] = cur
                    hit = True
                    break
                if nxt not in prev:
                    prev[nxt] = cur
                    queue.append(nxt)
        if not hit:
            continue
        chain = [j]
        while True:
            parent = prev.get(chain[-1])
            if parent is None:
                break
            chain.append(parent)
        ring = list(reversed(chain))
        if len(ring) <= max_size:
            found.setdefault(frozenset(ring), ring)
    return sorted(found.values(), key=len)


# -- shared π electrons (structure-workspaces 2e, §Structure fisheye) --------

#: Elements whose declared sp2/sp atom carries a lone pair that conjugates
#: when it sits on a π bond (the ``view='surface'`` acceptor rule).
_PI_LONE_PAIR = frozenset({"N", "O", "S"})

#: Perception cap — ``rings()`` is one BFS per bond, so huge slabs skip
#: perception at save rather than paying an unbounded graph walk; the row
#: records ``skipped`` so the eye can say so (never a silent absence).
PI_MAX_ATOMS = 2000


def _pi_bonds(scene: Scene) -> list[Bond]:
    """Bonds carrying π: ``kind='aromatic'`` or ``order ≥ 1.5``."""
    return [
        b
        for b in scene.bonds
        if b.kind == "aromatic"
        or (b.order >= 1.5 and b.i in scene.atoms and b.j in scene.atoms)
    ]


def pi_perceive(scene: Scene) -> dict[str, Any]:
    """Rings, conjugated systems and per-atom membership over the bond
    graph — ours, not RDKit's (the eye's canonical read; RDKit is the
    cross-check only — hexfold rims and periodic cells do not sanitise).

    A bond carries π when ``kind='aromatic'`` or ``order ≥ 1.5``; an atom
    is in a π system when it has ≥ 1 such bond, or is declared sp2/sp on
    N/O/S adjacent to a π-bonded atom (lone pair). A system is a connected
    component over that augmented graph; rings are
    :func:`rings(max_size=8)` — smallest cycles, deduplicated by atom set.

    A ring is aromatic when every member is in one π system and (size 6
    with all ring bonds aromatic/order 1.5 — the hexfold / ``from_smiles``
    case — or an isolated ring passing Hückel 4n+2 over its π electron
    count: 1 per π-bonded member, 2 per lone-pair member).

    Returns the ``structure_pi`` JSONB payload: ``{rings: [[ordinals,
    aromatic?]…], systems: [{id, atoms, aromatic_rings}], atom: {ord:
    {rings, system}}}`` — ordinals index ``scene.atoms`` order (the same
    numbering ``atoms:`` selectors and ``maker``/``touched`` use), system
    ids are 0..n sorted by lowest member ordinal so ``pi:<id>`` selectors
    are stable for identical input.
    """
    labels = list(scene.atoms)
    pos = {la: i for i, la in enumerate(labels)}
    if len(labels) > PI_MAX_ATOMS:
        return {"skipped": f"{len(labels)} atoms > {PI_MAX_ATOMS}"}

    pi_edges = _pi_bonds(scene)
    pi_adj: dict[str, set[str]] = {la: set() for la in labels}
    for b in pi_edges:
        pi_adj[b.i].add(b.j)
        pi_adj[b.j].add(b.i)
    pi_atoms = {la for la, nbrs in pi_adj.items() if nbrs}
    adj = _adjacency(scene)
    # lone-pair acceptors: declared sp2/sp N/O/S bonded to a π atom join
    # the system over their ordinary bond.
    augmented: dict[str, set[str]] = {la: set(n) for la, n in pi_adj.items()}
    for la, atom in scene.atoms.items():
        hyb = (atom.hybridization or "").lower()
        if hyb not in ("sp2", "sp") or atom.element not in _PI_LONE_PAIR:
            continue
        for nxt in adj[la]:
            if nxt in pi_atoms:
                augmented.setdefault(la, set()).add(nxt)
                augmented[nxt].add(la)
    members = pi_atoms | {
        la for la, nbrs in augmented.items() if nbrs and la not in pi_atoms
    }
    # connected components over the augmented π graph → systems
    seen: set[str] = set()
    systems: list[list[str]] = []
    for start in sorted(members):
        if start in seen:
            continue
        comp: list[str] = []
        stack = [start]
        seen.add(start)
        while stack:
            cur = stack.pop()
            comp.append(cur)
            for nxt in augmented.get(cur, ()):
                if nxt not in seen:
                    seen.add(nxt)
                    stack.append(nxt)
        systems.append(sorted(comp, key=lambda la: pos[la]))
    systems.sort(key=lambda c: pos[c[0]])

    all_rings = rings(scene, max_size=8)
    ring_ords = [sorted(pos[la] for la in r) for r in all_rings]
    bond_map: dict[frozenset[str], Bond] = {}
    for b in scene.bonds:
        bond_map[frozenset((b.i, b.j))] = b

    def _ring_electrons(ring_labels: list[str]) -> int:
        e = 0
        for la in ring_labels:
            on_pi = any(
                (b := bond_map.get(frozenset((la, m)))) is not None
                and (b.kind == "aromatic" or b.order >= 1.5)
                for m in ring_labels
                if m != la
            )
            if on_pi:
                e += 1
            elif scene.atoms[la].element in _PI_LONE_PAIR and (
                scene.atoms[la].hybridization or ""
            ).lower() in ("sp2", "sp"):
                e += 2
        return e

    def _aromatic(ring_labels: list[str], ring_ord: list[int]) -> bool:
        sys_of = next(
            (i for i, comp in enumerate(systems) if set(ring_labels) <= set(comp)),
            None,
        )
        if sys_of is None:
            return False
        ring_bonds = [
            bond_map[frozenset((a, b))]
            for a, b in zip(ring_labels, [*ring_labels[1:], ring_labels[0]])
        ]
        if len(ring_labels) == 6 and all(
            b is not None and (b.kind == "aromatic" or b.order >= 1.5)
            for b in ring_bonds
        ):
            return True
        # isolated ring: no member's π bond leaves the ring
        outside = any(
            pi_adj[la] - set(ring_labels) for la in ring_labels if la in pi_adj
        )
        return not outside and _ring_electrons(ring_labels) % 4 == 2

    ring_aromatic = [_aromatic(list(r), ring_ords[i]) for i, r in enumerate(all_rings)]
    sys_of_ord: dict[int, int] = {}
    for i, comp in enumerate(systems):
        for la in comp:
            sys_of_ord[pos[la]] = i

    atom_map: dict[str, Any] = {}
    for i, la in enumerate(labels):
        ring_idxs = [k for k, r in enumerate(ring_ords) if i in r]
        if not ring_idxs and i not in sys_of_ord:
            continue
        atom_map[str(i)] = {"rings": ring_idxs, "system": sys_of_ord.get(i)}
    return {
        "rings": [
            {"atoms": r, "aromatic": bool(a)} for r, a in zip(ring_ords, ring_aromatic)
        ],
        "systems": [
            {
                "id": i,
                "atoms": [pos[la] for la in comp],
                "aromatic_rings": [
                    k
                    for k, r in enumerate(ring_ords)
                    if ring_aromatic[k] and set(r) <= {pos[la] for la in comp}
                ],
            }
            for i, comp in enumerate(systems)
        ],
        "atom": atom_map,
    }


def pi_crosscheck(scene: Scene, perceived: dict[str, Any]) -> dict[str, Any]:
    """RDKit cross-check of :func:`pi_perceive` — read-only, canonical
    answer stays ours. Runs only on a finite scene ≤ 500 atoms with rdkit
    installed (the ``[chem]`` extra); anything else is ``skipped`` with
    the reason. ``agrees`` when RDKit's aromatic-ring atom sets match
    ours, ``differs`` naming the ring ordinals when not — the eye surfaces
    that as the ``pi.perception_differs`` finding, never a silent override.
    """
    if any(scene.cell.pbc):
        return {"rdkit": "skipped", "why": "periodic cell"}
    if len(scene.atoms) > 500:
        return {"rdkit": "skipped", "why": f"{len(scene.atoms)} atoms > 500"}
    try:
        from rdkit import Chem
    except ImportError:
        return {"rdkit": "skipped", "why": "rdkit not installed"}
    labels = list(scene.atoms)
    pos = {la: i for i, la in enumerate(labels)}
    try:
        rw = Chem.RWMol()
        for la in labels:
            rw.AddAtom(Chem.Atom(scene.atoms[la].element))
        _BT = {
            "aromatic": Chem.BondType.AROMATIC,
            "pairwise": Chem.BondType.SINGLE,
        }
        for b in scene.bonds:
            if b.i not in pos or b.j not in pos or b.image != (0, 0, 0):
                continue
            bt = _BT.get(b.kind, Chem.BondType.SINGLE)
            if b.kind != "aromatic":
                if b.order >= 3:
                    bt = Chem.BondType.TRIPLE
                elif b.order >= 1.9:
                    bt = Chem.BondType.DOUBLE
                elif b.order >= 1.4:
                    bt = Chem.BondType.AROMATIC
            rw.AddBond(pos[b.i], pos[b.j], bt)
        mol = rw.GetMol()
        Chem.SanitizeMol(mol)
        Chem.SetAromaticity(mol)
    except Exception as exc:
        return {"rdkit": "skipped", "why": f"rdkit could not sanitise ({exc})"}
    rdkit_aromatic: set[frozenset[int]] = set()
    for ring in mol.GetRingInfo().AtomRings():
        if len(ring) > 8:
            continue
        bonds_ok = all(
            mol.GetBondBetweenAtoms(a, b) is not None
            and mol.GetBondBetweenAtoms(a, b).GetIsAromatic()
            for a, b in zip(ring, [*ring[1:], ring[0]])
        )
        if bonds_ok:
            rdkit_aromatic.add(frozenset(ring))
    ours = {
        frozenset(r["atoms"]) for r in perceived.get("rings") or [] if r["aromatic"]
    }
    if rdkit_aromatic == ours:
        return {"rdkit": "agrees"}
    diff = sorted(sorted(r) for r in rdkit_aromatic.symmetric_difference(ours))
    return {
        "rdkit": "differs",
        "finding": "pi.perception_differs",
        "rings": diff,
    }


def bonded_shell(
    scene: Scene, labels: list[str], *, max_hop: int = 6
) -> dict[int, list[tuple[str, Bond | None]]]:
    """BFS over the bond graph from ``labels`` — ``{hop: [(label, the bond
    that reached it, None for the focus set itself)]}``, hop 0 = the focus
    set. Periodic images ride the returned ``Bond.image``."""
    adj_bond: dict[str, list[tuple[str, Bond]]] = {la: [] for la in scene.atoms}
    for b in scene.bonds:
        if b.i in adj_bond and b.j in adj_bond:
            adj_bond[b.i].append((b.j, b))
            adj_bond[b.j].append((b.i, b))
    out: dict[int, list[tuple[str, Bond | None]]] = {0: [(la, None) for la in labels]}
    depth = {la: 0 for la in labels}
    queue = list(labels)
    while queue:
        cur = queue.pop(0)
        d = depth[cur]
        if d >= max_hop:
            continue
        for nxt, bond in adj_bond[cur]:
            if nxt in depth:
                continue
            depth[nxt] = d + 1
            out.setdefault(d + 1, []).append((nxt, bond))
            queue.append(nxt)
    for d in out:
        out[d].sort(key=lambda t: pos_order(scene, t[0]))
    return out


def pos_order(scene: Scene, label: str) -> int:
    """``label``'s ordinal in ``scene.atoms`` order (0 when absent)."""
    return next((i for i, la in enumerate(scene.atoms) if la == label), 0)


# -- diff (the most insightful single relaxation view, §6.3) -----------------


@dataclass
class StructureDiff:
    """Per-atom displacement + graph delta between two designs/versions."""

    rmsd: float
    max_disp: float
    moved: list[tuple[str, float]]  # (label, displacement Å), largest first
    bonds_broken: list[tuple[str, str]]
    bonds_formed: list[tuple[str, str]]
    atoms_added: list[str]
    atoms_removed: list[str]


def _bond_set(scene: Scene) -> set[frozenset[str]]:
    return {frozenset((b.i, b.j)) for b in scene.bonds}


def diff(before: Scene, after: Scene) -> StructureDiff:
    """Compare two scenes by atom label: displacement (MIC in ``after``'s cell),
    RMSD, and which bonds/atoms broke, formed, appeared, or vanished (§6.3)."""
    common = [la for la in before.atoms if la in after.atoms]
    moved: list[tuple[str, float]] = []
    sq = 0.0
    for la in common:
        d, _ = after.cell.mic(before.atoms[la].frac, after.atoms[la].frac)
        moved.append((la, d))
        sq += d * d
    moved.sort(key=lambda t: t[1], reverse=True)
    rmsd = float((sq / len(common)) ** 0.5) if common else 0.0
    max_disp = moved[0][1] if moved else 0.0
    b0, b1 = _bond_set(before), _bond_set(after)
    broken = [tuple(sorted(s)) for s in (b0 - b1)]
    formed = [tuple(sorted(s)) for s in (b1 - b0)]
    return StructureDiff(
        rmsd=rmsd,
        max_disp=max_disp,
        moved=moved,
        bonds_broken=[(x, y) for x, y in broken],
        bonds_formed=[(x, y) for x, y in formed],
        atoms_added=sorted(set(after.atoms) - set(before.atoms)),
        atoms_removed=sorted(set(before.atoms) - set(after.atoms)),
    )


# -- embodiment (the uniform point-of-view readout, §6.6, stateless v1) ------


@dataclass
class Pov:
    """One embodiment's uniform readout: i_am · i_include · i_touch (§6.6)."""

    i_am: str  # 'atom' | 'fragment'
    i_include: list[str]  # the support atoms
    i_touch: list[tuple[str, float]]  # (label, distance Å) outside the support


def pov(scene: Scene, support: list[str], reach: float = 3.0) -> Pov:
    """The §6.6 embodiment readout over an atom or a fragment support.

    Stateless v1: the support is given explicitly (a single atom or a set —
    e.g. a fragment from ``fragments()`` or a ring from ``rings()``). The
    *persisted, named* eye with a bookmark stack is the stateful refinement
    (§6.8, vision). ``i_touch`` = atoms within ``reach`` Å of any support atom,
    excluding the support itself, nearest-first.
    """
    sset = set(support)
    touch: dict[str, float] = {}
    for member in support:
        if member not in scene.atoms:
            continue
        for other, _img, dist in scene.neighbors(member, reach):
            if other in sset:
                continue
            touch[other] = min(touch.get(other, dist), dist)
    ordered = sorted(touch.items(), key=lambda t: t[1])
    return Pov(
        i_am="atom" if len(support) == 1 else "fragment",
        i_include=list(support),
        i_touch=ordered,
    )


def toc(scene: Scene) -> dict[str, object]:
    """The structure summary: cell, pbc, composition, per-element counts.

    The symmetry-orbit collapse (spglib) is a later increment; v1 groups by
    element + the fixed flag, which is the cheap legible reduction.
    """
    comp = scene.composition()
    formula = "".join(f"{el}{comp[el]}" for el in sorted(comp))
    return {
        "formula": formula,
        "natoms": len(scene.atoms),
        "composition": comp,
        "pbc": scene.cell.pbc,
        "volume": scene.cell.volume,
        "nbonds": len(scene.bonds),
        "nfragments": len(fragments(scene)),
    }
