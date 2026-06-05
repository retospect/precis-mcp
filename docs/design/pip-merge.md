# pip-merge — fold acatome-extract + acatome-meta into precis-mcp

- **Status**: shipped — B0–B5 landed across 2026-05-21 → 2026-05-24
  (commits f02dad6 → 02c878c → ab5ab20). `acatome-extract` /
  `acatome-meta` have been vendored into `src/precis/ingest/`; see
  `pyproject.toml` `# (vendored from acatome-extract in B3/B4)`.
  Later steps (B7 removal of `Store.ingest_bundle()`, B10 TOON output)
  shipped subsequently; tracked in their own design docs.
- **Parent plan**: [`storage-v2.md`](./storage-v2.md) §step B
- **Branch**: merged from `feat/storage-v2-step-b` into `main`.

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
| `opener.py` | `precis/cli/show.py` | move + integrate | "open PDF by cite_key or pub_id" |
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
| `literature.py` | split | partial | `make_cite_key` → `precis.identity`; rest → `precis/ingest/literature.py` (no `make_slug`; dropped per ADR 0008) |
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

Single source of truth for IDs (per ADR 0006). Public surface:

```python
def make_paper_id(*, doi: str | None = None, arxiv_id: str | None = None,
                  pdf_hash: str | None = None) -> str: ...
def make_pub_id(paper_id: str) -> str: ...           # 6-char base32 lowercase
def make_cite_key(authors: Any, year: int | None,
                  taken: set[str]) -> str: ...       # miller23a (ADR 0006)
# (no make_slug; dropped per ADR 0008)
def make_node_id(paper_id: str, page: int, block_index: int) -> str: ...
def make_pdf_hash(pdf_bytes: bytes) -> str: ...      # sha256 hex
def make_content_hash(normalized_text: str) -> str: ...   # sha256 hex
```

All ID derivation lives here. Deterministic, no I/O, no model loads.
The `taken` argument to `make_cite_key` is the set of cite_keys
already in the corpus that share the prefix (e.g. all rows with
`cite_key LIKE 'miller23%'`); pass an empty set on first ingest.

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
     the v2 schema:
     - refs (identifier-free hub, kind FK, retraction +
       human-verified columns; per ADR 0008)
     - ref_identifiers (THE identifier table: pub_id, cite_key,
       paper_id, DOI, arxiv, s2, pubmed, openalex, pdf_sha256,
       content_hash)
     - v_refs view (exposes pub_id/cite_key/paper_id as columns)
     - pdfs (normalized; multi-paper-per-PDF)
     - chunks (kind-agnostic; NULL-able page_first/last;
       broadened chunk_kind enum per storage-v2.md)
     - chunk_embeddings, chunk_summaries (status / attempts /
       last_error — derived queue per ADR 0007; **no**
       block_jobs table)
     - embedders (registry, seeded with bge-m3)
     - links, tags, ref_tags, cache_state
   - update `src/precis/store/migrate.py` if any logic changes
   - rewrite tests that asserted intermediate schema states
3. **B2 — `precis.identity`**
   - new module with `make_paper_id`, `make_pub_id`,
     `make_cite_key`, `make_node_id`, `make_pdf_hash`,
     `make_content_hash` (ADRs 0006 + 0008; no `make_slug`)
   - tests: `tests/test_identity.py` — deterministic outputs,
     ASCII safety, collision-suffix progression
     (`miller23` → `miller23a` → `miller23b`)
   - no consumers wired yet
4. **B3 — vendor `precis.ingest.*` (no behaviour change)**
   - copy over module files from `acatome_extract` and
     `acatome_meta`, adjust imports, fix references to
     `acatome_meta.literature.make_slug` →
     `precis.identity.make_cite_key` (slug callers switch to
     cite_key per ADR 0008)
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
     `corpus/<letter>/<cite_key>.pdf`; on failure to
     `errors/<timestamp>/<filename>` with a `.error.txt` next to it
   - tests adapted from `acatome_extract.tests.test_watch`
