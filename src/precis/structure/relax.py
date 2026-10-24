"""The relax surface — rented fidelity levels.

`relax` is one verb with a ``fidelity`` rung: ``clean`` (rung 0) is **ours and
always available** — a pure geometric repair that pushes sub-covalent / overlapping
atoms apart toward their equilibrium bond length ("fix the stupid bonds", the
"put bonds in, relax, it fixes itself" of §8.1). ``geo`` sits BESIDE ``clean``
(also ours, always available, no energy of its own) — the graph-first
hybridization-aware relax (bond springs + non-bond repulsion + a VSEPR
angle-restoring term keyed on each atom's own hybridization,
:mod:`precis.structure.georelax`, promoted out of the cyclodextrin generator's
original cleanup pass, the closed se-nanobud-graph spec §1) — the capability
``clean``'s pairwise-only repair never had: it restores bond ANGLES too (an
sp2 vertex toward 120°, not just sp3's 109.47°), not only lengths. ``emt``
(rung 1) is also **ours** — a torch-free ASE-EMT relax, always available (ASE is a core
dependency, no MLIP needed), whose closed element coverage happens
to be exactly the fcc catalytic metals a Pd/Cu/Ni screen needs. ``xtb``
(GFN2-xTB via tblite's ASE calculator) is a real energy rung wherever the
tblite wheel installs — the relaxer of record for hexfold blocks (Reto
2026-10-11, gr472846); the MCP handler treats it as **dispatch-only**,
always staging a ``struct_relax`` job rather than running it in-request
(gr496562), so local ``_relax_xtb`` here serves the job's in-process run
and direct calls, not the request path. ``jiggle`` is the dev loop's
**fake rung** (dev-loop-scratch spec D0): a seeded Gaussian displacement
with a fabricated-but-consistent convergence report, for testing how the
system tolerates moved/"relaxed" geometry — never a default, reached
only by name, and honestly labelled (``rung='jiggle'``,
``forces_approx=True``). The remaining rungs are
**rented backends**
(``ff``/``ml``/``dft-fast``/``dft-tight``) gated behind the ``[dft-ml]`` /
``[dft-gpaw]`` extras — calling one without its backend raises
:class:`RelaxUnsupported`, surfaced as ``Unsupported`` at the handler,
never a crash.

Rung 0 honours the ``fixed`` constraint (a fixed axis never moves) and returns a
structured convergence envelope (converged + steps + max displacement + the
per-step curve), the §9/§22-D contract. It mutates the Scene in place. Rung
``geo`` honours ``fixed`` too, but only whole-atom (any fixed axis pins the
whole atom — the shared graph-relax core has no per-axis freedom); a caller
needing genuine per-axis pinning wants ``clean`` or an energy rung.

Unit enclave (package docstring): Å/eV-native throughout, following ASE
(``Atoms``, EMT, FIRE/BFGS) — energies in eV, forces in eV/Å, displacements
in Å; never converted to SI here.
"""

from __future__ import annotations

import importlib.util
import logging
import os
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

import numpy as np

from . import elements, export, georelax
from .scene import Scene

log = logging.getLogger(__name__)

#: Largest atom count :func:`_relax_ml` drives with ASE ``BFGS``; above it,
#: ``LBFGS``. BFGS keeps a dense (3N)² inverse Hessian and diagonalises it
#: every step, O((3N)³): at 1000 atoms that is a 3000² matrix (72 MB) and
#: ~3e10 flop per step, already more than an MLIP force call on a GPU; at
#: 5105 atoms (gr506395) it is 15315² (1.9 GB) and ~4e12 flop per step, a
#: CPU-bound relax that held the GPU idle for hours. LBFGS keeps a bounded
#: history (O(N) per step). Small systems stay on BFGS so existing results
#: and goldens do not move.
BFGS_MAX_ATOMS = 1000

#: Optimiser steps between progress log lines on the energy rungs, so a
#: long relax shows its step count and elapsed time in the worker log.
PROGRESS_EVERY = 10


def _attach_progress(
    opt: Any, *, rung: str, n_atoms: int, steps: int, curve: list[float]
) -> None:
    """Log ``step k/steps``, the latest max force and elapsed seconds every
    :data:`PROGRESS_EVERY` optimiser steps. Attach after the curve recorder
    so ``curve[-1]`` is the current step's force."""
    t0 = time.monotonic()

    def _log() -> None:
        n = opt.get_number_of_steps()
        if n == 0:
            return
        log.info(
            "relax[%s]: step %d/%d on %d atoms, max force %s eV/A, %.0fs elapsed",
            rung,
            n,
            steps,
            n_atoms,
            f"{curve[-1]:.4f}" if curve else "?",
            time.monotonic() - t0,
        )

    opt.attach(_log, interval=PROGRESS_EVERY)


def _attach_step_hook(
    opt: Any,
    atoms: Any,
    curve: list[float],
    on_step: Callable[[int, np.ndarray, list[float]], None] | None,
) -> None:
    """Wire ``relax()``'s ``on_step`` onto an ASE optimiser: every step
    hands over the steps completed, the atoms' current Cartesian
    positions (scene-atom order — the order ``_to_ase`` builds) and the
    convergence trace so far. Attach after the curve recorder so
    ``curve`` already carries this step's entry."""
    if on_step is None:
        return

    def _step() -> None:
        on_step(int(opt.get_number_of_steps()), atoms.get_positions(), list(curve))

    opt.attach(_step, interval=1)


#: Rungs that need a rented backend not bundled here. ``ml`` has a real backend
#: (ASE + an MLIP, the ``[dft-ml]`` extra) and ``xtb`` one too (tblite's ASE
#: calculator, the ``[estimate]`` extra); ``ff``/``dft-*`` stay gated.
#: ``emt`` (rung 1) is deliberately *not* here — it's ours, like ``clean``,
#: always-on (ASE is a core dependency).
_RENTED_RUNGS = {"ff", "ml", "dft-fast", "dft-tight"}

#: Fake spring constant (eV/Å²) backing the ``jiggle`` rung's fabricated
#: report: the perturbed geometry reads as a harmonic well, so the
#: reported ``energy``/``max_force``/per-atom ``forces`` stay
#: self-consistent with the displacement the rung actually applied.
#: Typical covalent bond-stretch scale (~5–15 eV/Å²).
_JIGGLE_SPRING = 10.0

#: Default per-axis σ (Å) of the ``jiggle`` rung's Gaussian displacement.
_JIGGLE_SIGMA = 0.05


