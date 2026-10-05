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
  `mof-tools.yaml` tag papers. The `structure` kind exports CIF but has no
  CIF import. The engine builds fcc111 slabs only (`quest/catalyst_seed.py`;
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
| CoRE MOF (2019 / 2024) | ~14k / ~40k computation-ready experimental MOF CIFs | MOF structure seeds | open; needs CIF import |
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

## Build order

1. **CIF import + a CoRE MOF / QMOF adapter.** Open sources, no
   credentials needed, and they unblock the MOF library.
2. **The reference-comparison hook + distrust flag.** First fed from the
   ODAC23 slice; Catalysis-Hub feeds it once the SUNCAT credentials land.
   How a record is matched to a Pd slab model and what is compared:
   `catalyst-library-pd-slab-tie-in.md`.
3. **The Catalysis-Hub credential path:** thread `X-API-Key` from a precis
   secret, and give a clean keyless error (`structure-import.md`).
4. **BEAST DB** (licence check first) and **Materials Project** adapters.

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
