"""The relax surface — a rented fidelity ladder.

`relax` is one verb with a ``fidelity`` rung: ``clean`` (rung 0) is **ours and
always available** — a pure geometric repair that pushes sub-covalent / overlapping
atoms apart toward their equilibrium bond length ("fix the stupid bonds", the
"put bonds in, relax, it fixes itself" of §8.1). ``emt`` (rung 1) is
also **ours** — a torch-free ASE-EMT relax gated only behind the light
``[dft]`` extra (numpy + ASE, no MLIP), whose closed element coverage happens
to be exactly the fcc catalytic metals a Pd/Cu/Ni screen needs. Every rung
above that is a **rented backend** (``ff``/``xtb``/``ml``/``dft-fast``/
``dft-tight``) gated behind the ``[dft-ml]`` / ``[dft-gpaw]``
extras — calling one without its backend raises :class:`RelaxUnsupported`,
surfaced as ``Unsupported`` at the handler, never a crash.

Rung 0 honours the ``fixed`` constraint (a fixed axis never moves) and returns a
structured convergence envelope (converged + steps + max displacement + the
per-step curve), the §9/§22-D contract. It mutates the Scene in place.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from . import elements, export
from .scene import Scene

#: Rungs that need a rented backend not bundled here. ``ml`` has a real backend
#: (ASE + an MLIP, the ``[dft-ml]`` extra); ``ff``/``xtb``/``dft-*`` stay gated.
#: ``emt`` (rung 1) is deliberately *not* here — it's ours, like ``clean``, just
#: gated behind the light ``[dft]`` extra instead of always-on.
_RENTED_RUNGS = {"ff", "xtb", "ml", "dft-fast", "dft-tight"}

#: EMT's built-in interatomic-potential coverage (ASE's ``emt.py`` parameter
#: table) — a closed set of fcc catalytic metals + light adsorbate elements.
#: This happens to be exactly the Pd/Cu/Ni catalyst screen's metals; anything
#: outside it (a lanthanide, a bcc metal, …) needs the ``ml`` rung instead.
EMT_ELEMENTS = frozenset({"Al", "Ni", "Cu", "Pd", "Ag", "Pt", "Au", "H", "C", "N", "O"})

#: Variable-cell relax modes for an energy rung. ``None`` / ``"fixed"`` relaxes
#: atoms only (the historical default). ``"inplane"`` frees the in-plane lattice
#: (the a/b vectors + the γ shear) while pinning the c-axis, so a slab's vacuum
#: gap can't collapse — "relax the box along with the slab". ``"full"`` frees all
#: six strain components (a bulk cell relax). Voigt strain-mask order is
#: (xx, yy, zz, yz, xz, xy), matching ASE's cell-filter ``mask``.
CELL_MODES: frozenset[str | None] = frozenset({None, "fixed", "inplane", "full"})
_CELL_MASKS: dict[str, list[bool]] = {
    "inplane": [True, True, False, False, False, True],
    "full": [True, True, True, True, True, True],
}


class RelaxUnsupported(RuntimeError):
    """A relax rung whose backend extra isn't installed."""


def estimate_forces_emt(scene: Scene) -> dict[str, list[float]] | None:
    """A cheap, always-available per-atom force *estimate* (gripe 161576).

    "Local only, there's cheap python approximation, we do that" — one ASE-EMT
    single-point force evaluation (no optimisation), so the ``clean`` rung
    (which has no calculator of its own) still gives the modeling LLM a
    qualitative sense of which atoms are under strain, ahead of a real
    MLIP/DFT relax later. Never fabricates a number: returns ``None`` when the
    element set falls outside EMT's closed coverage (:data:`EMT_ELEMENTS`),
    ASE isn't installed, or the single-point evaluation itself errors.
    """
    if not scene.atoms:
        return None
    if not export.ase_available():
        return None
    if {a.element for a in scene.atoms.values()} - EMT_ELEMENTS:
        return None
    from ase.calculators.emt import EMT

    try:
        atoms = export._to_ase(scene)
        atoms.calc = EMT()
        raw = atoms.get_forces()
    except Exception:  # any ASE/EMT failure ⇒ no estimate, not a crash
        return None
    labels = list(scene.atoms)
    return {la: [float(x) for x in raw[idx]] for idx, la in enumerate(labels)}


