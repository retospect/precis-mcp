---
status: draft
title: Source bundles — a design owns an immutable, hash-keyed set of source files
pillar: 3d-design
prio: normal
---

# Source bundles for design scripts

Draft for Reto's review. Parent gripe: gr520323; Reto's ruling
2026-10-26 (board scripts are stored in precis as source files of the
design — "how do we deal with multiple files? … just don't paint into
corner").

## Motivation / why

A board script's `to_batch()` for a 50-part board is ~36 KB of JSON
(`precis_geom.pcb::Board.to_batch` → the `put(kind='pcb')` batch dict —
`components`/`nets`/`connections`/`features`/…): the only door in is
pasting it inline into `put(kind='pcb', args=…)`, and the script itself
is stored nowhere — the batch lands but the code that produced it lives
on the agent's laptop. The se workspace door already shows both halves
of the missing piece: code as a `folder` ref's `plaintext` children
(`precis_se.workspace_import::_sync_workspace_files`, linked `part-of`
the design, `meta.workspace_path` relpaths) and a signed tar upload
(`/files/upload_tree`, gr517643) — but the folder rewrites in place
(chunk DELETE+INSERT), so a run's `script_sha` is "a string nobody
checks" (structure-workspaces §Code storage, which leaves its git-
objects (b) open, not ruled).

Reto's ruling, compressed:

1. A design owns a source SET, not a file — N files at relative paths
   (`board.py`, `comb.py`, `lib/footprints.py`, data CSVs), one marked
   entry point. Never a single text field.
2. Versioned as a whole — every upload is an immutable snapshot keyed
   by a content hash of the tree (per-file sha + manifest), so a
   place/route job records exactly which snapshot produced it. Old
   snapshots are kept and prunable.
3. Upload as a bundle through the signed upload door the paper PDFs
   use (3c44a411c), plus a small-file inline path. Never 36 KB of
   inline JSON.
4. Generic, not pcb-only — a source bundle attachable to any ref; se /
   cad / structure scripts reuse it later.
5. The batch is derived from the snapshot (the server runs the entry
   point, sandboxed) or uploaded alongside; either way it links to the
   snapshot hash.
6. Read side — list files, read one file, diff two snapshots.

## In scope (slice 1)

Store + manifest + read + the job stamp, wired on pcb only.

- **Tables** — `src/precis/migrations/<next>_source_bundles.sql` (0262
  is free at spec time):
  - `source_blobs(sha256 char(64) PK, bytes bytea, size_bytes bigint,
    created_at)` — content-addressed file bodies; identical files
    share one row across snapshots and designs.
  - `source_snapshots(tree_sha char(64) PK, ref_id bigint REFERENCES
    refs, manifest jsonb, n_files int, size_bytes bigint, created_by
    text, created_at, pruned_at timestamptz)` — `manifest` is
    `{"entry": "board.py", "files": {relpath: {"sha256":…,"size":…}}}`;
    `tree_sha` = sha256 over its canonical JSON. `ref_id` is the owner
    — generic by column (a links edge can't bind a snapshot: `links`
    joins refs, and a snapshot is not a ref — see Q1).
- **Upload door** — a third sentinel `file_links.UPLOAD_SOURCE_SHA =
  'upload_source'` beside `UPLOAD_SHA`/`UPLOAD_TREE_SHA`, and
  `POST /files/upload_src/{ref_id}` in `precis_web.routes.files` —
  the same `_stage_stream` contract (token verified before one body
  byte, nonce `.done` tombstone claimed first, streamed sha256,
  `MAX_UPLOAD_BYTES` 413 mid-read, `ustar`@257 as the format gate)
  writing `<ref_id>-<sha>.src.tar` under `PRECIS_UPLOAD_STAGING`.
  A distinct suffix on purpose: `.tar` is already the se out/-tree
  staged name under the same ref-bound scheme, and once se adopts
  bundles one ref can hold both kinds of staging.
