---
status: draft
title: MOF and catalyst database tie-ins — structure seeds, reference energies, screening
pillar: quests
---

# MOF and catalyst database tie-ins

Reto, 2026-10-02: "we should have mof and catalyst database tie ins". This
file maps the candidates and what each would feed. Reto decided the four
open questions on 2026-10-02 (see Decided).

## What exists already

- **The import seam.** `src/precis/structure/importers/`: one pure adapter
  per source, `raw_record → (Scene, ExternalRun, ExternalId)`. The rules:
  an external run never serves a compute cache hit, an external design
  refuses edit, `(dataset, config_id)` is the idempotent key, and the
  `method` fingerprint keeps external energies from being compared naively
  against ours.
- **Catalysis-Hub.** `catalysis_hub.py` (GraphQL) and `cathub_db.py`
  (batch import, proven on PengRole2020.db) are built. They are parked
  because every public channel now needs SUNCAT credentials (keyless
  GraphQL returns 401). `structure-import.md` § bulk corpus.
- **A pending source pick.** `structure-import.md` § bulk corpus has been
  waiting on Reto: the first open bulk source, OC20 or AQCat25. Decision
  `chemistry-3` below settles it.
- **BEAST DB** (grand-canonical DFT on HER/OER/CO2R/NRR, 2,000+
  catalysts, corpus pa254046). Proposed as one more adapter in
  `capability-landscape-steals.md`; its licence and download format are
  unchecked.
- **Catalysis-library pull.** `catalyst-discovery-quest.md` Slice 6 plans
  the same two uses (seed designs and reference anchors) with the source
  left open, and says not to reuse the stale `precis-dft` Materials
  Project ingest.
- **Reference numbers today.** catpath's validation checks literature
  values by hand (`catpath/docs/VALIDATION.md`: Pt(111) ORR passes on
  UMA/oc20, fails on MACE-MP-0). No database feeds those numbers.
- **MOFs: nothing beyond tags.** `data/topics/mof.yaml` and
  `mof-tools.yaml` tag papers. The `structure` kind exports CIF. The
  engine builds fcc111 slabs only (`quest/catalyst_seed.py`;
  the `slab` op uses `ase.build.fcc111`). A MOF active site sits in a 3D-
  periodic pore with no slab or vacuum, so the engine cannot use one as a
  catalyst candidate today.

## Candidates and what each would feed

Sizes and licences below are from memory and must be checked before
anything is built.

| Source | Content | Would feed | Access / catch |
|---|---|---|---|
| Catalysis-Hub | published adsorption/reaction energies + geometries | reference energies, slab seeds | adapter built; needs SUNCAT creds |
| OC20 / OC22 | ~1M+ adsorbate–slab relaxations (RPBE) | slab seeds beyond fcc111, screening priors | open (CC-BY), S3; **UMA's training set, so not an independent check on UMA** |
| AQCat25 | spin-polarised adsorption set | seeds, magnetic-metal references | HF gated:auto; also near UMA's training data |
| BEAST DB | GC-DFT, potential-dependent, includes NRR | independent reference for qu164903's CHE lever | licence/format unchecked |
| Materials Project | bulk crystals, formation energies, surface energies, Pourbaix | bulk stability, non-fcc111 facets, Pourbaix check (catalysis-selectivity) | free API key; do not revive precis-dft's ingest |
| CoRE MOF (2019 / 2024) | ~14k / ~40k computation-ready experimental MOF CIFs | MOF structure seeds | open |
| QMOF | ~20k MOFs with DFT-relaxed geometries and properties | MOF seeds with energies | open (CC-BY) |
| ODAC23 / OpenDAC | DFT CO2/H2O adsorption in ~8k MOFs; UMA has an `odac` task head | MOF adsorption reference energies; an engine path for MOF adsorbates | open; same circularity caveat as OC20 for UMA |
| CSD MOF subset | the largest experimental MOF set | seeds | commercial CCDC licence — excluded unless Reto has one |
| hMOF / MOFX-DB | hypothetical MOFs + simulated gas isotherms | separation/storage screening, not catalysis | open |

The three uses:
1. **Structure seeds.** A real relaxed slab or framework that the proposer
   edits, instead of a geometry built from scratch.
2. **Reference energies.** Independent numbers that check our ML/DFT
   energies, which the November trust-demo paper needs (td459589). A
   source UMA trained on (OC20, ODAC, AQCat-like data) shows only that UMA
   fits its own training data.
3. **Candidate screening.** Prefilter a quest's search space by a
   database property (stability, facet energy, pore size) before spending
   compute.

## Decided (Reto, 2026-10-02T13:24Z, review items chemistry-2..5)

- **MOFs (chemistry-2):** a MOF structure library (CoRE MOF and/or QMOF)
  plus ODAC23 adsorption energies as reference values. MOFs as catalyst
  candidates in the engine are out of scope until a quest asks for them.
- **Catalyst source (chemistry-3):** Catalysis-Hub first. Reto is
  requesting the SUNCAT credentials himself. BEAST DB is the fallback if
  the credentials stall; Materials Project comes second.
- **Storage (chemistry-4):** look up on demand and import on use. Only
  small calibration slices are bulk-imported.