@dataclass
class RelaxResult:
    """The convergence envelope of a relax.

    ``energy``/``max_force`` are ``None`` for the rung-0 ``clean`` geometry
    repair — it has no potential energy, "undefined until it is".
    ``curve`` is the per-step convergence trace (max force for an energy rung;
    the max atomic move for rung 0).
    """

    rung: str
    converged: bool
    n_steps: int
    max_disp: float  # Å, the last step's largest atomic move (rung-0 force proxy)
    curve: list[float] = field(default_factory=list)
    energy: float | None = None  # eV (None = undefined: clean rung / failure)
    max_force: float | None = None  # eV/Å (None for the geometry rung)
    model: str | None = None  # the backend that produced it (MLIP name)
    # ── per-atom forces (gripe 161576) — a *qualitative* "which atoms are
    # doing the work" signal for the modeling LLM, not physics-grade truth (a
    # real MLIP/DFT relax runs later). ``forces`` is keyed by design label
    # (pre-canonicalisation, the compute's own atom order); the handler
    # re-indexes it to canonical rank for storage, mirroring
    # ``final_geometry``. ``forces_approx`` marks a cheap EMT single-point
    # estimate (:func:`estimate_forces_emt`, used when the rung has no
    # calculator of its own — the ``clean`` geometry repair) so it is never
    # confused with a real emt/ml relax force.
    forces: dict[str, list[float]] | None = None
    forces_approx: bool = False
    forces_source: str | None = (
        None  # 'emt' (rung 1, or the clean estimate) or an MLIP name
    )
    # ── run-cube cache plumbing — populated by the *handler*, not
    # the pure compute: the content address of this relax, and, on a cache hit,
    # the fact that no compute ran. ``relax()`` itself never sets these.
    from_cache: bool = False
    cache_key: str | None = None
    structure_sha: str | None = None
    final_geometry: dict | None = None  # type: ignore[type-arg]
    # Storage-ready ``forces``, set by the handler like ``final_geometry``
    # above — but LABEL-paired, not canonical-rank-indexed (gripe 161576 FIX
    # 1): ``forces_labels[i]`` names the atom whose vector is
    # ``forces_vectors[i]``, captured together from the same raw ``forces``
    # dict in one pass, so they can never drift apart. A relax that moves an
    # atom across a periodic image boundary can change canonical rank (which
    # sorts on fractional position) between write time and a later read —
    # re-deriving rank at read time would then silently join the wrong
    # atom's vector. The label is stable; rank is not.
    forces_labels: list[str] | None = None
    forces_vectors: list[list[float]] | None = None


def relax(
    scene: Scene,
    *,
    fidelity: str = "clean",
    steps: int = 200,
    tol: float = 1e-3,
    model: str = "mace_mp",
    cell: str | None = None,
) -> RelaxResult:
    """Relax ``scene`` at the given ``fidelity`` rung (mutates in place).

    ``cell`` opts into a **variable-cell** relax on an energy rung (see
    :data:`CELL_MODES`): ``"inplane"`` relaxes the box in-plane with a slab (the
    c-axis / vacuum pinned), ``"full"`` relaxes all six strain components. It is
    only meaningful for an energy rung — the ``clean`` geometry repair has no
    stress, so requesting a cell relax there raises.
    """
    if cell not in CELL_MODES:
        raise RelaxUnsupported(
            f"unknown cell relax mode {cell!r} (use 'inplane', 'full', or omit)"
        )
    cell_mode = None if cell == "fixed" else cell
    if cell_mode is not None and fidelity in ("clean", "0"):
        raise RelaxUnsupported(
            "variable-cell relax needs an energy rung (fidelity='ml'); the "
            "'clean' geometry repair has no stress to relax the cell against"
        )
    if cell_mode is not None and fidelity == "emt":
        raise RelaxUnsupported(
            "variable-cell relax isn't supported at rung 'emt' — use fidelity='ml'"
        )
    if fidelity in ("clean", "0"):
        return _relax_clean(scene, steps=steps, tol=tol)
    if fidelity == "emt":
        return _relax_emt(scene, steps=steps, tol=tol)
    if fidelity == "ml":
        return _relax_ml(scene, steps=steps, tol=tol, model=model, cell=cell_mode)
    if fidelity in _RENTED_RUNGS:
        raise RelaxUnsupported(
            f"relax rung {fidelity!r} needs a rented backend "
            f"(install precis-mcp[dft-ml] for ml, [dft-gpaw] for dft). "
            f"Rung 'clean' (geometry repair) is always available."
        )
    raise RelaxUnsupported(f"unknown relax fidelity {fidelity!r}")