class JiggleDied(RuntimeError):
    """``relax(fidelity='jiggle', die_at_step=k)``'s simulated kill — the
    stand-in for a mid-run crash (OOM, SIGKILL) when testing
    kill/resume, never raised by a real rung."""


#: EMT's built-in interatomic-potential coverage (ASE's ``emt.py`` parameter
#: table) — a closed set of fcc catalytic metals + light adsorbate elements.
#: This happens to be exactly the Pd/Cu/Ni catalyst screen's metals; anything
#: outside it (a lanthanide, a bcc metal, …) needs the ``ml`` rung instead.
EMT_ELEMENTS = frozenset({"Al", "Ni", "Cu", "Pd", "Ag", "Pt", "Au", "H", "C", "N", "O"})

#: MACE-OFF23's element coverage — small-molecule organic chemistry (Kovács
#: et al. 2023, arXiv:2312.15211: H, C, N, O, F, P, S, Cl, Br, I), not the
#: Z1-89 materials palette MACE-MP-0 covers
#: (:data:`preflight.MACE_MP_ELEMENTS`). Used both to register the
#: ``mace_off`` calculator's coverage and to route a relax with no explicit
#: ``model=`` toward it (:func:`route_ml_model`).
ORGANIC_ELEMENTS: frozenset[str] = frozenset(
    {"H", "C", "N", "O", "F", "P", "S", "Cl", "Br", "I"}
)

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
    # The force-convergence target the caller explicitly asked for (eV/Å),
    # ``None`` when the rung's usual ``max(tol, 0.05)`` convention ran — the
    # handler folds an explicit fmax into the run-cube key and the recorded
    # params, so it is part of the run's identity, not bookkeeping.
    fmax: float | None = None
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
    # A human-readable caveat the rung wants shown beside the envelope (e.g. the
    # clean rung stopped with atoms still over-coordinated, gr461792). ``None``
    # when there is nothing to caveat.
    note: str | None = None
    # Whether an explicit dispersion (DFT-D3) correction was added on top of
    # the MLIP. Part of the calculator's *identity*, not bookkeeping: the same
    # model with and without D3 gives different energies, so the handler folds
    # it into the run-cube cache key and the recorded params.
    dispersion: bool = False
    # ── run-cube cache plumbing — populated by the *handler*, not
    # the pure compute: the content address of this relax, and, on a cache hit,
    # the fact that no compute ran. ``relax()`` itself never sets these.
    from_cache: bool = False
    cache_key: str | None = None
    structure_sha: str | None = None
    final_geometry: dict | None = None
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
    dispersion: bool = False,
    charge: int = 0,
    multiplicity: int | None = None,
    fmax: float | None = None,
    on_step: Callable[[int, np.ndarray, list[float]], None] | None = None,
    seed: int | None = None,
    sigma: float | None = None,
    die_at_step: int | None = None,
) -> RelaxResult:
    """Relax ``scene`` at the given ``fidelity`` rung (mutates in place).

    ``cell`` opts into a **variable-cell** relax on an energy rung (see
    :data:`CELL_MODES`): ``"inplane"`` relaxes the box in-plane with a slab (the
    c-axis / vacuum pinned), ``"full"`` relaxes all six strain components. It is
    only meaningful for an energy rung — the ``clean`` geometry repair has no
    stress, so requesting a cell relax there raises.

    ``fmax`` is an explicit force-convergence target (eV/Å) for an energy
    rung, overriding the usual ``max(tol, 0.05)`` convention — the way to ask
    for a *tighter* relax than the rung's floor. It is part of the run's
    identity (the handler folds it into the run-cube key and the recorded
    params), not a display knob. On a geometry rung there are no forces, so
    it raises rather than being ignored.

    ``dispersion`` adds an explicit DFT-D3 correction on top of the MLIP. It
    defaults **off**, matching the historical behaviour, but any design held
    together by van der Waals contact rather than covalent bonds — a host-guest
    complex, an interlocked mechanical bond, two stacked aromatic faces — is
    badly under-bound without it: the MLIP's short receptive field simply does
    not see the attraction. Only rung ``ml`` has a dispersion correction to add;
    asking for one at ``clean``/``emt`` raises rather than being ignored.

    ``charge`` (total, e) and ``multiplicity`` (2S+1, ``None`` = undeclared)
    reach GFN2 on rung ``xtb`` (:func:`_xtb_calculator`) — an NH2 or NO
    radical is ``multiplicity=2``. No other rung has a total-charge or spin
    input (MACE takes neither), so a non-default value elsewhere raises
    rather than being dropped.

    ``on_step`` — per-optimiser-step checkpoint hook (gr508493): rungs
    with an observable inner loop (``clean``/``emt``/``xtb``/``ml``) call
    it after every step with ``(steps_done, positions, curve)`` — the
    current Cartesian positions in scene-atom order (a fresh array the
    callback may keep) and the per-step convergence trace so far. The
    ``geo`` kernel runs its whole loop inside numba with no per-step
    view, so it never calls it.

    ``seed``/``sigma``/``die_at_step`` are ``jiggle``-rung options
    (:func:`_relax_jiggle`) — a seeded Gaussian displacement with a
    fabricated convergence report, the dev loop's tolerance-test rung
    (dev-loop-scratch spec D0). They are refused on every other rung
    rather than silently ignored.
    """
    if cell not in CELL_MODES:
        raise RelaxUnsupported(
            f"unknown cell relax mode {cell!r} (use 'inplane', 'full', or omit)"
        )
    cell_mode = None if cell == "fixed" else cell
    if cell_mode is not None and fidelity in ("clean", "0", "geo", "jiggle"):
        raise RelaxUnsupported(
            "variable-cell relax needs an energy rung (fidelity='ml'); the "
            "'clean'/'geo' geometry rungs have no stress to relax the cell "
            "against"
        )
    if cell_mode is not None and fidelity in ("emt", "xtb"):
        raise RelaxUnsupported(
            f"variable-cell relax isn't supported at rung {fidelity!r} — "
            "use fidelity='ml'"
        )
    if fmax is not None:
        if fmax <= 0:
            raise RelaxUnsupported(
                f"fmax must be a positive force target (eV/Å), got {fmax!r}"
            )
        if fidelity in ("clean", "0", "geo", "jiggle"):
            raise RelaxUnsupported(
                f"fmax is a force target — rung {fidelity!r} is a geometry "
                "repair with no forces to converge"
            )
    if (seed is not None or sigma is not None or die_at_step is not None) and (
        fidelity != "jiggle"
    ):
        raise RelaxUnsupported(
            f"seed/sigma/die_at_step are 'jiggle'-rung options; rung "
            f"{fidelity!r} has no use for them"
        )
    if dispersion and fidelity not in ("ml",):
        raise RelaxUnsupported(
            f"dispersion=True needs an MLIP rung (fidelity='ml'); rung "
            f"{fidelity!r} has no potential to correct"
        )
    if (charge != 0 or multiplicity is not None) and fidelity != "xtb":
        raise RelaxUnsupported(
            f"charge/multiplicity need rung 'xtb' (GFN2-xTB); rung {fidelity!r} "
            "has no total-charge or spin input"
        )
    if fidelity in ("clean", "0"):
        return _relax_clean(scene, steps=steps, tol=tol, on_step=on_step)
    if fidelity == "geo":
        return _relax_geo(scene, steps=steps, tol=tol)
    if fidelity == "jiggle":
        return _relax_jiggle(
            scene,
            steps=steps,
            tol=tol,
            seed=seed,
            sigma=sigma,
            die_at_step=die_at_step,
            on_step=on_step,
        )
    if fidelity == "emt":
        return _relax_emt(scene, steps=steps, tol=tol, fmax=fmax, on_step=on_step)
    if fidelity == "xtb":
        return _relax_xtb(
            scene,
            steps=steps,
            tol=tol,
            charge=charge,
            multiplicity=multiplicity,
            fmax=fmax,
            on_step=on_step,
        )
    if fidelity == "ml":
        return _relax_ml(
            scene,
            steps=steps,
            tol=tol,
            model=model,
            cell=cell_mode,
            dispersion=dispersion,
            fmax=fmax,
            on_step=on_step,
        )
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


