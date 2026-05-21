# pip-merge — fold acatome-extract + acatome-meta into precis-mcp

- **Status**: in-progress (2026-05-21)
- **Parent plan**: [`storage-v2.md`](./storage-v2.md) §step B
- **Branch**: `feat/storage-v2-step-b`

This is the file-by-file mapping and execution order for ADR 0001.

## Surveyed reality

`precis-mcp` does **not** import any `acatome_*` module today. The
existing dep `acatome-extract[embeddings]>=0.1` in `pyproject.toml`
is purely transitive: it pulls in `marker-pdf` and
`sentence-transformers` for the `[paper]` extra. The Python-level
coupling is zero.

The runtime coupling is:

- `infrastructure/compose.yaml`'s `acatome-watch` service runs the
  `acatome-extract watch` CLI. It produces `.acatome` bundles in
  `~/work/corpus/`.
- `Store.ingest_bundle()` in
  `src/precis/store/_ingest_ops.py` reads those bundles and
  populates `refs` + `blocks` rows.

So the merge is:

1. **Vendor the source code** of both repos into `precis/ingest/`
   and `precis/identity.py`.
2. **Replace bundle indirection** with direct DB writes (the
   `.acatome` file format dies).
3. **Replace the `acatome-watch` compose service** with
   `precis watch`.
4. **Drop** `acatome-extract` and `acatome-meta` from the dep graph;
   the equivalents (`marker-pdf`, `sentence-transformers`,
   `httpx`, etc.) become direct deps.

## File-by-file mapping

### From `acatome-extract/src/acatome_extract/`

| Source | Destination | Action | Notes |
|---|---|---|---|
| `__init__.py` | drop | — | obsolete entry-point |
| `am.py` | `precis/ingest/annotations.py` | move + adjust imports | annotation extraction |
| `bundle.py` | drop | — | bundle format dies in v2 |
| `chunker.py` | `precis/ingest/text_chunker.py` | move | text-level chunker (the *block→chunk* logic from storage-v2 lives in a new `precis/ingest/chunks.py`) |
| `cli.py` | partly → `precis/cli/add.py`, partly drop | refactor | extract `precis add` from `acatome-extract add`; drop `bundle` subcommand |
| `enrich.py` | drop | — | replaced by lazy worker queue |
| `figures.py` | `precis/ingest/figures.py` | move | figure extraction |
| `ids.py` | drop | — | thin wrapper; replaced by `precis.identity` |
| `marker.py` | `precis/ingest/marker.py` | move | core block extraction |
| `opener.py` | `precis/cli/show.py` | move + integrate | "open PDF by slug or pub_id" |
| `pdf_metadata.py` | `precis/ingest/pdf_metadata.py` | move | PDF metadata read/write |
| `pipeline.py` | `precis/ingest/pipeline.py` | rewrite | drop bundle write step; emit DB rows |
| `watch.py` | `precis/cli/watch.py` | move + simplify | becomes thin wrapper |

### From `acatome-meta/src/acatome_meta/`

| Source | Destination | Action | Notes |
|---|---|---|---|
| `__init__.py` | drop | — | |
| `citations.py` | `precis/ingest/citations.py` | move | citation parsing |
| `config.py` | merge into `precis/config.py` | partial | unify config surface |
| `crossref.py` | `precis/ingest/crossref.py` | move | DOI lookup client |
| `literature.py` | split | partial | `make_slug` → `precis.identity`; rest → `precis/ingest/literature.py` |
| `lookup.py` | `precis/ingest/lookup.py` | move | unified lookup over crossref/S2/arxiv |
| `pdf.py` | `precis/ingest/pdf_sidecar.py` | move | `.meta.json` sidecar handling |
| `semantic_scholar.py` | `precis/ingest/semantic_scholar.py` | move | S2 client |
| `verify.py` | `precis/ingest/verify_metadata.py` | move | metadata cross-check (vs. the `precis verify` CLI which sets `human_verified_at` — names must not collide) |

### Tests to migrate

From `acatome-extract/tests/`:
- `test_marker.py`, `test_pdf_metadata.py`, `test_chunker.py`,
  `test_figures.py`, `test_ids.py` → `tests/ingest/`