7. **B6 — `precis worker` skeleton (derived queue, ADR 0007)** ✅
   - **DONE**: `src/precis/workers/` package — `base.py` (ABC +
     `ChunkRow` + `ArtifactStatus`), `embed.py` (EmbedHandler),
     `summarize.py` (pure-Python RAKE + `RakeLemmaHandler`),
     `runner.py` (`run_handler_once` + `run_loop`); CLI in
     `src/precis/cli/worker.py` wired into the dispatcher
   - per-artifact claim queries:
     `LEFT JOIN chunk_embeddings WHERE chunk_id IS NULL` etc.
     with `FOR UPDATE OF chunks SKIP LOCKED`
   - handlers for `embed:bge-m3` and `summarize:rake-lemma`;
     failure path writes `status='failed'` row so the chunk is not
     re-picked
   - `--status` flag aggregates over output tables and prints
     `(total | ok | failed | pending)` per artifact (TSV)
   - tests use a real Postgres in the precis-dev container
     (psycopg pool against test DB), no in-memory stub — 60
     tests in `tests/workers/` + `tests/test_worker_cli.py`
   - **Deferred**: scispacy lemmatizer for `rake-lemma` —
     skeleton runs lowercased surface forms; see `summarize.py`
     module docstring for the wiring follow-up
8. **B7 — drop legacy ingest path** ✅
   - **DONE**: deleted `src/precis/store/_ingest_ops.py` (bundle
     ingest mixin); `Store` now composes six mixins.
   - **DONE**: deleted `src/precis/ingest/_legacy.py` (bundle
     parser + `ParsedBundle` + `IngestResult` + slug minting);
     `precis.ingest` package now re-exports the v2 `IngestResult`
     from `add.py` plus `ParsedBlock` / `classify_density` /
     `fill_embeddings` from the new `precis/ingest/blocks.py`.
   - **DONE**: dropped `ingest-bundle` / `ingest-bundles`
     subcommands from `src/precis/cli/ingest.py` + the dispatch
     entries in `src/precis/cli/main.py`.
   - **DONE**: relocated the three reusable helpers patent ingest
     still needs into `src/precis/ingest/blocks.py`; updated
     `_patent_ingest.py` + `patent_fulltext_sweep.py` imports.
   - **DONE**: paper handler docstring rewritten — `Store.ingest_bundle`
     reference replaced with `precis_add` / `precis add` / `precis
     watch`.
   - **DONE**: tests rewritten — `tests/test_ingest.py` deleted
     (bundle-parsing + end-to-end `Store.ingest_bundle` tests);
     replaced with `tests/test_ingest_blocks.py` (14 tests
     covering `classify_density` + `fill_embeddings`). Bundle CLI
     tests removed from `tests/test_cli.py`.
   - v2-scope test sweep (workers + watch + ingest + identity +
     initial-migration + ingest-blocks): **552 passed**.
9. **B8 — `pyproject.toml` cleanup** ✅
   - **DONE**: dropped `acatome-extract[embeddings]>=0.1` from
     the `[paper]` extra. The full PDF-→-paper pipeline now
     lives in :mod:`precis.ingest` (vendored in B3/B4); the
     extra carries the direct deps the vendored code needs.
   - **DONE**: `[paper]` extra now lists: `marker-pdf>=1.0`
     (gated `sys_platform != 'win32'`), `pymupdf>=1.24`,
     `rapidfuzz>=3.0`, `habanero>=2.0`, `semanticscholar>=0.8`,
     `tenacity>=8.0`, `sentence-transformers>=3.0`. `httpx` was
     not promoted because it's only used by the Wolfram handler
     (already in `[external]`).
   - **DONE**: promoted `watchdog>=4.0` to a top-level dep so
     `precis watch` keeps resolving on a bare install — it used
     to come in transitively via `acatome-extract[embeddings]`.
   - **DONE**: bumped version `6.0.0` → **`7.0.0`** (SemVer
     major; the v2 storage rewrite + schema cleanup are
     breaking).
   - **DONE**: added a `## v7.0.0 — pip-merge & v2 storage
     rewrite (2026-05-22)` entry to `CHANGELOG.md` with
     migration notes (PyPI install, re-ingest plan, worker
     deployment, watchdog promotion) and an internals summary.
   - **DONE**: lazified the `precis.ingest.pipeline` imports
     inside `precis.ingest.add._build_paper` so `precis serve`
     / `precis migrate` / `precis worker` keep loading on a
     bare install without `[paper]`. Updated
     `tests/ingest/test_add.py` to patch at the canonical
     pipeline module rather than the deferred-import name.
   - **DONE**: ran `uv lock` — removed 13 transitive packages
     that came in via the old `acatome-extract` chain
     (`acatome-meta`, `aiohttp`, `litellm`, `openai`,
     `precis-summary`, `tiktoken`, etc.). Smoke-tested:
     `import precis.cli.watch`, `import precis.cli.add`,
     `import precis.cli.main`, `import precis.workers` all
     succeed without paper deps loaded.
   - v2-scope test sweep: **552 passed** (same as B7
     baseline).
