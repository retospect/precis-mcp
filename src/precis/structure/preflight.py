"""The MLIP preflight gate — a fast, element-agnostic structural sanity check.

Sits between "the LLM proposed a catalyst candidate" and "spend an MLIP relax
on it": a synchronous, dependency-light (ASE + numpy only) gate that catches
the physically-dumb structures a relax would otherwise burn compute silently
failing on — an element the deployed model has never seen, two atoms sitting
on top of each other, an adsorbate floating untethered in the vacuum, a slab
with no headroom, a "slab" that's actually a sponge. Every element in the
catalyst screen (not just the EMT/Pd-Cu-Ni palette, :data:`relax.EMT_ELEMENTS`)
needs this — the box here is the *MLIP's* coverage, not EMT's.

Two phases: (1) an element-coverage check with no relax at all, and (2) a
cheap universal classical "settle" (:class:`_DumbField`, covalent-radii-based
repulsion + a shallow bonding well) followed by geometry judgments on the
settled positions. Every :class:`PreflightReason` collected — this never
short-circuits, so a caller can show every problem the candidate has at once.

Wired into two seams, both gated on :func:`_preflight_enabled` (env
``PRECIS_STRUCTURE_PREFLIGHT``, default OFF so this lands dark): the
structure handler's ``put``/``edit`` (a hard reject + undo — nothing persists
on a failing verdict) and ``quest.compute.dispatch_autocatpath`` (a hard
dispatch gate — no job minted on a failing substrate, plus a dead-end
logbook stamp so the proposer stops re-treading it).
"""

from __future__ import annotations

import os
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from . import elements, export
from .scene import FIX_ALL, Scene

try:  # pragma: no cover - exercised via the [dft] extra in the test env
    from ase.calculators.calculator import Calculator as _Calculator
    from ase.calculators.calculator import all_changes as _all_changes
except ImportError:  # ASE not installed — module still imports; using the
    # settle (check 2/3) without it fails earlier, in ``_scene_to_ase``.
    _Calculator = object  # type: ignore[assignment,misc]
    _all_changes = []  # type: ignore[assignment]

# ── element-in-box (the box is the *MLIP*, not EMT) ────────────────────────

#: MACE-MP-0/medium's element coverage: Z 1-89, excluding the noble gases
#: (He/Ne/Ar/Kr/Xe/Rn — MACE-MP wasn't trained on them, they don't bond).
#: TODO: source this from the active backend's actual coverage once the
#: relax rung's model selection is introspectable, rather than hardcoding
#: mace-medium's palette here.
MACE_MP_ELEMENTS: frozenset[str] = frozenset(
    {
        "H",
        "Li",
        "Be",
        "B",
        "C",
        "N",
        "O",
        "F",
        "Na",
        "Mg",
        "Al",
        "Si",
        "P",
        "S",
        "Cl",
        "K",
        "Ca",
        "Sc",
        "Ti",
        "V",
        "Cr",
        "Mn",
        "Fe",
        "Co",
        "Ni",
        "Cu",
        "Zn",
        "Ga",
        "Ge",
        "As",
        "Se",
        "Br",
        "Rb",
        "Sr",
        "Y",
        "Zr",
        "Nb",
        "Mo",
        "Tc",
        "Ru",
        "Rh",
        "Pd",
        "Ag",
        "Cd",
        "In",
        "Sn",
        "Sb",
        "Te",
        "I",
        "Cs",
        "Ba",
        "La",
        "Ce",
        "Pr",
        "Nd",
        "Pm",
        "Sm",
        "Eu",
        "Gd",
        "Tb",
        "Dy",
        "Ho",
        "Er",
        "Tm",
        "Yb",
        "Lu",
        "Hf",
        "Ta",
        "W",
        "Re",
        "Os",
        "Ir",
        "Pt",
        "Au",
        "Hg",
        "Tl",
        "Pb",
        "Bi",
        "Po",
        "At",
        "Fr",
        "Ra",
        "Ac",
    }
)

# ── the dumb settle field ───────────────────────────────────────────────────

#: Bonded-neighbour search radius, as a multiple of the covalent-radii sum
#: ``r0`` — a pair within this range feels the shallow attractive well; beyond
#: it, nothing (an unbonded atom is free to reveal itself by not moving).
BOND_FACTOR = 1.3

#: Stiff soft-sphere repulsion below ``r0`` (arbitrary internal units — this
#: is a geometry settle, not a real force field, so only the *relative*
#: scale to ``K_BOND`` matters).
K_REP = 50.0

