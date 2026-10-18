# MOF import fixtures

- `core_mof_ABAVIJ_clean.cif` — a real CoRE MOF 2019 record (refcode
  `ABAVIJ_clean`, 108 atoms, CC BY 4.0), recorded verbatim on 2026-10-18Z
  from the MOFX-DB JSON API's `cif` field (database "CoREMOF 2019"). The
  CoRE MOF 2024 bundle uses the same Materials Studio P1 layout.
- `qmof_synthetic.cif` — **synthetic**, not a QMOF entry. It copies the
  layout pymatgen's `CifWriter` produces (the writer MPContribs uses for
  QMOF structures); the atoms are made up. A real QMOF CIF could not be
  recorded: keyless MPContribs reads return the structure fields empty.
