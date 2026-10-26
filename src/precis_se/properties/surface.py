"""A posed molecule's surface, painted with properties — slice 1 of
docs/backlog/pocket-from-molecule.md, read as ``get(kind='se',
view='surface')``.

**What a point carries.** Position and outward normal (se world frame, Å),
the atom whose sphere it lies on, the electrostatic potential (V) of a
point-charge sum over GFN2 Mulliken charges, an H-bond class (``donor`` on
H bonded to N/O, ``acceptor`` on N/O with a lone pair, ``none``) with the
atom's H-bond direction, and a polarity class (``polar`` on N/O/F and H
bonded to them, else ``nonpolar``). Classes are per atom and copied onto
that atom's points; slice 2 clusters points into patches.

**Surface.** A union of scaled van der Waals spheres (Bondi radii, the
table :mod:`precis_se.atomic.clearance` measures gaps with), sampled per
atom on a Fibonacci sphere and kept where no other sphere covers the
point. "Nearest atom" is the owning sphere — the atom minimising
distance minus radius — not the Euclidean nearest centre, which on a
small sphere beside a large one can differ. The settings are Reto's
2026-10-25 ruling (spec open question 4;
:data:`SURFACE_SETTINGS`): change them there, and per read through
``args.scale``/``args.density``.

**Charge and spin.** ``charge`` (e) and ``multiplicity`` (2S+1) go to
tblite through :func:`precis.structure.relax._xtb_calculator`. When the
caller omits them, charge is the sum of the atoms' declared formal
charges and multiplicity the lowest the electron count allows; the read
labels both as defaults, because the lowest spin is wrong for a
high-spin ground state (O2 is a triplet). GFN2 here is not
spin-polarised: multiplicity sets the occupations, and tblite returns no
per-atom spin population, which the read says instead of inventing one.

**On read, not stored.** A GFN2 single point on a small molecule is
milliseconds, so nothing is cached; :data:`SURFACE_MAX_ATOMS` bounds the
read. tblite is the ``[estimate]`` extra with no linux-aarch64 PyPI
wheel; without it the view is ``Unsupported``, never a traceback, and
the read is not dispatched to a compute node.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, replace
from typing import Any

import numpy as np
from numpy.typing import NDArray

from precis.cad.vec import as_vec3 as cad_as_vec3
from precis.cad.vec import pose as cad_pose
from precis.errors import BadInput, Unsupported
from precis.format import render_agent_table
from precis.structure import elements as struct_elements
from precis.structure.relax import (
    RelaxUnsupported,
    _xtb_calculator,
    check_charge_multiplicity,
)
from precis_se.atomic.clearance import VDW_DEFAULT_A, VDW_RADII_A
from precis_se.atomic.validate import _A_TO_M, _M_TO_A

#: Coulomb's constant in eV·Å/e², so q (e) / r (Å) sums to volts.
COULOMB_EV_A = 14.399645

#: The read refuses a bound structure larger than this many atoms: a GFN2
#: single point and an all-pairs sphere test run on every read.
SURFACE_MAX_ATOMS = 200

#: Fewer points than this on one atom's sphere make a ragged patch.
MIN_POINTS_PER_ATOM = 12

#: Elements whose surface reads ``polar``, and the two an H donates from.
POLAR_ELEMENTS = frozenset({"N", "O", "F"})
DONOR_PARTNERS = frozenset({"N", "O"})
#: Valence electrons of the acceptor candidates (the lone-pair rule).
_VALENCE = {"N": 5, "O": 6}


@dataclass(frozen=True)
class SurfaceSettings:
    """How the surface is drawn. ``probe_A`` is ``None`` for a plain vdW
    surface; a solvent-excluded surface (a probe rolled over the spheres)
    is not built."""

    surface: str = "vdw"
    radii: str = "bondi"
    scale: float = 1.0
    density_per_A2: float = 3.0
    probe_A: float | None = None

    def describe(self) -> str:
        return (
            f"{self.radii} vdW spheres × {self.scale:g}, "
            f"{self.density_per_A2:g} points/Å², no probe"
        )


#: The surface settings, official since Reto's 2026-10-25 ruling (spec
#: open question 4, decided; reasoning in
#: docs/backlog/pocket-from-molecule.md): the contact surface itself
#: (scale 1.0, so slice 2's clearance is the only offset), no probe, and
#: 3 points/Å² (about 0.6 Å spacing, finer than H-bond geometry; 123
#: points on NO, 110 on water). Change these values here; nothing else
#: hard-codes them.
SURFACE_SETTINGS = SurfaceSettings()


@dataclass(frozen=True)
class AtomClass:
    """One atom's H-bond class, its unit direction (``None`` when the
    geometry gives none) and its polarity class."""

    hbond: str
    direction: NDArray[np.float64] | None
    polarity: str


@dataclass
class PaintedSurface:
    """Surface points with their properties, plus the per-atom classes
    they were copied from."""

    labels: list[str]
    elements: list[str]
    atom_charges: NDArray[np.float64]
    classes: list[AtomClass]
    points: NDArray[np.float64]
    normals: NDArray[np.float64]
    owner: NDArray[np.int64]
    esp_V: NDArray[np.float64]
    settings: SurfaceSettings


@dataclass(frozen=True)
class Gfn2SinglePoint:
    energy_eV: float
    charges: NDArray[np.float64]
    spin_population: NDArray[np.float64] | None


def vdw_radii(elements: list[str], scale: float) -> NDArray[np.float64]:
    return np.asarray(
        [VDW_RADII_A.get(e, VDW_DEFAULT_A) * scale for e in elements], dtype=float
    )


def _fibonacci_sphere(n: int) -> NDArray[np.float64]:
    k = np.arange(n, dtype=float) + 0.5
    z = 1.0 - 2.0 * k / n
    r = np.sqrt(np.clip(1.0 - z * z, 0.0, None))
    phi = k * math.pi * (3.0 - math.sqrt(5.0))
    return np.stack([r * np.cos(phi), r * np.sin(phi), z], axis=1)


def sample_surface(
    coords: NDArray[np.float64], radii: NDArray[np.float64], density_per_A2: float
) -> tuple[NDArray[np.float64], NDArray[np.float64], NDArray[np.int64]]:
    """``(points, outward normals, owning atom index)`` on the union of
    spheres ``radii`` about ``coords``."""
    pts: list[NDArray[np.float64]] = []
    nrm: list[NDArray[np.float64]] = []
    own: list[NDArray[np.int64]] = []
    for i, (centre, radius) in enumerate(zip(coords, radii, strict=True)):
        n = max(
            MIN_POINTS_PER_ATOM, math.ceil(density_per_A2 * 4 * math.pi * radius**2)
        )
        unit = _fibonacci_sphere(n)
        cand = centre + radius * unit
        dist = np.linalg.norm(cand[:, None, :] - coords[None, :, :], axis=2)
        covered = dist < radii[None, :] - 1e-9
        covered[:, i] = False
        keep = ~covered.any(axis=1)
        pts.append(cand[keep])
        nrm.append(unit[keep])
        own.append(np.full(int(keep.sum()), i, dtype=np.int64))
    if not pts:
        return np.zeros((0, 3)), np.zeros((0, 3)), np.zeros(0, dtype=np.int64)
    return np.concatenate(pts), np.concatenate(nrm), np.concatenate(own)


def _unit(v: NDArray[np.float64]) -> NDArray[np.float64] | None:
    norm = float(np.linalg.norm(v))
    return None if norm < 1e-6 else v / norm


def classify_atoms(
    elements: list[str], coords: NDArray[np.float64], formal_charges: list[int]
) -> list[AtomClass]:
    """Per-atom H-bond and polarity classes from the bond graph (pairs
    within :func:`precis.structure.elements.bond_cutoff`).

    - ``donor``: H bonded to N or O; direction X→H.
    - ``acceptor``: N or O with a lone pair, counted as valence electrons
      minus formal charge minus bonded neighbours ≥ 2 (NH4+ and a nitro
      N+ have none; NO's N and O, water's O and a pyridine N do). The
      count ignores pi delocalisation, so a pyrrole or amide N reads as an
      acceptor. Direction: away from the neighbours (the negated sum of
      unit bond vectors), ``None`` for a bare or symmetric atom.
    - ``polar``: N, O, F and H bonded to them.
    """
    n = len(elements)
    neighbours: list[list[int]] = [[] for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            cutoff = struct_elements.bond_cutoff(elements[i], elements[j])
            if float(np.linalg.norm(coords[i] - coords[j])) < cutoff:
                neighbours[i].append(j)
                neighbours[j].append(i)
    out: list[AtomClass] = []
    for i, el in enumerate(elements):
        nbrs = neighbours[i]
        if el == "H":
            partners = [j for j in nbrs if elements[j] in DONOR_PARTNERS]
            polar = any(elements[j] in POLAR_ELEMENTS for j in nbrs)
            if partners:
                x = min(
                    partners, key=lambda j: float(np.linalg.norm(coords[j] - coords[i]))
                )
                out.append(AtomClass("donor", _unit(coords[i] - coords[x]), "polar"))
            else:
                out.append(AtomClass("none", None, "polar" if polar else "nonpolar"))
            continue
        polarity = "polar" if el in POLAR_ELEMENTS else "nonpolar"
        if el in _VALENCE and _VALENCE[el] - formal_charges[i] - len(nbrs) >= 2:
            away = np.zeros(3)
            for j in nbrs:
                bond = _unit(coords[j] - coords[i])
                if bond is not None:
                    away -= bond
            out.append(AtomClass("acceptor", _unit(away), polarity))
            continue
        out.append(AtomClass("none", None, polarity))
    return out


def esp_at(
    points: NDArray[np.float64],
    coords: NDArray[np.float64],
    charges: NDArray[np.float64],
) -> NDArray[np.float64]:
    """Point-charge electrostatic potential (V) at ``points``."""
    if not len(points):
        return np.zeros(0)
    dist = np.linalg.norm(points[:, None, :] - coords[None, :, :], axis=2)
    return np.asarray(COULOMB_EV_A * (charges[None, :] / dist).sum(axis=1), dtype=float)


def paint_surface(
    labels: list[str],
    elements: list[str],
    coords: NDArray[np.float64],
    atom_charges: NDArray[np.float64],
    formal_charges: list[int],
    settings: SurfaceSettings = SURFACE_SETTINGS,
) -> PaintedSurface:
    """The surface of a molecule whose per-atom charges are already known
    (no backend needed — :func:`gfn2_single_point` supplies them on read)."""
    radii = vdw_radii(elements, settings.scale)
    points, normals, owner = sample_surface(coords, radii, settings.density_per_A2)
    return PaintedSurface(
        labels=labels,
        elements=elements,
        atom_charges=np.asarray(atom_charges, dtype=float),
        classes=classify_atoms(elements, coords, formal_charges),
        points=points,
        normals=normals,
        owner=owner,
        esp_V=esp_at(points, coords, np.asarray(atom_charges, dtype=float)),
        settings=settings,
    )


def gfn2_single_point(
    elements: list[str],
    coords: NDArray[np.float64],
    *,
    charge: int,
    multiplicity: int,
    spin_polarization: bool = False,
) -> Gfn2SinglePoint:
    """GFN2-xTB energy and Mulliken charges of an isolated molecule.
    Raises :class:`~precis.structure.relax.RelaxUnsupported` without
    tblite, ``ValueError`` on a charge/multiplicity that does not fit the
    atoms. ``spin_population`` is tblite's ``magmoms`` result when it sets
    one; the default non-spin-polarised GFN2 does not
    (``spin_polarization=True`` turns it on — open question 12)."""
    from ase import Atoms

    check_charge_multiplicity(elements, charge, multiplicity)
    calc = _xtb_calculator(
        charge=charge, multiplicity=multiplicity, spin_polarization=spin_polarization
    )
    atoms = Atoms(symbols=elements, positions=coords)
    atoms.calc = calc
    energy = float(atoms.get_potential_energy())
    charges = np.asarray(atoms.get_charges(), dtype=float)
    magmoms = calc.results.get("magmoms")
    return Gfn2SinglePoint(
        energy_eV=energy,
        charges=charges,
        spin_population=None if magmoms is None else np.asarray(magmoms, dtype=float),
    )


# ── the se read ────────────────────────────────────────────────────────


def _int_arg(args: dict[str, Any], key: str) -> int | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise BadInput(f"view='surface': args.{key} must be an integer, got {value!r}")
    return value


def _float_arg(args: dict[str, Any], key: str, lo: float, hi: float) -> float | None:
    value = args.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise BadInput(f"view='surface': args.{key} must be a number, got {value!r}")
    if not lo < float(value) <= hi:
        raise BadInput(f"view='surface': args.{key} must be in ({lo:g}, {hi:g}]")
    return float(value)


def _molecule_coords(node: Any, scene: Any) -> NDArray[np.float64]:
    """The bound scene as one isolated molecule in the se world frame (Å):
    periodic images are unwrapped about the first atom, then the block's
    ``pose``/``rot`` applied, as :func:`precis_se.atomic.validate.
    _posed_atom_cloud` places atoms."""
    cell = scene.cell
    frac = np.asarray([scene.atoms[la].frac for la in scene.atoms], dtype=float)
    delta = frac - frac[0]
    periodic = np.asarray(cell.pbc, dtype=bool)
    delta[:, periodic] -= np.round(delta[:, periodic])
    if periodic.any() and float(np.abs(delta[:, periodic]).max()) >= 0.45:
        raise BadInput(
            "view='surface' needs an isolated molecule: the bound structure "
            "spans about half its periodic cell or more",
            next="bind a molecule in a non-periodic or large vacuum box",
        )
    local_A = np.asarray([cell.frac_to_cart(f) for f in frac[0] + delta])
    placement = cad_pose(cad_as_vec3(node.pose), cad_as_vec3(node.rot))
    return np.asarray(
        [
            np.asarray(placement.apply(cad_as_vec3([c * _A_TO_M for c in p])), float)
            * _M_TO_A
            for p in local_A
        ],
        dtype=float,
    )


def _round(v: Any, nd: int = 4) -> Any:
    if v is None:
        return None
    return [round(float(x), nd) for x in v]


def render_surface(store: Any, node: Any, args: dict[str, Any]) -> str:
    """``view='surface'`` on one structure-bound block (module docstring).
    ``args``: ``charge``, ``multiplicity``, ``scale``, ``density``,
    ``format`` (``'text'`` default | ``'json'`` — the full point payload
    the se 3D viewer leaf takes)."""
    fmt = str(args.get("format") or "text").strip().lower()
    if fmt not in ("text", "json"):
        raise BadInput(
            f"view='surface': args.format must be 'text' or 'json', got {fmt!r}",
            options=["text", "json"],
        )
    if node.template is not None or node.array is not None:
        raise BadInput(
            f"view='surface': {node.name!r} is a template instance or array",
            next="read the template block itself",
        )
    if node.bound_kind != "structure" or not node.bound:
        raise BadInput(
            f"view='surface': block {node.name!r} binds no structure design",
            next="bind_structure or generate a molecule onto the block first",
        )
    ref = store.get_ref(kind="structure", id=node.bound)
    if ref is None:
        raise BadInput(
            f"view='surface': bound structure {node.bound!r} is missing",
            next="re-bind the block (bind_structure)",
        )
    scene, _handles = store.structure_load(ref.id)
    labels = list(scene.atoms)
    if not labels:
        raise BadInput(f"view='surface': structure {node.bound!r} has no atoms")
    if len(labels) > SURFACE_MAX_ATOMS:
        raise BadInput(
            f"view='surface': {len(labels)} atoms is over the "
            f"{SURFACE_MAX_ATOMS}-atom read limit (a GFN2 single point runs "
            "on every read)",
            next="cut the molecule or cluster you need onto its own block",
        )
    elements = [scene.atoms[la].element for la in labels]
    formal = [int(scene.atoms[la].charge or 0) for la in labels]
    settings = SURFACE_SETTINGS
    scale = _float_arg(args, "scale", 0.5, 3.0)
    density = _float_arg(args, "density", 0.0, 20.0)
    if scale is not None:
        settings = replace(settings, scale=scale)
    if density is not None:
        settings = replace(settings, density_per_A2=density)

    charge = _int_arg(args, "charge")
    charge_src = "args.charge"
    if charge is None:
        charge, charge_src = sum(formal), "sum of declared atom charges"
    multiplicity = _int_arg(args, "multiplicity")
    try:
        electrons = check_charge_multiplicity(elements, charge, multiplicity)
    except ValueError as exc:
        raise BadInput(
            f"view='surface': {exc}", next="pass args.charge / args.multiplicity"
        ) from exc
    mult_src = "args.multiplicity"
    if multiplicity is None:
        multiplicity = 1 if electrons % 2 == 0 else 2
        mult_src = (
            f"default: lowest spin for {electrons} electrons — pass "
            "args.multiplicity for a high-spin ground state such as O2"
        )
    coords = _molecule_coords(node, scene)
    try:
        sp = gfn2_single_point(
            elements, coords, charge=charge, multiplicity=multiplicity
        )
    except RelaxUnsupported as exc:
        raise Unsupported(
            f"view='surface' needs GFN2-xTB in this process: {exc}",
            next="read it where tblite is installed (pip install "
            "'precis-mcp[estimate]'); this read is not dispatched to a compute node",
        ) from exc
    except RuntimeError as exc:  # ase CalculationFailed / InputError
        raise BadInput(
            f"view='surface': the GFN2 single point failed: {exc}",
            next="check args.charge and args.multiplicity against the molecule",
        ) from exc
    surf = paint_surface(labels, elements, coords, sp.charges, formal, settings)
    spin_note = (
        "per-atom spin population: not returned — this GFN2 run is not "
        "spin-polarised, so tblite carries occupations but no spin density"
        if sp.spin_population is None
        else "per-atom spin population: tblite magmoms"
    )
    method = "GFN2-xTB (tblite), single point on the stored geometry, 1000 K electronic temperature"
    if fmt == "json":
        return json.dumps(
            {
                "block": node.name,
                "structure": node.bound,
                "frame": "se world (block pose applied)",
                "units": {"length": "Å", "esp": "V", "charge": "e", "energy": "eV"},
                "method": method,
                "charge": charge,
                "charge_source": charge_src,
                "multiplicity": multiplicity,
                "multiplicity_source": mult_src,
                "electrons": electrons,
                "energy_eV": round(sp.energy_eV, 6),
                "spin_population": _round(sp.spin_population),
                "spin_note": spin_note,
                "settings": {
                    "surface": settings.surface,
                    "radii": settings.radii,
                    "scale": settings.scale,
                    "density_per_A2": settings.density_per_A2,
                    "probe_A": settings.probe_A,
                },
                "atoms": [
                    {
                        "label": la,
                        "element": el,
                        "charge_e": round(float(q), 4),
                        "hbond": c.hbond,
                        "hbond_dir": _round(c.direction),
                        "polarity": c.polarity,
                    }
                    for la, el, q, c in zip(
                        labels, elements, sp.charges, surf.classes, strict=True
                    )
                ],
                "points": [
                    {
                        "pos": _round(p),
                        "normal": _round(nv),
                        "atom": int(i),
                        "esp_V": round(float(v), 4),
                        "hbond": surf.classes[i].hbond,
                        "hbond_dir": _round(surf.classes[i].direction),
                        "polarity": surf.classes[i].polarity,
                    }
                    for p, nv, i, v in zip(
                        surf.points, surf.normals, surf.owner, surf.esp_V, strict=True
                    )
                ],
            },
            ensure_ascii=False,
        )
    rows = []
    for i, (la, el) in enumerate(zip(labels, elements, strict=True)):
        mine = surf.esp_V[surf.owner == i]
        rows.append(
            {
                "atom": la,
                "el": el,
                "q (e)": f"{sp.charges[i]:+.3f}",
                "hbond": surf.classes[i].hbond,
                "polarity": surf.classes[i].polarity,
                "points": len(mine),
                "esp min (V)": f"{mine.min():+.3f}" if len(mine) else "—",
                "esp mean (V)": f"{mine.mean():+.3f}" if len(mine) else "—",
                "esp max (V)": f"{mine.max():+.3f}" if len(mine) else "—",
            }
        )
    return "\n".join(
        [
            f"# molecule surface — block {node.name!r} (structure:{node.bound})",
            "",
            f"method: {method}; energy {sp.energy_eV:.4f} eV",
            f"charge: {f'{charge:+d}' if charge else '0'} ({charge_src}); "
            f"multiplicity: {multiplicity} "
            f"({mult_src}); {electrons} electrons",
            spin_note,
            f"surface: {settings.describe()}",
            f"frame: se world (block pose applied), Å; {len(surf.points)} points; "
            f"ESP is a point-charge sum over the GFN2 charges",
            "",
            render_agent_table(
                rows,
                schema=[
                    "atom",
                    "el",
                    "q (e)",
                    "hbond",
                    "polarity",
                    "points",
                    "esp min (V)",
                    "esp mean (V)",
                    "esp max (V)",
                ],
            ),
            "",
            "every point (position, normal, atom, esp_V, hbond, hbond_dir, "
            "polarity): args={'block': ..., 'format': 'json'}",
        ]
    )
