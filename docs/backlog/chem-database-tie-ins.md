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
| QMOF | figshare doi:10.6084/m9.figshare.13147324; per-record mirror on MPContribs (`contribs-api.materialsproject.org`, project `qmof`) | likely CC-BY 4.0, **unconfirmed** (figshare 403'd) | per-record via MPContribs needs the MP API key; else the figshare bundle → NAS index. PBE energies (VASP) |
| ODAC23 | fairchem (arXiv 2311.00341), bulk LMDB | CC-BY 4.0 unconfirmed | step 2, not step 1 |
| Materials Project | `api.materialsproject.org`, `mp-api`; 25 req/s | CC-BY 4.0 | catalyst builder; free API key |
| Catalysis-Hub | `api.catalysis-hub.org/graphql` | unconfirmed | a third-party README says keyless GraphQL works again, against this file's 401 note; the catalyst builder re-probes first |
| BEAST DB | NREL `beastdb.nrel.gov` (DNS failed) | unconfirmed | none until hosting and licence are confirmed with the authors |

The builder confirms the QMOF licence on the figshare page before
committing the adapter's licence note. QMOF goes per-record through
MPContribs when `MP_API_KEY` is set; otherwise the bundle index.

## Step 1 in detail (the builder's brief source)

1. **`cif_to_scene(text) -> Scene`** in `structure/importers/` (lazy
   `ase.io.read(format="cif")` → the existing `scene_from_ase`,
   `structure/search.py`). Caps before parse: text size, and an atom-count
   cap after (huge cells hang ase). A 3D framework keeps
   `pbc=(True, True, True)`; nothing slab-specific.
2. **Generic agent entry.** A `put` on the structure kind that takes CIF
   text, or a URL fetched with `safe_get` (redirects followed, size-capped;
   `src/precis/utils/safe_fetch.py`). It yields an ordinary design (no
   external run) unless a source is named. Refusals carry `next=`.
3. **Adapters** `qmof` and `core-mof`, registered with `register_adapter`,
   pure over a recorded raw record `{id, cif_text, energy?, method?}`.
   `config_id` is the QMOF id / CoRE refcode; `dataset` the source name.
4. **Optional energy.** `ExternalRun.energy` is a required float today
   (`structure/importers/__init__.py`); CoRE MOF records have none. Make it
   `float | None`, with the store path (`store/_structure_ops.py`
   `structure_import`) writing a geometry-only external run, and the method
   fingerprint still set (`{"source": "core-mof-2024", "geometry":
   "experimental, solvent-removed"}`). If the column is NOT NULL, this is a
   forward-only migration; check before writing code.
5. **Lookup wiring.** Extend `_get_external` in `handlers/structure.py`
   (hard-wired to catalysis-hub today) with `source='qmof'|'core-mof'` and
   `id=`. It reads the NAS index, imports that one record through the
   adapter, and short-circuits on an existing `(dataset, config_id)` row.
   The index path comes from config (`docs/reference/config-variables.md`),
   never a literal host path. Missing index → a refusal naming the
   build-index command.
6. **Index build CLI** `precis structure index-bundle <source> <path>`:
   streams the downloaded bundle, writes `{id: (member, offset)}`. The
   download itself is an ops step (cluster-desk, after deploy).

Step 1 acceptance (replaces the general criteria for this step):

- A recorded CIF fixture of each source parses to a Scene with a 3D cell;
  no network in tests.
- The adapter output carries `provenance="external"` and a method
  fingerprint; a CoRE MOF record imports with no energy.
- The same `config_id` twice yields one ref.
- A CIF over the atom cap and a non-CIF body are refused with `next=`.
- A URL CIF import goes through `safe_get` (asserted by a test double).

Shipping step 1 deletes, from this file: the "**MOFs: nothing beyond
tags**" bullet's "has no CIF import" clause, and "needs CIF import" from
the CoRE MOF row.

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