#: Gentle harmonic well pulling a bonded pair back toward ``r0`` — deliberately
#: ``K_REP`` << so a genuinely unbonded atom (outside ``BOND_FACTOR * r0`` of
#: everything) feels nothing at all and just sits where it was placed.
K_BOND = 0.5

#: Force-convergence target for the settle (same arbitrary units as the
#: field above) — loose, because this is "does it visibly relax", not a real
#: minimisation.
SETTLE_FMAX = 0.05

#: Settle step cap — these are ~12-40 atom slabs, keep it fast.
MAX_SETTLE_STEPS = 30

# ── post-relax geometry thresholds ──────────────────────────────────────────

#: Below this, two non-frozen atoms are clashing even after the settle.
MIN_BOND = 0.7

#: An adsorbate atom farther than this from every slab atom is untethered.
MAX_ADS_HEIGHT = 3.5

#: Top-of-cell fractional band (along the surface normal) an atom shouldn't
#: sit in — a well-formed slab (thin metal + a tall vacuum) never puts an atom
#: this close to its own periodic image on top. Not spec'd numerically
#: upstream; chosen to be well inside a typical ~10 A vacuum padding without
#: tripping on a legitimately thick slab.
CEILING_FRAC = 0.05

#: Absolute-distance twin of ``CEILING_FRAC`` — catches the case where the
#: fractional band above is too loose (a very tall cell) or too tight (a
#: short one).
MIN_VACUUM_TOP = 2.0

#: The largest contiguous gap along the surface normal must be at least this
#: tall, or the slab reaches too close to its own periodic image.
MIN_VACUUM = 6.0

#: A settled structure below this fraction of ideal close-packed density is a
#: sponge, not a solid slab.
MIN_DENSITY_FRAC = 0.7

#: Fraction of the cell (along the surface normal) an *internal* gap has to
#: exceed before it counts as a void rather than ordinary interatomic spacing.
#: Not spec'd numerically upstream; picked comfortably below a legitimate
#: vacuum band's typical fraction (``MIN_VACUUM`` / a ~20 A cell is ~30%) but
#: well above normal interlayer spacing (a few percent of the cell).
INTERNAL_VOID_FRAC = 0.15

#: fcc close-packing fraction, used as the "ideal bulk" density reference for
#: the porosity check's close-packing estimate.
FCC_PACKING_FRACTION = 0.7405


@dataclass
class PreflightReason:
    """One preflight finding — always actionable, always names the atom."""

    code: str  # element_out_of_box | clash | detached | ceiling | no_vacuum
    #           | porous | internal_void
    message: str  # names the atom + element + problem + a fix verb
    atom: str | int | None = None  # the structure atom handle (preferred)
    element: str | None = None


@dataclass
class PreflightVerdict:
    """The gate's verdict — ``ok`` iff no reasons were collected."""

    ok: bool
    reasons: list[PreflightReason] = field(default_factory=list)


#: Ship-safe kill switch for both preflight seams (structure handler
#: put/edit + quest.compute.dispatch_autocatpath) — default OFF so this lands
#: dark; flip on deliberately once live-tested. Mirrors the existing
#: ``PRECIS_*`` boolean-flag idiom (e.g.
#: :func:`precis.quest.tick.quest_loop_enabled`).
_PREFLIGHT_ENABLED_ENV = "PRECIS_STRUCTURE_PREFLIGHT"


