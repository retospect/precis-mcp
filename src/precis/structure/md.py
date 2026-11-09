"""Molecular-dynamics helpers — params normalization, the content address,
the NVT runner and the RDF/MSD summaries (gr606465).

Pure pieces the ``struct_md`` job and the ``mode='md'`` mint share — the
gripe's gap is that relax (EMT/xTB/MACE/CHGNet) and CI-NEB exist but no
molecular-dynamics rung does, so ML-MD for membranes and electrode
interfaces could not run (the 2026-11-09 ALCHEM Lab gap analysis;
``docs/backlog/electrochem-mlip-ladder.md``):

- :func:`normalize_md_params` — the engine knob set with defaults. Strict:
  an unknown key raises rather than dropping a typo'd knob. Request-context
  stamps (``requested_by``…) are merged into the stored row's ``params``
  by the caller *after* this — they never reach :func:`md_cache_key`.
- :func:`md_cache_key` — the content address ``sha256(structure sha ·
  fidelity · model · engine params · code version)``: a repeat ``mode='md'``
  is a zero-compute cache hit.
- :func:`mobile_indices` — the free-atom set (``fixed == 0``) in band
  order. A fully-held design has nothing to propagate and refuses at mint.
- :func:`run_nvt` — the ASE Langevin NVT loop (the thermostat named by the
  proposal): Maxwell–Boltzmann init, COM drift removed, trajectory frames
  every ``traj_every`` steps, an extXYZ serialisation, the per-sample
  E_pot/T timeseries, and the RDF + MSD summaries the run row stores.
- :func:`msd_series` / :func:`rdf_summary` — pure-numpy summaries over the
  sampled frames, kept ASE-free so the summaries are checkable without the
  dynamics machinery.

Unit enclave (package docstring): Å-native; times are fs in the knobs
(``timestep_fs``), ps on the stored summaries (``t_ps``).
"""

from __future__ import annotations

import hashlib
import io
import json
import time
from collections.abc import Mapping
from typing import Any

import numpy as np

from .cache import structure_sha
from .scene import Scene

#: Bump when the MD call changes such that a cached trajectory would
#: differ from a fresh run (a changed default, a changed write-back
#: contract) — same contract as :data:`neb.NEB_CODE_VERSION`.
#: 2: the COM pin is mobile-only and the extXYZ serializes the sampled
#: positions verbatim (td607072 — the global FixCom ratcheted held
#: atoms, and constrained frame copies rewrote their coordinates).
MD_CODE_VERSION = "2"

#: The engine knob set + defaults (gr606465): a 300 K Langevin NVT run of
#: 1000 × 1 fs steps — the proposal's "1 ps NVT" — friction 0.02/fs, a
#: trajectory frame every 10 steps, the RDF to 6 Å in 60 bins, and a fixed
#: velocity seed so the content address really is content-addressed.
MD_DEFAULTS: dict[str, Any] = {
    "temperature_k": 300.0,
    "timestep_fs": 1.0,
    "steps": 1000,
    "friction_per_fs": 0.02,
    "traj_every": 10,
    "rdf_rmax_A": 6.0,
    "rdf_nbins": 60,
    "seed": 42,
}

_INT_KEYS = ("steps", "traj_every", "rdf_nbins", "seed")
_FLOAT_KEYS = ("temperature_k", "timestep_fs", "friction_per_fs", "rdf_rmax_A")


class MdTimeout(RuntimeError):
    """The wall-clock cap was spent mid-run (failure_class ``timeout`` —
    same contract as ``struct_relax._RelaxDeadline`` / ``NebTimeout``)."""