def _over_coordinated(scene: Scene) -> list[str]:
    """Labels of atoms whose bond-cutoff neighbour count exceeds their valence
    budget — the same test as ``validate``'s ``over_valence`` rule."""
    from . import probe

    cov_cn = probe.covalent_coordination_all(scene)
    out: list[str] = []
    for label, atom in scene.atoms.items():
        mv, _known = elements.effective_valence(atom.element, atom.charge)
        if mv is not None and cov_cn[label] > mv:
            out.append(label)
    return out


def _relax_clean(
    scene: Scene,
    *,
    steps: int,
    tol: float,
    on_step: Callable[[int, np.ndarray, list[float]], None] | None = None,
) -> RelaxResult:
    """Rung 0: iteratively separate too-close pairs toward their covalent bond
    length. Pure, fixed-aware. Not an energy minimiser — a geometry sanitiser."""
    cell = scene.cell
    labels = list(scene.atoms)
    curve: list[float] = []
    converged = False
    n = 0
    max_disp = 0.0
    from . import _pair_kernel

    lat, pbc = _pair_kernel.cell_arrays(cell)
    radii = _pair_kernel.radii_of(scene, labels)
    for n in range(1, steps + 1):
        frac = np.empty((len(labels), 3), dtype=np.float64)
        for k, label in enumerate(labels):
            frac[k] = scene.atoms[label].frac
        disp_arr = _pair_kernel.clean_displacements(frac, lat, pbc, radii)
        disp = {label: disp_arr[k] for k, label in enumerate(labels)}
        max_disp = 0.0
        for label in labels:
            atom = scene.atoms[label]
            dfrac = cell.cart_to_frac(disp[label]) * _free_axes(atom.fixed)
            atom.frac = cell.wrap(atom.frac + dfrac)
            moved = float(np.linalg.norm(cell.frac_to_cart(dfrac)))
            max_disp = max(max_disp, moved)
        curve.append(round(max_disp, 4))
        if on_step is not None:
            on_step(
                n,
                np.array([cell.frac_to_cart(scene.atoms[la].frac) for la in labels]),
                list(curve),
            )
        if max_disp < tol:
            converged = True
            break
    # Rung 0 has no calculator of its own — a cheap EMT single-point estimate
    # (gripe 161576) gives the LLM a qualitative force sense anyway, on the
    # geometry this pass just repaired. ``None`` (never fabricated) when the
    # element set falls outside EMT's coverage or ASE isn't installed.
    approx_forces = estimate_forces_emt(scene)
    # A pair sweep that goes quiet is not a repaired structure (gr461792): it
    # pushes pairs only to 98 % of the covalent-radii sum, which is still inside
    # the 1.2x bond-detection cutoff, and it cannot tell a bonded neighbour from
    # an interpenetrating one. Atoms left over-coordinated therefore mean the
    # clash is unresolved, whatever the last sweep's move was.
    over = _over_coordinated(scene)
    note = None
    if over:
        converged = False
        shown = ", ".join(over[:5]) + (", ..." if len(over) > 5 else "")
        note = (
            f"{len(over)} atom(s) still have more neighbours within bond cutoff "
            f"than their valence allows ({shown}); 'clean' only separates pairs "
            "to the covalent-radii sum and cannot pull non-bonded atoms out of "
            "bonding range - use fidelity='geo' (bond-aware) for interpenetrating "
            "parts"
        )
    return RelaxResult(
        rung="clean",
        note=note,
        converged=converged,
        n_steps=n,
        max_disp=max_disp,
        curve=curve,
        forces=approx_forces,
        forces_approx=approx_forces is not None,
        forces_source="emt" if approx_forces is not None else None,
    )


