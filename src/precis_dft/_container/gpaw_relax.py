"""``precis-dft-run gpaw-relax`` — the in-container relax runner.

Reads ``<in>/POSCAR`` + ``<in>/params.json``, relaxes the structure
with GPAW + ASE, and writes ``<out>/result.json`` in the shape
:func:`precis_dft.jobs.gpaw_relax.parse_result` expects:

    {"ok": true,
     "scalars": {"E_tot", "converged", "max_force", "steps", ...},
     "relaxed_poscar": "...", "gpaw_version": "...", "functional": "..."}

On any failure it writes ``{"ok": false, "error", "traceback"}`` and
returns a non-zero exit code, so the host side reports the failure
either way (rc != 0 *and* a parseable failure record).

**Parallel.** Under ``mpirun -np N`` every rank runs this same code:
GPAW splits the work internally, but the *file* writes must not be
duplicated — N ranks racing on one ``result.json`` is a torn file at
best. Every write here is therefore gated on rank 0
(:func:`_world`), and the result records the rank count it actually
ran with, so the host can tell a 1-rank run from an N-rank one without
reading ``gpaw.txt``. Reads are left ungated: each rank reads the same
two input files off the same mount, which is cheaper and simpler than
broadcasting them.

GPAW is imported lazily — this module must stay importable without it.
"""

from __future__ import annotations

import contextlib
import json
import traceback
from pathlib import Path
from typing import Any

from precis_dft.structures import atoms_from_poscar, canonical_poscar

#: LCAO basis. Plane-wave (mode='pw') is a v1.1 follow-up; see
#: ``job_types/_dft_settings.py``.
DEFAULT_BASIS = "dzp"


class _SerialWorld:
    """Stand-in for GPAW's communicator when GPAW is absent (this module
    stays importable outside the image) or the build is serial."""

    rank = 0
    size = 1


def _world() -> Any:
    """GPAW's MPI communicator, or :class:`_SerialWorld`.

    A serial GPAW build still exposes ``gpaw.mpi.world`` with
    ``size == 1``, so callers never need to branch on which one they got.
    """
    try:
        from gpaw.mpi import world
    except Exception:
        return _SerialWorld()
    return world


def gpaw_kwargs_from_params(params: dict[str, Any]) -> dict[str, Any]:
    """Translate the job's ``params.dft`` block into GPAW constructor
    kwargs. Pure — no GPAW import — so it's unit-testable.

    Phase 1 supports ``mode='lcao'`` only; ``'pw'`` raises so the
    failure is explicit rather than a silent wrong calculation.
    """
    dft = params.get("dft", {}) or {}
    mode = dft.get("mode", "lcao")
    if mode != "lcao":
        raise ValueError(f"Phase 1 supports mode='lcao' only, got {mode!r}")

    kpts_spec = dft.get("kpts", {}) or {}
    smearing = dft.get("smearing", {}) or {}
    kwargs: dict[str, Any] = {
        "xc": dft.get("functional", "RPBE"),
        "mode": "lcao",
        "basis": DEFAULT_BASIS,
        "h": dft.get("h", 0.18),
        # GPAW accepts a density-based k-point spec directly.
        "kpts": {
            "density": kpts_spec.get("density", 6.0),
            "gamma": kpts_spec.get("gamma_centered", True),
        },
        "spinpol": dft.get("spin_polarized", True),
        "occupations": {
            "name": smearing.get("method", "fermi-dirac"),
            "width": smearing.get("width_ev", 0.05),
        },
    }
    convergence = dft.get("convergence")
    if convergence:
        kwargs["convergence"] = convergence
    return kwargs


def _build_calculator(params: dict[str, Any], txt: str) -> Any:
    """Instantiate a GPAW calculator (lazy import — container only).

    Kwargs are computed (and validated, e.g. mode) *before* the GPAW
    import so param errors surface independent of whether GPAW is
    present."""
    kwargs = gpaw_kwargs_from_params(params)
    from gpaw import GPAW

    return GPAW(txt=txt, **kwargs)


def _optimizer(name: str) -> Any:
    from ase.optimize import BFGS, FIRE, LBFGS

    return {"bfgs": BFGS, "fire": FIRE, "lbfgs": LBFGS}[name.lower()]


def relax(atoms: Any, params: dict[str, Any], out_dir: str) -> dict[str, Any]:
    """Attach a GPAW calculator, run the optimizer to ``fmax``, and
    return the calc scalars. Container-only (uses GPAW)."""
    import numpy as np

    out = Path(out_dir)
    calc = _build_calculator(params, str(out / "gpaw.txt"))
    atoms.calc = calc

    optimizer_cls = _optimizer(params.get("optimizer", "bfgs"))
    fmax = params.get("fmax_ev_per_angstrom", 0.03)
    max_steps = params.get("max_steps", 200)
    opt = optimizer_cls(atoms, logfile=str(out / "opt.log"))
    converged = bool(opt.run(fmax=fmax, steps=max_steps))

    forces = atoms.get_forces()
    max_force = float(np.sqrt((forces**2).sum(axis=1).max()))
    scalars: dict[str, Any] = {
        "E_tot": float(atoms.get_potential_energy()),
        "converged": converged,
        "max_force": max_force,
        "steps": int(opt.get_number_of_steps()),
    }
    with contextlib.suppress(Exception):  # fermi level is best-effort metadata
        scalars["fermi"] = float(calc.get_fermi_level())
    if (params.get("dft", {}) or {}).get("spin_polarized", True):
        with contextlib.suppress(Exception):  # magmom is best-effort metadata
            scalars["magmom"] = float(atoms.get_magnetic_moment())
    return scalars


def run_cli(in_dir: str, out_dir: str) -> int:
    """Read inputs, relax, write ``result.json``. Returns 0 on success,
    1 on any failure (with the failure recorded in ``result.json``)."""
    world = _world()
    out = Path(out_dir)
    if world.rank == 0:
        out.mkdir(parents=True, exist_ok=True)
    try:
        poscar = (Path(in_dir) / "POSCAR").read_text()
        params = json.loads((Path(in_dir) / "params.json").read_text())
        atoms = atoms_from_poscar(poscar)
        scalars = relax(atoms, params, out_dir)

        import gpaw

        result = {
            "ok": True,
            "scalars": scalars,
            "relaxed_poscar": canonical_poscar(atoms),
            "gpaw_version": getattr(gpaw, "__version__", "?"),
            "functional": (params.get("dft", {}) or {}).get("functional", "RPBE"),
            # How much parallelism actually ran — a serial image and an
            # under-ranked mpirun look identical in the timings alone.
            "ranks": int(world.size),
        }
        if world.rank == 0:
            (out / "result.json").write_text(json.dumps(result, indent=2))
        return 0
    except Exception as exc:
        if world.rank == 0:
            (out / "result.json").write_text(
                json.dumps(
                    {
                        "ok": False,
                        "error": f"{type(exc).__name__}: {exc}",
                        "traceback": traceback.format_exc(),
                        "ranks": int(world.size),
                    },
                    indent=2,
                )
            )
        return 1


__all__ = ["DEFAULT_BASIS", "gpaw_kwargs_from_params", "relax", "run_cli"]
