"""``precis-dft-run gpaw-analysis`` — the in-container electronic-analysis
single point (gr592999).

Reads ``<in>/POSCAR`` + ``<in>/params.json``, runs one GPAW self-consistent
single point at the staged geometry (no relaxation — the geometry is the
relaxed input, verbatim), then derives the orbital-level descriptors the
quest needs to turn screening trends into mechanisms:

* **Bader charges** per atom — the Henkelman ``bader`` binary on the
  all-electron density written to a cube file. Honest
  ``"unavailable"`` when the binary or the density can't be produced —
  GPAW ships no Bader implementation of its own.
* **d-band centre and width** per requested atom set (the dopant, the
  surface atoms) — first and second moments of the projected d-DOS
  summed over both spin channels, referenced to the Fermi level.
* **Projected DOS at the dopant** — the d-channel LDOS grid, referenced
  to the Fermi level, for downstream plotting/comparison.
* **Projected COHP for named bonds** — an energy-resolved bond-weighted
  population: for each Kohn–Sham state, the pair term
  ``Re[H_mu_nu * c_mu* c_nu]`` summed over basis orbitals on the two
  atoms of each bond pair, plus the value integrated to the Fermi
  level. LCAO-only (the rung is LCAO today); any API drift reports
  ``"unavailable"`` with the reason rather than a fabricated number —
  the same rule as the relax path: different physics is worse than no
  answer.

  The descriptor reads go through GPAW interfaces that exist on *both*
  calculator flavours the image can hand us — the legacy
  ``gpaw.old`` calculator and the new ``ASECalculator`` (``from gpaw
  import GPAW``'s default since GPAW 25.x, whose ``.wfs`` is the
  ``FakeWFS`` shim):

  - AO→atom map: ``wfs.setups.M_a[a]`` + ``wfs.setups[a].nao`` — built
    unconditionally in ``Setups.__init__`` (``update_phases`` uses it);
    ``basis_functions.M_a`` is legacy-only.
  - Hamiltonian: ``gpaw.lcao.tools.get_lcao_hamiltonian(calc)`` — a
    module function, not a ``calc`` method — returning
    ``(H_skMM, S_kMM)`` in eV on world rank 0 and ``(None, None)``
    elsewhere, so the array is broadcast before use.
  - ``kpt.eps_n`` is Hartree (→ ×Ha); ``kpt.f_n`` already carries the
    k-point weight and spin degeneracy, so it is never weighted again.
  - Per-state curves aggregate across the k-point × band distribution:
    ``wfs.kptband_comm`` on legacy GPAW, ``wfs.bd.comm`` + ``wfs.kd.comm``
    on the new shim.

Every descriptor is a ``{"status": "computed"|"unavailable", "reason"?}``
envelope, so one missing capability (e.g. no ``bader`` binary in the
image) never sinks the whole run — the SCF energy + whatever computed
still land on the structure.

``params.json`` shape (the host writes it; see
``precis.workers.job_types.electronic_analysis``)::

    {"dft": {...same block as gpaw-relax...},
     "labels": ["Pd1", "Pd2", ...],        # POSCAR row order -> atom label
     "analysis": {
        "dopant": ["Ta1"],                # labels; optional
        "surface": ["Pd1", ...],          # labels; default: atoms of the
                                          # most frequent element
        "bonds": [{"name": "Pd-N", "a": "el:Pd", "b": "el:N"},
                  {"name": "Ta-N1", "a": "Ta1", "b": "N3"}],
        "bond_cutoff": 1.3                # x natural cutoffs (optional)
     }}

An ``a``/``b`` endpoint is an atom *label* or ``el:<symbol>`` (every
atom of that element); a named bond resolves to the label↔element-set
(or set↔set) pairs within the cutoff.

Output ``result.json``::

    {"ok": true,
     "scalars": {"E_tot", "fermi", "magmom"?},
     "descriptors": {
        "bader": {"status": ..., "charges": {"Ta1": -0.9, ...}},
        "dband": {"Ta1": {"center_ev", "width_ev", "d_states"}, ...},
        "pdos": {"Ta1": {"energies_ev": [...], "d": [...]}},
        "cohp": {"Pd-N": {"status": ..., "icohp_ev", "pairs": [...]}}
     },
     "gpaw_version", "functional", "ranks", "cpu_binding"}

On any failure it writes ``{"ok": false, "error", "traceback"}`` and
returns non-zero — the same failure-record contract as ``gpaw-relax``.

Parallel: descriptor reads go through the calculator's own gather APIs
and are gated on rank 0, mirroring :mod:`gpaw_relax`. GPAW is imported
lazily — this module must stay importable without it.
"""