def _free_axes(fixed: int) -> np.ndarray:
    """A 0/1 mask of the axes an atom may move along (0 = fixed)."""
    return np.array([0.0 if (fixed >> ax) & 1 else 1.0 for ax in range(3)])


def _relax_clean(scene: Scene, *, steps: int, tol: float) -> RelaxResult:
    """Rung 0: iteratively separate too-close pairs toward their covalent bond
    length. Pure, fixed-aware. Not an energy minimiser — a geometry sanitiser."""
    cell = scene.cell
    labels = list(scene.atoms)
    curve: list[float] = []
    converged = False
    n = 0
    for n in range(1, steps + 1):
        disp = {label: np.zeros(3) for label in labels}
        for ai in range(len(labels)):
            a = scene.atoms[labels[ai]]
            for bj in range(ai + 1, len(labels)):
                b = scene.atoms[labels[bj]]
                d, img = cell.mic(a.frac, b.frac)
                target = elements.covalent_radius(a.element) + elements.covalent_radius(
                    b.element
                )
                if 1e-6 < d < target * 0.98:
                    vec = (b.frac + np.array(img) - a.frac) @ cell.lattice
                    unit = vec / d
                    push = (target - d) * 0.5
                    disp[a.label] -= unit * push
                    disp[b.label] += unit * push
        max_disp = 0.0
        for label in labels:
            atom = scene.atoms[label]
            dfrac = cell.cart_to_frac(disp[label]) * _free_axes(atom.fixed)
            atom.frac = cell.wrap(atom.frac + dfrac)
            moved = float(np.linalg.norm(cell.frac_to_cart(dfrac)))
            max_disp = max(max_disp, moved)
        curve.append(round(max_disp, 4))
        if max_disp < tol:
            converged = True
            break
    # Rung 0 has no calculator of its own — a cheap EMT single-point estimate
    # (gripe 161576) gives the LLM a qualitative force sense anyway, on the
    # geometry this pass just repaired. ``None`` (never fabricated) when the
    # element set falls outside EMT's coverage or ASE isn't installed.
    approx_forces = estimate_forces_emt(scene)
    return RelaxResult(
        rung="clean",
        converged=converged,
        n_steps=n,
        max_disp=max_disp,
        curve=curve,
        forces=approx_forces,
        forces_approx=approx_forces is not None,
        forces_source="emt" if approx_forces is not None else None,
    )