def _relax_geo(scene: Scene, *, steps: int, tol: float) -> RelaxResult:
    """Rung ``geo``, beside ``clean``: the graph-first hybridization-aware
    relax (:mod:`precis.structure.georelax`, the closed se-nanobud-graph spec
    §1) — bond springs + non-bond repulsion + a VSEPR angle-restoring term
    keyed on each atom's own declared/default hybridization. Like ``clean``,
    it has no potential energy of its own (``energy``/``max_force`` stay
    ``None``); unlike ``clean``, it restores bond ANGLES too (an sp2 vertex
    toward 120°, an sp3 one toward 109.47°/its lone-pair-adjusted variant —
    :func:`precis.structure.vsepr.ideal_angle`), not just bond lengths.

    Scene atoms translate to :func:`~precis.structure.georelax.relax_graph`'s
    plain-array inputs: Cartesian coordinates (``frac`` through the cell),
    a bond graph over atom INDICES (``scene.bonds``, both endpoints
    resolved through this scene's own label set — a bond into an unrelated
    scene, never possible here, would simply be skipped), each atom's own
    ``hybridization`` (declared intent, defaulting to ``"sp3"`` when unset —
    the same default :func:`precis.structure.vsepr.infer_hybridization`
    falls back to for an undeclared atom with no bond-order signal to
    guess from), and ``fixed`` as a whole-atom pin (any fixed axis pins the
    whole atom — the shared core has no per-axis freedom, see the module
    docstring).

    Coordinates come from ``frac_to_cart`` directly, per atom, with NO
    minimum-image unwrapping across a bond (unlike ``clean``, which walks
    every pair through ``cell.mic``) — a bond that crosses a periodic wall
    reads its wrapped, not true, separation. This rung's intended input is
    the graph-surgery/generator molecular scene (like
    :func:`~precis.structure.georelax.register`, "molecular scenes only" —
    the closed se-nanobud-graph spec §1 item 4), which is non-periodic or
    sits in a large vacuum box with every atom already unwrapped; a genuine
    periodic-slab use of this rung is future work, not this slice.
    """
    labels = list(scene.atoms)
    if not labels:
        return RelaxResult(rung="geo", converged=True, n_steps=0, max_disp=0.0)
    elements_list = [scene.atoms[la].element for la in labels]
    label_index = {la: idx for idx, la in enumerate(labels)}
    before = np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])
    coords = before.copy()
    bonds = [
        (label_index[b.i], label_index[b.j])
        for b in scene.bonds
        if b.i in label_index and b.j in label_index
    ]
    pinned = {idx for idx, la in enumerate(labels) if scene.atoms[la].fixed}
    hybridizations = [scene.atoms[la].hybridization or "sp3" for la in labels]

    trace = georelax.relax_graph(
        elements_list,
        coords,
        bonds,
        pinned,
        hybridizations=hybridizations,
        iters=steps,
        tol=tol,
    )

    for idx, la in enumerate(labels):
        scene.atoms[la].frac = scene.cell.wrap(scene.cell.cart_to_frac(coords[idx]))
    max_disp = float(np.linalg.norm(coords - before, axis=1).max())
    # Same qualitative-forces estimate as ``clean`` (gripe 161576) — ``geo``
    # has no calculator of its own either.
    approx_forces = estimate_forces_emt(scene)
    return RelaxResult(
        rung="geo",
        converged=trace.converged,
        n_steps=trace.n_steps,
        max_disp=round(max_disp, 4),
        curve=trace.curve,
        forces=approx_forces,
        forces_approx=approx_forces is not None,
        forces_source="emt" if approx_forces is not None else None,
    )


def _relax_jiggle(
    scene: Scene,
    *,
    steps: int,
    tol: float,
    seed: int | None,
    sigma: float | None,
    die_at_step: int | None,
    on_step: Callable[[int, np.ndarray, list[float]], None] | None = None,
) -> RelaxResult:
    """Rung ``jiggle``: a seeded Gaussian perturbation wearing an energy
    rung's report (dev-loop-scratch spec D0 — Reto's FakeRelaxer: "jiggle
    things about a random amount so we can test how the system tolerates
    moving/relaxed things").

    Each free atom takes one per-axis ``N(0, sigma)`` draw (``sigma``
    defaults to :data:`_JIGGLE_SIGMA` = 0.05 Å), the per-atom displacement
    capped at 3σ and masked by ``fixed`` axes exactly like ``clean``.
    The displacement lands incrementally — ``1/steps`` of it per
    fabricated optimiser step — so ``die_at_step=k`` leaves the scene at
    a mid-run state (the model for a kill between checkpoints: step k's
    work never happened, steps 1..k-1 did).

    The report is fabricated but self-consistent: the perturbation reads
    as a harmonic well of stiffness :data:`_JIGGLE_SPRING`, so
    ``energy`` = −½kΣ‖d‖², per-atom ``forces`` = the spring force scaled
    by the converged fraction, and ``curve`` is a monotone geometric
    decay of the max force to below ``max(tol, 0.05)`` (the rungs' usual
    convergence floor). Progress lines match the energy rungs' format,
    and ``on_step`` fires per fabricated step with the same
    ``(steps_done, positions, curve)`` contract (gr508493) — a
    ``die_at_step`` kill therefore leaves a real mid-run checkpoint
    trail. It is **never** a default — only ``fidelity='jiggle'``
    reaches it — and ``forces_approx``/``forces_source='jiggle'`` label
    the numbers fabricated so they can never be mistaken for a
    calculator's.
    """
    labels = list(scene.atoms)
    if not labels:
        return RelaxResult(
            rung="jiggle",
            converged=True,
            n_steps=0,
            max_disp=0.0,
            curve=[],
            energy=0.0,
            max_force=0.0,
            model="jiggle",
        )
    if sigma is not None and sigma < 0:
        raise RelaxUnsupported(f"jiggle sigma must be >= 0 (Å), got {sigma!r}")
    sig = _JIGGLE_SIGMA if sigma is None else float(sigma)
    if die_at_step is not None and (
        isinstance(die_at_step, bool)
        or not isinstance(die_at_step, int)
        or die_at_step < 1
    ):
        raise RelaxUnsupported(
            f"jiggle die_at_step must be a step number >= 1, got {die_at_step!r}"
        )
    rng = np.random.default_rng(seed)
    disp = rng.normal(0.0, sig, size=(len(labels), 3))
    for k, la in enumerate(labels):
        disp[k] *= _free_axes(scene.atoms[la].fixed)
    norms = np.linalg.norm(disp, axis=1)
    cap = 3.0 * sig
    if cap > 0.0:
        over = norms > cap
        disp[over] *= (cap / norms[over])[:, None]
        norms = np.linalg.norm(disp, axis=1)
    max_move = float(norms.max()) if len(norms) else 0.0

    # The fake landscape: a harmonic well of stiffness _JIGGLE_SPRING.
    # f_start is the max force the full displacement would sit under;
    # the curve decays geometrically from it to below force_tol.
    force_tol = max(tol, 0.05)
    f_start = _JIGGLE_SPRING * max_move
    t0 = time.monotonic()
    curve: list[float] = []
    step_disp = disp / steps if steps else disp
    n = 0
    for n in range(1, steps + 1):
        if die_at_step is not None and n == die_at_step:
            raise JiggleDied(
                f"relax jiggle: die_at_step={n} — simulated kill "
                f"({n - 1}/{steps} steps applied)"
            )
        for k, la in enumerate(labels):
            atom = scene.atoms[la]
            dfrac = scene.cell.cart_to_frac(step_disp[k])
            atom.frac = scene.cell.wrap(atom.frac + dfrac)
        f_now = (
            f_start * (force_tol * 0.8 / f_start) ** (n / steps)
            if f_start > 0.0
            else 0.0
        )
        curve.append(round(f_now, 4))
        if on_step is not None:
            on_step(
                n,
                np.array(
                    [scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels]
                ),
                list(curve),
            )
        if n % PROGRESS_EVERY == 0:
            log.info(
                "relax[%s]: step %d/%d on %d atoms, max force %s eV/A, %.0fs elapsed",
                "jiggle",
                n,
                steps,
                len(labels),
                f"{curve[-1]:.4f}" if curve else "?",
                time.monotonic() - t0,
            )
    # Residual spring force per atom at the converged fraction — the same
    # scale the last curve point reports (max over atoms = curve[-1]).
    frac_left = curve[-1] / f_start if f_start > 0.0 else 0.0
    per_atom_forces = {
        la: [float(x) for x in (-_JIGGLE_SPRING * disp[k] * frac_left)]
        for k, la in enumerate(labels)
    }
    energy = -0.5 * _JIGGLE_SPRING * float((norms**2).sum())
    return RelaxResult(
        rung="jiggle",
        converged=True,
        n_steps=n,
        max_disp=round(max_move / steps, 4) if steps else 0.0,
        curve=curve,
        energy=round(energy, 4),
        max_force=curve[-1] if curve else 0.0,
        model="jiggle",
        forces=per_atom_forces,
        forces_approx=True,
        forces_source="jiggle",
        note="jiggle rung: fabricated report — seeded displacement, no "
        "calculator (dev-loop tolerance test)",
    )