def _preflight_enabled() -> bool:
    """True when the Tier-0 preflight gate is switched on (default OFF)."""
    return os.environ.get(_PREFLIGHT_ENABLED_ENV, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def preflight(
    scene: Scene, *, backend_elements: set[str] | None = None
) -> PreflightVerdict:
    """Run every check against ``scene``, collecting ALL reasons (no short-
    circuiting — a caller shows every problem at once).

    ``backend_elements`` overrides :data:`MACE_MP_ELEMENTS` for check 1 when
    the deployed backend's coverage differs. If the scene has no atoms, or
    can't be converted/settled (missing ASE, a conversion error), the geometry
    checks are skipped — "nothing to judge" rather than a crash — but any
    element-coverage reason already collected still stands.
    """
    box = backend_elements if backend_elements is not None else MACE_MP_ELEMENTS
    reasons: list[PreflightReason] = list(_check_elements(scene, box))

    if not scene.atoms:
        return PreflightVerdict(ok=not reasons, reasons=reasons)

    try:
        atoms, labels = _scene_to_ase(scene)
        _settle(atoms)
    except Exception:  # any ASE/conversion/settle failure ⇒ nothing to judge
        return PreflightVerdict(ok=not reasons, reasons=reasons)

    reasons.extend(_geometry_checks(scene, atoms, labels))
    return PreflightVerdict(ok=not reasons, reasons=reasons)


# ── check 1: element-in-box (no relax) ──────────────────────────────────────


def _check_elements(
    scene: Scene, box: frozenset[str] | set[str]
) -> list[PreflightReason]:
    reasons: list[PreflightReason] = []
    for label, atom in scene.atoms.items():
        if atom.element not in box:
            reasons.append(
                PreflightReason(
                    code="element_out_of_box",
                    atom=label,
                    element=atom.element,
                    message=(
                        f"atom {label} ({atom.element}) is outside the deployed "
                        f"MLIP's element coverage ({len(box)} elements) — swap "
                        "it for a supported element, or pass backend_elements= "
                        "to widen the box if the backend actually covers it."
                    ),
                )
            )
    return reasons


# ── check 2: the dumb universal settle ──────────────────────────────────────


def _scene_to_ase(scene: Scene):  # type: ignore[no-untyped-def]
    """Scene → ASE ``Atoms``, honouring the ``fixed`` bitmask as per-axis
    ``FixCartesian`` constraints (mirrors :mod:`relax`'s emt/ml rungs)."""
    from ase.constraints import FixCartesian

    labels = list(scene.atoms)
    atoms = export._to_ase(scene)
    constraints = []
    for idx, la in enumerate(labels):
        fixed = scene.atoms[la].fixed
        if fixed:
            mask = [bool((fixed >> ax) & 1) for ax in range(3)]
            constraints.append(FixCartesian(idx, mask=mask))
    if constraints:
        atoms.set_constraint(constraints)
    return atoms, labels


class _DumbField(_Calculator):  # type: ignore[misc]
    """A minimal, element-agnostic classical field for the settle.

    Per pair within :data:`BOND_FACTOR` * (covalent-radii sum): stiff soft
    repulsion below the sum, a gentle harmonic well pulling back toward it
    above — so a bonded neighbour (including an adsorbate genuinely resting
    on the slab) stays put, while an atom with no partner in range feels
    nothing at all. PBC-aware (minimum image) via ASE's own neighbor list.
    Analytic forces throughout (no numeric differencing).
    """

    implemented_properties = ["energy", "forces"]

    def calculate(
        self,
        atoms=None,
        properties=("energy", "forces"),
        system_changes=_all_changes,
    ):
        _Calculator.calculate(self, atoms, properties, system_changes)
        from ase.data import covalent_radii
        from ase.neighborlist import neighbor_list

        assert self.atoms is not None  # ASE sets it in Calculator.calculate
        n = len(self.atoms)
        numbers = self.atoms.get_atomic_numbers()
        radii = np.nan_to_num(covalent_radii[numbers], nan=1.5) * BOND_FACTOR
        energy = 0.0
        forces = np.zeros((n, 3))
        if n > 1:
            i_idx, j_idx, d, vec = neighbor_list("ijdD", self.atoms, radii)
            for i, j, dist, dvec in zip(i_idx, j_idx, d, vec):
                if dist < 1e-9:
                    # Coincident atoms: a true singularity — no gradient
                    # direction, so no force. The pair can't settle apart;
                    # it's a genuine clash, not one the settle resolves.
                    continue
                r0 = float(covalent_radii[numbers[i]]) + float(
                    covalent_radii[numbers[j]]
                )
                if dist < r0:
                    de_dd = K_REP * (dist - r0)
                    e_pair = 0.5 * K_REP * (r0 - dist) ** 2
                elif dist <= BOND_FACTOR * r0:
                    de_dd = K_BOND * (dist - r0)
                    e_pair = 0.5 * K_BOND * (dist - r0) ** 2
                else:
                    continue
                unit = dvec / dist
                forces[i] += de_dd * unit
                energy += 0.5 * e_pair
        self.results = {"energy": float(energy), "forces": forces}


def _settle(atoms) -> None:  # type: ignore[no-untyped-def]
    """Settle ``atoms`` in place under :class:`_DumbField`, capped + fast."""
    try:
        from ase.optimize import FIRE as _Optimizer
    except ImportError:
        from ase.optimize import BFGS as _Optimizer  # type: ignore[assignment]

    atoms.calc = _DumbField()
    opt = _Optimizer(atoms, logfile=None)
    opt.run(fmax=SETTLE_FMAX, steps=MAX_SETTLE_STEPS)


# ── check 3: post-relax geometry judgments ──────────────────────────────────


def _dominant_element(scene: Scene) -> str | None:
    """The most common element — the assumed slab metal, absent an explicit
    slab/adsorbate split (there is no such metadata on the Scene/Atom IR
    today; this is the pragmatic stand-in, matching how a candidate is built
    here: the ``slab`` op seeds the metal, ``add_atom`` appends adsorbates)."""
    if not scene.atoms:
        return None
    return Counter(a.element for a in scene.atoms.values()).most_common(1)[0][0]


def _slab_adsorbate_indices(
    scene: Scene, atoms, labels: list[str]
) -> tuple[list[int], list[int]]:
    """(slab_indices, adsorbate_indices) — ``atoms.info['n_slab']`` wins if
    the caller already set it; otherwise the dominant-element heuristic.

    TODO: neither of the two current callers (the structure handler's
    put/edit, ``quest.compute.dispatch_autocatpath``) can set ``n_slab`` today —
    the Scene/Atom IR carries no slab-vs-adsorbate provenance (no op records
    "these N atoms came from the `slab` op"). Until that's added at the
    slab-op layer, a doped slab (a Cu/Ag dopant swapped in via
    ``set_element``) risks being miscounted as a floating adsorbate by the
    dominant-element fallback below. See OPEN-ITEMS.md."""
    n_slab = atoms.info.get("n_slab") if hasattr(atoms, "info") else None
    if isinstance(n_slab, int) and 0 <= n_slab <= len(labels):
        return list(range(n_slab)), list(range(n_slab, len(labels)))
    dominant = _dominant_element(scene)
    slab = [i for i, la in enumerate(labels) if scene.atoms[la].element == dominant]
    ads = [i for i, la in enumerate(labels) if scene.atoms[la].element != dominant]
    return slab, ads


def _vacuum_gaps(scaled: np.ndarray, axis: int) -> list[tuple[float, float]]:
    """``(gap_frac, start_frac)`` for every contiguous gap along ``axis``,
    PBC-wrapped (sorted fractional coords, including the wrap-around gap)."""
    coords = np.sort(np.asarray(scaled[:, axis], dtype=float) % 1.0)
    n = len(coords)
    if n == 0:
        return []
    gaps = []
    for k in range(n):
        start = float(coords[k])
        end = float(coords[(k + 1) % n]) + (1.0 if k == n - 1 else 0.0)
        gaps.append((end - start, start))
    return gaps


def _geometry_checks(scene: Scene, atoms, labels: list[str]) -> list[PreflightReason]:  # type: ignore[no-untyped-def]
    reasons: list[PreflightReason] = []
    n = len(labels)
    if n == 0:
        return reasons

    is_frozen = [scene.atoms[la].fixed == FIX_ALL for la in labels]

    # -- clash: any non-frozen-frozen pair still sub-``MIN_BOND`` apart -----
    from ase.data import atomic_numbers, covalent_radii
    from ase.neighborlist import neighbor_list

    if n > 1:
        i_idx, j_idx, d = neighbor_list("ijd", atoms, MIN_BOND)
        for i, j, dist in zip(i_idx, j_idx, d):
            if i >= j:
                continue  # dedupe the symmetric (i,j)/(j,i) pair
            if is_frozen[i] and is_frozen[j]:
                continue  # both part of the rigid pre-existing lattice
            la, lb = labels[i], labels[j]
            reasons.append(
                PreflightReason(
                    code="clash",
                    atom=la,
                    element=scene.atoms[la].element,
                    message=(
                        f"atom {la} ({scene.atoms[la].element}) and {lb} "
                        f"({scene.atoms[lb].element}) are {dist:.2f} Å "
                        f"apart — below the {MIN_BOND:.2f} Å clash floor "
                        "even after settling; separate them or remove one."
                    ),
                )
            )

    # -- detached: an adsorbate atom too far from every slab atom ----------
    slab_idx, ads_idx = _slab_adsorbate_indices(scene, atoms, labels)
    if slab_idx and ads_idx:
        for i in ads_idx:
            dists = atoms.get_distances(i, slab_idx, mic=True)
            min_d = float(np.min(dists)) if len(dists) else float("inf")
            if min_d > MAX_ADS_HEIGHT:
                la = labels[i]
                reasons.append(
                    PreflightReason(
                        code="detached",
                        atom=la,
                        element=scene.atoms[la].element,
                        message=(
                            f"atom {la} ({scene.atoms[la].element}) floats "
                            f"{min_d:.1f} Å above the surface — not bonded "
                            "to the slab; lower it toward a hollow/bridge site "
                            "or remove it."
                        ),
                    )
                )

    # -- surface-normal axis: the one with the largest vacuum gap -----------
    scaled = atoms.get_scaled_positions(wrap=True)
    cell = np.asarray(atoms.get_cell())
    candidates = []
    for axis in range(3):
        height = float(np.linalg.norm(cell[axis]))
        if height < 1e-9:
            continue
        gaps = _vacuum_gaps(scaled, axis)
        if not gaps:
            continue
        top_gap_frac = max(g[0] for g in gaps)
        candidates.append((top_gap_frac * height, axis, gaps, height))
    if not candidates:
        return reasons
    _, axis, gaps, height = max(candidates, key=lambda c: c[0])
    top_idx = int(np.argmax([g[0] for g in gaps]))
    top_gap_frac, _top_start_frac = gaps[top_idx]
    top_gap_a = top_gap_frac * height

    # -- no_vacuum: the largest gap (the slab's headroom) isn't tall enough -
    if top_gap_a < MIN_VACUUM:
        reasons.append(
            PreflightReason(
                code="no_vacuum",
                message=(
                    f"the slab's vacuum gap along its surface normal is only "
                    f"{top_gap_a:.1f} Å (need ≥ {MIN_VACUUM:.0f} "
                    "Å) — it reaches too close to its own periodic "
                    "image; enlarge the cell's vacuum padding before adding "
                    "more."
                ),
            )
        )

    # -- ceiling: an atom dangling in the top vacuum band --------------------
    for i, la in enumerate(labels):
        frac_ax = float(scaled[i, axis])
        dist_to_top_a = (1.0 - frac_ax) * height
        if frac_ax > 1.0 - CEILING_FRAC or dist_to_top_a < MIN_VACUUM_TOP:
            reasons.append(
                PreflightReason(
                    code="ceiling",
                    atom=la,
                    element=scene.atoms[la].element,
                    message=(
                        f"atom {la} ({scene.atoms[la].element}) is dangling "
                        f"into the vacuum, {dist_to_top_a:.1f} Å from the "
                        "top of the cell — move it down onto the surface."
                    ),
                )
            )

    # -- internal_void: any OTHER gap as tall as the (expected) top vacuum --
    for k, (gap_frac, _start_frac) in enumerate(gaps):
        if k == top_idx:
            continue
        if gap_frac >= INTERNAL_VOID_FRAC:
            z_height = gap_frac * height
            reasons.append(
                PreflightReason(
                    code="internal_void",
                    message=(
                        f"an internal void {z_height:.1f} Å tall sits "
                        "inside the structure (not the surface vacuum band) "
                        "— close the gap or remove the floating layer."
                    ),
                )
            )

    # -- porous: settled density vs. a close-packing estimate ---------------
    dominant = _dominant_element(scene)
    if dominant is not None:
        occupied_frac = 1.0 - top_gap_frac
        occupied_z_a = occupied_frac * height
        other_axes = [a for a in range(3) if a != axis]
        lateral_area = float(
            np.linalg.norm(np.cross(cell[other_axes[0]], cell[other_axes[1]]))
        )
        if occupied_z_a > 1e-6 and lateral_area > 1e-6:
            occupied_volume = lateral_area * occupied_z_a
            density = n / occupied_volume
            z = atomic_numbers.get(dominant)
            if z is not None and 0 <= z < len(covalent_radii) and covalent_radii[z] > 0:
                r = float(covalent_radii[z])
            else:
                r = elements.covalent_radius(dominant)
            ideal_density = FCC_PACKING_FRACTION / ((4.0 / 3.0) * np.pi * r**3)
            density_frac = density / ideal_density if ideal_density > 0 else 1.0
            if density_frac < MIN_DENSITY_FRAC:
                pct_void = max(0.0, (1.0 - density_frac) * 100.0)
                reasons.append(
                    PreflightReason(
                        code="porous",
                        element=dominant,
                        message=(
                            f"structure is ~{pct_void:.0f}% void — not a solid "
                            f"slab (density is {density_frac * 100:.0f}% of "
                            f"bulk {dominant}); pack the atoms closer or "
                            "shrink the cell footprint."
                        ),
                    )
                )

    return reasons
