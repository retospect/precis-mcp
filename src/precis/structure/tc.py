"""Cheap-tier phonon-mediated-Tc estimator (gr709908; spec Reto 2026-11-19).

The slab lane's frontier was energy-only: every relaxed candidate carried
a total-energy signal but nothing speaking to the superconductivity
quest's mechanism-class / Tc rubric — every row showed
``mechanism evidence: absent``. A real λ needs DFPT (the deferred tier-2
lane); this is the approved tier 1: Hopfield–McMillan λ from a DFT DOS at
the Fermi level plus a Debye temperature, then Allen–Dynes T_c. Pure
Python + numpy-scale arithmetic, no new deps.

**Physics.** λ = Σ_j N_j(E_F)·η_j / (M_j·⟨ω²⟩) over species j — the
Hopfield–McMillan form with

- ``N_j(E_F) = n_ef·count_j``: the per-atom DOS apportioned equally over
  atoms (there is no partial-DOS input at this tier);
- ``η_j = ⟨I_j²⟩`` from a tabulated per-element range
  (McMillan/Gaspari–Gyorffy-scale Hopfield parameters, eV/Å²) — light,
  stiff-bonded elements carry the large values, which is why the
  per-species mass in the denominator matters: the same η couples far
  harder on a 11-amu boron than on a 93-amu niobium;
- ``⟨ω²⟩ ≈ ω_log²`` with ``ω_log = 0.75·θ_D`` (the spec's Debye
  shortcut; an explicit ``omega_log_k`` input overrides it).

T_c uses the plain Allen–Dynes modified-McMillan exponential at
μ* = 0.10 and 0.13 — no strong-coupling f₁/f₂ shape corrections
(MgB₂-class two-gap anisotropy is exactly what that form misses; the
honest lo→hi band covers it).

**Inputs are explicit.** The caller reads ``n_ef`` off the DFT run and
``θ_D`` off elastic constants (Anderson average-velocity) or Γ-point
phonons — the estimator never invents a number it wasn't given.
Per-element ⟨I²⟩ ranges are the uncertainty model: λ and T_c report a
lo→hi band (about a factor ~2) beside the geometric-mean central value.

**Verdicts** (consumed by the frontier's ``tc_verdict`` flag):

- ``phonon-mediated absent`` — ``n_ef <= 0``: no states at E_F, the
  phonon pairing channel is closed;
- ``phonon-mediated weak`` — central λ < 0.3, or T_c(μ*=0.10) < 0.5 K;
- ``phonon-mediated plausible`` — otherwise.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from dataclasses import dataclass

#: k_B/ℏ in rad·s⁻¹·K⁻¹ — turns a temperature into an angular frequency.
_KB_OVER_HBAR: float = 1.30919e11
#: eV/Å² carried by 1 amu vibrating at 1 rad/s: amu[kg]·ω² / (N/m per eV/Å²).
_AMU_OMEGA2_TO_EV_A2: float = 1.66053906660e-27 / 16.02176634

#: ω_log ≈ 0.75·θ_D — the spec's Debye shortcut.
_DEBYE_TO_OMEGA_LOG: float = 0.75

#: Verdict thresholds — see the module docstring.
_LAMBDA_WEAK: float = 0.3
_TC_FLOOR_K: float = 0.5

#: Per-element Hopfield ⟨I²⟩ ranges in eV/Å² (McMillan/Gaspari–Gyorffy
#: scale). The spread *is* the uncertainty model — light stiff elements
#: (H, B, C, N, O) genuinely sit an order above the simple metals.
_I2_EV_A2: dict[str, tuple[float, float]] = {
    # light, stiff-bonded elements — large Hopfield η
    "H": (10.0, 30.0),
    "B": (5.0, 12.0),
    "C": (6.0, 15.0),
    "N": (6.0, 15.0),
    "O": (5.0, 12.0),
    "F": (4.0, 10.0),
    "Si": (3.0, 8.0),
    "P": (3.0, 7.0),
    "S": (3.0, 7.0),
    # simple / sp metals
    "Li": (1.0, 3.0),
    "Na": (0.8, 2.5),
    "K": (0.6, 2.0),
    "Mg": (1.0, 3.0),
    "Al": (3.0, 6.0),
    "Ca": (1.5, 4.0),
    "Zn": (1.0, 3.0),
    "Ga": (2.0, 5.0),
    "Sn": (3.0, 6.0),
    "Pb": (4.0, 7.0),
    # early transition metals — the boride/nitride/hydride quest family
    "Sc": (3.0, 6.0),
    "Ti": (3.5, 7.0),
    "V": (4.0, 8.0),
    "Cr": (3.0, 6.0),
    "Y": (3.5, 7.0),
    "Zr": (4.0, 8.0),
    "Nb": (5.0, 8.0),
    "Mo": (4.0, 8.0),
    "Hf": (4.0, 8.0),
    "Ta": (5.0, 9.0),
    "W": (4.0, 8.0),
    "Re": (4.0, 7.0),
    # late transition / noble metals
    "Mn": (2.0, 5.0),
    "Fe": (3.0, 7.0),
    "Co": (2.5, 5.0),
    "Ni": (2.0, 5.0),
    "Cu": (1.5, 4.0),
    "Ru": (3.0, 6.0),
    "Rh": (2.0, 5.0),
    "Pd": (2.5, 5.0),
    "Ag": (1.5, 4.0),
    "Os": (3.0, 6.0),
    "Ir": (2.5, 5.0),
    "Pt": (2.0, 5.0),
    "Au": (1.5, 4.0),
    # rare earths
    "La": (3.0, 6.0),
    "Ce": (2.5, 5.5),
}

#: Elements outside the table get this deliberately wide range and are
#: named in ``TcEstimate.i2_fallback`` — never silently precise.
_I2_FALLBACK: tuple[float, float] = (2.0, 6.0)


def _mass_amu(element: str) -> float:
    """Standard atomic mass from ``ase.data`` (a core dep)."""
    from ase.data import atomic_masses, chemical_symbols

    try:
        return float(atomic_masses[chemical_symbols.index(element)])
    except ValueError as exc:
        raise ValueError(f"unknown element {element!r}") from exc


def _resolve_i2(
    element: str, overrides: dict[str, tuple[float, float]] | None
) -> tuple[tuple[float, float], bool]:
    """The (lo, hi) ⟨I²⟩ range for one element + whether the fallback
    table entry was used."""
    if overrides and element in overrides:
        return overrides[element], False
    if element in _I2_EV_A2:
        return _I2_EV_A2[element], False
    return _I2_FALLBACK, True


def allen_dynes(lambda_ep: float, omega_log_k: float, mu_star: float) -> float:
    """Modified-McMillan T_c in K; 0 when the exponential's denominator
    goes non-positive (λ too weak to pair at this μ*)."""
    if lambda_ep <= 0 or omega_log_k <= 0:
        return 0.0
    denom = lambda_ep - mu_star * (1.0 + 0.62 * lambda_ep)
    if denom <= 0:
        return 0.0
    return omega_log_k / 1.2 * math.exp(-1.04 * (1.0 + lambda_ep) / denom)


@dataclass(frozen=True)
class TcEstimate:
    """One λ/ω_log/T_c row — central values are geometric means of the
    per-element ⟨I²⟩ ranges; ``*_band`` re-runs the whole estimate at the
    all-lo / all-hi corners of that table (≈ a factor-2 band)."""

    lambda_ep: float
    lambda_band: tuple[float, float]
    omega_log_k: float
    #: T_c at the two conventional μ* values (central λ).
    tc_k: float
    tc_k_mu013: float
    #: T_c(μ*=0.10) re-evaluated at λ_lo / λ_hi — the honest band.
    tc_band_k: tuple[float, float]
    verdict: str
    n_ef: float
    theta_d_k: float | None
    #: Resolved per-element (lo, hi) ⟨I²⟩ actually used (after overrides).
    i2: dict[str, tuple[float, float]]
    #: Elements that fell back to the generic range — flagged in the row.
    i2_fallback: tuple[str, ...]


def tc_estimate(
    *,
    n_ef: float,
    composition: dict[str, int] | Iterable[tuple[str, int]],
    theta_d_k: float | None = None,
    omega_log_k: float | None = None,
    i2: dict[str, float | tuple[float, float] | list[float]] | None = None,
) -> TcEstimate:
    """Hopfield–McMillan λ + Allen–Dynes T_c for a relaxed candidate.

    ``n_ef`` — total DOS at E_F per atom per eV (both spins), from the
    DFT run. ``composition`` — ``{element: count}``. ``theta_d_k`` — the
    Debye temperature in K (Anderson average-velocity from elastic
    constants, or Γ-point phonons); ``omega_log_k`` overrides the
    0.75·θ_D shortcut when a better phonon moment exists. ``i2`` —
    optional per-element ⟨I²⟩ overrides: a number (fixed value, band
    collapses) or a ``[lo, hi]`` pair.
    """
    n_ef = float(n_ef)
    if n_ef < 0 or not math.isfinite(n_ef):
        raise ValueError(f"n_ef must be a finite number >= 0, got {n_ef!r}")
    if omega_log_k is None:
        if theta_d_k is None:
            raise ValueError("give theta_d_k (Debye temperature, K) or omega_log_k")
        omega_log = _DEBYE_TO_OMEGA_LOG * float(theta_d_k)
    else:
        omega_log = float(omega_log_k)
    if omega_log <= 0 or not math.isfinite(omega_log):
        raise ValueError(
            f"omega_log/theta_d must be finite and > 0, got {omega_log!r} K"
        )

    counts = dict(composition)
    if not counts or sum(counts.values()) <= 0:
        raise ValueError("composition must name at least one atom")

    overrides: dict[str, tuple[float, float]] = {}
    for el, raw in (i2 or {}).items():
        if isinstance(raw, bool):
            raise ValueError(f"i2[{el!r}] must be a number or a [lo, hi] pair")
        if isinstance(raw, (int, float)):
            v = float(raw)
            overrides[el] = (v, v)
        elif isinstance(raw, (tuple, list)) and len(raw) == 2:
            lo, hi = float(raw[0]), float(raw[1])
            overrides[el] = (min(lo, hi), max(lo, hi))
        else:
            raise ValueError(f"i2[{el!r}] must be a number or a [lo, hi] pair")

    resolved: dict[str, tuple[float, float]] = {}
    fallback: list[str] = []
    masses: dict[str, float] = {}
    for el in counts:
        resolved[el], fell_back = _resolve_i2(el, overrides)
        if fell_back:
            fallback.append(el)
        masses[el] = _mass_amu(el)

    omega = omega_log * _KB_OVER_HBAR  # rad/s
    omega2_ev_a2_per_amu = omega * omega * _AMU_OMEGA2_TO_EV_A2

    def _lambda(i2_pick: int) -> float:
        """λ at one corner of the η table — ``i2_pick`` 0 = lo, 1 = mid
        (geometric), 2 = hi."""
        total = 0.0
        for el, count in counts.items():
            lo, hi = resolved[el]
            if lo <= 0 or hi <= 0:
                raise ValueError(f"⟨I²⟩ for {el} must be > 0")
            eta = lo if i2_pick == 0 else hi if i2_pick == 2 else math.sqrt(lo * hi)
            # M_j·⟨ω²⟩ in eV/Å² — light atoms couple harder at the same η.
            denom = masses[el] * omega2_ev_a2_per_amu
            total += count * eta / denom
        return n_ef * total

    lam_lo, lam_mid, lam_hi = _lambda(0), _lambda(1), _lambda(2)
    tc_mid = allen_dynes(lam_mid, omega_log, 0.10)
    tc_13 = allen_dynes(lam_mid, omega_log, 0.13)
    tc_lo = allen_dynes(lam_lo, omega_log, 0.10)
    tc_hi = allen_dynes(lam_hi, omega_log, 0.10)

    if lam_mid <= 0:
        verdict = "phonon-mediated absent"
    elif lam_mid < _LAMBDA_WEAK or tc_mid < _TC_FLOOR_K:
        verdict = "phonon-mediated weak"
    else:
        verdict = "phonon-mediated plausible"

    return TcEstimate(
        lambda_ep=lam_mid,
        lambda_band=(lam_lo, lam_hi),
        omega_log_k=omega_log,
        tc_k=tc_mid,
        tc_k_mu013=tc_13,
        tc_band_k=(tc_lo, tc_hi),
        verdict=verdict,
        n_ef=n_ef,
        theta_d_k=float(theta_d_k) if theta_d_k is not None else None,
        i2=resolved,
        i2_fallback=tuple(sorted(fallback)),
    )


__all__ = [
    "TcEstimate",
    "allen_dynes",
    "tc_estimate",
]
