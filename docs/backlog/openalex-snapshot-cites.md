---
status: draft
title: Load citation edges from the OpenAlex snapshot into cites links
pillar: memory-graph
prio: normal
---

# Load citation edges from the OpenAlex snapshot into `cites` links

Reto approved 2026-10-24 ("also queue up the openalex dump and surrounding
infra"), alongside gr513755 (backfill `cites`/`cited-by` now that reference
lists are not embedded). Edge source for gr513755 stub-trickle priority,
gr513769 (cited-by seeds) and gr513768 (in-text citation resolution).

## Facts (confirmed 2026-10-24 from the bucket, not from memory)

| Fact | Value | Source |
|---|---|---|
| Latest release | 2026-10-14 (previous 2026-09-23, 2026-06-25) | `https://openalex.s3.amazonaws.com/RELEASE_NOTES.txt`; manifest `date` |
| `data/jsonl/works/` size | 675,298,563,638 bytes (**675 GB gzip**); whole jsonl tree 755.5 GB | `https://openalex.s3.amazonaws.com/data/jsonl/manifest.json` (entity `works`, `meta`) |
| Works records | 464,765,485 (whole tree 586,176,148) | same manifest |
| Files | **1,898** `part_NNNN.gz` under **396** `updated_date=` folders (2016-06-24 .. 2026-10-14); max file 985 MB, max 400,000 records | same manifest, summed per file |
| Layout | `s3://openalex/data/jsonl/works/updated_date=YYYY-MM-DD/part_NNNN.gz`; a parallel `data/parquet/` copy; `data/jsonl/manifest.json` is written last (presence = release complete); `legacy-data/` is frozen pre-2026 and unused | `https://openalex.s3.amazonaws.com/README.txt` |
| Partition meaning | each folder holds the records whose `updated_date` is that day; a release moves changed records into newer folders. 2026-dated folders hold 633 GB of the 675 GB | `https://help.openalex.org/access/sync/`; manifest |
| Licence | CC0 1.0 (`LICENSE.txt` at bucket root) | `https://openalex.s3.amazonaws.com/LICENSE.txt` |
| Cadence | public: quarterly, second Wednesday of Jan/Apr/Jul/Oct (next 2027-01-13), may slip a week; paid plans: daily | `https://help.openalex.org/access/sync/` |
| Deletions | cumulative works `deleted_ids.csv.gz` (`work_id`, `deleted_date`); IDs never reused; no merged-ID map | same sync page |
| `referenced_works` | yes: "OpenAlex IDs of the works this work cites (this work -> others)"; also `related_works`, `cited_by_count`, `doi`, `ids{doi,pmid,...}`. The snapshot work schema matches the API's. No `cited_by` list: inbound edges are the inverse of other works' `referenced_works` | `https://help.openalex.org/api-entities/works/work-object`; snapshot page `https://help.openalex.org/download-all-data/openalex-snapshot` |
| Anonymous access | yes: no AWS account, `aws s3 ... --no-sign-request`; bucket and prefix listing answered over plain HTTPS without credentials | snapshot page; listing `https://openalex.s3.amazonaws.com/?list-type=2&delimiter=/` |

Verified in slice 1 (`updated_date=2016-07-22/part_0000.gz`, 7.5 kB, 7
records, Range GET honoured — `206`, `Accept-Ranges: bytes`): `id` and
`ids.openalex` are `https://openalex.org/W...` URLs; `doi` and `ids.doi`
are `https://doi.org/...` URLs and can be absent; `referenced_works` is an
array of OpenAlex work URLs as in the API. Still unknown: how many held
papers carry an OpenAlex id — the loader reports the held-map sizes.

## Motivation / why

gr513755 excludes reference-list chunks from embedding permanently, so the
bibliography is reachable only through `cites` edges. Resolving each
reference through Crossref/OpenAlex/S2 per paper is rate-limited (the
`openalex` row in `0121_external_rate_limits.sql`) and
yields per-paper outbound edges only. One pass over the snapshot yields
every edge between held papers for the cost of bandwidth, plus the inbound
direction (who cites us) that the per-paper API cannot give cheaply.

## In scope

- A streaming loader (`src/precis/workers/openalex_snapshot.py`, name open)
  that reads `works/*.gz` one file at a time straight from S3, never
  materialising the 675 GB, and keeps only edges that touch a held paper.
