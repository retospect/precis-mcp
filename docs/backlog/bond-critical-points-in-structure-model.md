# Bond critical points (QTAIM) as a derived property of a DFT calculation

Prompted by Laerte Patera's talk at the OePG-CMD Joint Meeting 2026, Graz
(notes: draft `graz-oepg-cmd-2026`, section `dc4087528`). His group's claim that
borazine dimers on Au(111) are *bonded* rather than merely packed rests on a
QTAIM bond critical point in the computed charge density between the N-H and
B-H hydrogens — see Zeilerbauer et al. (2024), *Imaging Dihydrogen Bond-Driven
Assembly of Borazine on Au(111)* (`pa448129`, doi 10.1002/chem.202403996), and
the gas-phase precedent in Verma & Viswanathan (2017), *The borazine dimer: the
case of a dihydrogen bond competing with a classical hydrogen bond* (`pa448127`,
doi 10.1039/c7cp04056c).

## Why

Our atom model has no representation of a bond that is *derived from the
density* rather than asserted from a distance cutoff. Today a structure carries
positions; whether two atoms are bonded is either implicit or hand-declared.
That is exactly wrong for the interesting cases — dihydrogen bonds, hydrogen
bonds, agostic contacts, adsorbate-surface coupling — where the contact is
sub-van-der-Waals but the naive cutoff either misses it or over-reports it. A
bond critical point (a (3,-1) saddle in the electron density) plus the density
and Laplacian at that point is the standard, basis-set-robust answer, and it
also grades the interaction: rho_b magnitude separates a closed-shell contact
from a covalent one.

## Shape of the change

`dft_calculation` already has a `derived` subkey populated by `derive_*`
job_types, with Bader charges among them (`src/precis_dft/handlers/dft_calculation.py`,
`src/precis_dft/jobs/derive.py`). A QTAIM topology pass fits the same slot: run
over the GPAW all-electron density, emit the critical-point set with positions,
rho_b, Laplacian, ellipticity, and the pair of atoms each one bridges. The
structure side then gets a derived bond graph it can render and query, rather
than a cutoff heuristic.

## Open questions

- Which code does the topology search — a dependency (critic2, Multiwfn) or our
  own walk over the GPAW grid. Dependency is faster to ship, but adds a binary
  to the precis-dft image.
- All-electron reconstruction: GPAW's PAW density needs the augmentation
  spheres added back before the topology is meaningful near nuclei. Check what
  `gpaw.utilities.ps2ae` gives us.
- Does this become a first-class link on the structure (`bonded-to` with
  evidence), or stay inside `derived` as data? The first is more useful and more
  invasive.

Design call, Opus-tier. Owner `src/precis_dft/jobs/derive.py`,
`src/precis_dft/handlers/dft_calculation.py`, `src/precis_dft/handlers/structure.py`.