def _relax_emt(
    scene: Scene,
    *,
    steps: int,
    tol: float,
    fmax: float | None = None,
    on_step: Callable[[int, np.ndarray, list[float]], None] | None = None,
) -> RelaxResult:
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
        from ase.optimize import BFGS as _Optimizer

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
    _attach_progress(opt, rung="emt", n_atoms=len(labels), steps=steps, curve=curve)
    _attach_step_hook(opt, atoms, curve, on_step)
    force_tol = fmax if fmax is not None else max(tol, 0.05)
    converged = bool(opt.run(fmax=force_tol, steps=steps))

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
        fmax=fmax,
        forces=per_atom_forces,
        forces_approx=False,
        forces_source="emt",
    )


def _vet_charge_multiplicity(charge: int, multiplicity: int | None) -> None:
    """Type and range of a total charge (any int) and a multiplicity
    (``None`` or an int >= 1); ``ValueError`` otherwise."""
    if isinstance(charge, bool) or not isinstance(charge, int):
        raise ValueError(f"charge must be an integer (e), got {charge!r}")
    if multiplicity is not None and (
        isinstance(multiplicity, bool)
        or not isinstance(multiplicity, int)
        or multiplicity < 1
    ):
        raise ValueError(
            f"multiplicity must be an integer >= 1 (2S+1: 1 singlet, 2 "
            f"doublet, 3 triplet), got {multiplicity!r}"
        )


def check_charge_multiplicity(
    elements: list[str], charge: int, multiplicity: int | None
) -> int:
    """Vet a total ``charge`` and spin ``multiplicity`` (2S+1) against the
    atoms they describe; return the electron count (sum of Z minus charge).

    ``multiplicity=None`` means "not declared" and skips the parity test
    (:func:`_xtb_calculator`'s legacy default). A declared multiplicity
    must match the electron count's parity: an even count takes an odd
    multiplicity (singlet, triplet), an odd count an even one (NO with 15
    electrons is a doublet). Core electrons pair up, so the parity of the
    total count is the parity of GFN2's valence count. Raises
    ``ValueError`` naming the fix; the xtb backend would otherwise fail
    deep in the SCF with a bare "not compatible" message."""
    from ase.data import atomic_numbers

    _vet_charge_multiplicity(charge, multiplicity)
    unknown = sorted({e for e in elements if e not in atomic_numbers})
    if unknown:
        raise ValueError(f"unknown element symbol(s) {unknown}")
    electrons = sum(atomic_numbers[e] for e in elements) - charge
    if electrons < 0:
        raise ValueError(
            f"charge {charge:+d} removes more electrons than the "
            f"{electrons + charge} the atoms carry"
        )
    if multiplicity is not None and (electrons + multiplicity - 1) % 2:
        lowest = 1 if electrons % 2 == 0 else 2
        raise ValueError(
            f"multiplicity {multiplicity} does not fit {electrons} electrons "
            f"(charge {charge:+d}): an {'even' if electrons % 2 == 0 else 'odd'} "
            f"count takes an {'odd' if electrons % 2 == 0 else 'even'} "
            f"multiplicity — the lowest is {lowest}"
        )
    return electrons


def _xtb_calculator(
    *,
    charge: int = 0,
    multiplicity: int | None = None,
    spin_polarization: bool = False,
):
    """The GFN2-xTB ASE calculator (tblite), or raise RelaxUnsupported.

    The import is isolated here so a missing backend gives one clean
    ``RelaxUnsupported`` naming the extra AND the platform limit, never a
    stray ImportError — PyPI's tblite has no linux-aarch64 wheel, so
    ``[estimate]`` skips it there (``pyproject.toml``'s marker comment) and
    the aarch64 compute hosts take the wheelhouse build from
    ``scripts/build-tblite-wheel`` instead (gr488907). GFN2
    at 1000 K electronic temperature is the recipe the hexfold neck re-runs
    of 2026-10-11 validated (hexfold-corner/s4_xtb.py).

    ``charge`` (e) and ``multiplicity`` (2S+1) are handed to tblite as its
    own ``charge``/``multiplicity`` parameters (tblite runs
    ``multiplicity - 1`` unpaired electrons). The defaults are what every
    run before docs/backlog/pocket-from-molecule.md slice 1 got: neutral,
    and ``multiplicity=None`` so tblite counts unpaired electrons from the
    atoms' initial magnetic moments, which the scene→ASE bridges never set
    — zero unpaired electrons: a closed shell for an even count, and for
    an odd one an equal split that leaves the singly occupied orbital
    half-filled in each spin channel. Parity against the atoms is
    the caller's check (:func:`check_charge_multiplicity`), since this
    function never sees them. The calculator is not spin-polarised, so
    multiplicity changes the occupations, not a spin density — unless
    ``spin_polarization=True``, which hands tblite ``spin_polarization=1.0``
    (spin-polarised GFN2: a spin density, and for NO a 0.196 eV lower
    energy than the plain doublet). Off by default: pocket-from-molecule
    open question 12 is unruled."""
    _vet_charge_multiplicity(charge, multiplicity)
    # gr495146: a larger OpenMP worker stack than macOS's ~512 KB default,
    # set before the first TBLite instantiation; setdefault keeps operator
    # overrides. Not the cause of the macOS xdist segfault (see
    # :func:`_xtb_needs_isolation`); it is inherited by the isolated child.
    os.environ.setdefault("OMP_STACKSIZE", "1G")
    if _xtb_needs_isolation():
        from ._xtb_isolated import IsolatedTBLite

        return IsolatedTBLite(
            charge=charge,
            multiplicity=multiplicity,
            spin_polarization=spin_polarization,
        )
    return _native_tblite(
        charge=charge, multiplicity=multiplicity, spin_polarization=spin_polarization
    )


