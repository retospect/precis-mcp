# Structure import

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## structure_import isn't atomic end-to-end

_Grouped 2026-09-26; was `structure-import-atomicity`._

`structure_save` commits its own tx, then a second tx writes
ref_identifiers + the external run. A crash between the two leaves a ref
with no identifier row; on retry structure_save finds the orphan by its
deterministic slug (created=False), so the `if created`-guarded identifier
insert never fires again → the (dataset, config_id) lookup permanently
misses. Fold create + identifier + run into one transaction, or make the
identifier insert unconditional/idempotent. Sibling hygiene: escape/allowlist
the f-string-interpolated GraphQL filter values in
`structure/importers/catalysis_hub.py::fetch_config`. Owner
`src/precis/store/_structure_ops.py::structure_import`. Mechanical.

## External DFT import — bulk corpus + CLI (ADR 0053 residual slices)

_Grouped 2026-09-26; was `structure-import-bulk-corpus`._

The engine shipped (`cathub_db.batch_import`, proven on PengRole2020.db).
Remaining: a `precis import <source> --filter` CLI + resumable cursor, and
the first *open* bulk-source adapter — pivot to OC20 (anonymous S3) or
AQCat25 (HF gated:auto), batch-mirroring a filtered Pd/Cu/Ni × N/O/NHx slice
(few-thousand configs, embeds/searches cleanly); awaiting Reto's source pick.
Catalysis-Hub is parked: ALL public channels now need SUNCAT creds (GraphQL
401s keyless; the cathub "public" pg password was rotated server-side) — if
creds arrive, thread X-API-Key from a precis secret + a clean keyless error.
Small follow-ons: carry pub title/authors/year when dataset_doi is null;
promote `source=` to a first-class get param; the derivative loop + MLIP
fine-tuning on the imported corpus stays deferred (§4/§7). Owner
`src/precis/structure/importers/`.