- `test_bundle.py`, `test_enrich.py`, `test_migrate.py`, `test_gpu_embed.py`,
  `test_prompt_safety.py` → drop (target functionality dropped or
  superseded)
- `test_watch.py` → `tests/cli/test_watch.py`
- `conftest.py` → merge into `tests/conftest.py`

From `acatome-meta/tests/`:
- `test_literature.py`, `test_pdf.py`, `test_verify.py`,
  `test_crossref.py`, `test_lookup.py` → `tests/ingest/`
- `test_config.py` → merge into `tests/test_config.py`

## New `precis.identity` module

Single source of truth for IDs. Public surface:

```python
def make_paper_id(*, doi: str | None = None, arxiv_id: str | None = None,
                  pdf_hash: str | None = None) -> str: ...
def make_pub_id(paper_id: str) -> str: ...        # 6-char base32 lowercase
def make_slug(authors: Any, year: int | None, title: str) -> str: ...
def make_node_id(paper_id: str, page: int, block_index: int) -> str: ...
def make_pdf_hash(pdf_bytes: bytes) -> str: ...   # sha256 hex
def make_content_hash(normalized_text: str) -> str: ...
```

All ID derivation lives here. Deterministic, no I/O, no model loads.

## Greenfield migration question

`docs/design/storage-v2.md` originally proposed adding migrations
`0010`–`0014` on top of the existing `0001`–`0009`. The user has
since opened the door to **wiping migrations** since the DB has no
production data:

> "the schema, we can start over from the point where we want to,
> there's nothing in there now."

**Decision**: greenfield. We replace `0001`–`0009` with a single
new `0001_initial.sql` reflecting the v2 schema directly. ADR 0005
(to be written) records the rationale and the data we throw away.

This greenfield-migration work is **part of step B** because the
new ingest pipeline targets the v2 schema; we can't ship the
pip-merged ingest code against a schema that doesn't have
`pdfs`, `chunks`, `pub_id`, `block_jobs`. So step B grows.

## Execution order (small commits on `feat/storage-v2-step-b`)

Each commit is independent enough that tests pass after it. We
don't ship a half-built ingest path — but we do ship the schema
first, so subsequent commits can target it.

1. **B0 — sub-plan + ADR 0005 (greenfield migrations)**
   - this file + `docs/decisions/0005-greenfield-migrations.md`
2. **B1 — greenfield schema**
   - delete `src/precis/migrations/0001_initial.sql` through
     `0009_*.sql`; replace with one `0001_initial.sql` defining
     the v2 schema (refs/pub_id, ref_identifiers, blocks, chunks,
     chunk_embeddings, chunk_summaries, embedders, block_jobs,
     pdfs, links, tags, ref_tags, cache state)
   - update `src/precis/store/migrate.py` if any logic changes
   - rewrite tests that asserted intermediate schema states
3. **B2 — `precis.identity`**
   - new module with `make_paper_id`, `make_pub_id`, `make_slug`,
     `make_node_id`, `make_pdf_hash`, `make_content_hash`
   - tests: `tests/test_identity.py`
   - no consumers wired yet
4. **B3 — vendor `precis.ingest.*` (no behaviour change)**
   - copy over module files from `acatome_extract` and
     `acatome_meta`, adjust imports, fix references to
     `acatome_meta.literature.make_slug` → `precis.identity.make_slug`
   - copy tests under `tests/ingest/`
   - register the new package in `src/precis/__init__.py`
   - existing `precis.store._ingest_ops` (bundle ingest) keeps
     working through B3 — we haven't touched it
5. **B4 — `precis add` CLI command**
   - new `precis/cli/add.py`
   - direct DB writes (no `.acatome` bundle file)
   - `precis add file.pdf`, `precis add --doi <X>`, `precis add
     --arxiv <Y>`
   - tests use a fixture PDF + a stub embedder
6. **B5 — `precis watch` CLI command**
   - new `precis/cli/watch.py` (vendored from `acatome_extract.watch`)
   - calls `precis_add()` directly; on success moves the file to
     `corpus/<letter>/<slug>.pdf`; on failure to
     `errors/<timestamp>/<filename>` with a `.error.txt` next to it
   - tests adapted from `acatome_extract.tests.test_watch`