- **Attach op (pcb)** — `put(kind='pcb', id=<slug>,
  args={'op':'source_upload_link'})` mints the POST URL (the
  `workspace_upload_link_op` shape); `args={'op':'source',
  'staged':'<sha>', 'entry':'board.py'}` resolves the staged tar via
  `staged_src_path(sha, ref_id)` in src/precis/ingest/pdf_upload.py — existence IS the record,
  ref-bound — unpacks it under `_extract_staged_tree`'s rules (clean
  relative posix paths, regular files only, no file/dir nesting,
  `MAX_WS_FILES`/`MAX_WS_FILE_BYTES`/`MAX_WS_OUT_BYTES` caps), writes
  blobs + manifest + the snapshot row, answers `source_sha=<tree_sha>`.
  The small-file inline path is the same op with
  `args={'files': {relpath: text}, 'entry': …}` under the
  `_vet_relpath_map` vet — no tar, no HTTP, for trees that fit in an
  args dict.
- **Read side** — `get(kind='pcb', id=<slug>, view='source')` lists
  snapshots (`tree_sha`, entry, n_files, bytes, created_at, pruned
  mark); `args={'sha': <tree_sha>}` lists that snapshot's files;
  `args={'sha':…, 'path':'lib/footprints.py'}` returns the file body.