from __future__ import annotations

import contextlib
import json
import math
import subprocess
import traceback
from pathlib import Path
from typing import Any

from precis_dft._container.gpaw_relax import (
    _build_calculator,
    _world,
    cpu_binding,
)
from precis_dft.structures import atoms_from_poscar

#: Default multiplier on ASE ``natural_cutoffs`` for resolving which atom
#: pairs a named bond covers — comfortably over a stretched surface bond,
#: short of second-neighbour shells on a close-packed metal.
_BOND_CUTOFF_MULT_DEFAULT = 1.3

#: Grid cap for stored PDOS/COHP energy curves — keeps result.json (and the
#: struct_runs row it lands on) bounded on a dense GPAW energy grid.
_CURVE_MAX_POINTS = 600

#: LDOS Gaussian smearing width (eV) for the d-band moments and stored PDOS.
_PDOS_WIDTH_EV = 0.1


def _label_map(atoms: Any, labels: list[str]) -> dict[str, int]:
    """``label -> atom index`` from the staged labels (POSCAR row order).

    Returns an empty map when no labels were staged — descriptor sets
    that name labels then honestly resolve to nothing."""
    if len(labels) != len(atoms):
        return {}
    return {lab: i for i, lab in enumerate(labels)}


def _resolve_atom_set(spec: str, atoms: Any, label_map: dict[str, int]) -> list[int]:
    """Atom indices a selector names: an exact label, or ``el:<symbol>``
    (every atom of that element, POSCAR order)."""
    spec = spec.strip()
    if spec.startswith("el:"):
        el = spec[3:]
        return [i for i, sym in enumerate(atoms.get_chemical_symbols()) if sym == el]
    if spec in label_map:
        return [label_map[spec]]
    return []


def _resolve_bond_pairs(
    spec: dict[str, Any], atoms: Any, label_map: dict[str, int], mult: float
) -> list[list[int]]:
    """The atom-index pairs a named bond covers.

    Both endpoints may name a set; the resolved pairs are every
    within-cutoff (a, b) with a < b. An endpoint that resolves empty —
    a typo'd label or an absent element — yields no pairs, and the
    caller reports the bond as unresolvable rather than fabricating a
    pair."""
    from ase.neighborlist import natural_cutoffs

    a_set = _resolve_atom_set(str(spec.get("a") or ""), atoms, label_map)
    b_set = _resolve_atom_set(str(spec.get("b") or ""), atoms, label_map)
    if not a_set or not b_set:
        return []
    cutoffs = natural_cutoffs(atoms, mult=mult)
    pairs: list[list[int]] = []
    for i in a_set:
        for j in b_set:
            if i == j:
                continue
            d = atoms.get_distance(i, j, mic=True)
            if d <= cutoffs[i] + cutoffs[j]:
                pairs.append(sorted((int(i), int(j))))
    # de-dup when both endpoints are set-valued (a_i,b_j seen twice)
    uniq = {tuple(p) for p in pairs}
    return [list(p) for p in sorted(uniq)]


#: ``angular`` letter → l, for the ``raw_pdos(energies, a, l, ...)``
#: channel of the new-API DOSCalculator.
_ANGULAR_L = {"s": 0, "p": 1, "d": 2, "f": 3}

#: Energy-grid resolution for the new-API PDOS curves.
_PDOS_GRID_POINTS = 401


