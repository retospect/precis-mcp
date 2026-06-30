"""The relax surface — a rented fidelity ladder (ADR 0043 §9).

`relax` is one verb with a ``fidelity`` rung: ``clean`` (rung 0) is **ours and
always available** — a pure geometric repair that pushes sub-covalent / overlapping
atoms apart toward their equilibrium bond length ("fix the stupid bonds", the
"put bonds in, relax, it fixes itself" of §8.1). Every other rung is a **rented
backend** (``ff``/``xtb``/``ml``/``dft-fast``/``dft-tight``, ADR §9 table) gated
behind the ``[dft-ml]`` / ``[dft-gpaw]`` extras — calling one without its backend
raises :class:`RelaxUnsupported`, surfaced as ``Unsupported`` at the handler,
never a crash.

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
_RENTED_RUNGS = {"ff", "xtb", "ml", "dft-fast", "dft-tight"}


class RelaxUnsupported(RuntimeError):
    """A relax rung whose backend extra isn't installed."""


@dataclass
class RelaxResult:
    """The convergence envelope of a relax (ADR §9/§22-D).

    ``energy``/``max_force`` are ``None`` for the rung-0 ``clean`` geometry
    repair — it has no potential energy, "undefined until it is" (ADR §6 q9).
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
    # ── run-cube cache plumbing (ADR §23.16) — populated by the *handler*, not
    # the pure compute: the content address of this relax, and, on a cache hit,
    # the fact that no compute ran. ``relax()`` itself never sets these.
    from_cache: bool = False
    cache_key: str | None = None
    structure_sha: str | None = None
    final_geometry: dict | None = None  # type: ignore[type-arg]


def relax(
    scene: Scene,
    *,
    fidelity: str = "clean",
    steps: int = 200,
    tol: float = 1e-3,
    model: str = "mace_mp",
) -> RelaxResult:
    """Relax ``scene`` at the given ``fidelity`` rung (mutates in place)."""
    if fidelity in ("clean", "0"):
        return _relax_clean(scene, steps=steps, tol=tol)
    if fidelity == "ml":
        return _relax_ml(scene, steps=steps, tol=tol, model=model)
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
    return RelaxResult(
        rung="clean", converged=converged, n_steps=n, max_disp=max_disp, curve=curve
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


def _relax_ml(scene: Scene, *, steps: int, tol: float, model: str) -> RelaxResult:
    """Rung 3: relax on a machine-learned interatomic potential (ASE + MLIP).

    Real energies + forces — the cheap-but-physical rung that fixes a hand-built
    geometry before any DFT is spent (ADR §9). Honours the ``fixed`` bitmask via
    per-atom Cartesian constraints, records the per-step force convergence curve,
    and writes the relaxed geometry back onto the Scene. Fully ``[dft-ml]``-gated:
    a missing ASE/MLIP raises :class:`RelaxUnsupported`.
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

    opt = BFGS(atoms, logfile=None)
    opt.attach(_record, interval=1)
    converged = bool(opt.run(fmax=max(tol, 0.05), steps=steps))

    scaled = atoms.get_scaled_positions()
    for idx, la in enumerate(labels):
        scene.atoms[la].frac = scene.cell.wrap(np.asarray(scaled[idx], dtype=float))
    after = np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])
    max_disp = float(np.linalg.norm(after - before, axis=1).max()) if labels else 0.0
    forces = atoms.get_forces()
    max_force = float(np.sqrt((forces**2).sum(axis=1).max()))
    return RelaxResult(
        rung="ml",
        converged=converged,
        n_steps=int(opt.get_number_of_steps()),
        max_disp=round(max_disp, 4),
        curve=curve,
        energy=float(atoms.get_potential_energy()),
        max_force=round(max_force, 4),
        model=model,
    )