10. **B9 — `infrastructure/compose.yaml` update** ✅
    - **DONE**: renamed `acatome-watch` service to `precis-watch`
      and switched its build to
      `../projects/inbox_code/precis-mcp/docker/Dockerfile`
      (target=runtime), the same image used by `precis-cli`.
      Image tag: `precis-mcp:latest`.
    - **DONE**: command is now
      `["precis", "watch", "/inbox", "--corpus-dir",
       "/data/corpus", "--polling"]` — `--polling` is the safe
      default in the OrbStack/Docker bind-mount environment
      (inotify isn't reliable across the boundary).
    - **DONE**: renamed the `acatome-cache` volume to
      `precis-cache` and remounted it at
      `/home/precis/.cache` (was `/home/acatome/.cache`) to
      match the non-root user the `precis-mcp` image creates.
    - **DONE**: dropped the `OPENAI_API_KEY` env (no longer
      consumed; LLM summaries belong to the future worker
      handlers, not the watch loop) and the
      `ACATOME_OUTPUT_DIR` env (replaced by `--corpus-dir`
      flag).
    - **DONE**: healthcheck switched from
      `pgrep -f acatome-extract` to `pgrep -f "precis watch"`.
    - **DONE**: updated `infrastructure/README.md` — service
      table, quick-start steps 2 + 4, directory tree, data-flow
      diagram, secrets table (dropped OPENAI), troubleshooting
      log command. Added a v7.0.0 migration paragraph at the
      top.
    - **Out of scope** (deferred to B11 / archival): the
      legacy `infrastructure/acatome-extract/Dockerfile` and
      `docker-entrypoint.sh` are kept in-tree but no longer
      referenced by compose.yaml. They'll be removed when the
      acatome-extract repo itself is archived.
    - **Verification**:
      `docker compose -f infrastructure/compose.yaml config`
      validates cleanly; `precis-watch` resolves to image
      `precis-mcp:latest` with the expected build context,
      command, volumes, and `precis-cache` volume.
11. **B10 — TOON output module** (per ADR 0002) ✅
    - **Plan artefact**: `docs/design/b10-toon-output.md` —
      describes the dump/load contract, escape rules, the
      `SERIALIZERS` registry, the CLI `--format` precedence, and
      the demonstrable consumer (`precis worker --status`).
    - **DONE**: `src/precis/format/{__init__,toon,table,_json}.py`
      shipped. `toon.dump` / `toon.load` implement the flat
      homogeneous-rows TOON shape with RFC 4180-style quoting,
      schema-pinned column order, and tab-by-default delimiter.
      Pure Python; no new runtime dep.
    - **DONE**: `SERIALIZERS` registry with `serialize()` and
      `register()` exposed at the package level. Default format
      `"toon"` matches the pipe-default the CLI picks via
      `resolve_format`.
    - **DONE**: `precis.cli._common.add_format_argument()` and
      `resolve_format()` ship the standard `--format` flag with
      precedence `flag > isatty()→"table" > pipe→"toon"`.
    - **DONE**: `precis worker --status` is the demonstrable
      consumer — emits TOON when piped, an ASCII box-drawing
      table when interactive, or JSON via `--format json`. The
      status row schema (`handler/total/ok/failed/pending`) is
      pinned in one place so all three renderers agree.
    - **DONE**: `src/precis/data/skills/precis-toon.md` — agent-
      facing skill explaining the TOON parsing convention with a
      `csv.DictReader` fallback snippet.
    - **DONE**: 104 unit tests (`tests/format/`) plus an updated
      `tests/test_worker_cli.py` (TOON header / table glyph /
      JSON roundtrip) — all green. ruff + mypy clean. No
      regressions on the broader v2-scope sweep (pre-existing
      failures from the shared-tool-registry refactor and the
      Python runtrace exec gate are unchanged).
    - **Out of scope** (deferred — `OPEN-ITEMS.md` `B10-followup`):
      refactoring every handler `_render_list*` / search-renderer
      to flow through the registry. Today they emit preformatted
      text. The library is ready for the migration; that change
      can land per-handler without further ADR work.
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
- [x] CHANGELOG.md has a v7.0.0 entry summarising the merge.