def normalize_md_params(args: Mapping[str, Any] | None) -> dict[str, Any]:
    """Fill the engine knobs with defaults; coerce JSON-typed values.

    Unknown keys raise ``ValueError`` naming the key and the knob set —
    the handler maps that to ``BadInput`` with a ``next=``. Bounds are the
    rung's own sanity limits; the ceilings are GPU-time guards (an
    unbounded knob mints an unbounded node job).
    """
    args = dict(args or {})
    unknown = sorted(set(args) - set(MD_DEFAULTS))
    if unknown:
        raise ValueError(
            f"md params: unknown key(s) {unknown} — engine knobs are "
            f"{sorted(MD_DEFAULTS)}"
        )
    out: dict[str, Any] = {}
    for key, default in MD_DEFAULTS.items():
        raw = args.get(key, default)
        try:
            val: Any = int(raw) if key in _INT_KEYS else float(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"md params: {key!r} must be a {type(default).__name__}, got {raw!r}"
            ) from exc
        out[key] = val
    if not (0.0 < out["temperature_k"] <= 2000.0):
        raise ValueError("md params: 'temperature_k' must be in (0, 2000] K")
    if not (0.0 < out["timestep_fs"] <= 5.0):
        raise ValueError("md params: 'timestep_fs' must be in (0, 5] fs")
    if not (1 <= out["steps"] <= 50000):
        raise ValueError(
            "md params: 'steps' must be in [1, 50000] (50 ps at 1 fs is the "
            "GPU-time ceiling)"
        )
    if not (0.0 < out["friction_per_fs"] <= 1.0):
        raise ValueError("md params: 'friction_per_fs' must be in (0, 1] 1/fs")
    if not (1 <= out["traj_every"] <= 1000):
        raise ValueError("md params: 'traj_every' must be in [1, 1000]")
    if not (0.0 < out["rdf_rmax_A"] <= 20.0):
        raise ValueError("md params: 'rdf_rmax_A' must be in (0, 20] Å")
    if not (1 <= out["rdf_nbins"] <= 400):
        raise ValueError("md params: 'rdf_nbins' must be in [1, 400]")
    if out["seed"] < 0:
        raise ValueError("md params: 'seed' must be ≥ 0")
    return out