def _relax_emt(scene: Scene, *, steps: int, tol: float) -> RelaxResult:
    """Rung 1: relax on ASE's built-in Effective Medium Theory potential.

    Torch-free — ``numpy + ASE`` only (the light ``[dft]`` extra), no MLIP to
    install or download. EMT's coverage is the closed fcc-metal parameter
    table it ships with (:data:`EMT_ELEMENTS`), which happens to be exactly
    the Pd/Cu/Ni catalyst screen's metals; an out-of-set element raises
    :class:`RelaxUnsupported` with a legible hint toward the ``ml`` rung,
    never a stray KeyError from inside ASE. Honours the ``fixed`` bitmask via
    the same per-axis ``FixCartesian`` constraints as :func:`_relax_ml`. No
    variable-cell mode — rung 1 is atoms-only, not a DFT-grade stress tensor.
    """
    bad = {a.element for a in scene.atoms.values()} - EMT_ELEMENTS
    if bad:
        raise RelaxUnsupported(
            "EMT covers {Al,Ni,Cu,Pd,Ag,Pt,Au,H,C,N,O}; use fidelity='ml'"
        )
    if not export.ase_available():
        raise RelaxUnsupported(
            "relax rung 'emt' needs ASE — pip install 'precis-mcp[dft]'"
        )
    from ase.calculators.emt import EMT
    from ase.constraints import FixCartesian

    try:
        from ase.optimize import FIRE as _Optimizer
    except ImportError:
        from ase.optimize import BFGS as _Optimizer  # type: ignore[assignment]

    atoms = export._to_ase(scene)
    labels = list(scene.atoms)
    before = np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])

    constraints = []
    for idx, la in enumerate(labels):
        fixed = scene.atoms[la].fixed
        if fixed:
            mask = [bool((fixed >> ax) & 1) for ax in range(3)]
            constraints.append(FixCartesian(idx, mask=mask))
    if constraints:
        atoms.set_constraint(constraints)
    atoms.calc = EMT()

    curve: list[float] = []

    def _record() -> None:
        f = atoms.get_forces()
        curve.append(round(float(np.sqrt((f**2).sum(axis=1).max())), 4))

    opt = _Optimizer(atoms, logfile=None)
    opt.attach(_record, interval=1)
    converged = bool(opt.run(fmax=max(tol, 0.05), steps=steps))

    scaled = atoms.get_scaled_positions()
    for idx, la in enumerate(labels):
        scene.atoms[la].frac = scene.cell.wrap(np.asarray(scaled[idx], dtype=float))
    after = np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])
    max_disp = float(np.linalg.norm(after - before, axis=1).max()) if labels else 0.0
    forces = atoms.get_forces()
    max_force = float(np.sqrt((forces**2).sum(axis=1).max()))
    per_atom_forces = {
        la: [float(x) for x in forces[idx]] for idx, la in enumerate(labels)
    }
    return RelaxResult(
        rung="emt",
        converged=converged,
        n_steps=int(opt.get_number_of_steps()),
        max_disp=round(max_disp, 4),
        curve=curve,
        energy=float(atoms.get_potential_energy()),
        max_force=round(max_force, 4),
        model="emt",
        forces=per_atom_forces,
        forces_approx=False,
        forces_source="emt",
    )


def _ml_calculator(model: str):  # type: ignore[no-untyped-def]
    """Instantiate an ASE calculator for an MLIP, or raise RelaxUnsupported.

    The import is isolated here so a missing backend gives one clean
    ``RelaxUnsupported`` with an install hint, never a stray ImportError.
    """
    name = (model or "mace_mp").lower().replace("-", "_")
    if name in ("mace", "mace_mp", "mace_mp_0"):
        try:
            from mace.calculators import mace_mp
        except ImportError as exc:
            raise RelaxUnsupported(
                "relax rung 'ml' (MACE) needs the [dft-ml] extra — "
                "pip install 'precis-mcp[dft-ml]'"
            ) from exc
        return mace_mp(default_dtype="float64", dispersion=False)
    if name == "chgnet":
        try:
            from chgnet.model.dynamics import CHGNetCalculator
        except ImportError as exc:
            raise RelaxUnsupported(
                "relax rung 'ml' (CHGNet) needs the [dft-ml] extra — "
                "pip install 'precis-mcp[dft-ml]'"
            ) from exc
        return CHGNetCalculator()
    raise RelaxUnsupported(f"unknown MLIP model {model!r} (try 'mace_mp' or 'chgnet')")


