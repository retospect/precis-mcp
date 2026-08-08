# External DFT import — bulk corpus + CLI (ADR 0053 residual slices)

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