def _ldos_reader(calc: Any) -> Any:
    """A ``(atom_index, angular, spin) -> (energies, weights)`` reader for
    whichever GPAW flavour is running.

    The legacy calculator has ``calc.get_orbital_ldos``; the new
    ``ASECalculator`` (the ``from gpaw import GPAW`` default since GPAW
    25.x) dropped it — there the same PAW projector weights fold onto an
    energy grid through ``calc.dos()`` →
    ``gpaw.dos.DOSCalculator.raw_pdos``. The DOSCalculator is built once
    here (it collects every eigenvalue) rather than once per atom.
    ``shift_fermi_level=False`` keeps the grid in absolute eV like the
    legacy path; the caller references it to the Fermi level itself.

    Raises when neither interface exists — the caller folds that into an
    honest ``unavailable`` rather than guessing."""
    import numpy as np

    if hasattr(calc, "get_orbital_ldos"):
        return lambda a, angular, spin: calc.get_orbital_ldos(
            a, angular=angular, spin=spin, width=_PDOS_WIDTH_EV
        )
    if hasattr(calc, "dos"):
        dos = calc.dos(shift_fermi_level=False)
        eigs = np.asarray(dos.eig_skn, dtype=float)
        grid = dos.get_energies(
            emin=float(eigs.min()) - 3 * _PDOS_WIDTH_EV,
            emax=float(eigs.max()) + 3 * _PDOS_WIDTH_EV,
            npoints=_PDOS_GRID_POINTS,
        )

        def _pdos(a: int, angular: str, spin: int) -> tuple[Any, Any]:
            try:
                ell = _ANGULAR_L[angular]
            except KeyError as exc:
                raise RuntimeError(
                    f"angular channel {angular!r} unsupported on this GPAW"
                ) from exc
            return grid, dos.raw_pdos(grid, a, ell, spin=spin, width=_PDOS_WIDTH_EV)

        return _pdos
    raise RuntimeError("no LDOS interface on this GPAW version")


def _orbital_ldos(
    calc: Any, ldos: Any, index: int, angular: str
) -> tuple[list[float], list[float]]:
    """``(energies, ldos)`` for atom ``index``'s ``angular`` channel,
    summed over spins — via the gather APIs of whichever GPAW flavour is
    running, so it works under MPI.

    ``ldos`` is the per-run reader :func:`_ldos_reader` builds once."""
    import numpy as np

    energies: list[float] | None = None
    total: Any = None
    n_spins = getattr(calc, "get_number_of_spins", lambda: 1)()
    for spin in range(int(n_spins)):
        e, w = ldos(index, angular, spin)
        energies = list(e)
        total = (
            np.asarray(w, dtype=float)
            if total is None
            else total + np.asarray(w, dtype=float)
        )
    if energies is None or total is None:
        raise RuntimeError("no LDOS returned")
    return energies, [float(x) for x in total]


def _dband_moments(
    energies: list[float], ldos: list[float], fermi: float
) -> dict[str, float]:
    """d-band centre (first moment) and width (sqrt of the central second
    moment) of a PDOS curve, energies referenced to the Fermi level.

    All d states enter — occupied and unoccupied; the standard
    Hammer–Nørskov moment definition. Raises on an empty/zero-weight
    curve (e.g. 'd' channel on an sp-only adsorbate)."""
    import numpy as np

    e = np.asarray(energies, dtype=float) - float(fermi)
    d = np.asarray(ldos, dtype=float)
    total = float(np.trapezoid(d, e))
    if not math.isfinite(total) or abs(total) < 1e-12:
        raise RuntimeError("d-channel DOS integrates to ~0 — no d states")
    m1 = float(np.trapezoid(d * e, e)) / total
    m2 = float(np.trapezoid(d * (e - m1) ** 2, e)) / total
    return {
        "center_ev": m1,
        "width_ev": math.sqrt(max(m2, 0.0)),
        "d_states": total,
    }


def _downsample(xs: list[float], ys: list[float]) -> tuple[list[float], list[float]]:
    """Thin an energy curve to at most :data:`_CURVE_MAX_POINTS` points —
    evenly, keeping both ends."""
    n = len(xs)
    if n <= _CURVE_MAX_POINTS:
        return xs, ys
    step = (n - 1) / (_CURVE_MAX_POINTS - 1)
    idx = sorted({round(i * step) for i in range(_CURVE_MAX_POINTS)})
    return [xs[i] for i in idx], [ys[i] for i in idx]