- Held-paper matching, in order: OpenAlex id, then DOI.
  `ref_identifiers(id_kind, id_value, ref_id)` already stores
  `id_kind='openalex'` (set by `ingest/openalex_meta.py` via
  `store.set_ref_identifier`) and `id_kind='doi'` (lowercase, guarded by
  migration 0049). Per file: load the held `openalex`/`doi` -> `ref_id` map
  once per run into memory (held corpus is small next to 465M works).
- An edge is kept when the citing work OR the cited work is held. Both
  endpoints held: becomes a `cites` link. One endpoint held: stays in
  staging only (feeds the consumers below).
- Staging table (new forward-only migration, `0239_openalex_edges.sql`;
  re-check the next free number at build time, siblings may take it):

  `openalex_cite_edges`
  - `src_oa_id text NOT NULL` -- citing work, short form `W123`
  - `dst_oa_id text NOT NULL` -- cited work, short form
  - `src_ref_id bigint NULL REFERENCES refs(ref_id)` -- set when citing work is held
  - `dst_ref_id bigint NULL REFERENCES refs(ref_id)` -- set when cited work is held
  - `src_doi text NULL` -- citing work's DOI (lowercase), kept for stub seeding
  - `snapshot_date date NOT NULL` -- release the row came from
  - `loaded_at timestamptz NOT NULL DEFAULT now()`
  - `PRIMARY KEY (src_oa_id, dst_oa_id)`; partial indexes on `src_ref_id` and
    `dst_ref_id` WHERE NOT NULL; CHECK `src_ref_id IS NOT NULL OR dst_ref_id IS NOT NULL`.

  `openalex_snapshot_files` (checkpoint)
  - `snapshot_date date NOT NULL`, `s3_key text NOT NULL`, `content_length bigint NOT NULL`
  - `records_seen bigint`, `edges_kept bigint`, `status text NOT NULL` (`done` | `failed`)
  - `done_at timestamptz`, `PRIMARY KEY (snapshot_date, s3_key)`

  The in-corpus cite count is `count(*)` grouped by `dst_oa_id`; no extra column.
- Link minting: reuse the `cites` link writer from gr513755 slice 1 (tree
  gf-citeslink, landing soon). Do not write a second `links` insert path.
  The writer is idempotent on (src, dst, relation); minting reads staging
  rows with both ref ids set and `minted_at IS NULL` (add `minted_at
  timestamptz` to staging) and stamps it.
- Idempotent and resumable: upsert staging `ON CONFLICT DO NOTHING`; before
  each file check the checkpoint (`status='done'` and same `content_length`
  => skip); mark `done` in the same transaction as the file's last batch.
  A killed run restarts at the first file without a `done` row.
- Monthly refresh, incremental by partition: fetch the manifest, compare
  against the checkpoint table, process only `updated_date=` folders newer
  than the highest `done` folder of the previous snapshot, plus any file whose
  `content_length` differs. Because moved records re-appear in newer folders,
  stale copies in older folders are harmless (edge upsert is by key). Apply the
  deletion log: drop staging rows whose `src_oa_id` is in `deleted_ids.csv.gz`
  (links already minted are retired through the normal link path, not deleted).
  Public data changes quarterly, so a monthly timer is a cheap no-op on the
  manifest `date` in 2 of 3 runs.
- Budget per run (first full pass): network 675 GB inbound, one file at a
  time, no persistent copy. Scratch disk: one in-flight file (<= 1 GB gz,
  streamed through `gzip` without writing the decompressed text), plus the
  held-id map in RAM. Wall time: bound by bandwidth and gzip + JSON parse,
  roughly 1,898 files at ~100 MB/s is ~2 h of transfer; plan for a day of
  CPU on one core, resumable. Incremental refresh: only new/changed
  folders, a few GB typical (2026-07-01 alone is ~1.2 GB over 10 parts).
  Postgres writes: a few million staging rows, small.
- Rate limiting: S3 is not the OpenAlex API, so it does not draw on the
  `openalex` outbound row; the loader takes one `resource_slots` hold so it
  never competes with ingest for the node's network (confirm the slot name
  at build time).
