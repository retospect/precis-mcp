# structure kind → internal import filter (staged demotion)

Reto, 2026-09-14: se is now the origin of atoms; structure is "arguably
an import filter" — no direct agent access, like pcb's internals vs its
work surface. Staged, not a switch:

1. New capabilities land se-side only (`se-view-figures.md`,
   `se-nanobud-graph.md` already comply).
2. Bare molecules get a trivial single-block se wrapper — se becomes
   the *uniform* authoring surface.
3. De-teach: drop `structure` from skill/toc surface, mark internal.
4. Hide from the kind registry last; 3Dmol viewer survives as the se
   block detail view. `bound_design` slugs remain internal handles.

**Units stay split — RULED (Reto asked 2026-09-14, "should they merge
and be M?"; answer: no).** `structure` stays Å/eV-native, `se` metres.
Not a precision argument (floats are scale-invariant). The argument is
boundary count: everything `structure` talks to is Å-native (ASE, MACE,
pymatgen; POSCAR/extxyz/CIF both ways; every literature constant —
covalent radii, the 1.6 Å bond cutoff, VSEPR tables). Today there is
exactly ONE crossing, the se atomic seam (`ingest_envelope`, guarded by
`tests/test_se_atomic_angstrom_seam.py`), plus presentation conversion
at the MCP. Going metres-native does not remove that seam; it
multiplies it into every relax call, file round-trip and constant —
where a silent e-10 slip is hardest to catch. Atom storage is already
unitless anyway (`Atom.frac` is fractional; the Å lives in the cell).
Don't reopen without a new argument.

Blocker before 3–4: `se_propose_atomic` returns *structure op scripts*
as proposal payloads — Apply (`se-atomic-round2.md`) must make the
payload se-mediated first, or the hidden kind is still on the agent
surface. The compute ladder (relax/ML/DFT, run-cube cache) stays in
`precis/structure/` regardless — demotion is about the *agent surface*,
not the package.
