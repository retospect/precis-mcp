"""The ``smooth_drum`` generator (docs/backlog/precis-surface-kernel.md
"Slice -- smooth drum"): the "carbon wrapper" -- a sp2 carbon sheet that
rises through a catenoid foot into a zigzag ``(n, 0)`` stalk, flares back
out through a second catenoid onto the floor of a wider zigzag ``(w, 0)``
drum, and closes over a flat lid, with filleted corners picked from the
C60-first icosahedral table (:mod:`hexfold.radii`).

**Pipeline** (each stage is its own tested module; this one only wires
them and gates the result): :func:`precis_surface.revolution.drum_meridian`
(the smooth target, a surface of revolution) ->
:func:`precis_surface.rowfit.fit_rows` / :func:`~precis_surface.rowfit.realise`
(a hex-lattice net on that target carrying exactly the planned 12
pentagons and 12 heptagons, as the dual of a row-lofted triangulation) ->
:func:`precis_surface.relax.relax_net` (harmonic bonds + 1-3 springs + an
umbrella term and a weak normal-only tether to the smooth target, FIRE)
-> :class:`GeneratedBlock`. Bond order is the same
Pauling 4/3 ``"aromatic"`` estimate :mod:`.tpms` and the sp2 families use.

**Gates, not reports.** After relaxation the block is refused
(:class:`GeneratorError`) if the largest POAV1 pyramidalisation exceeds
C60's (:data:`hexfold.radii.THETA_P_C60_DEG`) or the mean C--C bond leaves
tpms's carbon window -- geometry that is not carbon must not be emitted.
Per-bond spread and the pyramidalisation maximum are reported in
``topology``.

**Frame.** Like the other generators the atoms sit in the block's local
frame: x/y centred on the drum axis, z shifted so the lowest atom (the
sheet's rim) is at ``z = 0``, matching the DSL ``cyl`` primitive's base at
``z = 0``. ``topology["surface_meridian"]`` is the smooth target in this
*same* shifted frame, as ``[r_A, z_A]`` pairs about 0.5 Å apart: revolving
it about the z axis draws the target surface over the atoms.
"""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

import numpy as np

from hexfold import radii
from hexfold.lattice import tube_radius
from precis_se.atomic.generators._types import (
    GeneratedBlock,
    GeneratedPort,
    GeneratorError,
    fmt_length_A,
)
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A
from precis_se.atomic.generators.tpms import _MEAN_BOND_MAX_A, _MEAN_BOND_MIN_A
from precis_surface import revolution as rv
from precis_surface import rowfit
from precis_surface.relax import relax_net, theta_p_deg

#: Same delocalized-sp2 Pauling estimate as :mod:`.tpms` / the sp2 families.
_BOND_ORDER = 4.0 / 3.0

_SIGMA_CC_A = 1.42
_EDGE_A = math.sqrt(3.0) * _SIGMA_CC_A

#: The narrowest stalk the meridian machinery carries: below it the foot
#: catenoid's cut radius swallows the stalk (measured, see the tests).
_MIN_NECK = 6

#: Generator-geometry sanity cap, mirroring tpms's replica cap -- not a
#: physical limit; keeps a typo (sheet_radius_A=4000) from running away.
_MAX_ATOMS = 40000

#: Arc-length spacing of the reported target meridian (Å).
_MERIDIAN_DS_A = 0.5

#: Relaxation budget. The default drum reaches max-force 5e-3 in a few
#: seconds; the last decade of force only shuffles soft modes that move no
#: bond by more than ~0.01 Å (measured), so the tighter relax_net default is
#: not worth the wall time here.
_RELAX_FMAX = 5e-3
_RELAX_MAX_STEPS = 4000

#: Normal-only tether of each atom to the smooth target: the row fit leaves
#: bonds 0.8-2.7 Å, and untethered the spring energy spends that strain by
#: moving atoms up to ~7 Å off the target surface. Atoms still slide along
#: the surface, so the strain relaxes tangentially. 0.01 is the measured
#: compromise on the default drum: mean off-surface 0.7 Å with bonds
#: 1.31-1.68 Å; 0.2 pins the surface (mean 0.17 Å) but stretches bonds to
#: 1.2-1.9 Å, 0.0 gives bonds 1.35-1.54 Å but a 7 Å drift. The root cause is
#: the fit being ~7 % sparser than graphene's areal density (fit bonds
#: 0.8-2.7 Å, mean 1.50), which the relaxation can only spend as shrinkage
#: or stretch.
_RELAX_K_SURFACE = 0.01
_TETHER_DS_A = 0.25


def _int_param(raw: dict[str, Any], key: str) -> int:
    if key not in raw:
        raise GeneratorError(
            f"smooth_drum needs {key!r} (int, the zigzag ({key[0]}, 0) tube index)"
        )
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise GeneratorError(f"smooth_drum {key!r} must be an integer, got {value!r}")
    if int(value) != value:
        raise GeneratorError(
            f"smooth_drum {key!r} must be an exact integer, got {value!r}"
        )
    return int(value)