- Node class: capability `worker_system`-style pass is wrong (runs on every
  node and the file loop would multiply 675 GB). Run it as a singleton
  capability on the worker role (`precis_worker`) of the **data** node class,
  the one with the most local disk and a wired uplink; declare it as a new
  entry in `precis_capabilities` in
  `deploy/inventory.example/group_vars/all/topology.example.yml` (for
  example `openalex_snapshot: [node-data]`, placeholder name only) and gate
  the role on membership, as `oa_fetch` does for the gateway. Real
  hostnames stay in the gitignored overlay.
- Consumers (each already filed; this spec only supplies the table):
  - gr513755 stub-trickle priority: in-corpus cite count of a stub =
    `count(*) FROM openalex_cite_edges WHERE dst_oa_id = <stub's id>` over
    held citing papers.
  - gr513769 cited-by seeds: replaces the daily OpenAlex `filter=cites:` poll
    for the historical backlog; rows with `src_ref_id IS NULL` are exactly
    "papers outside the corpus citing a held paper", ranked by seed weight.
  - gr513768 citation resolution: a resolved `cites` edge between two held
    papers narrows "which paper is [32]" to the bibliography entries whose
    DOI/OpenAlex id matches an outbound edge.

## Explicitly NOT in scope

- Not ingesting works metadata, abstracts, authors, or the `parquet` tree.
- Not creating stub refs for unheld papers (gr513755 owns stub minting; this
  only records the edge and the citing DOI).
- Not the in-text `[32]` -> bibliography-entry resolution (gr513768).
- Not Semantic Scholar contexts (`s2-citation-contexts.md`).
- Not the paid daily snapshot or the live API poll (gr513769 keeps the API
  for freshness between quarterly releases).
- Not mirroring the bucket (`aws s3 sync`) to local storage.

## Slices

| # | Slice | Acceptance criteria |
|---|---|---|
| 1 | Loader, filter, staging — **landed** (gf-oasnap1, migration 0240 not 0239) | Migration 0239 applies on dev DB and prod-shaped schema dump; unit test on a synthetic 3-record `.gz` fixture keeps only edges touching held papers, matches by OpenAlex id then DOI, and is a no-op on a second run; one real small partition (`updated_date=2016-07-22`) loads through `--no-sign-request` into dev; a full dry run over 20 files reports files/records/edges and peak RSS under 2 GB; kill mid-file then restart resumes without duplicate rows. |
| 2 | Link minting | After slice 1 on dev, every staging row with both ref ids set has a `cites` link via the gf-citeslink writer and `cited-by` shows in the fisheye; re-run mints nothing new; rows get `minted_at`; a held paper's outbound count equals its staging rows with `src_ref_id` set. |
| 3 | Refresh | A second snapshot date (simulated: edit the manifest fixture) processes only newer/changed folders; deletion log applied; the timer is a no-op when manifest `date` is unchanged; capability entry and role gate present in `topology.example.yml` and the secret-gate test still passes (no hostnames/IPs). |

Slice 2 is `blocked-by` gr513755 slice 1 landing. Prod first full pass is a
separate, Reto-confirmed run after slices 1-2 are green on dev (675 GB of
inbound transfer on a shared uplink).

## Target + blast radius

New: `src/precis/workers/openalex_snapshot.py`, migration `0239_*.sql`,
topology capability + role gate. Reads `ref_identifiers`; writes only the two
new tables and, through the shared writer, `links`. No change to ingest,
embeddings or existing workers.

## Open questions / decisions log

- Run location: confirm the data node class has the uplink for a 675 GB
  read (disk is not the constraint; one file in flight). No other role
  exists to fall back to.
- Match-rate unknown: how many held papers have `openalex` identifiers? If
  low, run `enrich_openalex` first or rely on DOI (the snapshot carries
  `doi` on most works). Measure in slice 1.
- OpenAlex ids are not stable across merges (no merged-id map is published);
  tolerate dangling `dst_oa_id` and re-key by DOI where possible.
- Keep staging rows with no held endpoint? Current answer no (size). Revisit
  if gr513769 wants "top cited unheld papers" over the full graph; that would
  need a separate aggregate pass, not stored edges.
- Whether to load from `parquet/` (columnar, `referenced_works` + `id` + `doi`
  only, far less bytes read) instead of `jsonl/`. Parquet is 770 GB total but
  column projection could cut transfer sharply; needs a pyarrow dependency
  check (`uv`). Decide in slice 1.
