"""Tier-1 (composition) panel — the `estimate` kind's slice-1 surface.

No geometry needed: every number here is a per-element property lookup
(`mendeleev`) or a pairwise arithmetic combination of two such lookups.
Renders as `precis.format.toon` tables (the same shape `search` and
`precis_pathway`'s views use) so the panel reads as data, not prose.

`mendeleev` is imported **inside** `composition_panel` (call time), not at
module scope — this module stays importable (and `pytest.importorskip`-
friendly for tests that don't need it) even in a venv without the
`[estimate]` extra. `ase.data` is a core precis-mcp dependency and is
imported at module scope like everywhere else in the codebase.
"""

from __future__ import annotations

import re
from itertools import combinations
from typing import Any

from ase.data import atomic_numbers, covalent_radii, ground_state_magnetic_moments

from precis.format import toon
from precis_estimate.data.hammer_norskov import D_BAND_CENTERS_EV

_DASH = "—"

#: Hume-Rothery-style rule of thumb: >~15% covalent-radius mismatch is the
#: classic threshold beyond which substitutional solid solubility drops off
#: sharply (size-strain dominates over chemical affinity). Used only to pick
#: which formulaic sentence to print — never a numeric prediction on its own.
_LARGE_RADIUS_MISMATCH_PCT = 15.0

#: Past this, the smaller partner (almost always H, or occasionally B/C/N/O)
#: is no longer a plausible substitutional guest at all — the mismatch is
#: normalised to the *smaller* radius, so any H-metal pair routinely clears
#: 300-500%. That's not "very strained alloying", it's a different physical
#: regime entirely: interstitial insertion (hydrides, carbides, nitrides),
#: where the small atom sits in a lattice void rather than on a lattice site.
_INTERSTITIAL_REGIME_PCT = 50.0

#: A commonly used threshold (Miedema-style intermetallic-formation
#: heuristics) above which a Pauling electronegativity difference signals
#: real charge-transfer / polar-bond character rather than a near-metallic
#: solid solution.
_SIGNIFICANT_EN_GAP = 0.4

#: Kept in sync with `handler.py`'s `_PLANNED_VIEWS` — duplicated rather than
#: imported to keep this module a one-way dependency of the handler, not a
#: cycle.
_PLANNED_VIEWS = (
    "structure",
    "whatif",
    "compare",
    "shape",
    "orbitals",
    "spin",
    "kinetics",
    "card",
)

_ORBITAL_RE = re.compile(r"(\d+)([spdf])(\d*)")


def _fmt(x: float | None, spec: str = "{:.2f}") -> str:
    return _DASH if x is None else spec.format(x)


def _d_electron_count(econf: str) -> int:
    """Electron count in the outermost occupied d-subshell, parsed from
    mendeleev's electron-configuration string (e.g. ``'[Kr] 4d10'`` -> 10,
    ``'[Xe] 5d 6s2'`` -> 1 — a bare orbital letter with no digit means a
    single electron, standard shorthand). 0 for elements with no occupied
    d-subshell (e.g. H, O)."""
    d_counts = [
        int(count) if count else 1
        for _n, orb, count in _ORBITAL_RE.findall(econf)
        if orb == "d"
    ]
    return d_counts[-1] if d_counts else 0


def _element_row(symbol: str) -> dict[str, Any]:
    import mendeleev

    el = mendeleev.element(symbol)
    z = atomic_numbers[symbol]
    r_cov = covalent_radii[z]  # Å, ase.data (Cordero et al. 2008)
    magmom = ground_state_magnetic_moments[z]  # mu_B, ase.data
    eps_d = D_BAND_CENTERS_EV.get(symbol)
    return {
        "Z": z,
        "el": symbol,
        "group": "-" if el.group_id is None else str(el.group_id),
        "period": "-" if el.period is None else str(el.period),
        "block": el.block or "-",
        "EN_pauling": _fmt(el.en_pauling),
        "r_cov_A": _fmt(r_cov),
        "magmom_muB": _fmt(magmom),
        "d_electrons": str(_d_electron_count(el.econf or "")),
        "eps_d_eV_HN95": _fmt(eps_d) if eps_d is not None else _DASH,
    }