7. **B6 — `precis worker` skeleton**
   - new `precis/cli/worker.py`
   - polls `block_jobs`; runs `embed:bge-m3` and `summarize:rake`
     handlers
   - tests with an in-memory queue stub
8. **B7 — drop legacy ingest path**
   - delete `src/precis/store/_ingest_ops.py` (bundle ingest)
   - delete `src/precis/ingest.py` (the legacy bundle dispatcher)
   - delete `src/precis/cli/ingest.py` `ingest-bundle` /
     `ingest-bundles` subcommands
   - update `src/precis/store/store.py` mixin list
   - rewrite tests that referenced bundle ingest to use
     `precis_add()` directly
9. **B8 — `pyproject.toml` cleanup**
   - drop `acatome-extract[embeddings]` from `[paper]` extra
   - add direct deps: `marker-pdf`, `httpx`, anything else that
     was transitive via acatome-extract
   - bump version to `0.7.0`, add `CHANGELOG.md` entry
10. **B9 — `infrastructure/compose.yaml` update**
    - rename `acatome-watch` service to `precis-watch`
    - point at `precis-mcp:latest` image, `command: ["precis", "watch", "/inbox"]`
    - rename `acatome-cache` volume to `precis-cache`
    - this commit lives on `feat/precis-mcp-dev-container` in the
      `infrastructure` repo (not in `precis-mcp`)
11. **B10 — TOON output module** (per ADR 0002)
    - `precis/format/toon.py` + serializer registry
    - integrate into MCP tabular responses
    - integrate into CLI default piped output
12. **B11 — green-light cutover**
    - apply migration 0001 to a fresh DB
    - drop the existing `acatome-watch` container, start
      `precis-watch`
    - watch ingests `~/work/new_papers/` from scratch into the v2
      schema
    - verify search returns sensible results
    - close the OPEN-ITEMS.md mojibake item if covered by the
      ftfy roundtrip in the new chunker

Each commit message names the step (`B1: greenfield schema`,
`B2: precis.identity module`, …) so `git log --oneline` reads as a
plan trail.

## Reverse compatibility

There is none for the bundle format. Any `.acatome` files in
`~/work/corpus/` become orphans after this work lands. Rationale:
no external consumers, the format was an implementation detail of
the acatome-extract → precis-mcp handoff, and re-ingesting from
PDFs is fast (we already need to do it for the schema cutover).

## Risk register

- **B1 (schema rewrite) is the biggest blast radius.** Every test
  that asserts table shapes needs a once-over. Mitigation: run
  `pytest --co` after each schema commit to spot collection
  errors early.
- **B3 (vendoring) doubles import paths temporarily.** While both
  the legacy `_ingest_ops.py` and the new `ingest/` exist,
  someone running `precis ingest-bundle` is using the legacy
  path. Steps B4–B7 close this gap.
- **B8 (drop deps).** Anyone with a stale `uv.lock` will fail to
  resolve `acatome-extract` after the drop. Document in CHANGELOG;
  the cutover step is the natural moment to reseed the lock.
- **Worker DB pressure.** B6's worker polls every 1s by default;
  with thousands of pending jobs after the cutover that's fine.
  Add a backoff if pressure shows up.

## Definition of done for step B

- [ ] `feat/storage-v2-step-b` merged into `main` (precis-mcp).
- [ ] `feat/precis-mcp-dev-container` already merged
      (infrastructure).
- [ ] New `feat/storage-v2-step-b-compose` merged (infrastructure)
      with the watch-service rename.
- [ ] Fresh DB + `precis migrate` applies the single
      `0001_initial.sql`.
- [ ] `precis add file.pdf` produces a ref + chunks + pending jobs.
- [ ] `precis worker` drains the queue.
- [ ] `precis watch /inbox` ingests a directory end-to-end.
- [ ] `acatome-extract` and `acatome-meta` repos are read-only
      (archived in their READMEs with pointers here).
- [ ] All tests pass in `precis-dev` container.
- [ ] CHANGELOG.md has a 0.7.0 entry summarising the merge.