def _native_tblite(
    *, charge: int, multiplicity: int | None, spin_polarization: bool = False
):
    """The in-process tblite ASE calculator (GFN2-xTB, 1000 K electronic
    temperature). ``spin_polarization`` is passed only when on, so the
    default call is the one every earlier run made."""
    try:
        from tblite.ase import TBLite
    except ImportError as exc:
        raise RelaxUnsupported(
            "relax rung 'xtb' (GFN2-xTB) needs tblite — pip install "
            "'precis-mcp[estimate]'; PyPI has no linux-aarch64 wheel, so "
            "those hosts install the wheelhouse build (scripts/build-tblite-wheel)"
        ) from exc
    extra: dict[str, Any] = {"spin_polarization": 1.0} if spin_polarization else {}
    return TBLite(
        method="GFN2-xTB",
        charge=charge,
        multiplicity=multiplicity,
        electronic_temperature=1000.0,
        verbosity=0,
        **extra,
    )


def _xtb_needs_isolation() -> bool:
    """Whether tblite must run in a child process instead of this one
    (gr495146).

    tblite's macOS wheel carries its own OpenMP runtimes (``libgomp`` and a
    ``libomp``) and torch — the MLIP rungs, and anything that imports mace —
    carries a different ``libomp``. They cannot share a process, in either
    import order: tblite's ``singlepoint`` segfaults once torch is loaded, and
    ``import torch`` aborts (``OMP: Error #15``) once tblite is. Measured on
    arm64 macOS with OMP_STACKSIZE unset/64M/256M/1G and OMP_NUM_THREADS
    unset/1/2/3 (no effect), and KMP_DUPLICATE_LIB_OK only trades the crash
    for ``OMP: Error #179``. Linux wheels share one ``libgomp``, and a macOS
    host without torch has nothing to clash with. A long-lived worker can run
    both rungs, so isolate whenever torch is installed, not only after it has
    been imported. A substituted or import-blocked ``tblite.ase`` is not the
    native library and stays in-process."""
    if sys.platform != "darwin":
        return False
    mod = sys.modules.get("tblite.ase", False)
    if mod is None or (mod is not False and not hasattr(mod, "__file__")):
        return False
    return importlib.util.find_spec("torch") is not None


def _relax_xtb(
    scene: Scene,
    *,
    steps: int,
    tol: float,
    charge: int = 0,
    multiplicity: int | None = None,
    spin_polarization: bool = False,
    fmax: float | None = None,
    on_step: Callable[[int, np.ndarray, list[float]], None] | None = None,
) -> RelaxResult:
    """Rung ``xtb``: relax on GFN2-xTB via tblite's ASE calculator.

    The relaxer of record for hexfold blocks (Reto 2026-10-11, gr472846):
    a semi-empirical quantum rung that resolves what the ``stick``/``geo``
    previews and the closed-element-set ``emt`` rung cannot — real
    energies + forces over the whole light-element palette at molecular
    cost. Settings mirror the reference recipe the 2026-10-11 neck
    re-runs converged on (hexfold-corner/s4_xtb.py): GFN2-xTB at 1000 K
    electronic temperature, ASE FIRE (``maxstep`` 0.1 Å), the ladder's
    usual ``fmax=max(tol, 0.05)`` convention.

    Honours ``fixed`` as a **whole-atom** pin (like ``geo``: any fixed
    axis pins the atom — ``ase.constraints.FixAtoms``), and the cell is
    never relaxed — a molecule stays whatever box it was saved in
    (variable-cell requests are refused in :func:`relax` before this
    runs). A missing tblite raises :class:`RelaxUnsupported` naming the
    platform limit (no linux-aarch64 wheel on PyPI).

    ``charge``/``multiplicity`` reach the calculator unchanged
    (:func:`_xtb_calculator`); a declared multiplicity is first checked
    against the atoms' electron count (:func:`check_charge_multiplicity`,
    ``ValueError`` on a parity mismatch). ``spin_polarization`` turns on
    spin-polarised GFN2 (:func:`_xtb_calculator`); only the
    pocket-from-molecule held-shape strain passes it.
    """
    if not export.ase_available():
        raise RelaxUnsupported(
            "relax rung 'xtb' needs ASE + tblite — pip install 'precis-mcp[estimate]'"
        )
    from ase.constraints import FixAtoms
    from ase.optimize import FIRE

    check_charge_multiplicity(
        [a.element for a in scene.atoms.values()], charge, multiplicity
    )
    atoms = export._to_ase(scene)
    labels = list(scene.atoms)
    before = np.array([scene.cell.frac_to_cart(scene.atoms[la].frac) for la in labels])

    pinned = [idx for idx, la in enumerate(labels) if scene.atoms[la].fixed]
    if pinned:
        atoms.set_constraint(FixAtoms(indices=pinned))
    atoms.calc = _xtb_calculator(
        charge=charge, multiplicity=multiplicity, spin_polarization=spin_polarization
    )

    curve: list[float] = []

    def _record() -> None:
        f = atoms.get_forces()
        curve.append(round(float(np.sqrt((f**2).sum(axis=1).max())), 4))

    opt = FIRE(atoms, maxstep=0.1, logfile=None)
    opt.attach(_record, interval=1)
    _attach_progress(opt, rung="xtb", n_atoms=len(labels), steps=steps, curve=curve)
    _attach_step_hook(opt, atoms, curve, on_step)
    force_tol = fmax if fmax is not None else max(tol, 0.05)
    converged = bool(opt.run(fmax=force_tol, steps=steps))

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
        rung="xtb",
        converged=converged,
        n_steps=int(opt.get_number_of_steps()),
        max_disp=round(max_disp, 4),
        curve=curve,
        energy=float(atoms.get_potential_energy()),
        max_force=round(max_force, 4),
        model="gfn2-xtb",
        fmax=fmax,
        forces=per_atom_forces,
        forces_approx=False,
        forces_source="gfn2-xtb",
    )