def _bader_charges(
    atoms: Any, calc: Any, out_dir: Path, labels: list[str]
) -> dict[str, Any]:
    """Bader analysis on the all-electron density via the Henkelman
    ``bader`` binary.

    ``ACF.dat``'s CHARGE column is the Bader-partitioned *all-electron
    count* per atom, so the net charge is the nuclear Z minus it —
    never a PAW *valence* count (that would report the core electrons
    as net charge). The density gather is collective (every rank must
    join it), but the cube write, the ``bader`` subprocess and the
    ``ACF.dat`` parse are rank-0 only — one rank owns those files and
    the result is broadcast, rather than N ranks racing over the same
    outputs in the shared out-dir."""
    import shutil

    exe = shutil.which("bader")
    if exe is None:
        return {
            "status": "unavailable",
            "reason": "no 'bader' binary in the image — install the "
            "Henkelman Bader tool to enable charge partitioning",
        }
    world = _world()
    # Collective call — every rank participates in the gather; only the
    # rank-0 copy is used.
    density = calc.get_all_electron_density(gridrefinement=2)
    result: dict[str, Any] | None = None
    if world.rank == 0:
        try:
            from ase.io.cube import write_cube
            from ase.units import Bohr

            cube = out_dir / "density.cube"
            with cube.open("w", encoding="utf-8") as fh:
                # The cube grid is written in Bohr and bader assumes the
                # data is Bohr^-3, but get_all_electron_density returns
                # Å^-3 (it divides the internal Bohr^-3 array by Bohr**3
                # for the ASE interface) — scale it back or every
                # integrated electron count is inflated by Bohr^-3
                # (~6.75x).
                write_cube(fh, atoms, data=density * Bohr**3)
            # Single-grid pass: partition AND integrate the all-electron
            # density, so CHARGE is the full electron count and the net
            # charge is Z - count.
            proc = subprocess.run(
                [exe, str(cube)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                cwd=str(out_dir),
                timeout=3600,
                check=False,
            )
            acf = out_dir / "ACF.dat"
            if proc.returncode != 0 or not acf.exists():
                result = {
                    "status": "unavailable",
                    "reason": f"bader rc={proc.returncode}: "
                    f"{(proc.stderr or proc.stdout or '')[:300]}",
                }
            else:
                counts = _parse_acf_charges(acf, len(atoms))
                if counts is None:
                    result = {
                        "status": "unavailable",
                        "reason": "ACF.dat produced no per-atom charge column",
                    }
                else:
                    zs = atoms.get_atomic_numbers()
                    if labels:
                        label_of = {i: labels[i] for i in range(len(atoms))}
                    else:
                        label_of = {i: str(i) for i in range(len(atoms))}
                    result = {
                        "status": "computed",
                        "tool": "bader",
                        "charges": {
                            label_of[i]: round(float(zs[i]) - counts[i], 4)
                            for i in range(len(atoms))
                        },
                    }
        except Exception as exc:
            # A rank-0 failure — a cube-write error, a bader timeout, an
            # unreadable ACF.dat — must still ride the broadcast as an
            # honest envelope. Letting it raise leaves every peer rank
            # blocked in ase.parallel.broadcast below (review td687448).
            result = {
                "status": "unavailable",
                "reason": f"bader pass on rank 0 failed: {type(exc).__name__}: {exc}",
            }
    if int(getattr(world, "size", 1)) > 1:
        # GPAW's world.broadcast takes an ndarray; the result dict rides
        # a pickled byte channel instead — ase's object broadcast, the
        # same helper gpaw.mpi.rank0_call itself delegates to.
        from ase.parallel import broadcast as _broadcast_obj

        result = _broadcast_obj(result, root=0, comm=world)
    if result is None:
        return {
            "status": "unavailable",
            "reason": "bader result not broadcast from rank 0",
        }
    return result


def _parse_acf_charges(acf: Path, n_atoms: int) -> list[float] | None:
    """The CHARGE column of a Henkelman ``ACF.dat`` — one float per atom,
    in atom order. ``None`` on an unparseable file or a count mismatch."""
    vals: list[float] = []
    for line in acf.read_text(encoding="utf-8").splitlines():
        toks = line.split()
        if len(toks) >= 6 and toks[0].strip().isdigit():
            with contextlib.suppress(ValueError):
                vals.append(float(toks[4]))
    return vals if len(vals) == n_atoms else None


def _lcao_hamiltonian_ev(calc: Any) -> Any:
    """The full LCAO Hamiltonian ``H_skMM`` in eV, on every rank.

    ``gpaw.lcao.tools.get_lcao_hamiltonian`` is the real entry point — a
    *module* function taking the calculator, never a ``calc`` method. It
    works on both GPAW flavours, is collective (every rank must reach it
    — it sums over the k-point communicator internally, and on legacy
    GPAW re-runs ``wfs.set_positions`` when ``S_qMM`` was dropped), and
    returns ``(H_skMM, S_kMM)`` on world rank 0 only — ``(None, None)``
    on the other ranks, so the array is broadcast afterwards for the
    per-state contraction each rank runs on its local k-points."""
    wfs = calc.wfs
    if getattr(wfs, "mode", "lcao") != "lcao":
        raise RuntimeError("pCOHP needs an LCAO-mode calculation")
    try:
        from gpaw.lcao.tools import get_lcao_hamiltonian
    except ImportError as exc:
        raise RuntimeError(f"gpaw.lcao.tools unreadable: {exc}") from exc
    H_skMM, _S_kMM = get_lcao_hamiltonian(calc)
    world = getattr(wfs, "world", None) or _world()
    if int(getattr(world, "size", 1)) > 1:
        # Slaves got (None, None) — pick up rank 0's array over the
        # pickled-object channel, the same one the Bader result rides.
        from ase.parallel import broadcast as _broadcast_obj

        H_skMM = _broadcast_obj(H_skMM, root=0, comm=world)
    if H_skMM is None:
        raise RuntimeError("LCAO Hamiltonian not returned on rank 0")
    return H_skMM


def _kpt_band_comms(wfs: Any) -> list[Any]:
    """The communicator(s) spanning ``wfs.kpt_u``'s k-point × band
    distribution: legacy GPAW's joint ``kptband_comm``, or the new
    ``FakeWFS``'s separate ``bd.comm`` (bands) + ``kd.comm`` (k-points).

    Domain-decomposition ranks never appear in these comms, so gathering
    the per-state scalar lists across each of them collects every
    (spin, k-point, band) state exactly once."""
    kb = getattr(wfs, "kptband_comm", None)
    if kb is not None:
        return [kb]
    comms = []
    for attr in ("bd", "kd"):
        comm = getattr(getattr(wfs, attr, None), "comm", None)
        if comm is not None:
            comms.append(comm)
    return comms


def _gather_states(
    states: tuple[list[float], list[float], list[float]], comms: list[Any]
) -> tuple[list[float], list[float], list[float]]:
    """All-gather ``(eps, cohp, occ)`` state lists across each comm —
    sequentially, so a ``bd.comm`` + ``kd.comm`` pair still yields the
    full k-point × band grid on every rank."""
    eps_all, cohp_all, occ_all = states
    if comms:
        from ase.parallel import broadcast as _broadcast_obj

        for comm in comms:
            if int(getattr(comm, "size", 1)) <= 1:
                continue
            gathered_e: list[float] = []
            gathered_c: list[float] = []
            gathered_o: list[float] = []
            for r in range(int(comm.size)):
                e_r, c_r, o_r = _broadcast_obj(
                    (eps_all, cohp_all, occ_all) if int(comm.rank) == r else None,
                    root=r,
                    comm=comm,
                )
                gathered_e.extend(e_r)
                gathered_c.extend(c_r)
                gathered_o.extend(o_r)
            eps_all, cohp_all, occ_all = gathered_e, gathered_c, gathered_o
    return eps_all, cohp_all, occ_all


def _cohp_for_bond(
    calc: Any, H_skMM: Any, pair: list[int], fermi: float
) -> dict[str, Any]:
    """Energy-resolved projected COHP for one atom pair, plus its value
    integrated to the Fermi level.

    pCOHP(E_nk) = -Re[ Σ_{μ∈a, ν∈b} c*_nμ H_μν c_nν ] per Kohn–Sham
    state (the Dronskowski projected-COHP sign convention: positive =
    bonding). ``H_skMM`` comes from :func:`_lcao_hamiltonian_ev` (eV);
    AO blocks come from ``wfs.setups.M_a[a]`` / ``wfs.setups[a].nao`` —
    the AO→atom map ``update_phases`` itself uses, populated
    unconditionally in ``Setups.__init__`` so it holds on legacy GPAW
    and on the new ``ASECalculator``'s ``FakeWFS`` alike
    (``basis_functions.M_a`` is legacy-only). ``kpt.eps_n`` is Hartree
    (→ ×Ha to share the Fermi level's unit) and ``kpt.f_n`` already
    carries the k-point weight and spin degeneracy, so it is not
    weighted again; per-state terms gather across the k-point × band
    communicators before the ICOHP integral. An interface drift raises
    so the bond reads ``unavailable`` instead of a wrong curve.
    """
    import numpy as np
    from ase.units import Ha

    a_i, b_j = int(pair[0]), int(pair[1])
    wfs = calc.wfs

    try:
        setups = wfs.setups
        start_a = int(setups.M_a[a_i])
        start_b = int(setups.M_a[b_j])
        width_a = int(setups[a_i].nao)
        width_b = int(setups[b_j].nao)
        nao = int(setups.nao)
    except (AttributeError, IndexError, TypeError, ValueError) as exc:
        raise RuntimeError(
            f"AO→atom basis map unreadable on this GPAW version: {exc}"
        ) from exc
    a_orbs = list(range(start_a, start_a + width_a))
    b_orbs = list(range(start_b, start_b + width_b))
    if not a_orbs or not b_orbs:
        raise RuntimeError("no AO basis functions mapped to a bond endpoint")
    if np.asarray(H_skMM).shape[-1] != nao:
        raise RuntimeError(
            f"LCAO Hamiltonian shape {np.asarray(H_skMM).shape} "
            f"doesn't cover nao={nao} basis functions"
        )

    eps_all: list[float] = []
    cohp_all: list[float] = []
    occ_all: list[float] = []
    for kpt in wfs.kpt_u:
        if kpt.s is None:
            raise RuntimeError(
                "non-collinear wave functions — pCOHP needs collinear spins"
            )
        # eps_n is in Hartree → eV so it shares the Fermi level's unit.
        eps_n = np.asarray(kpt.eps_n, dtype=float) * Ha
        # GPAW's f_n already carries the k-point weight (and spin
        # degeneracy) — weighting again by kpt.weight would double-count.
        f_n = np.asarray(kpt.f_n, dtype=float)
        C_nM = np.asarray(kpt.C_nM)  # (mynbands, nao)
        if C_nM.ndim != 2 or C_nM.shape[1] != nao:
            raise RuntimeError(
                "coefficients not in the full AO basis on this rank "
                "(ScaLAPACK/BLACS layout) — unsupported"
            )
        H_MM = np.asarray(H_skMM)[kpt.s, kpt.k]  # eV
        sub = H_MM[np.ix_(a_orbs, b_orbs)]  # (na, nb)
        # Per-state pair term: Re[ c*_μn H_μν c_νn ] summed over μ∈a, ν∈b.
        ca = C_nM[:, a_orbs].conj()  # (mynbands, na)
        cb = C_nM[:, b_orbs]  # (mynbands, nb)
        term = np.einsum("nm,mk,nk->n", ca, sub, cb).real
        eps_all.extend(eps_n.tolist())
        cohp_all.extend((-term).tolist())
        occ_all.extend(f_n.tolist())

    # k-points / bands are spread over ranks: gather every rank's states
    # so the cumulative integral and ICOHP cover the whole spectrum.
    eps_all, cohp_all, occ_all = _gather_states(
        (eps_all, cohp_all, occ_all), _kpt_band_comms(wfs)
    )

    e = np.asarray(eps_all)
    c = np.asarray(cohp_all)
    occ = np.asarray(occ_all)
    order = np.argsort(e)
    e, c, occ = e[order], c[order], occ[order]
    # Occupation-weighted (f_n) cumulative integral up to each state — the
    # ICOHP read at E_F is its value at the topmost occupied state.
    cumulative = np.cumsum(c * occ)
    below = e <= float(fermi)
    icohp = float(cumulative[below][-1]) if below.any() else 0.0
    e_rel = (e - float(fermi)).tolist()
    xs, ys = _downsample(e_rel, c.tolist())
    _, cums = _downsample(e_rel, cumulative.tolist())
    return {
        "status": "computed",
        "method": "pcohp-lcao",
        "icohp_ev": icohp,
        "energies_ev": [round(x, 4) for x in xs],
        "cohp": [round(y, 6) for y in ys],
        "icohp_cumulative": [round(y, 6) for y in cums],
    }


def analyze(atoms: Any, params: dict[str, Any], out_dir: str) -> dict[str, Any]:
    """Run the single point + descriptor pass; return the ``descriptors``
    dict for ``result.json``. Container-only (uses GPAW)."""
    out = Path(out_dir)
    calc = _build_calculator(params, str(out / "gpaw.txt"))
    atoms.calc = calc
    e_tot = float(atoms.get_potential_energy())
    fermi = float(calc.get_fermi_level())

    scalars: dict[str, Any] = {"E_tot": e_tot, "fermi": fermi}
    if (params.get("dft", {}) or {}).get("spin_polarized", True):
        with contextlib.suppress(Exception):  # best-effort metadata
            scalars["magmom"] = float(atoms.get_magnetic_moment())

    labels = [str(x) for x in params.get("labels") or []]
    label_map = _label_map(atoms, labels)
    spec = params.get("analysis") or {}
    mult = float(spec.get("bond_cutoff") or _BOND_CUTOFF_MULT_DEFAULT)

    descriptors: dict[str, Any] = {}

    # ── d-band moments + dopant PDOS ─────────────────────────────────
    dband: dict[str, Any] = {}
    pdos: dict[str, Any] = {}
    sets: dict[str, list[int]] = {}
    for name, sel in (("dopant", spec.get("dopant")), ("surface", spec.get("surface"))):
        idxs = [
            i for s in (sel or []) for i in _resolve_atom_set(str(s), atoms, label_map)
        ]
        if sel is not None:
            sets[name] = sorted(set(idxs))
    if "surface" not in sets:
        # Default: every atom of the most frequent element (the slab's
        # host metal on an adatom/dopant design).
        syms = atoms.get_chemical_symbols()
        if syms:
            host = max(set(syms), key=syms.count)
            sets["surface"] = [i for i, s in enumerate(syms) if s == host]
    ldos_reader: Any = None

    def _ldos() -> Any:
        # Build the per-run DOS reader lazily — the DOSCalculator
        # construction collects every eigenvalue, so it happens once
        # for the whole descriptor pass, not once per atom.
        nonlocal ldos_reader
        if ldos_reader is None:
            ldos_reader = _ldos_reader(calc)
        return ldos_reader

    for set_name, idxs in sets.items():
        if not idxs:
            dband[set_name] = {
                "status": "unavailable",
                "reason": "no atoms resolved for this set",
            }
            continue
        try:
            energies: list[float] | None = None
            import numpy as np

            total: Any = None
            for i in idxs:
                e_i, w_i = _orbital_ldos(calc, _ldos(), i, "d")
                energies = e_i
                total = np.asarray(w_i) if total is None else total + np.asarray(w_i)
            dband[set_name] = {
                "status": "computed",
                "atoms": [labels[i] if i < len(labels) else str(i) for i in idxs],
                **_dband_moments(energies or [], total.tolist(), fermi),
            }
            if set_name == "dopant" and energies is not None:
                # PDOS *at the dopant*: the summed d-channel curve the
                # moments integrate — stored for the compare view.
                e_rel = [x - fermi for x in energies]
                xs, ys = _downsample(e_rel, [float(v) for v in total])
                pdos[set_name] = {
                    "status": "computed",
                    "channel": "d",
                    "energies_ev": [round(x, 4) for x in xs],
                    "d": [round(y, 6) for y in ys],
                }
        except Exception as exc:
            dband[set_name] = {"status": "unavailable", "reason": str(exc)}
            if set_name == "dopant":
                pdos[set_name] = {"status": "unavailable", "reason": str(exc)}
    descriptors["dband"] = dband
    descriptors["pdos"] = pdos

    # ── Bader charges ────────────────────────────────────────────────
    try:
        descriptors["bader"] = _bader_charges(atoms, calc, out, labels)
    except Exception as exc:
        descriptors["bader"] = {"status": "unavailable", "reason": str(exc)}

    # ── COHP for named bonds ─────────────────────────────────────────
    cohp: dict[str, Any] = {}
    bonds = spec.get("bonds") or []
    # The full LCAO Hamiltonian is one collective fetch shared by every
    # bond — computing it per pair would rebuild the whole (s,k,nao²)
    # array for each. A failure here marks every bond unavailable with
    # the same reason rather than retrying a broken interface per pair.
    H_skMM: Any = None
    H_err: str | None = None
    if bonds:
        try:
            H_skMM = _lcao_hamiltonian_ev(calc)
        except Exception as exc:
            H_err = f"{type(exc).__name__}: {exc}"
    for bond in bonds:
        name = str(bond.get("name") or f"{bond.get('a')}-{bond.get('b')}")
        if H_err is not None:
            cohp[name] = {"status": "unavailable", "reason": H_err}
            continue
        pairs = _resolve_bond_pairs(bond, atoms, label_map, mult)
        if not pairs:
            cohp[name] = {
                "status": "unavailable",
                "reason": "no atom pair within the bond cutoff resolved "
                f"for {bond.get('a')!r}–{bond.get('b')!r}",
            }
            continue
        try:
            per_pair = [_cohp_for_bond(calc, H_skMM, p, fermi) for p in pairs]
            ok_pairs = [p for p in per_pair if p.get("status") == "computed"]
            if not ok_pairs:
                cohp[name] = {
                    "status": "unavailable",
                    "reason": per_pair[0].get("reason", "no pair computed"),
                }
                continue
            icohp = sum(float(p["icohp_ev"]) for p in ok_pairs)
            # The single-pair case keeps its energy curves; a multi-pair
            # bond reports the summed ICOHP plus the pair list (per-pair
            # curves are identical grids only when the k-point sets
            # match, so no blind summation).
            entry: dict[str, Any] = {
                "status": "computed",
                "icohp_ev": icohp,
                "pairs": pairs,
            }
            if len(ok_pairs) == 1:
                entry.update(
                    {
                        k: ok_pairs[0][k]
                        for k in ("energies_ev", "cohp", "icohp_cumulative")
                    }
                )
            cohp[name] = entry
        except Exception as exc:
            cohp[name] = {"status": "unavailable", "reason": str(exc)}
    descriptors["cohp"] = cohp

    return {"scalars": scalars, "descriptors": descriptors}


def run_cli(in_dir: str, out_dir: str) -> int:
    """Read inputs, run the single point + descriptors, write
    ``result.json``. Returns 0 on success, 1 on any failure (with the
    failure recorded in ``result.json``) — the ``gpaw-relax`` contract."""
    world = _world()
    out = Path(out_dir)
    if world.rank == 0:
        out.mkdir(parents=True, exist_ok=True)
    try:
        poscar = (Path(in_dir) / "POSCAR").read_text(encoding="utf-8")
        params = json.loads((Path(in_dir) / "params.json").read_text(encoding="utf-8"))
        atoms = atoms_from_poscar(poscar)
        payload = analyze(atoms, params, out_dir)

        import gpaw

        result = {
            "ok": True,
            **payload,
            "gpaw_version": getattr(gpaw, "__version__", "?"),
            "functional": (params.get("dft", {}) or {}).get("functional", "RPBE"),
            "ranks": int(world.size),
            "cpu_binding": cpu_binding(),
        }
        if world.rank == 0:
            (out / "result.json").write_text(
                json.dumps(result, indent=2), encoding="utf-8"
            )
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
                        "cpu_binding": cpu_binding(),
                    },
                    indent=2,
                ),
                encoding="utf-8",
            )
        return 1


__all__ = [
    "analyze",
    "run_cli",
]
