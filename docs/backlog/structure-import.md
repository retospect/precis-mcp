---
status: draft
pillar: 3d-design
---

# Structure import

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## structure_import isn't atomic end-to-end

_Grouped 2026-09-26; was `structure-import-atomicity`._

**Landed 2026-10-21** (atomic import in one tx; GraphQL filter values
escaped via `catalysis_hub._reactions_query`).

## External DFT import — bulk corpus + CLI (ADR 0053 residual slices)

_Grouped 2026-09-26; was `structure-import-bulk-corpus`._

The engine shipped (`cathub_db.batch_import`, proven on PengRole2020.db).
Remaining: a `precis import <source> --filter` CLI + resumable cursor, and
the first *open* bulk-source adapter — pivot to OC20 (anonymous S3) or
AQCat25 (HF gated:auto), batch-mirroring a filtered Pd/Cu/Ni × N/O/NHx slice
(few-thousand configs, embeds/searches cleanly); Reto picked
Catalysis-Hub first (2026-10-02, `chem-database-tie-ins.md`); he is
requesting the SUNCAT credentials.
Catalysis-Hub is parked: ALL public channels now need SUNCAT creds (GraphQL
401s keyless; the cathub "public" pg password was rotated server-side) — the
`PRECIS_CATHUB_API_KEY` path and the clean refusal are built; waits on creds.
Small follow-ons: carry pub title/authors/year when dataset_doi is null;
promote `source=` to a first-class get param; the derivative loop + MLIP
fine-tuning on the imported corpus stays deferred (§4/§7). Owner
`src/precis/structure/importers/`.