def route_ml_model(scene: Scene) -> str:
    """Composition-based default for the ``ml`` rung's calculator when the
    caller passes no explicit ``model=`` (gripe 285774).

    MACE-MP-0 is a *materials* potential (periodic solids, Materials Project
    relaxation trajectories); MACE-OFF23 is trained on small organic
    molecules and captures the noncovalent contacts (H-bonds, pi-stacking)
    a materials MLIP sits off-distribution for. Routes to ``'mace_off'``
    only when the scene is all-organic-element (:data:`ORGANIC_ELEMENTS`),
    non-periodic (no cell axis carries ``pbc``), and formally neutral (the
    declared per-atom ``oxidation`` states sum to zero) — every other case,
    including the ambiguous/mixed one, keeps the historical ``'mace_mp'``
    default rather than silently guessing toward the less-battle-tested
    organic model. This is only the *default*: an explicit ``model=`` always
    wins over this routing (see ``relax()``/the handler)."""
    elements_present = {a.element for a in scene.atoms.values()}
    if not elements_present or elements_present - ORGANIC_ELEMENTS:
        return "mace_mp"
    if any(scene.cell.pbc):
        return "mace_mp"
    if sum(a.oxidation or 0 for a in scene.atoms.values()) != 0:
        return "mace_mp"
    return "mace_off"


def _dftd3_wrap(calc, *, device: str = "cpu", dtype: str = "float64"):
    """Layer a DFT-D3(BJ) correction onto ``calc`` via ``torch_dftd`` (an ASE
    ``SumCalculator``) — the manual equivalent of what
    ``mace_mp(dispersion=True)`` does internally. Needed for ``mace_off``,
    which (unlike ``mace_mp``) takes no ``dispersion`` kwarg of its own.
    Mirrors mace_mp's own D3 defaults (BJ damping, PBE functional, 40 Bohr
    cutoff) so a dispersion-corrected relax means the same physics
    regardless of which MLIP it rides on. The caller has already confirmed
    ``torch_dftd`` imports before calling this."""
    import torch
    from ase import units
    from ase.calculators.mixing import SumCalculator
    from torch_dftd.torch_dftd3_calculator import TorchDFTD3Calculator

    torch_dtype = torch.float64 if dtype == "float64" else torch.float32
    d3 = TorchDFTD3Calculator(
        device=device,
        damping="bj",
        dtype=torch_dtype,
        xc="pbe",
        cutoff=40.0 * units.Bohr,
    )
    return SumCalculator([calc, d3])


#: How many distinct ``(family, dispersion)`` MLIP calculators to keep
#: loaded. Three families × two dispersion states is the whole space, and a
#: long-lived worker realistically touches one or two.
_ML_CALC_CACHE_MAX = 6


@lru_cache(maxsize=_ML_CALC_CACHE_MAX)
def _cached_ml_calculator(family: str, dispersion: bool):
    """Build — once per ``(family, dispersion)`` — the torch-backed ASE
    calculator, and hold it for the life of the process.

    **Why this is cached (gr450103).** Every call constructs a full torch
    ``nn.Module`` with its weights at ``float64``. ``struct_relax`` exposes
    only ``dispatch`` (no submit/poll), so each relax runs inline in the
    long-lived ``job_ssh_node`` worker; an uncached build left a discarded
    model per dispatch that the allocator never returned to the OS. Measured
    on castor 2026-09-25: RSS ~117 GB after two days on a 122 GB box, its
    16 GB swap exhausted, ``kswapd0`` at 84% CPU and the handler silent for
    ~48 h — starved, not blocked; a restart came back at 216 MB. This
    mirrors the loaded-once-per-process discipline ``BgeM3Embedder``
    (``precis/embedder.py``) already follows.

    **Reuse is safe, concurrent use is not.** An ASE ``Calculator`` keys its
    cached ``results`` on the atoms' state, so handing the same instance to
    one relax after another recomputes correctly — that is the ordinary ASE
    pattern. It is NOT safe to drive one instance from two threads at once;
    the lane that runs these is serial by construction (``precis worker
    --only job_ssh_node --batch-size 1``, playbook 43, one dispatch at a
    time under a capacity-1 ``gpu`` slot). Keep it that way, or give each
    worker thread its own calculator.

    The caller (:func:`_ml_calculator`) has already normalised ``family``
    and probed every import, so this raises no ``RelaxUnsupported`` of its
    own; an exception here is a genuine backend failure and is deliberately
    not cached (``lru_cache`` only stores successful returns).
    """
    if family == "mace_mp":
        from mace.calculators import mace_mp

        return mace_mp(default_dtype="float64", dispersion=dispersion)
    if family == "mace_off":
        from mace.calculators import mace_off

        calc = mace_off(default_dtype="float64")
        return _dftd3_wrap(calc) if dispersion else calc
    from chgnet.model.dynamics import CHGNetCalculator

    return CHGNetCalculator()


def _ml_calculator(model: str, *, dispersion: bool = False):
    """Return a cached ASE calculator for an MLIP, or raise RelaxUnsupported.

    The import is isolated here so a missing backend gives one clean
    ``RelaxUnsupported`` with an install hint, never a stray ImportError.

    ``dispersion`` asks MACE to add a DFT-D3 correction, which needs
    ``torch-dftd`` alongside the MLIP itself. That import is probed *here*,
    before the model loads, so a missing D3 backend is one clean
    ``RelaxUnsupported`` rather than either a stray ImportError deep inside
    ``mace_mp`` or — far worse — a silently uncorrected relax reported as a
    dispersion-corrected one.

    Normalisation, those probes and every ``RelaxUnsupported`` stay on this
    (uncached) side of :func:`_cached_ml_calculator` on purpose: the probes
    are what make the error messages clean, they must re-run on every call
    so a backend that disappears is still reported, and only the expensive
    model build belongs in the cache.
    """
    name = (model or "mace_mp").lower().replace("-", "_")
    if name in ("mace", "mace_mp", "mace_mp_0"):
        try:
            from mace.calculators import mace_mp  # noqa: F401
        except ImportError as exc:
            raise RelaxUnsupported(
                "relax rung 'ml' (MACE) needs the [dft-ml] extra — "
                "pip install 'precis-mcp[dft-ml]'"
            ) from exc
        if dispersion:
            try:
                import torch_dftd
            except ImportError as exc:
                raise RelaxUnsupported(
                    "dispersion=True (DFT-D3 on top of MACE) needs torch-dftd — "
                    "pip install 'precis-mcp[dft-ml]'"
                ) from exc
        return _cached_ml_calculator("mace_mp", dispersion)
    if name in ("mace_off", "mace_off23"):
        try:
            from mace.calculators import mace_off  # noqa: F401
        except ImportError as exc:
            raise RelaxUnsupported(
                "relax rung 'ml' (MACE-OFF) needs the [dft-ml] extra — "
                "pip install 'precis-mcp[dft-ml]'"
            ) from exc
        if dispersion:
            try:
                import torch_dftd  # noqa: F401
            except ImportError as exc:
                raise RelaxUnsupported(
                    "dispersion=True (DFT-D3 on top of MACE-OFF) needs "
                    "torch-dftd — pip install 'precis-mcp[dft-ml]'"
                ) from exc
        return _cached_ml_calculator("mace_off", dispersion)
    if name == "chgnet":
        if dispersion:
            raise RelaxUnsupported(
                "CHGNet has no dispersion correction — use model='mace_mp' "
                "for a dispersion-corrected relax"
            )
        try:
            from chgnet.model.dynamics import CHGNetCalculator  # noqa: F401
        except ImportError as exc:
            raise RelaxUnsupported(
                "relax rung 'ml' (CHGNet) needs the [dft-ml] extra — "
                "pip install 'precis-mcp[dft-ml]'"
            ) from exc
        return _cached_ml_calculator("chgnet", False)
    raise RelaxUnsupported(
        f"unknown MLIP model {model!r} (try 'mace_mp', 'mace_off', or 'chgnet')"
    )