def _cell_filter(atoms, cell: str):  # type: ignore[no-untyped-def]
    """Wrap ``atoms`` in an ASE strain filter so the optimiser relaxes the
    lattice alongside the positions, masked per :data:`_CELL_MASKS`.

    Prefers the modern ``FrechetCellFilter`` (ASE ≥ 3.23); falls back to the
    deprecated ``ExpCellFilter`` on older ASE. The import is isolated here so a
    stale ASE gives one clean signal rather than a stray ImportError.
    """
    mask = _CELL_MASKS[cell]
    try:
        from ase.filters import FrechetCellFilter

        return FrechetCellFilter(atoms, mask=mask)
    except ImportError:
        from ase.constraints import ExpCellFilter  # type: ignore[attr-defined]

        return ExpCellFilter(atoms, mask=mask)


def _relax_ml(
    scene: Scene, *, steps: int, tol: float, model: str, cell: str | None = None
) -> RelaxResult:
    """Rung 3: relax on a machine-learned interatomic potential (ASE + MLIP).

    Real energies + forces — the cheap-but-physical rung that fixes a hand-built
    geometry before any DFT is spent. Honours the ``fixed`` bitmask via
    per-atom Cartesian constraints, records the per-step force convergence curve,
    and writes the relaxed geometry back onto the Scene. Fully ``[dft-ml]``-gated:
    a missing ASE/MLIP raises :class:`RelaxUnsupported`.

    ``cell`` (``"inplane"`` / ``"full"``, see :data:`CELL_MODES`) wraps the atoms
    in a masked ASE cell filter so the box relaxes with the atoms; the relaxed
    lattice is written back onto ``scene.cell``.
    """
    if not export.ase_available():
        raise RelaxUnsupported(
            "relax rung 'ml' needs ASE + an MLIP — pip install 'precis-mcp[dft-ml]'"
        )
    from ase.constraints import FixCartesian
    from ase.optimize import BFGS

    calc = _ml_calculator(model)  # raises RelaxUnsupported if the MLIP is absent
    atoms = export._to_ase(scene)
    labels = list(scene.atoms)
    before = np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])

    constraints = []
    for idx, la in enumerate(labels):
        fixed = scene.atoms[la].fixed
        if fixed:
            mask = [bool((fixed >> ax) & 1) for ax in range(3)]
            constraints.append(FixCartesian(idx, mask=mask))
    if constraints:
        atoms.set_constraint(constraints)
    atoms.calc = calc

    curve: list[float] = []

    def _record() -> None:
        f = atoms.get_forces()
        curve.append(round(float(np.sqrt((f**2).sum(axis=1).max())), 4))

    # Variable-cell: the optimiser drives a filter over (positions + strain); the
    # convergence curve stays the atom-force max so it's comparable across modes.
    opt_target = _cell_filter(atoms, cell) if cell is not None else atoms

    opt = BFGS(opt_target, logfile=None)
    opt.attach(_record, interval=1)
    converged = bool(opt.run(fmax=max(tol, 0.05), steps=steps))

    if cell is not None:
        from .cell import Cell

        scene.cell = Cell(np.asarray(atoms.get_cell(), dtype=float), scene.cell.pbc)
    scaled = atoms.get_scaled_positions()
    for idx, la in enumerate(labels):
        scene.atoms[la].frac = scene.cell.wrap(np.asarray(scaled[idx], dtype=float))
    after = np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])
    max_disp = float(np.linalg.norm(after - before, axis=1).max()) if labels else 0.0
    forces = atoms.get_forces()
    max_force = float(np.sqrt((forces**2).sum(axis=1).max()))
    per_atom_forces = {
        la: [float(x) for x in forces[idx]] for idx, la in enumerate(labels)
    }
    return RelaxResult(
        rung="ml",
        converged=converged,
        n_steps=int(opt.get_number_of_steps()),
        max_disp=round(max_disp, 4),
        curve=curve,
        energy=float(atoms.get_potential_energy()),
        max_force=round(max_force, 4),
        model=model,
        forces=per_atom_forces,
        forces_approx=False,
        forces_source=model,
    )