def _length_param(raw: dict[str, Any], key: str, default: float) -> float:
    value = raw.get(key, default)
    if isinstance(value, bool):
        raise GeneratorError(f"smooth_drum {key!r} must be a number, got {value!r}")
    try:
        x = float(value)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"smooth_drum {key!r} must be a number, got {value!r}"
        ) from exc
    if not (x > 0.0) or not math.isfinite(x):
        raise GeneratorError(f"smooth_drum {key!r} must be > 0, got {value!r}")
    return x


def _validate(raw: dict[str, Any]) -> tuple[int, int, float, float, float, float, bool]:
    n = _int_param(raw, "neck")
    w = _int_param(raw, "wall")
    if n < _MIN_NECK:
        raise GeneratorError(
            f"smooth_drum 'neck' must be >= {_MIN_NECK} (a narrower zigzag "
            f"stalk is swallowed by the foot catenoid), got {n}"
        )
    if w <= n:
        raise GeneratorError(
            f"smooth_drum 'wall' must be > 'neck' (the drum is wider than "
            f"its stalk), got wall={w}, neck={n}"
        )
    stalk = _length_param(raw, "stalk_length_A", 10.0)
    height = _length_param(raw, "wall_height_A", 24.0)
    sheet = _length_param(raw, "sheet_radius_A", 40.0)
    min_flat = _length_param(raw, "min_flat_A", 2.46)
    relax = raw.get("relax", True)
    if not isinstance(relax, bool):
        raise GeneratorError(f"smooth_drum 'relax' must be a bool, got {relax!r}")
    return n, w, stalk, height, sheet, min_flat, relax


