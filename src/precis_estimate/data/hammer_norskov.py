"""Vendored Hammer–Nørskov d-band centers.

Source: Hammer, B.; Nørskov, J. K. "Why gold is the noblest of all the
metals." *Nature* **1995**, *376*, 238–240 (doi:10.1038/376238a0); the same
six values are reproduced in every later Hammer–Nørskov review (e.g.
Hammer, B.; Nørskov, J. K. "Theoretical surface science and catalysis —
calculations and concepts." *Adv. Catal.* **2000**, *45*, 71–129) and in
the canonical d-band-center trend figure used throughout the catalysis
literature since. Values are εd, the d-band center **relative to the Fermi
level** (eV), for the flat, close-packed facet of each metal — (111) for
the fcc metals here.

These six are the *only* metals vendored: they are the ones the surrounding
autocatpath/EMT toolchain already treats as its standard set (real
effective-medium-theory support: Ni/Cu/Pd/Ag/Pt/Au — see
`docs/backlog/estimate-kind-ms-chemistry-workup.md` "Toolbox"), and the only
ones this author holds with high confidence to the specific published
digit. Per the design doc's own instruction — omit rather than guess — every
other transition metal (Fe, Co, Ru, Rh, Ir, Ti, ... ) is deliberately left
out of this table; `compute/composition.py` renders "—" for any element not
listed here, never a fabricated number. A later slice can extend this table
against a primary source, metal by metal.

**V²ad (adsorbate–metal coupling) is deliberately NOT vendored here.**
Unlike εd, there is no single well-known per-metal constant for it in the
literature — the coupling matrix element genuinely depends on the
adsorbate orbital it's coupling to, not just the metal. Vendoring a static
per-metal number would be exactly the kind of guess the design doc warns
against. The Newns–Anderson arithmetic that actually needs V²ad is slice 2
(structure tier, on a real adsorbate geometry via the ~200-line
extended-Hückel carve-out) — this table only carries what's safe to state
composition-only: the bare d-band center.
"""

from __future__ import annotations

#: symbol -> d-band center epsilon_d relative to the Fermi level, in eV.
D_BAND_CENTERS_EV: dict[str, float] = {
    "Ni": -1.29,
    "Cu": -2.67,
    "Pd": -1.83,
    "Ag": -4.30,
    "Pt": -2.25,
    "Au": -3.56,
}

__all__ = ["D_BAND_CENTERS_EV"]