def _radius_mismatch_pct(r_a: float, r_b: float) -> float:
    lo = min(r_a, r_b)
    return abs(r_a - r_b) / lo * 100.0 if lo else 0.0


def _pair_read(d_en: float | None, mismatch_pct: float | None) -> str:
    """A formulaic, thresholded read — no free-text generation. Combines
    the size-mismatch heuristic (`_LARGE_RADIUS_MISMATCH_PCT`) with the
    electronegativity-gap heuristic (`_SIGNIFICANT_EN_GAP`); either, both,
    or neither may fire."""
    parts: list[str] = []
    if mismatch_pct is not None:
        if mismatch_pct >= _INTERSTITIAL_REGIME_PCT:
            parts.append(
                "very large radius mismatch — interstitial regime "
                "(hydride/carbide/nitride-like), not substitutional alloying"
            )
        elif mismatch_pct >= _LARGE_RADIUS_MISMATCH_PCT:
            parts.append("large radius mismatch — strain-dominated alloying")
        else:
            parts.append("size-compatible radii")
    if d_en is not None and d_en >= _SIGNIFICANT_EN_GAP:
        parts.append("electronegativity gap — polar/charge-transfer character likely")
    if not parts:
        return "insufficient data for a qualitative read"
    return "; ".join(parts)


def _pairwise_row(sym_a: str, sym_b: str) -> dict[str, Any]:
    import mendeleev

    el_a, el_b = mendeleev.element(sym_a), mendeleev.element(sym_b)
    z_a, z_b = atomic_numbers[sym_a], atomic_numbers[sym_b]
    r_a, r_b = covalent_radii[z_a], covalent_radii[z_b]
    en_a, en_b = el_a.en_pauling, el_b.en_pauling

    d_en = abs(en_a - en_b) if en_a is not None and en_b is not None else None
    mismatch = _radius_mismatch_pct(r_a, r_b)
    return {
        "pair": f"{sym_a}-{sym_b}",
        "dEN": _fmt(d_en),
        "dr_cov_pct": _fmt(mismatch, "{:.1f}"),
        "read": _pair_read(d_en, mismatch),
    }


def composition_panel(symbols: list[str]) -> str:
    """The full tier-1 panel body for a validated, sorted list of element
    symbols. Callers (the handler's `_fetch`) are expected to have already
    parsed and validated `symbols` — this function does no input
    validation of its own."""
    rows = [_element_row(s) for s in symbols]
    # Chemistry-natural reading order: atomic number, not the alphabetical
    # order the cache key sorts by.
    rows.sort(key=lambda r: r["Z"])

    lines: list[str] = [
        "# estimate — composition tier: " + " · ".join(r["el"] for r in rows),
        "",
        "## Elements",
        toon.dump(
            rows,
            schema=[
                "Z",
                "el",
                "group",
                "period",
                "block",
                "EN_pauling",
                "r_cov_A",
                "magmom_muB",
                "d_electrons",
                "eps_d_eV_HN95",
            ],
        ),
    ]

    if len(symbols) >= 2:
        ordered = [r["el"] for r in rows]
        pair_rows = [_pairwise_row(a, b) for a, b in combinations(ordered, 2)]
        lines += [
            "",
            "## Pairwise",
            toon.dump(pair_rows, schema=["pair", "dEN", "dr_cov_pct", "read"]),
        ]

    lines += [
        "",
        "---",
        "ms element-descriptor tier — hypothesis-generating only; measure "
        "before ruling. eps_d_eV_HN95: Hammer-Norskov d-band center vs "
        "Fermi level, vendored for Ni/Cu/Pd/Ag/Pt/Au only (Nature 1995, "
        "376, 238) — '—' means not vendored, NOT zero.",
        "Drill-down (slice 2, not built yet): " + ", ".join(_PLANNED_VIEWS) + ".",
    ]
    return "\n".join(lines)


__all__ = ["composition_panel"]