def md_cache_key(
    scene: Scene,
    *,
    fidelity: str,
    model: str | None,
    params: Mapping[str, Any] | None = None,
    code_version: str = MD_CODE_VERSION,
) -> str:
    """The content address for one MD run.

    ``params`` should be :func:`normalize_md_params`'s output — the key
    filters to the engine knob set regardless, so request-context stamps
    merged into the stored row's ``params`` can never fork the address of
    an otherwise-identical request. No engine token rides the key: the
    MLIP model name is already in it and, unlike autocatpath, there is no
    separately-pinned engine build (same contract as the relax run-cube).
    """
    payload = {
        "structure_sha": structure_sha(scene),
        "fidelity": fidelity,
        "model": model,
        "params": {k: v for k, v in (params or {}).items() if k in MD_DEFAULTS},
        "code_version": code_version,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def mobile_indices(scene: Scene, order: list[str]) -> list[int]:
    """Indices into ``order`` of the atoms free to move — ``fixed == 0``.
    A partially-pinned atom (one axis held) counts as held: the Atoms
    builder pins the whole atom on any nonzero mask, matching the relax
    rungs' whole-atom pin."""
    return [i for i, la in enumerate(order) if not scene.atoms[la].fixed]


def _held_indices(atoms: Any) -> set[int]:
    """The held set off the atoms' own ``FixAtoms`` — ``neb_atoms`` writes
    whole-atom pins from the scene's fixed flags, which is what "held
    slab" means here."""
    from ase.constraints import FixAtoms

    held: set[int] = set()
    for c in atoms.constraints:
        if isinstance(c, FixAtoms):
            held.update(int(i) for i in c.get_indices())
    return held


def msd_series(
    positions: list[Any],
    cell: Any,
    pbc: tuple[bool, bool, bool],
    mobile_idx: list[int],
) -> dict[str, Any]:
    """MIC-unwrapped mean-squared displacement of the mobile atoms,
    measured from frame 0 (Å²), one entry per sampled frame.

    Wrap-safe, not trajectory-unwrapped: a displacement longer than half
    the cell between samples folds back — the honest read for the
    ps-scale NVT runs this feeds."""
    cell_arr = np.asarray(cell, dtype=float)
    inv = np.linalg.inv(cell_arr)
    pos0 = np.asarray(positions[0], dtype=float)
    out: list[float] = []
    for pos in positions:
        d = np.asarray(pos, dtype=float) - pos0
        dfrac = d @ inv
        for ax in range(3):
            if pbc[ax]:
                dfrac[:, ax] -= np.round(dfrac[:, ax])
        dcart = dfrac @ cell_arr
        out.append(
            float(np.mean(np.sum(dcart[mobile_idx] ** 2, axis=1)))
            if mobile_idx
            else 0.0
        )
    return {
        "t_ps": [],
        "msd_A2": out,
        "n_mobile": len(mobile_idx),
        "final_A2": out[-1] if out else None,
    }


def rdf_summary(
    positions: list[Any],
    symbols: list[str],
    cell: Any,
    pbc: tuple[bool, bool, bool],
    mobile_idx: list[int],
    *,
    rmax: float,
    nbins: int,
) -> dict[str, Any]:
    """Per-element-pair RDF g(r) averaged over the sampled frames,
    restricted to atom pairs with at least one mobile endpoint — the
    adsorbate/interface structure an MD run is minted to answer.

    g(r) is the per-pair histogram normalized against the cell-average
    density — ``hist / (n_frames · n_pairs · shell_vol / V_cell)`` — so a
    homogeneous fluid tends to 1 at large r. Over a slab-with-vacuum cell
    the normalisation counts the gap, so tails read low and the peak
    positions, not the tail height, are the signal. Distances are MIC
    where the cell is periodic."""
    edges = np.linspace(0.0, float(rmax), int(nbins) + 1)
    centers = 0.5 * (edges[:-1] + edges[1:])
    shell_vol = (4.0 / 3.0) * np.pi * (edges[1:] ** 3 - edges[:-1] ** 3)
    cell_arr = np.asarray(cell, dtype=float)
    volume = abs(float(np.linalg.det(cell_arr)))
    mobile = set(int(i) for i in mobile_idx)

    groups: dict[str, tuple[list[int], list[int]]] = {}
    n = len(symbols)
    for i in range(n):
        for j in range(i + 1, n):
            if i in mobile or j in mobile:
                key = "-".join(sorted((str(symbols[i]), str(symbols[j]))))
                gi, gj = groups.get(key, ([], []))
                gi.append(i)
                gj.append(j)
                groups[key] = (gi, gj)
    if not groups or volume <= 0.0 or not positions:
        return {"rmax": float(rmax), "nbins": int(nbins), "pairs": {}}

    inv = np.linalg.inv(cell_arr)
    hists = {k: np.zeros(int(nbins)) for k in groups}
    for pos in positions:
        p = np.asarray(pos, dtype=float)
        dfrac = (p[:, None, :] - p[None, :, :]) @ inv
        for ax in range(3):
            if pbc[ax]:
                dfrac[:, :, ax] -= np.round(dfrac[:, :, ax])
        dist = np.linalg.norm(dfrac @ cell_arr, axis=2)
        for key, (gi, gj) in groups.items():
            hists[key] += np.histogram(dist[gi, gj], bins=edges)[0]

    pairs: dict[str, Any] = {}
    for key, (gi, gj) in sorted(groups.items()):
        g = hists[key] / (len(positions) * len(gi) * shell_vol / volume)
        pairs[key] = {
            "r": [round(float(x), 4) for x in centers],
            "g": [round(float(x), 4) for x in g],
            "n_pairs": len(gi),
        }
    return {"rmax": float(rmax), "nbins": int(nbins), "pairs": pairs}


def _fix_mobile_com(mobile: list[int]) -> Any:
    """A COM pin scoped to the free atoms.

    ASE's global ``FixCom`` corrects the COM by shifting EVERY atom
    (``new += diff``) — appended after ``FixAtoms`` it repositions
    pinned atoms every step, and ``FixAtoms`` re-pins to the *current*
    (already-shifted) positions, so a held slab ratchets. Scoping both
    the COM and the correction to the mobile set still pins the
    whole-cell COM — held atoms never move — while leaving pinned
    coordinates exact. The inherited ``adjust_momenta``/
    ``adjust_forces`` already act on ``self.index`` alone."""
    from ase.constraints import FixCom

    class _FixMobileCom(FixCom):
        index: Any = slice(None)

        def __init__(self, index: np.ndarray) -> None:
            super().__init__()
            self.index = index

        def adjust_positions(self, atoms: Any, new: Any) -> None:
            masses = atoms.get_masses()[self.index]
            old_cm = atoms.get_center_of_mass(indices=self.index)
            new_cm = masses @ new[self.index] / masses.sum()
            new[self.index] += old_cm - new_cm

    return _FixMobileCom(np.asarray(mobile, dtype=int))


def run_nvt(
    atoms: Any, params: Mapping[str, Any], *, timeout_s: float
) -> dict[str, Any]:
    """One NVT Langevin trajectory on ``atoms`` — the calculator is the
    caller's (the job's runner seam attaches the rung's backend; the held
    set rides the atoms' own ``FixAtoms``).

    ``steps × timestep_fs`` is the simulated time — ``steps=1000,
    timestep_fs=1`` IS the proposal's "1 ps NVT". The wall-clock cap is
    checked between trajectory samples; a force call in flight can't be
    preempted, so the cap overruns by at most ``traj_every`` steps of
    compute (the same contract as ``struct_neb``'s deadline calc).

    Returns the normalized outcome the job's write-back consumes: the
    extXYZ trajectory, the per-sample ``t_ps``/``e_pot_ev``/``temp_k``
    timeseries, the :func:`msd_series`/:func:`rdf_summary` summaries, and
    ``wall_s``.
    """
    from ase import units
    from ase.io import write as ase_write
    from ase.md.langevin import Langevin
    from ase.md.velocitydistribution import (
        MaxwellBoltzmannDistribution,
        Stationary,
    )

    steps = int(params["steps"])
    dt_fs = float(params["timestep_fs"])
    every = int(params["traj_every"])
    temp_k = float(params["temperature_k"])
    friction = float(params["friction_per_fs"]) / units.fs
    seed = int(params["seed"])

    held = _held_indices(atoms)
    mobile = [i for i in range(len(atoms)) if i not in held]

    t0 = time.monotonic()
    deadline = t0 + float(timeout_s)
    MaxwellBoltzmannDistribution(
        atoms, temperature_K=temp_k, rng=np.random.default_rng(seed)
    )
    # Held-slab MD wants no COM drift: Stationary rescales the free
    # momenta once (preserve_temperature keeps the MB temperature), and a
    # COM pin holds it for the run — ASE ≥3.28 deprecated Langevin's own
    # fixcm because it doesn't sample NVT correctly. The pin is scoped
    # to the mobile set: the global FixCom's uniform correction moves
    # pinned atoms every step (see _fix_mobile_com).
    Stationary(atoms)
    if mobile:
        atoms.set_constraint([*atoms.constraints, _fix_mobile_com(mobile)])
    dyn = Langevin(
        atoms,
        dt_fs * units.fs,
        temperature_K=temp_k,
        friction=friction,
        fixcm=False,
        rng=np.random.default_rng(seed + 1),
    )

    positions: list[np.ndarray] = [atoms.get_positions().copy()]
    e_pot: list[float] = [float(atoms.get_potential_energy())]
    temp: list[float] = [float(atoms.get_temperature())]
    # ASE fires attached observers at step 0 as well as every `interval`
    # steps, so the sample grid is recorded, not assumed.
    sample_steps: list[int] = [0]

    def _sample() -> None:
        n = int(dyn.get_number_of_steps())
        if n == sample_steps[-1]:
            return
        sample_steps.append(n)
        positions.append(atoms.get_positions().copy())
        e_pot.append(float(atoms.get_potential_energy()))
        temp.append(float(atoms.get_temperature()))
        if time.monotonic() > deadline:
            raise MdTimeout(
                f"exceeded the {timeout_s:.0f}s wall-clock cap "
                f"({n} of {steps} steps done)"
            )

    dyn.attach(_sample, interval=every)
    dyn.run(steps)
    if sample_steps[-1] < steps:
        _sample()
    t_ps = [s * dt_fs / 1000.0 for s in sample_steps]

    symbols = [str(s) for s in atoms.get_chemical_symbols()]
    cell = np.asarray(atoms.cell, dtype=float)
    pbc: tuple[bool, bool, bool] = (
        bool(atoms.pbc[0]),
        bool(atoms.pbc[1]),
        bool(atoms.pbc[2]),
    )
    msd = msd_series(positions, cell, pbc, mobile)
    msd["t_ps"] = t_ps
    rdf = rdf_summary(
        positions,
        symbols,
        cell,
        pbc,
        mobile,
        rmax=float(params["rdf_rmax_A"]),
        nbins=int(params["rdf_nbins"]),
    )

    frames = []
    for k, pos in enumerate(positions):
        f = atoms.copy()
        # The frame carries the SAMPLED positions verbatim: applying the
        # constraints here would rewrite them — FixAtoms re-pins held
        # atoms to the post-run positions and the COM pin shifts every
        # frame's mobile set onto the final COM.
        f.set_positions(pos, apply_constraint=False)
        f.calc = None
        f.info = {"time_ps": round(t_ps[k], 6), "e_pot_ev": round(e_pot[k], 6)}
        frames.append(f)
    buf = io.StringIO()
    ase_write(buf, frames, format="extxyz")

    return {
        "extxyz": buf.getvalue(),
        "t_ps": t_ps,
        "e_pot_ev": e_pot,
        "temp_k": temp,
        "temperature_k": temp_k,
        "temp_mean_k": float(np.mean(temp)),
        "e_pot_mean_ev": float(np.mean(e_pot)),
        "e_pot_std_ev": float(np.std(e_pot)),
        "msd": msd,
        "rdf": rdf,
        "n_steps": steps,
        "n_frames": len(positions),
        "n_mobile": len(mobile),
        "wall_s": time.monotonic() - t0,
    }


__all__ = [
    "MD_CODE_VERSION",
    "MD_DEFAULTS",
    "MdTimeout",
    "md_cache_key",
    "mobile_indices",
    "msd_series",
    "normalize_md_params",
    "rdf_summary",
    "run_nvt",
]