def ml_optimizer(n_atoms: int) -> Any:
    """The ASE optimiser class :func:`_relax_ml` uses for ``n_atoms``:
    ``BFGS`` up to :data:`BFGS_MAX_ATOMS`, ``LBFGS`` above (the reason is
    on the constant)."""
    if n_atoms > BFGS_MAX_ATOMS:
        from ase.optimize import LBFGS

        return LBFGS
    from ase.optimize import BFGS

    return BFGS


def calculator_device(calc: Any) -> str | None:
    """The torch device an MLIP calculator's model sits on (``"cuda:0"``,
    ``"cpu"``), or ``None`` when it cannot be read. Reads the first model
    parameter (MACE keeps ``models``, CHGNet ``model``), else the
    calculator's ``device``; looks through ASE mixing wrappers (the DFT-D3
    ``SumCalculator``)."""
    for model in getattr(calc, "models", None) or [getattr(calc, "model", None)]:
        params = getattr(model, "parameters", None)
        if callable(params):
            try:
                return str(next(iter(params())).device)
            except (StopIteration, TypeError, AttributeError):
                pass
    device = getattr(calc, "device", None)
    if device is not None:
        return str(device)
    mixer = getattr(calc, "mixer", None)
    for inner in getattr(mixer, "calcs", None) or getattr(calc, "calcs", None) or []:
        found = calculator_device(inner)
        if found is not None:
            return found
    return None


def ml_device_check(
    model: str, *, dispersion: bool = False, cuda_requested: bool = False
) -> dict[str, Any] | None:
    """Load the MLIP calculator (the cached one :func:`_relax_ml` will use)
    and report where its model actually sits: ``{model, device,
    cuda_available, gpu_present, cuda_requested}`` plus ``warning`` when the
    model is on CPU although CUDA was requested, torch sees CUDA, or the
    host has an NVIDIA GPU torch cannot use (a CPU-only torch build). Logs
    the device. ``None`` when the backend is absent — the relax itself then
    raises :class:`RelaxUnsupported`."""
    import shutil

    try:
        calc = _ml_calculator(model, dispersion=dispersion)
    except RelaxUnsupported:
        return None
    device = calculator_device(calc)
    cuda_available: bool | None = None
    if importlib.util.find_spec("torch") is not None:
        import torch

        cuda_available = bool(torch.cuda.is_available())
    gpu_present = shutil.which("nvidia-smi") is not None
    report: dict[str, Any] = {
        "model": model,
        "device": device,
        "cuda_available": cuda_available,
        "gpu_present": gpu_present,
        "cuda_requested": cuda_requested,
    }
    if (device or "").startswith("cpu") and (
        cuda_requested or cuda_available or gpu_present
    ):
        why = (
            "CUDA was requested"
            if cuda_requested
            else "torch sees CUDA"
            if cuda_available
            else "the host has an NVIDIA GPU but torch sees no CUDA"
        )
        report["warning"] = f"MLIP {model} runs on CPU although {why}"
        log.warning("relax[ml]: %s", report["warning"])
    else:
        log.info("relax[ml]: %s loaded on device %s", model, device or "unknown")
    return report


def _cell_filter(atoms, cell: str):
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
    scene: Scene,
    *,
    steps: int,
    tol: float,
    model: str,
    cell: str | None = None,
    dispersion: bool = False,
    fmax: float | None = None,
    on_step: Callable[[int, np.ndarray, list[float]], None] | None = None,
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

    ``dispersion`` adds DFT-D3 on top of the MLIP (see :func:`_ml_calculator`)
    and is recorded on the result, since it changes the energy the run-cube
    caches under.

    The optimiser is ``BFGS`` up to :data:`BFGS_MAX_ATOMS` atoms and
    ``LBFGS`` above (:func:`ml_optimizer`; the dense-Hessian cost is on the
    constant). The model's device is logged at start and progress every
    :data:`PROGRESS_EVERY` steps.
    """
    if not export.ase_available():
        raise RelaxUnsupported(
            "relax rung 'ml' needs ASE + an MLIP — pip install 'precis-mcp[dft-ml]'"
        )
    from ase.constraints import FixCartesian

    # raises RelaxUnsupported if the MLIP (or, with dispersion, D3) is absent
    calc = _ml_calculator(model, dispersion=dispersion)
    atoms = export._to_ase(scene)
    labels = list(scene.atoms)
    optimizer = ml_optimizer(len(labels))
    log.info(
        "relax[ml]: %s on device %s, %d atoms, optimizer %s",
        model,
        calculator_device(calc) or "unknown",
        len(labels),
        optimizer.__name__,
    )
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

    opt = optimizer(opt_target, logfile=None)
    opt.attach(_record, interval=1)
    _attach_progress(opt, rung="ml", n_atoms=len(labels), steps=steps, curve=curve)
    _attach_step_hook(opt, atoms, curve, on_step)
    force_tol = fmax if fmax is not None else max(tol, 0.05)
    converged = bool(opt.run(fmax=force_tol, steps=steps))

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
        fmax=fmax,
        forces=per_atom_forces,
        forces_approx=False,
        forces_source=model,
        dispersion=dispersion,
    )