def build_smooth_drum(raw: dict[str, Any]) -> GeneratedBlock:
    """A smooth carbon drum on a stalk on a sheet: ``{"neck": int >= 6,
    "wall": int > neck, "stalk_length_A"?: 10, "wall_height_A"?: 24,
    "sheet_radius_A"?: 40, "min_flat_A"?: 2.46, "relax"?: True}`` (module
    docstring's pipeline and gates)."""
    n, w, stalk, height, sheet, min_flat, relax = _validate(raw)
    tag = f"smooth_drum neck={n} wall={w}"

    try:
        m = rv.drum_meridian(
            neck=tube_radius(n, 0),
            wall_radius=tube_radius(w, 0),
            stalk_length=stalk,
            wall_height=height,
            sheet_radius=sheet,
            fillet_candidates=radii.fillet_radii(),
            curvature_sum_max=radii.curvature_sum_bound(),
            min_flat=min_flat,
        )
    except ValueError as exc:
        raise GeneratorError(
            f"{tag}: {exc} -- widen 'wall' (or lengthen 'wall_height_A' / "
            "shrink 'min_flat_A'), or widen 'neck', until the smooth target exists"
        ) from exc
    try:
        rows = rowfit.fit_rows(m, {"stalk": n, "wall": w}, edge=_EDGE_A)
        fit = rowfit.realise(m, rows)
    except ValueError as exc:
        raise GeneratorError(
            f"{tag}: row fit failed: {exc} -- the planned defect rows do not "
            "close for these radii and lengths; try a wider neck, a taller "
            "wall or a different stalk_length_A"
        ) from exc

    # Interior ring census: vertex degrees of the loft, sheet-edge ring out.
    # Ruling (Reto, 2026-09-26, see .tpms): rings are {5, 6, 7} only.
    deg = fit.degree()
    interior = deg[: len(deg) - fit.counts[-1]] if fit.counts[-1] > 0 else deg
    rings = dict(sorted(Counter(int(x) for x in interior).items()))
    bad_rings = {k: v for k, v in rings.items() if k not in (5, 6, 7)}
    if bad_rings:
        raise GeneratorError(
            f"{tag}: the row fit leaves ring(s) outside {{5,6,7}}: {bad_rings} "
            f"(full histogram {rings}) -- defect rows collided for these radii; "
            "try a wider neck (the measured clean cases have neck >= 10) or a "
            "different stalk_length_A / wall_height_A"
        )
    pentagons = rings.get(5, 0)
    heptagons = rings.get(7, 0)

    if len(fit.atoms) > _MAX_ATOMS:
        raise GeneratorError(
            f"{tag}: {len(fit.atoms)} atoms exceeds the generator cap "
            f"{_MAX_ATOMS} -- reduce 'sheet_radius_A' or the wall size"
        )

    bonds_arr = fit.bonds
    info: dict[str, float | int | bool] = {}
    coords = fit.atoms
    if relax:
        coords, info = relax_net(
            fit.atoms,
            bonds_arr,
            surface=m.sample(_TETHER_DS_A)[0],
            k_surface=_RELAX_K_SURFACE,
            fmax=_RELAX_FMAX,
            max_steps=_RELAX_MAX_STEPS,
        )

    d = np.linalg.norm(coords[bonds_arr[:, 0]] - coords[bonds_arr[:, 1]], axis=1)
    bond_mean = float(d.mean())
    if not (_MEAN_BOND_MIN_A <= bond_mean <= _MEAN_BOND_MAX_A):
        raise GeneratorError(
            f"{tag}: mean C-C bond {bond_mean:.3f} Å is outside the carbon "
            f"window [{_MEAN_BOND_MIN_A}, {_MEAN_BOND_MAX_A}] -- not carbon"
        )
    theta_max = float(theta_p_deg(coords, bonds_arr).max())
    if theta_max > radii.THETA_P_C60_DEG:
        raise GeneratorError(
            f"{tag}: pyramidalisation theta_p max {theta_max:.2f}° exceeds "
            f"C60's {radii.THETA_P_C60_DEG}° -- too strained to emit as "
            "carbon; widen the neck/wall or lengthen the drum"
            + ("" if relax else " (relax=False leaves the raw fitted net)")
        )

    # Frame: x/y already on the axis; lowest atom at z = 0 (cyl base).
    dz = -float(coords[:, 2].min())
    coords = coords.copy()
    coords[:, 2] += dz
    r_atoms = np.hypot(coords[:, 0], coords[:, 1])
    envelope = (
        f"cyl:r{fmt_length_A(float(r_atoms.max()) + VDW_MARGIN_A)}"
        f"h{fmt_length_A(float(coords[:, 2].max()))}"
    )

    meridian_pts, _tilt, _seg = m.sample(_MERIDIAN_DS_A)
    surface_meridian = [
        [round(float(r), 3), round(float(z) + dz, 3)] for r, z in meridian_pts
    ]

    # One ring port over the sheet's whole open edge, ordered by angle --
    # the hexfold ``s_rim`` convention (GeneratedPort.atoms): 99 single-atom
    # ports read as 99 identical unconnected_port warnings (prod dogfood
    # 2026-10-02). The edge is in the sheet plane, so the port faces
    # radially outward; ``direction`` is that of atom 0.
    coord = np.bincount(bonds_arr.ravel(), minlength=len(coords))
    rim = np.flatnonzero(coord == 2)
    rim = rim[np.argsort(np.arctan2(coords[rim, 1], coords[rim, 0]), kind="stable")]
    ports: list[GeneratedPort] = []
    if len(rim):
        a0 = int(rim[0])
        radial = np.array([coords[a0, 0], coords[a0, 1], 0.0])
        norm = float(np.linalg.norm(radial))
        direction = radial / norm if norm > 1e-9 else np.array([1.0, 0.0, 0.0])
        ports.append(
            GeneratedPort(
                name="rim",
                atom_index=a0,
                direction=[float(x) for x in direction],
                roles=["covalent", "sp2-rim"],
                expected_element="C",
                atoms=[int(a) for a in rim],
            )
        )

    n_atoms = len(coords)
    bonds = [(int(i), int(j), _BOND_ORDER, "aromatic") for i, j in bonds_arr.tolist()]
    topology: dict[str, Any] = {
        "family": "smooth_drum",
        "neck": [n, 0],
        "wall": [w, 0],
        "fillet_radius_A": round(float(m.fillet_radius or 0.0), 4),
        "rings": rings,
        "pentagons": pentagons,
        "heptagons": heptagons,
        "n_atoms": n_atoms,
        "n_bonds": len(bonds),
        "bond_mean_A": round(bond_mean, 4),
        "bond_min_A": round(float(d.min()), 4),
        "bond_max_A": round(float(d.max()), 4),
        "theta_p_max_deg": round(theta_max, 3),
        "relaxed": relax,
        "relax_steps": int(info.get("steps", 0)),
        "surface_meridian": surface_meridian,
    }
    provenance = (
        f"Smooth carbon drum, zigzag stalk ({n},0) on wall ({w},0), stalk "
        f"{stalk:g} Å, wall height {height:g} Å, sheet radius {sheet:g} Å, "
        f"fillet {topology['fillet_radius_A']} Å (C60-first icosahedral "
        "table) -- surface of revolution (catenoid bends, table fillets) -> "
        "row-lofted triangulation with the planned defect rows -> dual "
        f"hex net ({pentagons} pentagons, {heptagons} heptagons; "
        "docs/backlog/precis-surface-kernel.md 'Slice -- smooth drum') -> "
        + (
            "spring + umbrella FIRE relaxation (precis_surface.relax). "
            if relax
            else "NOT relaxed (relax=False): raw fitted net. "
        )
        + f"{n_atoms} atoms, {len(bonds)} bonds, one sheet-rim ring "
        f"port over {len(rim)} edge atoms; bonds {topology['bond_min_A']}-{topology['bond_max_A']} Å "
        f"(mean {topology['bond_mean_A']}), theta_p max {topology['theta_p_max_deg']}°."
    )

    return GeneratedBlock(
        envelope=envelope,
        ports=ports,
        topology=topology,
        provenance=provenance,
        elements=["C"] * n_atoms,
        coords=coords,
        bonds=bonds,
    )