- **Quest hook (chemistry-5):** show and flag only. The candidate shows its
  own energy next to the external reference with the method difference
  stated. A gap above a threshold sets a distrust flag, the same mechanism
  as the `wrong_site` gate. Ranking stays on our own numbers; no
  calibration offset until the gaps have been measured.

## Decided (Reto, 2026-10-18, starting today)

- **Step 1 covers both MOF sources in one builder:** QMOF (geometry + DFT
  energy) and CoRE MOF (geometry only).
- **Generic CIF import now**, agent-facing, not only behind the adapters.
- **Storage when no per-record API exists:** download the bundle once to
  the NAS, build a small name → offset index, import a single record on
  `get`. The DB stays on-demand (chemistry-4); only the lookup file is
  bulk.

## Sources checked (web, 2026-10-18)

| Source | Location | Licence | What step 1 uses |
|---|---|---|---|
| CoRE MOF 2024 | Zenodo doi:10.5281/zenodo.15055758 (v1.1): `CoREMOF2024DB_SI_20250204.zip` 44 MB of CIFs + ASR/FSR/ION CSVs; 8,300 structures, 2,664 computation-ready | CC-BY 4.0 (confirmed) | bundle → NAS index; geometry only, no DFT energies |
| CoRE MOF 2019 | Zenodo doi:10.5281/zenodo.7691378 | CC-BY 4.0 (snippet) | skip; 2024 supersedes |
| QMOF | figshare doi:10.6084/m9.figshare.13147324; per-record mirror on MPContribs (`contribs-api.materialsproject.org`, project `qmof`) | CC-BY 4.0 per the QMOF README and MPContribs' CCA4 label; the figshare page itself is **unconfirmed** (connection refused 2026-10-18) | the figshare bundle → NAS index first (geometry + PBE-D3(BJ) energy, VASP); MPContribs per-record only when no index is configured and `PRECIS_MP_API_KEY` is set, geometry only |
| ODAC23 | fairchem (arXiv 2311.00341), bulk LMDB | CC-BY 4.0 unconfirmed | step 2, not step 1 |
| Materials Project | `api.materialsproject.org`, `mp-api`; 25 req/s | CC-BY 4.0 | catalyst builder; free API key |
| Catalysis-Hub | `api.catalysis-hub.org/graphql` | unconfirmed | a third-party README says keyless GraphQL works again, against this file's 401 note; the catalyst builder re-probes first |
| BEAST DB | NREL `beastdb.nrel.gov` (DNS failed) | unconfirmed | none until hosting and licence are confirmed with the authors |

QMOF reads the bundle index first and MPContribs only when there is no
index (witty-forging-bee, 2026-10-18): MPContribs' `qmof` columns hold
band gaps and charges but no total energy, so a record fetched there is
geometry only. The key is the existing vault secret `PRECIS_MP_API_KEY`.

## Step 1 left: ops and one fixture

Step 1 (CIF import, the `qmof` / `core-mof` adapters, the bundle index)
is in the code; `precis-structure-help` documents the surface. Left:

- **Ops (cluster-desk, after deploy):** download the CoRE MOF 2024 zip and
  the QMOF figshare bundle to the NAS, extract any nested archive, run
  `precis structure index-bundle core-mof|qmof <path>`, and set
  `PRECIS_STRUCT_BUNDLE_DIR` to that NAS dir on the serving hosts.
  Check that the QMOF `qmof.json` keys the index reads (`qmof_id`,
  `name`, `outputs.pbe.energy_total`) match the downloaded bundle; they
  come from the QMOF documentation, not a downloaded copy.
- **A real QMOF test fixture.** `tests/fixtures/structure/mof/qmof_synthetic.cif`
  is synthetic (CifWriter layout, made-up atoms): keyless MPContribs reads
  return the structure empty. Record one real QMOF CIF from the bundle or
  a keyed MPContribs read and swap it in.

## Build order

1. **The reference-comparison hook + distrust flag.** First fed from the
   ODAC23 slice; Catalysis-Hub feeds it once the SUNCAT credentials land.
   How a record is matched to a Pd slab model and what is compared:
   `catalyst-library-pd-slab-tie-in.md`.
2. **The Catalysis-Hub credential path:** thread `X-API-Key` from a precis
   secret, and give a clean keyless error (`structure-import.md`).
3. **BEAST DB** (licence check first) and **Materials Project** adapters.

## Acceptance criteria

- Each source has an adapter in `structure/importers/` with a test that
  runs on a recorded raw record, with no network.
- An imported record carries `provenance="external"` and the source's
  `method` fingerprint, and never serves a compute cache hit.
- An on-demand lookup imports only the record it returns; a repeat lookup
  reuses the `(dataset, config_id)` row.
- One quest candidate shows its own energy next to the external reference
  with a method-mismatch note, readable through `get`. A gap above the
  threshold sets the distrust flag, and the candidate's rank is unchanged.

Owner: `src/precis/structure/importers/`; the quest hook is in
`src/precis/quest/`. Overlaps `structure-import.md` § bulk corpus and
`catalyst-discovery-quest.md` Slice 6: whichever ships first deletes the
overlap from the others.