- **Provenance on builds** — the batch put gains `args['source'] =
  <tree_sha>`: vetted as a live snapshot of THIS design, stamped
  `meta.generated.source_sha` beside the existing `trace`/`run_job`
  binding (`put`'s `ws_trace` block, handlers/pcb.py ~line 1349), and
  `_enqueue_op` copies the design's current `source_sha` into the
  `pcb_place` job's `params` and through into `run_record`
  (`precis.pcb.run_record` — params/input_digest/result_digest already
  recorded on the job meta), so a placement run answers "which
  snapshot produced it".
- **Shared module** — `src/precis/source_bundles.py` owns the manifest
  vet, the tar extraction, snapshot writes and the readers; the pcb
  op/view bodies are thin calls into it so a second kind adopts by
  registering two ops and one view, nothing else. The pure vetter
  cores in `precis_se.workspace_import` (`_vet_relpath_map`,
  `_extract_staged_tree`) move DOWN into it — `precis` cannot import
  `precis_se` (the layering rule `_python_ws.py` works around with
  plain SQL) — and `precis_se` re-imports them there.

## Explicitly NOT in scope (slice 1)

- **Server-side derivation** — the container runs the entry point and
  the manifest applies: `sandbox_run mode:script` stages a `folder`
  `code_ref` into `/work/code` today (`src/precis/workers/executors/_sandbox_inputs.py::
  stage_code`), and a snapshot is not a folder ref. Slice 2 — and
  pcb-workspace-code owns the manifest-apply half regardless (its
  `import_workspace` analog for `manifest.kind == 'pcb'`).
- **Snapshot diff** (ruling §6) — the manifests make it cheap; its own
  view, not this slice.
- **se / cad / structure adoption** — tables and helpers are generic;
  ops and views register on pcb only.
- **Git history** — no `parent_sha`, no commit DAG; that is
  structure-workspaces §1b's still-open (b) and layers on later
  without re-keying (`tree_sha` is already a valid commit payload).
- **Zip uploads**, automatic pruning sweeps, mutation or partial
  fetch of a snapshot.

## Acceptance criteria

- Door round-trip: `op='source_upload_link'` → POST a 3-file tar
  (`board.py`, `comb.py`, `lib/footprints.py`) → `op='source'` answers
  `source_sha`; `view='source'` lists it; `args={'path':
  'lib/footprints.py'}` returns the uploaded bytes.
- Idempotent + immutable: re-uploading the unchanged tree answers the
  same `tree_sha` and writes no new `source_blobs` rows; a one-byte
  change mints a new snapshot and the old one still reads.
- Provenance: a batch put with `args['source']` applies and stamps
  `meta.generated.source_sha`; the next `pcb_place` job carries
  `source_sha` in `params` and in its stored `run_record`.
- Refusals: a tar violating the vet (absolute/`..`/dup paths, links or
  devices, over caps) is BadInput naming the member; a `staged` sha
  bound to another ref, an `entry` not in the manifest, and a
  `source:` sha not owned by the design are each BadInput with `next=`
  naming the fix; inline `files` over cap refuses before any row
  writes.
- `precis_se` keeps passing its staged-tree tests after the vetters
  move (no behaviour change on the `import_workspace` door).

## Target + blast radius

New: `src/precis/migrations/<next>_source_bundles.sql`;
`src/precis/source_bundles.py`; `POST /files/upload_src/{ref_id}` in
`src/precis_web/routes/files.py`; `file_links.UPLOAD_SOURCE_SHA`;
`staged_src_name`/`staged_src_path` in src/precis/ingest/pdf_upload.py (the staging sweep
already covers every file under the dir). Touched:
`src/precis/handlers/pcb.py` (`put` ARGS — `source`, `staged`,
`files`, `entry`, `path`, `sha` — two ops, `view='source'`, the
`args['source']` stamp, `_enqueue_op` params),
`src/precis/pcb/run_record.py` (param pass-through),
`src/precis_se/workspace_import.py` (vetter cores moved down,
re-imported), `precis-pcb-help` skill; tests beside
`tests/precis_web/test_files.py` and `tests/test_pcb_board_script.py`.
Deploy: none — `PRECIS_UPLOAD_STAGING` and the vault file-link key
already exist.

## Open questions / decisions log

1. **MCP surface: per-kind ops vs a `source` kind.** A snapshot could
   be a first-class ref (`put(kind='source')` — links, handles,
   eyes/fisheye for free). *Recommendation:* per-kind ops mirroring
   the proven `workspace_upload_link`/`import_workspace` door shape —
   `tree_sha` already addresses every snapshot, and a `source` kind
   can wrap these tables later without re-keying.
2. **Ownership: `ref_id` column vs a links edge.** `links` joins
   refs; a snapshot is not a ref (Q1), so the edge cannot express it.
   *Recommendation:* `source_snapshots.ref_id` + index; revisit if Q1
   ever flips.
3. **Batch→snapshot binding.** `args['source']` stamped
   `meta.generated.source_sha` (as scoped) vs carrying it inside
   `args['trace']`'s run record vs a new relation. *Recommendation:*
   the args key + generated meta — it rides the provenance block the
   `trace`/`run_job` keys already write, and `_enqueue_op` reads it
   off design state without a second lookup path.
4. **Server-side derivation (ruling §5a).** Materialize a snapshot
   under `/work/code` as a `stage_source` sibling to `stage_code`, or
   mint folder refs so `code_ref` works unmodified. *Recommendation:*
   defer to slice 2 — the batch rides `put` as today with `source:`
   marking provenance, and the materialization shape lands with Q1's
   kind decision (refs would make `code_ref` free).
5. **Format gate.** Uncompressed tar only — the `upload_tree` gate
   (`ustar`@257) and the `_extract_staged_tree` vet as-is — vs
   accepting zip. *Recommendation:* tar only; the stream already
   sniffs it and the vet covers every evil-path case.
6. **Relation to structure-workspaces §1b** (git objects in Postgres —
   discussion, not ruled). This spec is its (b) minus the commit DAG:
   content-addressed blobs + a manifest hash, no commits.
   *Recommendation:* say so — `parent_sha` joins the table later if
   the DAG is wanted; nothing here forecloses it.
7. **Pruning semantics.** Ruling: "kept and prunable". *Recommendation:*
   `pruned_at` marks the snapshot; pruning drops its `source_blobs`
   rows that no other snapshot shares and never touches the manifest
   — a pruned snapshot still lists/diffs and every job's provenance
   still resolves, only `path` reads say "pruned". The op is
   `op='source_prune'` in slice 2; no automatic sweep.
8. **Entry-point mutability.** `entry` inside the manifest (a
   different entry = a different `tree_sha`, blobs still shared) vs a
   mutable column on an immutable row. *Recommendation:* inside the
   manifest — immutability stays absolute and re-pointing the entry is
   just a new snapshot that shares every blob.
