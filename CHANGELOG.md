# Changelog

The v6.0 line is the ground-up redesign that started as the `v2`
branch and merged into `main` 2026-04-30. The last v1 release on
PyPI is `5.2.6`; everything below `## v6.0.0` represents the
post-merge state. Phases pre-merge are kept here as historical
context — see also `docs/phase*-plan.md` and `docs/v2-cutover.md`.

## v8.1.0 — finding-chase + OA fetcher cascade + event log (2026-06-01)

### Added

- **New ref kind: `finding`** — chain head over a citation chase to
  a primary source. A finding carries the claim (`finding_body`
  chunk), the setup envelope (`refs.meta.scope` JSONB), and the
  ordered hops of the chase chain (`refs.meta.chain`). The chase
  worker walks the chain one hop per pass, terminating at a
  primary measurement; the chain-snapshot pass populates
  `meta.primary_cite_key` + `meta.via_cite_keys` and re-emits the
  `card_combined` chunk via DELETE+INSERT so search picks up the
  established phrasing.
  - Determinism: `make_finding_paper_id(body, scope, cited_in)` →
    `make_pub_id` produces a stable 6-char `pub_id` so two agents
    creating the same finding collapse at the
    `ref_identifiers (id_kind='pub_id')` UNIQUE constraint.
  - **Setup-context awareness**: same number under different
    setups → distinct findings. `meta.scope` is the structured
    slice for filtering; the skill mandates `search` before `put`
    keyed on `(claim, scope)`.
  - **Not externally citable**: `cite(kind='finding', ...)` raises
    `Unsupported`. The placeholder → primary substitution via
    `precis resolve` is the only path findings reach published text.
  - Migration `0004_finding_and_queue_family.sql` seeds the kind,
    two `chunk_kinds` (`finding_body`, `finding_context` — the
    latter dormant under Path B; setup is folded into body),
    two relations (`misattributes`/`misattributed-by`), and one
    actor (`chase`). Design: `docs/design/finding-chase.md`.
- **`precis.workers.chase.run_finding_chase_pass`** — sibling
  worker per ADR 0018 that advances `STATUS:tracing` findings
  one hop per pass. Walks the source paper's `chunks` + S2
  references list to detect inline cites; resolves each to a
  ref_id (existing via `ref_identifiers`, or new chase-minted
  stub when the cited paper isn't in the corpus yet). Cycle
  protection via `meta.chain` membership. Per-pass outcome
  (`advanced` / `terminated` / `dead_chain` / `multi_candidate` /
  `cycle` / `waiting`) writes to the new `ref_events` table
  (`source='chase'`). Wired into `precis worker --only chase`.
- **LLM hooks via `precis.utils.claude_p`** — `claude -p`
  subprocess wrapper (project-wide reusable utility). The chase
  uses three default-off hooks gated by `--with-llm` or env
  `PRECIS_CHASE_LLM=1`: `_disambiguate_candidates` (multi-cite
  picker), `_locate_chunk_in_target` (ANN top-1 confirmation),
  `_verify_support_with_caveats` (does this chunk support the
  claim under the setup; captures caveats + cited-others).
  Deterministic chase is fully functional without LLM; hooks
  improve quality where they fire.
- **ADR 0017 — Derived-queue family** — formalises the
  `*_artifacts` substrate + `artifact_kinds` registry as a
  cross-cutting pattern. Migration `0004_*.sql` creates
  `ref_artifacts` + `artifact_kinds`. §4 (WorkerHandler refactor)
  is superseded by ADR 0018's sibling-worker decision; substrate
  tables remain valid.
- **Generic event log: `ref_events` table** (migration
  `0009_ref_events.sql`) — cross-subsystem chronological audit
  trail. One row per event with `(ref_id, ts, source, event,
  payload, duration_ms, cost_usd)`. New `EventsMixin` on
  `Store` exposes `append_event` / `events_for` / `recent_events`.
  Consumers writing today: `chase`, `fetcher:unpaywall`,
  `fetcher:arxiv`, `fetcher:s2`. Read via
  `get(kind='finding'|'paper', view='log')` or directly.
- **`view='log'` on `FindingHandler` and `PaperHandler`** — renders
  the per-ref chronology with per-subsystem one-line summaries.
  FindingHandler scopes to `source='chase'`; PaperHandler is
  cross-source.
- **`precis.workers.fetch_oa.run_oa_fetch_pass`** — sibling
  worker that fetches OA PDFs for stub papers in a cascade:
  **Unpaywall → arXiv → S2.openAccessPdf**. First `fetch_ok`
  wins; intermediate failures (`no_oa_version` / `fetch_failed`)
  fall through to the next provider, each writing its own audit
  event. Downloads validate the PDF magic bytes before keeping
  the file (publisher HTML interstitials are rejected); polite
  User-Agent with email tag. Cost cap via per-pass `--limit`;
  24-hour retry window per stub. Wired into `precis worker
  --only fetch` (default `precis worker` runs alongside embed +
  summarize + segments + chase). Requires `PRECIS_UNPAYWALL_EMAIL`
  for the Unpaywall leg; arXiv and S2 always available.
- **`precis_add` stub-upgrade + multi-hash alias path** — new
  helper `register_aliases_and_maybe_upgrade` in `db_writer.py`.
  On `probe_existing` hit: always register the new
  `(pdf_sha256, content_hash)` rows in `ref_identifiers`
  (multiple hashes per ref are first-class — preprint vs
  publisher vs repository). When the existing ref is a stub
  (`pdf_sha256 IS NULL`), additionally promote it: UPDATE
  `refs.pdf_sha256`, insert the extracted chunks, embed via
  derived queue. Findings waiting on the stub resume on the
  next chase pass without any extra plumbing.
- **`precis resolve` CLI subcommand** — substitutes
  `[<pub_id>]` placeholders with the primary `cite_key` once
  findings establish. Plain / markdown / latex output;
  `--strict` exits 3 if any placeholder is still in flight
  (CI-gate friendly); `--keep-id` keeps dead-chain placeholders
  annotated. LaTeX `--bib` writes stub `@misc{...}` entries
  so documents compile during the in-flight period. Visible
  ⏳ marker (ASCII `*` via `--ascii`) so authors don't ship
  placeholders by accident.
- **`precis stubs` CLI subcommand** — lists paper refs with
  `pdf_sha256 IS NULL` joined with the latest fetcher event per
  ref. TOON-table output; `--awaiting` filters to "what would
  the fetcher try next".
- **New skill: `precis-finding-help`** — full workflow shape,
  when-to-create / when-NOT-to-create, mandatory
  search-before-create rule, FAQ on chase outcomes.

### Changed

- **`paper.py` empty-search DOI hint** — when a DOI lookup
  misses, the response now offers `put(kind='finding',
  cited_in='doi:...')` plus `precis stubs --awaiting` as the
  preferred structured path. The legacy
  `edit(kind='plaintext', id='./request_doi.md', ...)` queue
  is retained as a deprecated fallback (one release of
  warnings; remove in the next).
- **`precis worker --only`** gains `chase` and `fetch` choices
  alongside the existing `embed` / `summarize` / `segments`.
  Plus `--with-llm` (chase) and `--fetch-inbox` / `--unpaywall-email`
  (fetcher).
- **`PaperHandler.accepted_views`** gains `log`; renders via the
  shared `precis.handlers._event_log_render` module.

### Migrations

- `0004_finding_and_queue_family.sql` — finding kind + queue
  family scaffolding (ADR 0017).
- `0009_ref_events.sql` — cross-subsystem per-ref event log.

Both are additive (no ALTER on existing tables); apply cleanly
to a fresh DB and to a live v8.0.0 DB.

### Deprecated

- **`./request_doi.md` plaintext DOI queue** — superseded by
  the finding-chase + fetcher pipeline. Still works; the
  empty-search hint now labels the option `(legacy)`. Remove
  in the release after next.

## v8.0.0 — 2026-05-31

### Added

- **Discovery layer for `paper` kind** — persistent per-segment
  artifacts now back `view='toc'` and the search-result excerpt
  sub-lines. Two new tables (`ref_segments`,
  `ref_segment_sentences`), a ref-level worker
  (`precis worker --only segments`), and read-side store mixin
  (`SegmentsMixin`). Pipeline:
  - **DP-uniform-cost segmentation** (replaces TextTiling) on
    bge-m3 chunk embeddings with K = `ceil(body/20)` clamped
    `[3, 9]`.
  - **Matryoshka-ordered keywords** per segment — scored via
    KeyBERT-style cosine to segment centroid with a
    distinctiveness penalty against sibling centroids (λ ≈ 0.3),
    so `keywords[0]` is what's most-distinctive about this
    segment rather than most-frequent. Stored as JSONB
    `{long, short, aliases[], score}` with a denormalized
    GIN-indexed `forms TEXT[]` for cross-paper surface-form
    lookups.
  - **Per-sentence bge-m3 embeddings** — every body sentence
    gets an embedding + centroid score. TOC excerpts pick top-1
    by centroid score; search-result excerpts rerank against the
    query embedding via pgvector `<=>`.
  - Migration `0005_segments_and_sentences.sql` (requires
    `btree_gist` for the mixed-type segment-range GiST index).
  - Documented in ADR 0018; schema diagram refreshed in
    `docs/design/schema-v2.puml`.
- **New kind: `citation`** — verifier-workflow scaffold for
  writing-thread agents. Write-once `put(text=<claim>,
  source_handle, source_quote, char_offset, verifier_confidence,
  verifier_caveats, link='paper:<slug>', rel='cites')` persists
  a verified claim → source-quote pointer to `refs.meta`.
  Migration `0007_citation_kind.sql` seeds the kind; the `cites`
  relation was already in the vocab. See
  [`precis-citation-help`](src/precis/data/skills/precis-citation-help.md).
- **`chunks.numerics TEXT[]` lexical numeric-token index** —
  ingest extracts every `<number><unit>` token (eV/V/A/Hz/cm⁻¹/
  %/K/°C/Pa/M/nm/cycles/s/…) and stores them GIN-indexed for
  cheap exact-value lookups (path-2 from the tables-curveball
  discussion; structured `paper_facts` remains deferred).
  Migration `0006_chunk_numerics.sql`.
- **References detection at ingest** — `pipeline._retag_references`
  runs the boilerplate classifier on body chunks and rewrites
  detected bibliography rows to `chunk_kind='references'` before
  insert. `EmbedHandler` and `RakeLemmaHandler` carry
  `skip_chunk_kinds=("references",)` which extends the claim SQL
  with `AND c.chunk_kind <> ALL(%s)` — references never enter
  the work queue, bibliography stops polluting search.
- **pysbd-backed sentence splitter** — `precis.utils.sentences`
  wraps pysbd 0.3.4 with char-offset bookkeeping. Wired into
  `text_chunker` via a `SENTENCE_SEPARATOR` sentinel in the
  recursive splitter's fallback chain, so abbreviations like
  `"et al."`, `"Fig."`, `"i.e."`, `"e.g."`, `"vs."` no longer
  cause mid-clause splits. `CHUNKER_VERSION = "2.0+pysbd-0.3-1"`
  is now a real constant in `text_chunker.py`. Adds
  `pysbd>=0.3.4` to the `[paper]` extra.
- **Dehyphenation in the cleaner** — `marker._clean_text` gains
  a regex pass joining `-\s*\n\s*` when both sides are lowercase
  ASCII. Semantically-significant hyphens (`Z-scheme`, `Cu-MOF`,
  any compound with uppercase boundaries) are preserved, and the
  join never crosses paragraph breaks.
- **Retraction banner on paper views** — `kind='paper'` `view=
  'overview'`, `view='toc'`, and chunk drill-in views now lead
  with a `> [!] RETRACTED` (or EoC / corrected) banner when
  `refs.retraction_status` is set, with the retraction date and
  reason inline and a pointer at
  `get(kind='provenance', id='<doi>')` for the full notice.

- **New kind: `provenance`** — retraction and amendment monitoring
  for paper DOIs. Five phases, all shipped:
  - **Phase 1** — single-DOI Crossref check. Validates DOI shape,
    fetches `/works/{doi}`, classifies any `update-to` notices by
    severity (`retraction` → 🔴 blocker, `expression_of_concern` →
    🟠 review, `corrigendum`/`erratum` → 🟡 note,
    `addendum`/`clarification` → 🟢 info), and when the parent
    paper is in the local store: auto-ingests retraction / EoC
    notice DOIs as `paper` refs (slug rule:
    `<parent>-r<n>` / `-e<n>` / `-c<n>`), writes `retracted-by` /
    `concern-raised-by` / `corrected-by` links, sets
    `refs.retraction_status`, applies a `STATUS:retracted` /
    `:concern` / `:corrected` tag. Migration `0002_provenance.sql`
    adds the six new relation slugs and the `retraction_watch`
    provider. Notice refs carry `STATUS:notice`.
  - **Phase 2** — batch input via `q='doi1,doi2,…'`, `view='blockers'`
    (only 🔴/🟠 entries with a count of hidden 🟡/🟢), `view='json'`
    (structured payload for downstream tooling),
    `ThreadPoolExecutor(max_workers=8)` for the fan-out, order
    preservation, per-DOI failure isolation
    (`status='check_failed'` on transport errors never kills the
    batch). New CLI: `precis jobs check-provenance --refs <file>
    --view default|blockers|json --out <file>`.
  - **Phase 2.5** — `view='verify'` metadata verification. Catches
    "right DOI, wrong paper" — common with LLM-generated bibs.
    Token-set Jaccard on titles with NFKD normalisation
    (`Müller`→`muller`, `H₂O`→`h2o`, `ﬁ`→`fi`) plus German-phonetic
    alt for surnames (`Müller`↔`Mueller`, `Schröder`↔`Schroeder`)
    plus reverse-phonetic fold for the ASCII↔ASCII case
    (`Mueller`↔`Muller`) — trade-off: false positives on
    `Sue`↔`Su`, `Press`↔`Pres` accepted because the cost (a
    suppressed warning) is bounded. Year ±1 tolerance for
    online-first vs print drift. No hardcoded pass/fail
    thresholds; raw scores emitted in JSON for downstream rules.
  - **Phase 3** — Retraction Watch reason codes joined into the
    report (`+Falsification/Fabrication of Data` etc., not just
    "retracted"). Migration `0003_provenance_rw_cache.sql` adds
    the cache + sync ledger tables. New job
    `precis jobs sync-retraction-watch --mailto <email>
    [--source auto|labs|gitlab]` — tries Crossref Labs API
    primary, falls back to the GitLab mirror
    (`gitlab.com/crossref/retraction-watch-data`). ~40 MB CSV,
    ~50k rows, batched upsert in 10k-row chunks, idempotent on
    RW Record ID. Match strategy: exact notice-DOI match, with
    single-row-per-nature fallback.
  - **Phase 3.5** — Numbered `#N` output across all batch views
    matching the project's standardised LLM-output convention
    (`utils/search_merge.py:208`). Every batch result carries a
    1-based `input_index` reflecting *input* order (not thread-
    pool completion order); the same `#47` appears in default,
    blockers, and JSON views even when intervening entries are
    hidden. Eliminates LLM off-by-one errors when generating
    follow-up actions against a numbered report.
  - **Phase 4** — `transitive=True` flag enables depth-1
    cite-walk via Crossref's `message.reference` field. For each
    parent: shallow-checks every cited DOI, surfaces only
    ≥ 🟠 findings as `cited_findings`, skips corrigenda
    (too noisy at depth 1). Per-batch dedup cache so a cited
    paper shared by N parents hits Crossref once. Clean-itself
    papers that cite retracted work are promoted into the
    🟠 Review bucket so blockers view doesn't hide them.
  - **Phase 5** — three additions:
    - `paper view='health'` shim — looks up the paper's DOI from
      `ref_identifiers` and delegates to `provenance` so agents
      with a slug skip the manual DOI lookup.
    - `view='exists'` shortcut — compact ✓/✗ output for "does
      this DOI resolve?" without the retraction-classification
      overhead. Useful for validating a DOI list before doing
      real work.
    - `suggest_candidates=True` — when a DOI 404s *and* a
      `BibEntry` with bibliographic metadata is supplied, calls
      Crossref `/works?query.bibliographic=…&query.author=…`
      and attaches ranked candidates as **advisory hints** under
      the Unknown DOI section. **Never substitutes** — the
      supplied DOI's status stays `unknown`. Fuzzy
      auto-resolution was explicitly rejected; see
      `docs/provenance-kind-plan.md` § "Rejected: fuzzy DOI
      auto-resolution" for the rationale.
  - Source layout: `ingest/provenance.py`, `ingest/_text_norm.py`
    (Phase 2.5 helpers), `ingest/_rw_csv.py` (Phase 3 parser),
    `jobs/provenance_rw_sync.py`, `handlers/provenance.py`,
    `handlers/_provenance_report.py`, `cli/provenance.py`. Skill
    cards at `data/skills/precis-provenance-help.md` and
    `data/skills/precis-preflight.md`. Migrations `0002` and
    `0003` in `src/precis/migrations/`. Tests at
    `tests/ingest/test_provenance{,_verify,_rw,_transitive,_phase5}.py`.
  - Design doc: `docs/provenance-kind-plan.md`.

- **Phase 6.1 — RW cache as Crossref fallback.** `check_doi` now
  always consults the local Retraction Watch cache regardless of
  Crossref's outcome. Three concrete behaviours that the previous
  Crossref-first flow missed:
  - **Publisher never deposited an `update-to` relation.** Pre-CrossMark
    retractions (notably the Hwang stem-cell paper, retracted 2006)
    return clean from Crossref but appear in RW. We now synthesise a
    `Notice` from the RW row (`update_type=""` + `rw_notice_nature`
    populated) so they surface in the report with a `(RW)` source
    label.
  - **Crossref returned 404.** Previously `status='unknown'` and no
    further work. Now: if RW has data for the same DOI we surface
    the retraction with `status='ok'`, falling back the paper title
    from the RW row.
  - **Crossref timed out / transport error.** Previously
    `status='check_failed'`. Now: if RW has data, the report assembles
    from local-only data with `status='ok'`; the error string is
    preserved on `result.error` and the renderer surfaces a
    `⚠️ Crossref unavailable` banner so the reader knows the live
    source wasn't consulted.
  - The internal `_merge_crossref_and_rw_notices` helper covers both
    enrichment (Crossref notice matched to RW row → reasons attached)
    and synthesis (RW row with no Crossref match → new Notice). Dedup
    by `notice_doi` so a paper with both sources doesn't double-count.
    Renderer updated: `(RW)` label for synthesised notices, suppresses
    the "notice DOI: \`\`" line when the DOI is empty (common for
    older RW entries).
  - No schema change; uses existing `provenance_rw_cache` columns.
    Backwards-compatible with the Phase 3 enrichment-only helper via
    `_enrich_notices_with_rw(_synthesize_rw_only=False)`.

- **Postgres advisory-lock work claims for multi-host ingest**
  (`precis.ingest.claim.Claim`). Each PDF ingest opens a dedicated
  psycopg session and calls `pg_try_advisory_lock(key)` where `key`
  is the first 64 bits of `pdf_sha256`. If acquired, the host owns
  the work; if busy, `precis_add` returns `None` and the watcher
  leaves the file in place for the owning host to handle. The
  claim auto-releases when the session closes — clean exit,
  exception, container OOM, Mac crash, network partition. No
  heartbeat, no stale-row reaper, no schema migration. Enables
  multiple Macs to share a single `/inbox` (via SMB) talking to a
  central Postgres, with no risk of two hosts running Marker on the
  same content. See ADR 0016. Tests at
  `tests/ingest/test_claim.py::TestKeyDerivation` (pure) and
  `::TestClaimIntegration` (skipped without `PRECIS_DATABASE_URL`).

- **`Store.dsn`** attribute exposes the original connection string
  so callers needing a non-pooled connection (like `Claim`) can
  open one. ``None`` when the Store was constructed from a
  pre-built pool (tests).

- **`_move_to` / `_move_to_corpus` tolerate FileNotFoundError on
  rename**, treating it as "another host already moved this file."
  Required for multi-host operation against a shared inbox where
  the same physical file can be observed by N watchers
  concurrently.

### Changed

- **`precis_add` may return `None`.** New return type
  `IngestResult | None`. `None` means another host owns the
  advisory-lock claim on this PDF's hash; the caller should not
  touch the file. Existing single-host callers (one watcher,
  no other hosts) never see `None` in practice.

### Removed

- **Filesystem-based crash locks** (`.processing/*.lock`,
  `_acquire_lock`, `_release_lock`, `_lock_path_for`,
  `_recover_crashed`). Superseded by the Postgres advisory-lock
  claim above. The `--lock-dir` flag on the hidden
  `_watch_batch_ingest` subcommand is also gone. Leftover
  `.processing/` directories on disk from prior deployments are
  harmless and can be deleted manually.

### Added (continued)

- **Subprocess-per-batch backfill** (`precis watch
  --subprocess-batch-size N`). When positive, startup backfill
  spawns ``precis _watch_batch_ingest`` subprocesses of N PDFs each
  and waits for each before starting the next. Marker / surya /
  transformers memory leaks accumulate inside the long-running
  watcher process across consecutive ingests; subprocess isolation
  reclaims them at child exit. Default 0 (in-process — legacy
  behaviour); production `precis-watch` compose service now ships
  with `--subprocess-batch-size 50`. Cost: 1 Marker reload (~15 s)
  per batch. Benefit: bounded per-batch RSS rather than monotonic
  growth into OOM. Live (watchdog) events keep the in-process
  path since they're rate-limited by arrival. See ADR 0015.

- **In-process Marker cache cleanup**
  (`precis.ingest.marker._release_marker_caches`). Called at the
  end of every `extract_blocks_marker` to run `gc.collect()` and
  (when available) `torch.cuda.empty_cache()` /
  `torch.mps.empty_cache()`. All branches import-and-feature
  guarded; safe on CPU-only deployments without torch. ~10 ms/PDF
  overhead. Layered on top of subprocess isolation as a no-regret
  probe — may help with ref-cycle leaks but isn't a substitute for
  Fix B's structural isolation. See ADR 0015.

- **`python -m precis` entry point** (`src/precis/__main__.py`).
  Mirrors the `precis` console script. Used by the subprocess
  spawner above to avoid depending on `$PATH` resolution for the
  child invocation.

- **XMP write + signed-PDF detection in ``pdf_writer``.** The
  metadata write-back path now emits a minimal RDF/XMP packet
  carrying ``dc:title``, ``dc:creator`` (authors), ``dc:identifier``
  (DOI prefixed with ``doi:``), ``prism:doi`` (raw DOI), and
  ``prism:url`` (arXiv URL) alongside the standard Info-dict write.
  Exiftool's ``-Identifier`` flag now reads our DOI from the
  canonical XMP slot rather than just the Keywords fallback.
  Cryptographically-signed PDFs (``Signature`` widget present)
  return ``PatchOutcome(skipped_reason="signed")`` without touching
  the file — incremental save preserves signatures *usually*, not
  *always*. The check is bounded to ``doc.is_form_pdf`` so unsigned
  PDFs pay zero cost. AcroForms with only text widgets still patch
  normally. Closes the two follow-ups noted in the initial
  ADR 0014 cut. Tests at
  ``tests/ingest/test_pdf_writer.py::TestXmpWrite`` and
  ``::TestSignedPdfSkip``.

### Changed

- **`precis-watch` memory cap raised from 12 GiB to 64 GiB.** Was
  a backstop for single-PDF OOMs; now also absorbs cumulative
  Marker memory leakage across long backfills until the
  subprocess-per-batch isolation lands fully. The 16 GiB host-total
  budget in the older comment is now stale; compose deploys
  assume the host has enough headroom for this cap. Comment
  updated. See ADR 0015.

- **`precis-watch` backfill processes smallest PDFs first.** Sort
  key in ``_PdfHandler.backfill`` changed from path name to file
  size. Means a single giant PDF that OOMs the container only
  blocks itself; the small files behind it have already been
  ingested. Stat errors sort last so a broken symlink doesn't
  abort the whole backfill. Test
  ``tests/test_watch.py::TestBackfillOrder``.

## v7.1.0 — baked-in models, fast-path ingest, MCP cold-start budget (2026-05-28)

First v7.x line release on PyPI. Highlights: `precis-mcp:latest` ships
with bge-m3 + Marker weights baked in (no first-ingest download); a
sha256-keyed fast-path skips Marker on re-ingest of a known PDF; the
MCP cold-start banner / `tools/list` shrinks under a pinned token
budget with three operator env vars (`PRECIS_STARTUP_SKILLS`,
`PRECIS_KINDS_DISABLED`, `PRECIS_DEFAULT_TAGS`); new `precis-worker`
service drains the embedding/summary queue continuously; new `tex`
file kind; `PRECIS_ROOT` consolidates the prose-file env vars;
`search(exclude=[...])` enables ref-level pagination; nightly
`precis maintenance run` driver lands.

### Added

- **PDF metadata write-back during ingest.** New
  ``precis.ingest.pdf_writer`` module patches the resolved canonical
  Title / Author / DOI into each successfully-ingested PDF's Info
  dict (Title, Author, Subject, Keywords). Uses pymupdf's incremental
  save so the existing content stream stays byte-identical; only an
  update section is appended. Both the pre-patch and post-patch
  ``pdf_sha256`` land in ``ref_identifiers`` as separate alias rows
  pointing at the same ``ref_id``, so re-ingesting either byte
  sequence still hits the fast-path probe. ``PaperToWrite`` grows a
  ``pdf_sha256_aliases: list[str]`` field; ``write_paper()`` inserts
  one extra ``ref_identifiers`` row per alias. Default ON; operator
  off-switch ``PRECIS_PATCH_PDFS=0`` / ``false`` / ``no`` / ``off`` /
  empty. Skip cases: ``encrypted`` (DRM), ``noop`` (fields already
  match — keeps re-ingest of an already-patched PDF from drifting),
  ``disabled``, ``error`` (any exception during open/save logs
  WARNING and skips). Reverses the B4b removal of
  ``write_pdf_metadata()`` from ``acatome_extract``; safe now because
  v2's multi-row ``ref_identifiers`` model absorbs the hash drift
  that motivated the original removal. New tests at
  ``tests/ingest/test_pdf_writer.py`` (10 cases). See ADR 0014.

- **Wrapper scripts in ``scripts/``** for the day-to-day Docker
  workflow, all honouring ``PRECIS_COMPOSE`` if the infra repo
  lives outside ``~/work/infrastructure``:
  - ``scripts/precis-shell`` — standalone dev shell (no compose
    dep); auto-builds ``precis-mcp:dev`` on first use, mirrors the
    compose bind-mounts but degrades gracefully on missing host
    paths. ``--rebuild`` forces a fresh build.
  - ``scripts/precis-add <pdf | --doi | --arxiv>`` — one-shot ingest
    via ``precis-cli``. Auto-mounts any positional file argument at
    ``/inbox/<basename>``.
  - ``scripts/precis-watch [start|stop|restart|status|logs|fg|tail]``
    — manage the PDF ingestion daemon. Default ``tail`` brings the
    watcher up detached and follows logs.
  - ``scripts/precis-index [path] [--force] [--kinds md,plaintext,tex]``
    — one-shot ``precis jobs ingest`` to pre-warm prose files
    under ``PRECIS_ROOT``.
  - ``scripts/precis-embed`` and ``scripts/precis-summarize`` —
    symmetric wrappers around the worker queue. Subcommands
    ``once`` / ``status`` / ``start`` / ``stop`` / ``logs``. Both
    daemons share the ``precis-worker`` container (one daemon
    handles both handlers; running two would double-claim chunks).

### Fixed

- **``exiftool`` no longer warns "not found in PATH" in
  ``precis-watch`` logs.** Runtime image now apt-installs
  ``libimage-exiftool-perl`` in ``docker/Dockerfile`` stage 2.
  The warning surfaced once per ingest at
  ``src/precis/ingest/pdf_metadata.py:162`` and was non-fatal
  (extraction falls back to ``{}``), but it noised up the daemon
  log and reduced embedded-metadata recall on papers whose only
  DOI signal is the publisher-set ``-Identifier`` XMP field.

- **`BgeM3Embedder.model` now returns the precis registry key
  (`bge-m3`) rather than the HuggingFace id (`BAAI/bge-m3`).**
  The previous behaviour caused a ``ForeignKeyViolation`` on
  every ``chunk_embeddings`` insert because the column FKs
  against ``embedders.name`` (seeded as ``bge-m3``), and the
  worker wrote ``embedder.model`` straight into the column.
  HF id is now an internal constant (``_BGE_M3_HF_ID``) used
  only when ``SentenceTransformer`` actually loads weights;
  the registry key (``_BGE_M3_REGISTRY_KEY``) is what flows
  through ``EmbedHandler.write_ok``. Test
  ``tests/test_embedder.py::test_bge_m3_construction_is_lazy``
  updated to assert the registry-key contract.

- **`Store.embedding_dim()` no longer reads from the absent
  `system` table.** The method now sources the dim from
  ``embedders.dim WHERE is_default = TRUE`` — the migration
  already seeds that row (``bge-m3, 1024``). Search via
  ``precis tools search`` and ``precis serve`` were both
  failing on every fresh DB with ``UndefinedTable: relation
  "system" does not exist``. The companion ``get_setting`` /
  ``set_setting`` methods are unchanged and remain effectively
  dead until the ``system`` table is added by a follow-up
  migration; ``oracle_sync.py`` already swallows their
  ``AttributeError`` / ``Exception`` and re-ingests on miss,
  so its observable behaviour is unchanged.

### Changed

- **New `precis-worker` compose service** in
  ``infrastructure/compose.yaml``: continuously drains the
  derived-artifact queue (``chunk_embeddings`` +
  ``chunk_summaries``). Same image as ``precis-watch``, no
  ``/inbox`` or ``/data/corpus`` mounts, command
  ``precis worker --batch-size 32 --idle-seconds 5``. Bring
  up with ``docker compose up -d precis-worker``. The watcher
  writes chunks; the worker turns them into vectors +
  summaries. See ADR 0007.

- **`precis-mcp:latest` ships with model weights baked in.** A new
  ``models`` stage between ``builder`` and ``runtime`` in
  ``docker/Dockerfile`` runs ``marker.models.create_model_dict()``
  and ``SentenceTransformer('BAAI/bge-m3')`` at build time and
  COPYs the resulting caches into the runtime image at
  ``/opt/precis/models/`` (``HF_HOME=/opt/precis/models/hf``,
  ``MODEL_CACHE_DIR=/opt/precis/models/datalab/models``). First
  ``precis add`` / ``precis watch`` start no longer downloads
  ~3 GB from HuggingFace + datalab. Image grows from ~6.4 GB to
  ~10 GB; RAM behaviour unchanged (lazy mmap on first use).
  The GoNoto font ``marker`` writes to ``site-packages/static/
  fonts/`` is pre-populated in the ``builder`` stage so the
  read-only venv at runtime never trips on it. See
  ``docs/design/bake-models-into-image.md``.
- **`infrastructure/compose.yaml`: `precis-watch` no longer mounts
  `precis-cache:/home/precis/.cache`.** Models live in the image
  now; the named volume is retired. On hosts where it already
  exists, free it with
  ``docker volume rm precis-infra_precis-cache``.

### Fixed

- **Ingest fast-path: re-ingesting a known PDF no longer re-runs
  Marker.** :func:`precis.ingest.add.precis_add` now probes
  ``ref_identifiers (id_kind='pdf_sha256')`` for ``PdfInput``
  *before* invoking the extraction pipeline. A hit short-circuits
  to ``IngestResult(inserted=False, ref_id=...)`` without paying
  for the ~30–60 s Marker run. The slow-path probe (paper_id /
  DOI / arXiv / content_hash) still runs after extraction to
  catch "same paper, different bytes" collisions. No behavioural
  change for fresh ingests. Regression test:
  ``tests/ingest/test_add.py::TestPrecisAddIdempotent::test_fast_path_skips_marker_when_pdf_sha256_known``.
  See ``docs/design/extract-once.md``.


### MCP session ergonomics — cold-start token budget + kind enablement + default tags (2026-05-26)

Six-phase rollout (`docs/design/mcp-cold-start-token-budget.md`)
that shrinks the unconditional cold-start cost on a fresh MCP
session and adds three operator-controlled env vars for session
context. Every byte saved on the cold-start banner / `tools/list`
is a byte the agent gets to spend on the user's actual request.

- **Per-verb docstrings trimmed** (Phase 1). Each of the seven
  verbs (get, search, put, edit, delete, tag, link) keeps a
  ~5-line docstring that points at `precis-<verb>-help.md` for
  detail. The detail moved into ten new skill files in
  `src/precis/data/skills/` so it's still discoverable via
  `search(kind='skill', q=...)` — just not on every connecting
  agent's cold-start budget. CLI `--help` shows the same trimmed
  shape with per-arg help strings threaded through the CLI
  adapter. `precis-edit-protocol` renamed to `precis-edit-help`.
- **Cold-start banner re-framed** (Phase 2). `serverInfo.instructions`
  now leads with "First action: `search(kind='skill', q='<topic>')`"
  instead of a verb cheat sheet. Trailing `Kinds loaded:` line
  summarises the live registry so the agent sees what's actually
  wired without an exploratory call.
- **`PRECIS_STARTUP_SKILLS`** (Phase 3) — comma list of skill
  slugs to pin at session start. Cumulative body size capped at
  `PRECIS_STARTUP_SKILLS_CAP_KB` (default 50). Drop-tail truncation
  at the cap; banner notice surfaces both invalid ids and the
  truncation event. Pinned skills are tagged on the `prompts/list`
  response so MCP clients with server-pinned-prompt support can
  render them at handshake.
- **`PRECIS_KINDS_DISABLED`** (Phase 4) — comma list of kind names
  to prohibit at boot. Prohibition wins over resource availability
  (a disabled kind is hidden even when its env requirements are
  met). The patent handler's inline `EPO_OPS_KEY` / `EPO_OPS_SECRET`
  env gate moved into `PatentHandler.__init__` to converge with
  the gate machinery. Banner now carries a `Kinds unavailable:`
  line with distinct reasons (`prohibited` vs the specific
  `missing <ENV_VAR>`).
- **`PRECIS_DEFAULT_TAGS`** (Phase 5) — comma list of tags merged
  into every `put` on note-like kinds. `KindSpec.note_like: bool`
  flag (default False) opts kinds into the merge; nine handlers
  flipped to `True` (memory, todo, gripe, flashcard, quest,
  conversation, markdown, plaintext, tex); ingested / cache /
  generator kinds (paper, patent, web, youtube, math, oracle,
  skill, calc) stay False so they don't accumulate session tags.
  The `tag` verb emits a non-mutating suggestion hint listing
  defaults not yet present rather than auto-mutating — operator-
  explicit calls stay operator-explicit.
- **Regression guards** (Phase 6). `tests/test_token_budget.py`
  pins `tools/list` JSON < 12 KB (measured ~9 KB baseline),
  per-verb description < 1 KB, `serverInfo.instructions` < 2 KB
  on clean / < 4 KB with every Phase-3-5 feature engaged.
  Anchor strings (`First action`, skill-search CTA, `Kinds loaded:`)
  pinned. Cross-cutting test pins the shared comma-list parse
  semantics across `startup_skills.parse`, `kind_gate.parse_disabled`,
  and `default_tags.parse`.

New modules: `src/precis/startup_skills.py`, `src/precis/kind_gate.py`,
`src/precis/default_tags.py`. New config fields: `startup_skills`,
`startup_skills_cap_kb`, `kinds_disabled`, `default_tags`. New
hint topics: `default_tags.merged`, `default_tags.suggested`.

Test coverage: 88 new tests across `test_startup_skills.py`,
`test_kind_gate.py`, `test_default_tags.py`, `test_token_budget.py`,
plus extensions to `test_mcp_critic_regressions.py` for the new
banner shape. Suite: 1718 passed, 829 skipped, 5 xfailed.

### `find-citing-papers` — noise-reduction filters + bge-m3 rerank (2026-05-09)

The full sweep across the ~4k corpus surfaces ~900k unique citing
papers from S2 — way past human-readable. Five new aggregation flags
let you stack cheap filters into a digestible report without re-
fetching. All run in `--no-fetch` mode against the existing
`paper-ingest/.citing-papers-cache/` so iteration is seconds, not
hours.

- **`--min-co-cites N`** — drop citing papers that cite fewer than N
  of ours. Strongest single signal: 909k → 212k @ N=2; → 25k @ N=5;
  → ~3k @ N=10. The 294-of-our-papers review naturally floats to
  the top of the global sort.
- **`--min-citing-citations N`** — drop citing papers with fewer
  than N citations of their own. Filters out the 18% zero-cite
  fresh-preprint tail when you want "stuff that already got
  traction."
- **`--min-similarity X`** — bge-m3 cosine gate. Embeds the source
  corpus' title+abstract once, embeds surviving citing papers'
  title+abstract, drops anything whose max cosine across cited
  sources is below X. Typical usable threshold 0.50–0.65; ~30
  citing papers/sec on Apple Silicon. Uses the same
  `precis.embedder.make_embedder("bge-m3")` contract the rest of
  the package lives on.
- **`--top-n N`** — hard cap after sort.
- **`--per-source-top K`** — alternative aggregation: emit the top
  K most-recent / most-influential citations PER OUR paper instead
  of the global aggregate. Useful for "what's new for paper X"
  digests rather than corpus-wide topic surveys.

Sort precedence in global mode: co-citations DESC, similarity DESC
(when computed), publication date DESC, title. The
`AggregatedHit.similarity` field is surfaced both in the markdown
header (`sim 0.75` per-row meta) and in the JSONL feed
(`"similarity": 0.7501`) so downstream consumers can re-rank.

Files: `scripts/_find_citing_papers.py`, `scripts/README.md`,
`paper-ingest/README.md` (new).

### `search(... exclude=[slugs])` — ref-level pagination (2026-05-09)

New `exclude=` kwarg on the agent-facing `search` tool. Coarse / ref-
level skip-list of slugs (or DOIs, or copy-pasted hit handles like
`'wang2020~38'`) pushed down to both lex and sem CTEs in
`search_blocks_fused` so `LIMIT` runs **after** exclusion. The
canonical "I saw the top 5, give me hits 6..N" pagination idiom now
works without hacks.

- **`server.py::search`** — new `exclude: list[str] | None = None`
  kwarg, forwarded only when set (matches the `tags=` / `source=`
  conventions). Docstring teaches the shape; that's the cold-start
  discovery channel.
- **`paper.search` / `paper.search_hits`** — accept `exclude=`,
  resolve via the new `Store.fetch_ref_ids_by_slugs` bulk helper,
  forward `exclude_ref_ids=` to the store. Stale slugs are silent
  no-ops (the agent's exclude list may carry ids that no longer
  resolve; failing the whole call would be unfriendly).
- **`Store.search_blocks_fused` / `search_blocks_lexical` /
  `count_blocks_lexical`** — new `exclude_ref_ids=` kwarg. Applied
  via the shared `where_extra` clause so the predicate inlines into
  both CTEs in `search_blocks_fused` (otherwise RRF would fuse a
  filtered set against an unfiltered one — same reasoning as the
  existing tag filter). Total-count helper honours it too so the
  `N of K` header reflects the post-exclude universe.
- **`runtime._dispatch_cross_kind`** — forwards `exclude=` through
  the cross-kind fan-out via a new `_cross_kind_invoke_search_hits`
  helper that retries on `TypeError` (drops `exclude`, then `tags`)
  so handlers with smaller signatures degrade cleanly. Per-kind
  slug collisions are non-issues: `fetch_ref_ids_by_slugs` filters
  by kind, so a paper slug in the exclude list silently no-ops on
  memory et al.
- **`Next:` trailer** — multi-hit responses now render an
  `exclude=[...]` continuation pre-filled with the slugs of refs
  returned this page UNION'd with any prior `exclude=` the caller
  passed. The agent copy-pastes; no client-side bookkeeping. The
  singleton-hit branch keeps the existing `top_k=10` widen hint.
- **Docs** — new "Skip what you've seen" section in
  `precis-paper-help.md`, one-line example in `precis-overview.md`,
  shipped entry in `docs/search-future-filters.md`.
- **Tests** — 4 store-level (`test_block_search.py`: drops listed
  refs, `LIMIT` post-exclude, lex-only fallback path, count-lex
  honours exclude) and 8 handler-level (`test_paper.py`: drops
  paper, slug-with-selector accepted, DOI accepted, stale slugs
  silent, header reflects remainder, trailer pre-fills, trailer
  unions prior exclude, singleton branch keeps widen).

### `PRECIS_ROOT` consolidation — single root for prose-file kinds (2026-05-02)

`PRECIS_MARKDOWN_ROOT`, `PRECIS_PLAINTEXT_ROOT`, and the just-shipped
`PRECIS_TEX_ROOT` have been collapsed into a **single** env var,
`PRECIS_ROOT`. The three handlers walk the same tree and filter by
extension. Hard cut — no backward-compat fallback (the package is
pre-PyPI, only used internally).

- **`PrecisConfig.precis_root`** replaces `markdown_root` /
  `plaintext_root` / `tex_root`. Env: `PRECIS_ROOT`.
- **`dispatch.boot(precis_root=...)`** registers all three file
  handlers under the same root. Whole trio hidden when unset.
- **`runtime.build()`** threads `config.precis_root` through.
- `precis jobs ingest-md` falls back to `PRECIS_ROOT` instead of
  `PRECIS_MARKDOWN_ROOT`.
- **Write-access boundary documented** in `precis-files-help`:
  `PRECIS_ROOT` is the only writable area for the prose-file trio.
  Every read/write goes through `Path.resolve()` +
  `Path.relative_to(self.root)` — rejects `../` traversal **and**
  symlink escapes (since `resolve()` follows symlinks). This was
  always the behaviour; the consolidation makes the boundary
  explicit and named.
- **No absolute paths in LLM-visible output.** The LLM sees
  `PRECIS_ROOT` as `./` — it doesn't know (and shouldn't see) the
  absolute filesystem path of the configured root. Every error
  message and rendered listing in the markdown / plaintext / tex
  handlers now names the symbolic `PRECIS_ROOT`, never `self.root`.
  Affected sites: `NotFound("... not found under PRECIS_ROOT")`,
  `_render_index` header + empty-state message, `BadInput("file
  already exists: '<slug>'")` (was leaking the absolute path).
  Pinned by `tests/test_plaintext_handler.py::test_*_does_not_leak_absolute_*`
  (4 tests covering index, empty index, NotFound, put-on-existing).
- **Auto-applied `workspace` flag tag.** Every ref ingested via the
  prose-file handlers (markdown / plaintext / tex) is stamped with
  the `workspace` flag on ingest, with `set_by='system'`. Gives the
  LLM a simple filter for "things in my working directory":
  ```python
  search(q='kinetics', tags=['workspace'])   # scope to PRECIS_ROOT
  ```
  Refs outside `PRECIS_ROOT` (papers, web caches, memories, todos)
  stay un-stamped. The tag is registered in `flag_names` via
  migration `0008_workspace_flag.sql`. Idempotent:
  `ON CONFLICT DO NOTHING` means re-ingest doesn't duplicate rows.
  Pinned by 5 tests in `test_plaintext_handler.py::test_workspace_flag_*`
  (first-ingest, idempotent, survives mtime-bump, survives content
  change, `set_by='system'`).
- **New CLI: `precis jobs ingest`** — walks `PRECIS_ROOT` and
  pre-warms every prose-file kind into the store. The handlers are
  lazy by default (so the MCP handshake doesn't trip the ~7s
  bge-m3 load timeout); running this command before `precis serve`
  means the LLM finds every workspace file via `search` from the
  first query.
  ```bash
  precis jobs ingest                  # md + plaintext + tex
  precis jobs ingest --kinds tex      # scope to one kind
  precis jobs ingest --force          # re-embed even unchanged files
  precis jobs ingest && precis serve  # launcher-script prefix
  ```
  Mtime-gated (cheap re-runs), per-kind summary output. The old
  `ingest-md` command stays one release cycle as a deprecation
  shim that forwards to `ingest --kinds md`. Pinned by 8 tests in
  `tests/test_cli.py::test_ingest_*`.
- Skill docs updated: `precis-files-help`, `precis-overview`,
  `precis-edit-protocol`, `precis-plaintext-help`, `precis-tex-help`.
  README env var table simplified.
- **Migration**: rename `PRECIS_MARKDOWN_ROOT` (and / or
  `PRECIS_PLAINTEXT_ROOT` / `PRECIS_TEX_ROOT`) to `PRECIS_ROOT`.
  If you had separate trees, pick one shared parent — the kinds
  filter by extension, so they coexist cleanly.

### `tex` kind — section-aware LaTeX file handler (2026-05-02)

A new R/W file kind for `.tex` files with section-aware block
boundaries and a recursive `/toc` view that expands `\input{}` /
`\include{}` across files. Gated on `PRECIS_ROOT` (shared with
`markdown` and `plaintext`).

- **Section-aware block grammar** — `\part`, `\chapter`, `\section`,
  `\subsection`, `\subsubsection`, `\paragraph`, `\subparagraph`
  (and their starred forms with optional short titles) drive block
  boundaries alongside blank lines. The sectioning command line is
  always its own one-line block, so an agent can edit a heading
  without touching the body. Each block records `section_level` /
  `section_title` (when it's a heading) and `section_path` (its
  ancestor stack) in `meta`. Search-result rendering can show "hit
  in Methods > Kinetics" without re-parsing. Parser:
  `src/precis/utils/tex_parse.py`.
- **`/toc` view** — `get(kind='tex', id='main/toc')` walks the
  file's section blocks in source order and recursively expands
  `\input{path}` / `\include{path}`, inlining each child file's
  sections at the correct indent. Cycles (`a → b → a`) terminate
  with a `⇺` marker rather than recursing forever. `\input{}`
  targets are resolved relative to the parent file's directory and
  passed through the same `Path.resolve()` + `relative_to(self.root)`
  gate as every other read — paths that escape `PRECIS_ROOT` are
  reported as `not found`, not followed.
- **`PlaintextHandler` refactored** to make the parsing pipeline
  subclass-extensible:
    - `_KIND` / `_EXTENSIONS` / `_DEFAULT_EXT` ClassVars + the
      `spec` define kind identity.
    - `_parse_blocks(content)` / `_block_meta(block)` are
      overrideable hooks. The base implementation just delegates to
      `parse_plaintext` and stores `line_start` / `line_end`;
      `TexHandler` overrides both to inject section metadata.
    - `_SUPPORTED_VIEWS` + `_render_view(view, ref, slug=...)`
      dispatch any `view=` argument. Base supports `raw`; `TexHandler`
      adds `toc` by overriding the dispatcher.
    - `_walk_files()` now canonicalises every candidate via
      `Path.resolve()` + `relative_to(self.root)` so a symlink whose
      target lives outside the root no longer surfaces in the index
      (it would have failed at read time anyway; this just makes
      the listing match reachability).
- **Write-access boundary documented** — `precis-files-help` now
  spells out the `Path.resolve()` + `relative_to(PRECIS_ROOT)`
  contract that gates every read and write (rejects `../` traversal
  *and* symlink escapes). Pinned by new regression tests:
    - `test_symlink_inside_root_targeting_outside_is_invisible` —
      a symlink whose target escapes the root must not appear in
      the index and must `BadInput` on direct access.
    - `test_input_outside_root_silently_dropped` — same gate
      applied to `\input{../escape}` in the recursive TOC walker.
- New skill: `precis-tex-help.md`. Cross-references updated in
  `precis-files-help`, `precis-overview`, `precis-edit-protocol`,
  `precis-plaintext-help`.
- Pinned by:
    - `tests/test_tex_parse.py` — 17 tests for the parser
      (sectioning levels, ancestry stack, `\input{}` extraction,
      slug stability, line spans).
    - `tests/test_tex_handler.py` — 30 tests including ingest with
      section meta, `/toc` rendering, `\input{}` recursion, cycle
      detection, path-traversal escape attempts.
    - `tests/test_plaintext_handler.py` — 33 tests including the
      new symlink-escape regressions (apply to all prose-file
      kinds via the shared `_walk_files` / `_resolve_path`
      contract).
- **Deferred** (next refinement): macro expansion, environment
  grouping, `\subfile{}` package, `\cite{}` cross-link to `paper`
  kind.

### Cache-backed bookmarking + nightly maintenance (2026-05-02)

Closes the remaining two phases of `gripe:3681` and lands a
cron-driver to compose them into a daily-driver loop.

- **`get(kind=<cache>, ..., tags=['bookmark'])`** — one-call
  bookmark for every cache-backed kind (`web`, `youtube`, `math`,
  Perplexity tiers). Tags + untags pre-validated **before** the
  upstream fetch, so a bad axis no longer pays the API cost
  before failing. Apply on cache hit OR miss; idempotent.
  Owner: `src/precis/handlers/_cache_base.CacheBackedHandler.get`.
  Pinned by `tests/test_cache_base.py::test_get_with_tags_*`.
- **`get(kind=<cache>, ..., mode='refresh')`** — bypass cache
  freshness and re-fetch upstream **in place**, preserving
  ref id, slug, tags, and links. New
  `Store.update_cache_entry()` does an `UPDATE refs` + replace-
  blocks rather than the previous `DELETE FROM refs` cascade
  that destroyed annotations on every TTL-expired re-fetch.
  Stale-cache re-fetches now also flow through this path, so
  a `bookmark` survives expiry without `mode='refresh'` being
  explicitly requested. Per-kind `_recover_key(ref, cache)`
  hook lets a slug-only refresh call (e.g. from cron) re-derive
  the canonical fetch input from `cache.meta` — implemented for
  `web`, `youtube`, `math`, Perplexity tiers.
  Pinned by `tests/test_cache_base.py::test_mode_refresh_*` and
  `test_stale_cache_refetch_preserves_tags`.
- **`WATCH:<interval>` closed-axis tag** — closed vocabulary
  `{hourly, daily, weekly, monthly}` enforced via
  `Tag.parse_strict`. Allowed on cache-backed kinds only
  (`web`, `youtube`, `research`, `think`, `websearch`).
  `math` is intentionally excluded (Wolfram results don't
  drift). A typo (`WATCH:dialy`) fails loud at write time
  with the four valid intervals listed in `options=`.
- **`precis maintenance run`** — top-level CLI for the
  nightly cron. Three phases, each independently togglable:
  - **WATCH refresh sweep** — iterates
    `search(tags=['WATCH:<interval>'])` shortest-interval-
    first, calls `get(..., mode='refresh')` on each match
    whose `cache.fetched_at` exceeds the interval cutoff.
    Per-pass cap (`--max-refresh-per-pass=200`) bounds API
    spend; remaining work resumes on the next tick.
  - **Soft-delete purge** — hard-deletes `deleted_at`-
    tombstoned refs older than `--purge-after-days=30`
    (cascades blocks / cache_state / tags / links via the
    schema's `ON DELETE CASCADE`).
  - **`VACUUM ANALYZE`** on the hot tables (`refs`,
    `blocks`, `cache_state`, `ref_tags_*`, `ref_flags`,
    `ref_links`) so pgvector + tsvector planner stats stay
    fresh after large ingest passes.

  Suggested cron: `17 3 * * *  precis maintenance run` (one
  invocation per day; `--intervals=hourly` separately wired
  for an hourly tick if needed).
  Implementation: `src/precis/cli/maintenance.py`.

### Open backlog cleanup

- **`OPEN-ITEMS.md`** introduced as the canonical durable
  backlog. Replaces the per-issue gripe trail (gripes 3667 +
  3681 retired 2026-05-02 after the seven-verb surface
  refactor closed their original framing).
- **`docs/precis-bench/`** moved out of `openclaw-cluster` into
  its own package skeleton at `pips/packages/precis-bench/`.

## v6.0.0 — seven-verb surface, twenty-one kinds (2026-05-02)

First stable release of the v6 line. The package surface is now
**seven verbs** (`get`, `search`, `put`, `edit`, `delete`, `tag`,
`link`) discriminated by a single `kind=` argument, replacing the
v1 era's per-kind bespoke tools. Twenty-one kinds ship across ref /
tool / discovery categories; help skills are surfaced via
progressive disclosure (`get(kind='skill', id='precis-help')` for
the live registry dump, plus a `precis-<kind>-help` per kind).

Highlights since `5.2.6`:

- **Seven-verb tool surface** — see
  [`docs/seven-verb-surface-migration.md`](docs/seven-verb-surface-migration.md)
  for the design rationale.
- **New kinds**: `python` (AST navigator), `patent` (EPO OPS),
  `random` (discovery), `fc` (flashcards / spaced repetition),
  `oracle` (curated wisdom), `quest`, `conv`, `plaintext`,
  Perplexity tiers (`websearch` / `think` / `research`).
- **Anchored edits** (`mode='edit'`, `mode='insert'`) on every
  R/W file kind, with content-anchored resolution and `dry_run`.
- **Hybrid search** — lexical `tsvector` + semantic `pgvector`
  (`bge-m3`) RRF fusion at block level. Cross-kind fan-out via
  `kind='*'` or `kind='paper,memory'`.
- **In-tree handler registry** + entry-point plugin surface —
  third-party kinds register via `precis.handlers` group; one bad
  plugin cannot brick the server.
- **MCP critic-driven hardening** — multiple review passes pinned
  by regression tests; latest in
  [`docs/mcp-critic-review-2026-05-02.md`](docs/mcp-critic-review-2026-05-02.md).
- **Attribution footers** on every external-data handler
  (Wolfram, YouTube, Perplexity, web, EPO patents) — legal
  compliance, not UX polish.
- **Open backlog** moved to [`OPEN-ITEMS.md`](OPEN-ITEMS.md)
  (durable replacement for the per-issue gripe trail).

Migration from v5.x: there is none. v6 is a clean break — different
schema, different verbs, different config surface. Pin `<6` if you
need to stay on the v1 line. The README's *Install* and *Run*
sections cover the fresh-start path.

## `python` code-navigator kind (April 2026)

**New kind** `python` joins the file-handler family: a multi-root,
AST-indexed navigator over Python repos. Configured at startup via
`PRECIS_PYTHON_ROOTS=alias:/abs/path,…`; hidden when unset (same
gating pattern as `markdown`). No DB persistence — index lives
in-memory in a per-root `RepoCache` keyed by `(file, mtime_ns,
sha256)`. First `get` builds the index; subsequent calls only
reparse files whose mtime changed.

**Two-track addressing.** Line-range track `alias/path/file.py~L42-58`
is durable across edits but resolves to whatever symbol overlaps;
qualname track `alias::pkg.mod.Class.method` is content-addressable
and survives line shifts. `_parse_id()` accepts both. The repo
overview (`get(kind='python', id=alias)`) is the entry into either.

**Read views.**

- `toc` — package tree per repo, modules grouped by package depth
- `outline` — file-level outline with class hierarchy + signatures
- `source` — raw lines for a resolved region with header
- `entries` — pyproject `[project.scripts]` console scripts plus
  every `if __name__ == '__main__':` guard, each linked to its
  callable for the agent to drill into
- `callgraph` — entry-rooted static call tree built from a
  per-module call-edge index. Cycle detection (`[cycle]`),
  deduplication (`[see above]`), depth limit (`[truncated]`),
  multiplicity for repeated edges, `[ext]` for unresolved
  stdlib/third-party/dynamic. Cross-repo resolution via
  `cross_repo=True` (looks up unresolved callees in sibling
  configured roots).
- `runtrace` — *dynamic* overlay. Spawns the entry under
  `sys.setprofile` in a subprocess (gated by
  `PRECIS_PYTHON_ALLOW_EXEC=1`), forwards `argv`/`env`/`timeout`,
  reads the JSON trace back, builds a `TraceNode` tree with
  per-call timings + multiplicity, renders it in the same box-
  drawn style as the static `callgraph`, then appends a
  `Static-only` diff listing qualnames the static graph reaches
  but the runtime didn't. Useful for finding dead code,
  config-flag-gated paths, and import-time vs call-time wiring.

**Stdlib subtree collapse.** `runtrace` argparse-heavy entries blew
the response token budget (a single `--help` produced 5030 events
across `argparse.*` / `gettext.*` / `re.*` / `os.*`). The renderer
now folds anything whose top-level module is in
`sys.stdlib_module_names` into its root and annotates the row with
`(+N stdlib)` so the agent sees what was elided. Default
`max_events` lowered from 10000 → 2000. Both behaviours are
opt-out via `args={'expand_stdlib': True}` and tunable via
`args={'max_events': N}`. The `Static-only` diff is computed on the
*un-collapsed* event stream so it stays accurate regardless of
display.

**Search.** Lexical hybrid over the symbol index — qualname tokens,
docstring excerpt, signature. `scope=` accepts repo / package /
file. Each hit comes back as a canonical address you can drill
into directly. The single most token-efficient way to orient in an
unfamiliar repo: better than `grep` because it understands
intent (`q='where do we handle stale data'` works), better than
reading files cold because it returns symbols not bodies.

**Write surface.** `put` accepts `create` / `append` / `replace` /
`delete` plus the new anchored modes (`edit` / `insert`) inherited
from phase 8. Three validation gates run on every write:

1. `ast.parse` validates the result is syntactically valid Python
   (rolls back on `SyntaxError`).
2. **Qualname-drop gate** — extracts the set of qualnames defined
   in the modified region before and after; rejects any write that
   silently disappears a name unless `allow_rename=True` is passed.
   Catches accidental renames in anchored edits where the search
   region overlapped a `def` / `class` line.
3. `ruff check --fix` followed by `ruff format` — applies safe
   autofixes and reformats. Reports added/removed/modified line
   counts in the response. Skipped (with a warning header) if
   `ruff` is unavailable, never blocks the write.

After a successful write, the affected file's cache entry is
invalidated so the next `get` reindexes from disk.

**Skill.** `precis-python-help` (in `data/skills/`) documents the
addressing grammar, every view, the `args=` dict's accepted keys,
the write surface with all three gates, and a recipe section
(orienting in unknown repos, mapping stack traces, finding
dead code via the static-vs-runtime diff). The MCP `get` tool
gained an `args: dict[str, Any] | None = None` kwarg so view-
specific payloads (`{'entry': ..., 'depth': ...}` for callgraph;
`{'entry': ..., 'argv': [...], 'expand_stdlib': True}` for
runtrace) flow through. Reserved keys (`kind`/`id`/`view`/`q`)
inside `args=` are rejected at the boundary.

Pinned by `tests/test_python_indexer.py` (25 cases),
`test_python_cache.py` (9), `test_python_handler.py` (44 read-path),
`test_python_handler_writes.py` (37 + 12 anchored-edit), 
`test_python_callgraph.py` (17), `test_python_entries.py` (18),
`test_python_runtrace.py` (39, with real-subprocess end-to-end),
`test_python_config_wire.py` (18), and `test_mcp_args_kwarg.py`
(13). Full design rationale in `docs/python-kind-spec.md`.

## Anchored edit protocol — v1 (April 2026)

**New write modes** `mode='edit'` and `mode='insert'` join the
existing four (`create`/`append`/`replace`/`delete`) on every R/W
file kind. v1 ships for `markdown` and `python`; the protocol
surface is already universal so other R/W kinds (`plaintext`,
`rmk`, `docx`, `tex`, `book`) inherit it when they ship.

**Why.** `mode='replace'` swaps a *whole resolved region* (a
markdown block, a python qualname, a line range). Right granularity
for structural edits, far too coarse for surgical changes — every
"the fox jumps over **the** fence" → "...over **a** fence" round-
trip rewrote the whole paragraph. Cost: tokens, risk (model has to
reproduce the unchanged tail verbatim), lost provenance. The new
modes resolve by *content*: literal `find=`, optional `before=`/
`after=` anchors, `match='unique|first|all|nth'` policy.

**Pure resolver.** `precis.utils.edit_resolve` is no-I/O and shared
across kinds. `EditOp` dataclass with construction-time validation;
`find_candidates` (literal + anchor filter, overlapping matches
seen); `select_candidates` with sharp `BadInput` errors:

- *not found*: error includes up to 3 fuzzy-nearest lines via
  sliding-window `SequenceMatcher` (typo aid like "dpoamine" →
  "dopamine") and a `next=` hint to widen the region.
- *ambiguous* (`match='unique'` + ≥2 hits): error lists every
  candidate's line + 80-char context plus disambiguation hints
  (`before=` / `after=` / `match='all'` / `match='nth'`).

`apply_edit` splices end-to-start so byte offsets stay valid across
multi-match replacements. No-op detection — `find` == `text` raises
rather than silently writing nothing.

**Markdown wiring.** `MarkdownHandler.put` accepts the new modes;
`_put_anchored` resolves the search region (whole file when no
selector, the addressed block's text when `id='slug~BLOCK'`), runs
`apply_edit`, splices back via `_replace_lines`, atomic-writes,
re-ingests. The block's content-derived slug may change after an
edit; the next `get` re-tokens.

**Python wiring.** `PythonHandler.put` routes the new modes
through the same `_finalize_write` pipeline `replace`/`delete` use,
so the three gates (`ast.parse` validation, qualname-drop
prevention, `ruff check --fix && ruff format`) all apply
automatically. The qualname-drop gate's `pre_in_region` set is
computed as "any symbol whose source overlaps any edited span,"
catching anchored renames of `def` / `class` lines unless
`allow_rename=True`.

**Skills.** New `precis-edit-protocol` (tier 2) documents the
universal grammar; `precis-markdown-help`, `precis-python-help`,
and `precis-files-help` gain "Surgical edits" / "Anchored edits"
sections. Skill discovery: protocol skill is loaded on demand via
cross-links from per-kind skills and from error-message footers.

**`dry_run` for anchored edits.** Both `mode='edit'` and
`mode='insert'` accept `dry_run=True | 'diff' | 'full'`. The same
resolver, splice, and validation gates run; the disk write and
re-ingest are skipped. The response carries:

- a structured header (region label, edited spans, match policy,
  per-gate pass/fail — `ast.parse`, `qualname-drop`, `ruff` for
  python; `re-parse`, `hunks within/outside` for markdown), and
- a body that's either a unified diff (`dry_run=True` / `'diff'`,
  the default) with standard `--- a/<label>` / `+++ b/<label>`
  headers and 3 lines of context, or the post-edit lines around
  each edited span with `> ` markers (`dry_run='full'`).

Rationale: the diff is the right default — it answers "did the
resolver pick the right candidate?", "will the result look
right?", and "did anything unexpected happen (e.g. ruff
autofix)?" in one token-efficient view. `'full'` is the escape
hatch for cases where the agent wants to see the post-edit region
in its natural form. Ruff's incidental changes are surfaced
separately as `outside-spans` hunks so the agent isn't surprised
by autofixes that touch lines it didn't address.

**Still deferred to v2.** Regex (`regex=True` + `flags=`), atomic
multi-edit batches (`edits=[…]`), `expect_lines=N`, explicit
cross-region rejection. The v1+`dry_run` surface is still
additive; future deferred features will not break existing calls.

Pinned by `tests/test_edit_resolve.py` (59 unit tests covering
`normalize_dry_run`, `format_unified_diff`, `classify_diff_hunks`,
`render_dry_run_header`, `render_dry_run_full`),
`tests/test_markdown_handler.py` (20 anchored-edit tests including
7 dry_run cases), `tests/test_python_handler_writes.py` (20 anchored
edit tests including 8 dry_run cases that verify gates run + disk
untouched + diff/full formats). Full design rationale in
`docs/edit-protocol-spec.md`.

## bge-m3 char-truncation guard (April 2026)

`BgeM3Embedder.embed()` now truncates each input string to
`_BGE_M3_MAX_CHARS` (16,000) before handing it to
`sentence_transformers.SentenceTransformer.encode()`. Defends against
malformed blocks that escape upstream chunking — e.g. a corrupted-OCR
markdown table block of 192,633 chars that triggered a 73 GiB MPS-OOM
during ingest of `animeshjana2024recent`. The 1:1
`len(texts) == len(returned_vectors)` contract is preserved (truncation
is lossy on the suffix, never splits or drops blocks), so the store's
`blocks ↔ vectors` mapping is unchanged.

This complements the structure-aware `split_table` /
`enforce_hard_max` helpers added in `acatome-extract@0.6.2`.
Splitting belongs at the producer (where retrieval-meaningful chunk
boundaries can be preserved with header context); truncation belongs
at the consumer (where the hard MPS/CUDA constraint actually lives)
and protects every other handler that bypasses acatome-extract
(markdown, docx, tex, web, voice). Pinned by
`tests/test_embedder.py::TestBgeM3CharTruncation` (6 cases).

## MCP critique re-probe fixes (April 2026, second pass)

The critic re-probed after the first round and approved with one
remaining MAJOR (image-marker leak hadn't been wired into the search
preview) plus a modality-coverage observation: the server was
tools-only and silent on `prompts`, `resources`, `sampling`, and
`elicitation`.  Five tracks landed.

**MAJOR — figure markers no longer leak in search previews.**
The previous cut wired `_render_block_body`'s marker substitution
into `_render_chunks` (the get-with-`~N` path) but left the search
hit preview going through `_excerpt(block.text)` untreated.  Three
re-probed papers all returned raw `<span id="page-3-0"></span>![]
(_page_3_Figure_3.jpeg)` in their preview lines.  Centralised the
substitution in `_scrub_block_text` and applied it at both call
sites.  Bonus: dropped the asset path from `_render_block_body`'s
placeholder ("for diagnostics" still leaked the same `_page_*`
token).  Pinned by `test_paper_search_preview_strips_image_markers`
plus a stricter `test_figure_block_renders_structured_placeholder`.

**MAJOR — skills surface as MCP prompts.**
New `precis.mcp_modalities` module wires every skill that passes
`_availability_gap` into `prompts/list`, with the body served by
the same `_load_skill` / `SkillHandler.get` path the `get` verb
uses.  No skill text is duplicated.  Each prompt carries `tags=
{precis, skill, tier-N, kind:<X>, …}` so modern clients can
group / filter; we deliberately do **not** hide tier-2 ("power-
user") or draft skills — the menu is small (~16 entries) and the
reviewer asked for exposure, not curation.  `prompts/get` route
hits the synthesised `precis-help` and `precis-status` renderers
without any special-casing.  Six tests in `tests/test_mcp_modali
ties.py`.

**MAJOR — refs surface as MCP resources.**
Two surfaces, both DRY:
- `resources/list` enumerates only the bounded sets — skills
  today.  **Papers are deliberately not enumerated** (3000+ refs
  would blow client context and make `listChanged: true` near-
  useless).
- `resources/templates/list` advertises URI templates for the
  high-cardinality kinds: `precis://paper/{id}`, `precis://
  memory/{id}`, `precis://todo/{id}`, etc.  Modern clients use
  templates for autocomplete; concrete URIs are constructed by
  callers (typically from search hits).
Resource reads dispatch through `runtime.dispatch("get", ...)` so
there is no parallel rendering pipeline.  Numeric-ref kinds
str→int coerce in the read path.

**MAJOR — `precis-status` synth skill probes optional deps.**
The first-round CRITICAL (sentence-transformers missing from
`[paper]`) would have been caught by a health probe; this skill
is that probe.  `get(kind='skill', id='precis-status')` walks an
`_OPTIONAL_DEP_PROBES` table (10 entries: sentence-transformers,
sympy, wolframalpha, youtube-transcript-api, httpx, trafilatura,
python-docx, lxml, python-epo-ops-client, matplotlib), reports OK
/ MISSING / ERROR per probe with install hints, plus an "Overall:
OK | DEGRADED" summary and the live registered-kinds list.
Adding a probe is one row.  Refactor: replaced the single
`_SYNTHESIZED_SLUG = "precis-help"` with a `_SYNTHESIZED_SKILLS`
dict so future synth skills are a one-line addition.

**NIT — version vs status numbering bridge.**
Added a one-line block at the top of `precis-overview.md` mapping
`status: phase-N` (skill front-matter, build-phase markers) to
`serverInfo.version` (the canonical release marker).  Cheaper
than touching 19 front-matter blocks.

**Test count:** 1282 passed, 1 skipped (was 1271/1).

## MCP critique fixes (April 2026)

Five findings from the latest MCP critic pass — one CRITICAL plus
four MAJOR/MINOR. Five new regression tests in
`tests/test_mcp_critic_regressions.py`; full suite 1270 → 1271 green
(one previously-skipped paper-search test now runs because
sentence-transformers actually lands), lint + format + mypy clean.

**CRITICAL — `[paper]` extra didn't carry sentence-transformers.**
`BgeM3Embedder._ensure_loaded` raises with the hint `pip install
'precis-mcp[paper]'`, but the `[paper]` extra previously declared
only `acatome-extract>=0.1` (whose own `[embeddings]` extra is
where sentence-transformers lives). Result: a fresh
`pip install 'precis-mcp[paper]'` venv crashed with -32603 on every
search() against an embedder-backed kind, even though tools/list
advertised the verb as supported. The extra now declares
`acatome-extract[embeddings]>=0.1` and pins
`sentence-transformers>=3.0` directly so the install hint matches
reality. Verified by `pip install -e '.[all]' && python -c
'from sentence_transformers import SentenceTransformer'`.

**MAJOR — searched-kind annotation must surface on error paths.**
`_dispatch_inner` previously only prepended `(searched kind='X')` to
successful responses, so a search() that crashed inside the
defaulted kind's handler left the caller blind to which kind was
actually tried. The dispatcher now wraps the handler call: a
`PrecisError` has its `cause` annotated; non-Precis exceptions are
re-raised as `Internal` with the prefix already in the message. Both
branches verified by `test_search_default_kind_annotates_error_path`.

**MINOR — calc recovery hint uses `q=` to match canonical example.**
`precis-overview` and `precis-help` show `q='2+3*4'` everywhere for
tool-kinds. The calc handler still accepts both `id=` and `q=`, but
its `next=` trailers no longer teach `id=` — that was training
agents to mix kwargs and trip over the q= vs id= split elsewhere.
Two next-strings updated in `handlers/calc.py`; pinned by
`test_calc_recovery_hint_uses_q_kwarg`.

**MINOR — empty numeric-ref + quest searches grow Next: trailer.**
`memory`/`todo`/`gripe`/`fc`/`quest` empty searches used to return a
single line ("no memory entries match 'X'") with no recovery
affordance, while empty *list* responses on the same kinds carried
a clean Next: block. Asymmetry fixed in
`handlers/_numeric_ref.py::search` and `handlers/quest.py::search`:
empty searches now suggest a broader query, dropping any tag filter
(when one was applied), and the recent-list view. Pinned by two
new tests.

**MINOR — `view='fig/<N>'` is reserved, not a typo.**
`precis-paper-help` advertises `view='fig/<N>'` as a future-reserved
affordance. The handler used to lump it into the generic "unknown
view" Unsupported error, making a caller who'd read the help skill
assume the docs were wrong. `PaperHandler._render_view` now special-
cases `view.startswith('fig/')` and surfaces a deliberate "reserved
view" error citing `precis-paper-help` and the caption-only
workaround. Pinned by `test_paper_view_fig_n_is_reserved_not_unknown`.

**Deferred (not in this cut):** the critic also suggested a
registry-time embedder probe that downgrades `tools/list` when the
optional dep is genuinely absent (e.g. on a stateless deployment
that intentionally skipped `[paper]`). Worth doing for parity with
how the markdown / python kinds gate themselves on roots, but not
required now that the install path actually works. Queued.

## Planned — Phase 9: `patent` kind (EPO OPS)

Read-only third durable knowledge corpus alongside `paper` and
`markdown`. Spec at `docs/patent-kind-spec.md`; agent skills at
`src/precis/data/skills/precis-patent-help.md` (entry-level) and
`precis-patent-power.md` (raw CQL); deferred filter affordances at
`docs/search-future-filters.md`.

Phase-1 surface:
- `search(kind='patent', q=, tags=, scope=, top_k=)` — merged
  local + remote OPS hits, `[local]` markers, 7-day cache on the
  remote leg.
- `get(kind='patent', id=<docdb-slug>)` — fetch-as-ingest from
  OPS (biblio + description + claims), ST.36 XML parsed and
  embedded, raw XML mirrored on disk under
  `$PRECIS_PATENT_RAW_ROOT/<cc>/<num>/<kc>/`.
- `get(kind='patent', id='/recent' | '/published')` — list views
  (ingest time vs publication date).
- Tags use open lowercase prefixes (`cpc:`, `ipc:`, `applicant:`,
  `country:`, `kind:`, `family:`, `topic:`) lifted to OPS CQL on
  the remote leg via `_TAG_TO_CQL` translation table.
- Migration `0006_patent_kind.sql` registers the kind, two
  providers (`epo_ops`, `epo_ops_search`), and adds
  `"patent": frozenset({"SRC", "CACHE"})` to
  `store/types.py::_KIND_ALLOWED_AXES`.
- Required env: `EPO_OPS_CLIENT_KEY`, `EPO_OPS_CLIENT_SECRET`,
  `PRECIS_PATENT_RAW_ROOT`.

Phase 2 (separate cut): saved CQL watches table, runner, CLI,
launchd plist on balthazar.

Decisions captured this round:
- Search signature stays cross-kind uniform — no patent-specific
  kwargs (`ti=`/`ab=`/`pd=`/etc.). Power users put raw CQL in
  `q=`; the simple-vs-power-user split is two skill files.
- Date-range / state-marker / `source=local|remote|both` knobs
  captured in `docs/search-future-filters.md` for cross-kind
  consistency rather than implemented patent-only.
- Slug normalisation strips whitespace only (no dots) so DOIs /
  arXiv ids in other kinds aren't undermined by precedent.

## MCP critic phase-8 — deferred items: axis enforcement, total_hits, inverse-rewrite, paper+research+conv+oracle crosslink

Five of the six items the previous CHANGELOG flagged as "deferred"
landed this session. **894 → 970 tests green, 1 skip** in this
package's own suite (74 new regression tests across
`tests/test_critic_phase8.py`, `tests/test_paper_research_crosslinking.py`,
and `tests/test_conv_oracle_crosslinking.py`, plus three test
files updated for the new axis discipline).

### Cross-linking on read-only kinds (paper, research, think, websearch, conv, oracle)

Six previously-immutable kinds gained a *narrow* link/tag put
surface this session. Body content stays read-only — paper
ingests still come from `.acatome` bundles, transcripts arrive
from the chat-bridge, oracle bodies are seeded externally,
Perplexity reports come from the API or paste-import — but
``link``/``unlink``/``tags``/``untags``/``rel`` are now first-
class on every one of them. The motivating use case was
"paper-A cites paper-B"; the same surface lets a research
report link back to its prompting paper, a conversation
record-link the todo it produced, and an oracle reference the
papers underlying its rubric.

New module `precis/handlers/_link_tag_ops.py` factors the
validation and store-call wiring out of `NumericRefHandler.put`:

- `validate_link_args(link, unlink, rel, kind)` — mutual exclusion
  + bare-`rel=` rejection.
- `validate_relation(rel) -> Relation` — registered-vocab check
  with `BadInput` on miss; defaults to `related-to` when omitted.
- `apply_link_ops(store, src_ref_id, ...) -> (n_added, n_removed)`
  — wraps `parse_link_target` + `add_link`/`remove_link`.
- `apply_tag_ops(store, kind, ref_id, ...) -> (n_added, n_removed)`
  — wraps `Tag.parse_strict` (kind-aware) + `add_tag`/`remove_tag`.
- `format_link_tag_ack(...)` — the one-line response renderer
  used by both new handlers, dropping zero-op segments.

`PaperHandler.put`:

- Accepts only ``link``/``unlink``/``tags``/``untags``/``rel``
  with ``id=<slug>``. Defaults are unchanged (`related-to`).
- Rejects ``text=`` with a hint pointing at the bundle ingest CLI.
- Rejects ``mode=`` because there's no body mutation surface.
- Rejects chunk selectors (`slug~46`) and path views
  (`slug/cite/bib`) — link/tag ops are ref-level only. Re-uses
  `_parse_paper_id` so the parser-side rejection is consistent
  with the read surface.
- Per-kind axis enforcement: `STATUS:` and `PRIO:` are rejected;
  `SRC:` and `CACHE:` are accepted (matches `_KIND_ALLOWED_AXES`).

`_PerplexityBase.put` (research / think / websearch) now
dispatches:

- `mode='import'` → existing $0 cache-import path, unchanged.
- `mode is None` + any of `link`/`unlink`/`tags`/`untags`/`rel`
  → new `_put_link_tag_ops` path. Resolves `id=` as a slug
  (NOT a query — query resolution would require re-hashing the
  canonical key and wouldn't cover slugs from direct ingest).
- `mode='import'` + link/tag kwargs → `BadInput`. Mixing the
  two surfaces is a misuse; the error suggests splitting into
  two calls.
- Anything else → `BadInput` with both options enumerated.

`OracleHandler.put` and `ConversationHandler.put` follow the
same template as paper: link/tag-only, ``text=``/``mode=``
rejected with hints pointing at the proper write path (corpus
seed pipeline for oracles, chat-bridge for conv). `conv`
additionally rejects chunk selectors (`slug~12`) and path
views (`slug/transcript`) since link/tag ops are ref-level.

Spec changes: `supports_put=True` on `paper`, `oracle`, `conv`.
The Perplexity trio kept `supports_put=True` (already there for
import mode); description and put-modes documentation updated
to mention link/tag.

Per-kind axis enforcement covers paper (`{SRC, CACHE}`), the
cache trio (`{CACHE}`), and conv/oracle (empty — open tags only);
`STATUS:`/`PRIO:` on any of these raises `BadInput` at the agent
boundary, matching the discipline added elsewhere this session.

`NumericRefHandler` was *not* refactored to use the new helpers
this pass — it works, and rewiring it for marginal DRY would
add regression risk. The shared module exists for paper + cache
+ any future read-only kinds that gain link/tag surfaces.

## MCP critic phase-8 — deferred items: axis enforcement, total_hits, inverse-rewrite

Three of the six items the previous CHANGELOG flagged as
"deferred" landed this session. **894 → 923 tests green, 1 skip**
(27 new regression tests in `tests/test_critic_phase8.py`,
plus three test files updated for the new axis discipline).

### Per-kind axis enforcement

The MCP critic flagged ``STATUS:open`` on a memory as a smell —
memory has no workflow state, so the tag is decorative and a
filter query (``search(kind='todo', tags=['STATUS:open'])``)
cannot find it. The validator accepted it anyway because closed-
prefix tags weren't gated on the kind.

Fix: ``Tag.parse_strict`` and ``Tag.normalize_filter`` accept
an optional ``kind=`` kwarg. When set, the parser checks
``_KIND_ALLOWED_AXES`` (a new map in ``store/types.py``) and
rejects closed-prefix tags whose axis isn't whitelisted for
that kind. The map is conservative:

- **Workflow kinds** (`todo`, `gripe`, `quest`) — `{STATUS, PRIO}`
- **Free-form notes** (`memory`) — `{}` (open tags only)
- **Flashcards** (`fc`), **conversations** (`conv`),
  **oracles**, **skills** — `{}` (no closed axes today)
- **Papers** — `{SRC, CACHE}` (primary/secondary lit + cache state)
- **Cache kinds** (`research`, `think`, `web`, `websearch`,
  `youtube`) — `{CACHE}`

Kinds absent from the map remain unrestricted (backwards-
compatible). Callers that don't know their kind at validation
time (filter queries that span kinds, migrations) pass
``kind=None`` and get the global vocabulary check unchanged.

Threading: every `_numeric_ref.search`/`put`, plus
`PaperHandler.search`, now passes `kind=self.kind` (or the
literal slug) into `Tag.normalize_filter`/`parse_strict`. Open
tags and bare flags are unaffected — the gate only fires on
closed-prefix tags.

Tests updated: `test_memory.py`'s closed-prefix tests moved to
todos (where the axis is real) or rewritten to exercise the
open-tag accumulation pattern. `test_search_tag_filter.py`'s
memory cases switched to open tags (`topic-co2-capture`).
`test_untags_on_put.py`'s closed-prefix value-match test moved
to TodoHandler. The new `test_critic_phase8.py::TestPerKindAxisEnforcement`
class pins the contract end-to-end.

### Auto-mirror inverse relations — read-side rewrite (not auto-insert)

The MCP critic flagged ``cites`` from A to B as not auto-
discoverable as ``cited-by`` from B to A — `links_for(B,
relation='cited-by', direction='out')` returned nothing because
the schema stores exactly one row per edge.

There were two ways to fix this:

- **Option A (auto-insert):** insert *two* rows on every
  asymmetric link. Pro: the outbound query "just works".
  Con: doubles ``direction='both'`` cardinality, contradicts
  the explicit one-row-per-edge design in
  `migrations/0005_link_relations.sql`, and creates a drift
  surface (the two halves of the edge can disagree if one is
  mutated independently).
- **Option B (read-side rewrite):** keep one row per edge,
  rewrite the relation filter at *query* time. The user
  picked B after I laid out the tradeoff.

Implementation: `Store.links_for` now consults
`_INVERSE_RELATIONS` (a Python mirror of `relations.inverse_slug`).
When the requested `relation=` has a registered inverse, the
WHERE clause becomes a disjunction:

- "literal-relation in the requested direction" OR
- "inverse-relation in the opposite direction"

So `links_for(B, relation='cited-by', direction='out')` matches
both literal `cited-by` rows where B is src (rare; only if
explicitly inserted) and `cites` rows where B is dst (the
canonical encoding of the same edge from the cited side).
Returned `Link` rows keep their *stored* relation slug — the
caller compares against the requested filter to label them,
exactly the same job the renderer already does for
`direction='both'`.

`add_link` is unchanged: still one row per edge. `remove_link`
likewise stays single-row — there's no shadow row to clean up.

The relations covered: every asymmetric pair listed in
`migrations/0001_initial.sql` and `0005_link_relations.sql`
(`blocks`/`blocked-by`, `contradicts`/`contradicted-by`,
`cites`/`cited-by`, `derived-from`/`derived-into`,
`supports`/`supported-by`, `generalises`/`specialises`).
Symmetric (`related-to`) and inverse-less (`see-also`) skip
the rewrite — they don't appear in `_INVERSE_RELATIONS`.

### `total_hits` header in search responses

Every search handler now emits a leading line of the form

    # 10 of 1234 paper matches for 'photocatalysis'

The MCP critic flagged the missing "of K" readout as a
pagination footgun: an agent that asks for `top_k=10` and gets
exactly 10 hits couldn't tell whether it had everything or
just the first page. Now the header makes the cardinality
explicit.

Implementation:

- New `Store.count_refs_lexical(q, kind, tags)` and
  `Store.count_blocks_lexical(q, kind, scope_ref_id, tags)` —
  same WHERE clauses as their `search_*_lexical` companions
  (including the noise-floor `char_length(btrim(text)) >= 4`
  guard from phase 7), no LIMIT. Two queries per search call;
  on TSV-indexed columns the COUNT is sub-millisecond.
- New helper `precis.utils.search_header.format_search_headline`
  centralises the header wording. The "N of K" suffix is
  suppressed when `total <= n_returned` (no value in saying
  "10 of 10") and when `total is None` (semantic-only search,
  where every embedded block is a hit at some distance).
  Pluralisation handles `-ch`/`-sh`/`-s`/`-x`/`-z` endings so
  `paper match` → `paper matches` rather than `matchs`.
- Wired into every search handler: `_numeric_ref.py`,
  `paper.py`, `markdown.py`, `oracle.py`, `quest.py`,
  `conversation.py`, `skill.py`, `python.py`. The Python and
  Skill handlers compute total in-process (`total = len(hits)`
  before the `top_k` slice).

For RRF-fused searches (`paper`, `markdown`, `conv`), the
total is the *lexical* count — RRF only re-ranks lexically-
matching rows, so the lexical universe is the meaningful "K".
The handler comment notes this; the docstring on
`count_blocks_lexical` does too.

### Brief: why I'm not adding `prompts/list` and `meta/registry`

Both are optional MCP protocol extensions. `prompts/list`
advertises named prompt templates — our skill system already
covers this via `get(kind='skill')` and would just duplicate
the surface in a more rigid shape. `meta/registry` exposes the
tool/kind schema as structured data; today agents discover
kinds via `precis-overview`, and every frontier model handles
the prose form. Skipping both keeps us flexible against the
MCP spec evolving.

### Out of scope this session

Still queued from the phase-7 deferred list:

- **Relevance floor on rank scores.** Soft threshold beyond
  the noise-floor filter. Needs empirical per-kind tuning.
- (Done — see the cross-linking entry above.) Paper, research,
  think, websearch, conv, and oracle all gained the link/tag put
  surface this session.

## MCP critic phase-7 follow-up — schema, skill index, search hardening

The second MCP critic pass (Apr 2026) found 3 CRITICAL, 9 MAJOR,
and 6 MINOR issues against the live phase-7 build. **862 → 894
tests green, 1 skip** (32 new regression tests in
`tests/test_mcp_critic_phase7.py`).

### CRITICAL #1 — `rel=` / `unlink=` / `untags=` weren't on the MCP `put` schema

The handler layer landed these kwargs in the previous pass, but the
FastMCP tool wrapper in `precis/server.py` only forwarded
`{kind, id, text, mode, tags, link}`. `rel='cites'` was silently
discarded; every linked claim collapsed to `related-to`.

Fix: tool schema now declares `untags`, `unlink`, `rel`. Tool
description rewritten to drop the retired colon-suffix shortcut
("`'target_slug:relation'`") and document the canonical
`'kind:identifier[~selector]' + rel='…'` form. Mode hint clarified
to name only `delete` (the only widely-supported mode on numeric
kinds) plus the file-kind list.

### CRITICAL #2 — unknown `mode=` values silently no-opped

`mode='untag'` and `mode='unlink'` returned `updated memory id=N`
without doing anything — the handler's `if mode == 'delete'` branch
was followed by the regular update path, which simply ignored the
unknown mode. The critic identified this as the worst possible
failure mode for an agent loop (silent state divergence on undo).

Fix: `NumericRefHandler.put` now validates `mode=` against
`_SUPPORTED_PUT_MODES = ("delete",)` up front. Anything else
(`'note'`, `'untag'`, `'unlink'`, typos like `'deelete'`) raises
`BadInput` with the supported list and a hint pointing at the
correct kwargs (`untags=`, `unlink=`, `mode='delete'`).

### CRITICAL #3 — skill index advertised skills for unregistered kinds

`get(kind='skill')` listed 17 skills as authoritative; 3 of them
(`precis-markdown-help`, `precis-python-help`, `precis-files-help`)
documented kinds the registry rejected. Following any of them
produced `NotFound` cascades on the agent's first call.

Fix: `SkillHandler` now filters the index via `_availability_gap()`,
which checks two gates per skill:

- **Subject-kind gate.** Slugs of the form `precis-<kind>-help`
  with `<kind>` not in `registry.kinds()` are filtered.
- **Status gate.** Front-matter `status: planned` /
  `status: aspirational` filters the skill regardless of name.

Filtered skills remain **retrievable by exact slug** with a
`> **Heads up:**` banner explaining why they're hidden — the docs
stay accessible if you know what you're looking for, they just
don't pollute discovery.

The trailer at the bottom of the index also swapped its second
suggestion from `precis-navigation` (now aspirational) to
`precis-tags` (active).

### MAJOR #4 — `precis-cache` documented `kind='ask'` that doesn't exist

`ask` was never wired in this build. The skill listed it as one
of four cached kinds with a TTL table that was wrong on every
row.

Fix: rewrote `precis-cache.md` against the live handlers. The
TTL table now reads from the canonical source (`math` pinned,
`web` 7d, `websearch` 7d, `think` 30d, `research` pinned,
`youtube` 30d) with a note that the handler classes are the
single source of truth. Every example uses a live kind. Same
fix in `precis-memory-help.md` (`kind='ask'` → `kind='research'`)
and `precis-navigation.md` (which was already gated by status:
aspirational, see #7).

### MAJOR #6 + #7 — `precis-density` and `precis-navigation` are aspirational

Neither skill describes the live runtime: `precis-density`
documents `view='representatives'/'echoes'/'coverage'` and a
`DENSITY:*` tag prefix, none of which exist; `precis-navigation`
contains 9 recipes, 8 of which contain at least one
non-functional call.

Fix: front-matter `status: planned` (density) / `aspirational`
(navigation), with explicit "Heads up" banners enumerating the
runtime mismatches. The skill-index filter then hides them from
discovery, as does `precis-files-help` (which documents file
kinds queued for a later phase).

### MAJOR #8 — unregistered `UPPERCASE:` tag prefixes were silently accepted

`Tag.parse_strict` validated values inside *registered* prefixes
(`STATUS:bogus` rejected) but let unregistered prefixes pass
through unchecked. So `DENSITY:sparse` and `CONFIDENCE:moderate` —
both documented in `precis-tags.md` as "would be rejected" — were
accepted at runtime and silently joined the corpus. Same for
typos like `STATSU:open`.

Fix: any uppercase prefix not in `_CLOSED_VOCAB` raises `BadInput`
with the registered axis list and a recovery hint (use a
registered axis, or write as a lowercase open tag like
`density-sparse`). This catches typos too — `STATSU:open` no
longer survives into queries silently. Two memory tests that
relied on the old behaviour were updated to use `PRIO:` (a real
registered axis); the `confidence-*` examples in
`precis-memory-help.md` were rewritten as open tags with the
``untags=`` removal idiom for swapping.

### MAJOR #9 + MINOR m1 — paper default overview leaked `<jats:*>` XML

`view='abstract'` stripped JATS namespace tags, but the default
`get(kind='paper', id=…)` overview rendered the abstract verbatim.
Worse, even `view='abstract'` produced
`AbstractMetal–organic frameworks…` (heading word fused with the
body's first word).

Fix: `_render_overview` now calls the same `_strip_jats` helper
the abstract view uses, so both paths produce identical clean
output. `_strip_jats` was also rewritten:

- `<jats:title>Abstract</jats:title>` is dropped outright (the
  view name and rendering context already name the section, and
  the heading word was the only thing causing the mash).
- Closing tags get a single space rather than empty-string, so
  adjacent paragraphs don't fuse word-to-word.
- Whitespace runs collapsed to keep paragraph structure clean.

Two new tests pin both the no-mash and the no-`<jats:` invariants.

### MAJOR #10 — `top_k` was unbounded; `top_k=9999` returned 7 326 hits

`search(kind='paper', q='photocatalytic', top_k=9999)` returned
~2.7 MB in a single response, large enough to exhaust a 7B
model's context window in one call. The schema didn't document
a maximum.

Fix: `precis/server.py` enforces `top_k ∈ [1, 100]` at the MCP
boundary (the `_SEARCH_TOP_K_MAX` constant is exported and pinned
by a regression test). Larger values raise `BadInput` with the
cap value and a hint to narrow with `scope=` or paginate. The
internal-caller path (tests, SDK consumers calling `dispatch`
directly) is unchanged — the cap is the agent-facing-only
guardrail.

### MAJOR #11 — search returned punctuation-only blocks as hits

Adversarial queries (`q='🚀💩'`, `q='xyzzy_definitely_no_match_…'`)
returned 10 hits dominated by punctuation-only chunks (".", ",",
"kinetics."). These are formatting artefacts whose embeddings
cluster near the noise floor; cosine similarity puts any query
close to them.

Fix: every block-search method (`search_blocks_lexical`,
`search_blocks_semantic`, `search_blocks_fused`) adds
`char_length(btrim(b.text)) >= 4` to its WHERE clause. Blocks
shorter than 4 stripped chars don't appear in any search output —
they're not deleted (so direct chunk reads via `id=slug~N` still
work), just filtered from search results.

A relevance floor on rank scores would land in a follow-up; the
current fix removes the lowest-quality hits without needing
empirical threshold tuning.

### MAJOR #12 — cross-kind error options listed kinds that don't support search

`search(kind='all', q='…')` raised `NotFound: unknown kind: all`
with `options=` containing **every** active kind, including
`calc`, `math`, `web`, `websearch`, `think`, `research`, and
`youtube` — all of which support `get` only. Agents retrying
against the suggested options hit a second `Unsupported` error.

Fix: the runtime catches the registry's `NotFound` in
`_dispatch_inner` and re-raises with `options=
self._kinds_for_verb(verb)` — the verb-filtered subset. The
comma-list catch-all does the same now. Same one-line shape as
the no-kind path that was already correct.

### MAJOR #13 — `mode='note'` was advertised but no kind accepted it

The tool description listed `'note'` as a supported mode; the
critic checked `precis-memory-help`'s prose and found it
referenced `mode='note'` but never called it. No handler
implements it.

Fix: `_SUPPORTED_PUT_MODES` (numeric kinds) is now strictly
`("delete",)` and the tool description names only the modes
that have working handlers. The reference in
`precis-memory-help.md` was rewritten to point at the real
pattern (create memory + `link=` back to the ref with a
specific `rel=`).

### MINOR m2 — empty-list responses on read-only kinds had no Next: trailers

`oracle`, `conv`, and `quest` empty-list views returned bare
strings ("no oracles defined yet") with no recovery hint — the
critic flagged this as a consistency violation against `gripe`
and `fc` which already emitted trailers.

Fix: each empty-list path now emits a one-line `Next:` block
with a concrete next-call shape. Three small handler edits.

### MINOR m3 — paper-id underscore returned `BadInput: unparseable`

7B models reflexively use snake_case. `get(kind='paper', id='nonexistent_paper_xyz')`
raised `BadInput` instead of either `NotFound` (slug doesn't exist)
or a clear "underscore is illegal" message. The slug regex's
permissive prefix-match meant the trailing `_paper_xyz` ended up
in the chunk-selector branch and produced a generic error.

Fix: post-match check that catches non-`~`/non-`/` rest. If the
first leftover char is `_`, the error names the rule
("underscores not allowed; slugs match `[a-z0-9-]+`"); for any
other illegal char, the error names that char specifically.

### MINOR m4 — `calc` echoed gibberish identifiers as symbolic expressions

`calc(id='malformed**broken')` returned `malformed**broken =
malformed**broken` because sympy parsed the identifiers as free
symbols and `simplify(symbol**symbol)` is itself. No signal to
the agent that the input wasn't a math expression.

Fix: when the simplified result is identical to the input string
**and** contains free symbols, raise
`BadInput: expression simplifies to itself` with a hint pointing
at concrete patterns (`solve(Eq(…))`, `integrate(…)`). Pure
numerics (`2+3*4`) and real symbolic calculus
(`integrate(sin(x), x)`) still work — the heuristic only fires on
the "no operator simplified anything" case.

### MINOR m6 — single-block trailers rendered as `~N..N`

The chunk-range trailer formatter emitted `~77..77` for a
degenerate single-block range, training agents to write
`~5..5` for unrelated singletons later. The canonical
single-block form is `~N`.

Fix: trailer collapses `lo == hi` to `~N` with the label
`"next chunk"` (vs `"next chunk range"` for true ranges).

### Out of scope / deferred

These are real findings I haven't shipped yet:

- **Relevance floor on rank scores (MAJOR #11a).** The noise-floor
  filter handles the most egregious case (punctuation-only
  blocks); a soft threshold on rank scores still wants empirical
  tuning per kind.
- **`total_hits` in search responses (MAJOR #10b).** Pagination
  intent needs a header field that says "you're seeing N of K".
  Not landed yet — would require changes to every search
  handler's render.
- **Cross-kind link CRUD on file-/cache-backed handlers
  (MAJOR #4 follow-up).** Numeric refs accept `link=` /
  `unlink=` / `rel=`; paper / markdown / python / cache kinds
  don't yet.
- **Auto-mirroring inverse relations.** `cites` from A to B is
  not automatically inserted as `cited-by` from B to A. The
  renderer shows both directions correctly via two queries, but
  a "who cites me?" filter still requires `direction='in'`.
- **`prompts/list` and `meta/registry` resource.** The critic
  recommended these as MCP-spec extensions; not in scope this
  pass.
- **Per-kind axis enforcement.** The critic noted `STATUS:open`
  on a memory is a smell (memories don't have a STATUS axis),
  but per-kind axis restriction is a bigger feature than the
  CRITICAL/MAJOR items here. Still queued.

## Link CRUD — `link=` / `unlink=` / `rel=` end to end

The previous critic pass flagged `link=` as silently no-op
(`if link is not None: pass  # links CRUD lands later`). That
placeholder is now replaced with a working end-to-end implementation
on every numeric-ref kind (`memory`, `todo`, `gripe`, `fc`, `quest`,
`conv`). **819 → 861 tests green, 1 skip** (42 new link CRUD tests).

### Migration: full relations vocabulary

`migrations/0005_link_relations.sql` seeds the relations the docs
have always promised but the schema never carried:

- Citation graph: `cites` / `cited-by`.
- Provenance: `derived-from` / `derived-into`.
- Evidential support: `supports` / `supported-by`.
- Generalisation: `generalises` / `specialises`.
- Asymmetric pointer: `see-also` (no inverse).

Each row has its `inverse_slug` populated for the renderer's
direction-aware labelling. **Inverse handling stays app-level** —
adding a `cites` link does *not* auto-mirror as `cited-by`. The
`view='links'` renderer queries both directions and shows the
relation as stored, with arrows for direction.

The `Relation` typing literal in `precis/store/types.py` was
extended to mirror the seed exactly. A regression test
(`TestRelationsVocabularyMatchesSchema`) pins that every literal
slug exists in the seeded table, so future drift between Python
and SQL fails loudly at test time rather than silently at INSERT.

### Canonical syntax: `kind:identifier[~selector]` + separate `rel=`

The agent-facing skill docs were inconsistent: `precis-todo-help`
said `link='target_id:relation'` was the only syntax and there was
no `rel=` parameter; `precis-relations` and `precis-memory-help`
showed the opposite. Both forms had cases where they were ambiguous
(numeric ids vs slugs vs relation suffixes all sharing `:`).

Settled on the strict form:

```python
put(kind='memory', id=47,
    link='paper:wang2020state~38',
    rel='cites')

put(kind='memory', id=47, unlink='paper:wang2020state', rel='cites')
get(kind='memory', id=47, view='links')
```

- **Kind prefix is mandatory** — no bare slugs, no implicit fallback
  to the source kind.
- **`rel=` is a separate kwarg** — no overloaded colon-suffix.
- **Block selector** unchanged: `~pos` (numeric) or `~slug`.

The kwarg parsing rejects two genuine misuses up front:
- `link=` and `unlink=` together → `BadInput` ("mutually exclusive").
- `rel=` without `link=`/`unlink=` → `BadInput` ("rel= requires
  link= or unlink=") — silent swallowing here would let typos
  vanish.

### Target parser

New module `precis/handlers/_link_target.py`:

```python
@dataclass(frozen=True, slots=True)
class LinkTarget:
    ref_id: int
    pos: int | None
    kind: str
    raw: str

def parse_link_target(target: str, *, store: Store) -> LinkTarget: ...
```

The parser hits the store to resolve the target *before* any
mutation, so a `link='paper:does-not-exist'` on a put-create is
rejected up-front and doesn't leave a half-created memory row in
the corpus (regression test
`test_bad_link_target_rejected_before_create` pins this).

Validation paths:
- Missing `:` prefix → `BadInput` with canonical-form hint.
- Empty kind / identifier / selector → `BadInput`.
- Unknown kind → `BadInput` with the full options list (queried
  from the live `kinds` table, so file-kinds added in 0004 show
  up automatically).
- Numeric kind with non-numeric id → `BadInput`.
- Slug kind ref not found → `NotFound`.
- Block pos out of range / block slug missing → `NotFound` with
  hint to `get(kind=…, id=…)` for the block list.
- Negative pos → `BadInput` (the runtime sentinel `-1` is
  internal-only).

### Store CRUD

Three new methods on `Store`:

- `add_link(...)` — idempotent on the unique tuple via
  `ON CONFLICT (...) DO UPDATE SET set_by = links.set_by RETURNING *`.
  Schema-level `CHECK (NOT (src_ref_id = dst_ref_id AND src_pos =
  dst_pos))` is mirrored as a `BadInput` at the runtime boundary
  (sharper hint than the DB error). Same-ref different-pos links
  are allowed (block~5 → block~7 within one ref).
- `remove_link(...)` — `relation=None` removes all rows between
  the (src, src_pos, dst, dst_pos) tuple regardless of relation;
  the handler-level `unlink=` without `rel=` uses this. Returns
  rowcount; missing rows are silent no-ops.
- `links_for(ref_id, *, direction=..., relation=...)` — `out` /
  `in` / `both`, with optional relation filter. Used by the
  `view='links'` renderer.

`_row_to_link` maps the DB sentinel `pos = -1` back to Python
`None` at the boundary so callers always see "ref-level" as
Pythonic absence.

### Handler integration

`NumericRefHandler.put` accepts `link=`, `unlink=`, `rel=` on
both create and update paths. `_validate_relation` provides the
canonical-form check at the agent boundary, raising `BadInput`
with the full options list — sharper than the FK violation the
DB would otherwise raise.

`NumericRefHandler.get(view='links')` renders both directions:

```
# memory 42 — links

## outbound
→ paper:wang2020state~38  (cites)
→ memory:7  (related-to)

## inbound
← memory:55  (derived-from)
```

Endpoints are bulk-fetched with one round trip
(`SELECT … WHERE id = ANY(%s)`) so the render is O(N) on link
count, not O(N) on `link × DB-query`. Soft-deleted targets render
with a `(deleted)` marker rather than vanishing — agents need to
know when a link points at a tombstoned ref.

`view=` is now eagerly validated against the per-handler views
tuple (currently just `('links',)` on numeric kinds). Subclasses
with extra views override `get()` in the usual way.

### Documentation reconciliation

- `precis-relations`: rewritten as the canonical reference. Full
  vocabulary table with inverses, validation-error catalogue,
  semantics notes (idempotent, position-aware, kind-agnostic,
  inverse-as-documentation).
- `precis-todo-help`: `link='158:blocked-by'` → `link='todo:158'`,
  `rel='blocked-by'`. The "no `untags=`" line — outdated since
  last session — was replaced with a pointer to `precis-tags`.
  The "tag-filter not yet implemented" disclaimer was replaced
  with a working example.
- `precis-memory-help`: bare-slug examples → `paper:`/`research:`
  prefixed.
- `precis-navigation`: same.
- `precis-markdown-help`: already canonical (`markdown:notes/x.md`),
  no change.

### Tests

`tests/test_link_crud.py` — 42 tests across:

- **`TestParseLinkTarget`** (14): all syntax forms, rejection
  paths, missing kind/ref/block, negative pos, empty selector.
- **`TestStoreLinkCRUD`** (10): basic add, idempotency, multi-
  relation rows, ref-level self-loop rejection, block-level
  self-loop allowed, specific-relation removal, broad removal,
  no-op missing removal, direction filter, relation filter.
- **`TestRelationsVocabularyMatchesSchema`** (1): the
  Python literal mirrors the seeded SQL.
- **`TestMemoryHandlerLink`** (11): create-time link, explicit
  rel, block target, update-time link, unlink with rel, unlink
  without rel (broad), mutual-exclusion check, rel-without-
  link/unlink rejection, unlink-on-create rejection, unknown
  relation, atomic rejection of bad target before insert.
- **`TestMemoryHandlerLinksView`** (6): no-links hint, outbound
  arrow, inbound arrow, block-pos rendering, unknown view
  rejection, deleted-target marker.

### Out of scope this pass (still queued)

- **Block-level link source positions on numeric refs.** The
  schema and target parser support `src_pos`, but the handler
  surface only writes ref-level (`src_pos = None`) sources. Memory
  has no block subdivision today; the moment a numeric kind grows
  blocks, this becomes interesting.
- **Cross-handler link CRUD.** PaperHandler / MarkdownHandler /
  PythonHandler don't accept `link=` yet — only the numeric refs
  do. The schema doesn't care about source kind, but the
  agent-facing kwargs need to be wired per handler.
- **Symmetric-relation auto-mirroring.** `cites` from A to B is
  *not* auto-inserted as `cited-by` from B to A. The renderer
  shows both directions correctly, but a query like "who cites
  me?" still requires `direction='in'`. Auto-mirroring tempts
  consistency bugs (which side is canonical?); leaving it manual
  for now.
- **Bulk link operations.** `link=` accepts one target per call.
  Multi-link should land if a real workflow needs it; for now
  iteration on the agent side is enough.

## MCP critic follow-up — TOC heuristic + tag filter + untags

Closing the deferred items from the previous critic pass.
**730 → 819 tests green, 1 skip** (53 TOC-rejection tests, 24
tag-filter tests, 12 untags tests).

### Paper TOC — reject metadata as headings

The hierarchical TOC heuristic (`_paper_toc.detect_heading`) was
treating publisher metadata blocks (`**DOI: 10.1002/...**`,
`**Keywords: ...**`, `**Received: 12 Mar 2024**`) as H2 headings,
because they're bold-only single-line blocks. The MCP critic
flagged a paper where 357 of 460 blocks landed under a single
``DOI:…`` pseudo-heading, making the hierarchical TOC useless.

`detect_heading` now applies an anti-pattern filter:

- `_METADATA_PHRASE_RE` — always-metadata phrases (`©`, `Copyright`,
  `License`, `Funding`, `Article history`, `Supplementary`,
  `Conflict of interest`, `Available online`, `Cite this article`).
  Match anywhere → reject.
- `_METADATA_LEAD_RE` ∧ `_METADATA_SHAPE_RE` — conditional-metadata
  leads (`DOI`, `Keywords`, `Authors`, `Affiliations`, `Received`,
  `Accepted`, `Published`, `Corresponding`, `Email`, `ORCID`,
  `Cite this`, `Submitted`, `Revised`) only count as metadata when
  the title also carries a metadata-shape signal (`:`, em-dash,
  en-dash, `@`, or any digit). This keeps a real subsection title
  like "DOI tracking subsection" — no colon, no digit — from being
  false-flagged.
- DOI strings (`10.NNNN/...`) and URLs (`https?://`) anywhere in the
  title → reject.
- Title length > 60 chars → reject (real subsection titles are short).

The filter applies uniformly to H1, H2, MD-H1, and MD-H2 paths so a
``■ **DOI: 10.x/y**`` artefact also gets caught even though it
carries the H1 marker. **This is v2-only** — v1 (`precis-mcp`)
indexed from typed nodes that already carried heading level, so no
fix is owed there.

### `tags=` filter on search — DRY at the SQL layer

The schema already had a unified `ref_tags` view over the three
narrow tag tables (`ref_closed_tags`, `ref_flags`, `ref_open_tags`),
indexed for prefix-and-value lookups. The "missing piece" was a
single helper that emits a SQL fragment usable by every store
query that selects refs.

New module `precis/store/_tag_filter.py`:

```python
def build_tag_filter(
    tags: list[str] | None,
    *,
    ref_alias: str = "r",
    block_level: bool = False,
) -> tuple[str, list[Any]]:
    """Build the SQL AND fragment + params for a tags filter."""
```

Returns `("", [])` for `None`/`[]` so callers splice
unconditionally. The fragment uses `r.id IN (SELECT ref_id FROM
ref_tags WHERE tag IN (...) AND pos IS NULL GROUP BY ref_id HAVING
COUNT(DISTINCT tag) = N)` — AND semantics across all tags through a
single subquery, so the planner narrows on the indexed prefix-value
columns before the lexical/semantic ranking runs.

Wired into six store methods:

- `list_refs(tags=...)` — list view filter.
- `count_refs(tags=...)` — paginated headers.
- `search_refs_lexical(tags=...)` — title search.
- `search_blocks_lexical(tags=...)` — block lex search.
- `search_blocks_semantic(tags=...)` — pgvector cosine search.
- `search_blocks_fused(tags=...)` — RRF, applied to **both** CTEs
  (a regression test pins this — fusing a filtered against an
  unfiltered set defeats the filter).

`list_refs` and `count_refs` were also re-aliased to `r` for
consistency with the rest of the search surface; this is a private
breaking change but everything internal to the package was updated
in the same edit.

### Validation: `Tag.normalize_filter`

New classmethod on `Tag` that runs `parse_strict` over each tag and
returns canonical-form strings. Same rejection set as `put(tags=)`:
unknown closed-vocab values (`STATUS:bogus`) and bare flags that
collide with a closed value (`'urgent'`) raise `BadInput` with the
canonical alternative in the next: hint.

Surfaced in:

- `PaperHandler.search(tags=...)` — block search with topic filter.
- `NumericRefHandler.search(tags=...)` — covers memory, todo,
  gripe, fc, conv, quest by inheritance.

Empty result responses on numeric-ref kinds now mention the active
filter in the body so an agent that wrote `tags=['topic-typo']`
sees why the search returned nothing.

### `untags=` parameter on put

Closed-prefix overwrite already handled "switch STATUS to done"
via `tags=['STATUS:done']`, but there was no path to *remove* a
lowercase or bare tag once set. Added on `NumericRefHandler.put`:

```python
put(kind='memory', id=48, untags=[
    'topic:co2-capture',  # remove this lowercase tag
    'star',               # clear the flag
    'STATUS:done',        # remove only if STATUS is currently 'done'
])
```

- **Value-matched** for closed prefixes (`STATUS:open` against a
  `STATUS:done` ref is a silent no-op — same shape as a SQL
  DELETE finding zero rows).
- **Idempotent** — removing a tag that isn't there is a no-op.
- **`STATUS:` empty form rejected** at parse time, so removing
  "any STATUS regardless of value" is impossible by accident.
- **Rejected on create** (`id=None`) — there's nothing to remove
  from yet; raises `BadInput`.
- Same `Tag.parse_strict` validation as `tags=` — bare-flag
  collisions and unknown closed-vocab values raise the same
  `BadInput` shape they do on the write path.
- The "at least one of" check on update was widened to accept
  `untags=` alone as a sufficient update.

Sub-class `_remove_tags(ref_id, untags)` mirrors `_apply_tags`, so
every NumericRef kind picks up the parameter without per-class
wiring.

### Documentation

- `precis-tags`:
  - "Remove tags" section added with value-matched semantics.
  - "Filter by tags" section added with AND semantics, perf note,
    validation-error reference.
  - `applies-to` updated to `put (tags=, untags=) and search (tags=)`.
  - "Not yet implemented" pruned to the genuinely-deferred items
    (list-view-level filter exposure; block-level positional
    tag filter).

### Tests

- `tests/test_paper_toc_metadata_rejection.py` — 53 tests covering
  the metadata rejection vocabulary, length cap, DOI/URL anywhere,
  H1/H2/MD-H1/MD-H2 paths, the synthetic 357-of-460 regression,
  and the metadata-only-paper "no fake headings" check.
- `tests/test_search_tag_filter.py` — 24 tests across
  `build_tag_filter` unit tests, store-level (`list_refs`,
  `count_refs`, `search_refs_lexical`, `search_blocks_lexical/
  semantic/fused`), handler-level validation, and the ref-vs-block
  pos-boundary regression.
- `tests/test_untags_on_put.py` — 12 tests across removal flows,
  closed-prefix value matching, idempotency, validation symmetry
  with `tags=`, and the create-path rejection.

### Out of scope this pass (still queued)

- **Cross-kind / comma-list search.** Real feature, runtime
  rejects with a precise hint today.
- **Slug dedupe one-shot.** It's an `.acatome` bundle-file
  workflow, not a runtime fix; tracked for the bundle-repo
  session.
- **Block-level (positional) tag filtering** end-to-end. The
  helper has the `block_level=True` path and the schema supports
  it, but no handler writes block-level tags or surfaces a kwarg
  to filter on them. Picked up when a real consumer needs it.
- **`tags=` on agent-facing `get(kind=K)` list views.** Already
  exposed at `Store.list_refs(tags=...)`; piping through the list
  view path is a small extension once the right handler surface
  decides which list-views accept which kwargs.

## MCP critic pass — surface honesty + tag validation

Tightened the agent-facing surface in response to the April 2026
MCP critic findings (2 CRITICAL, 11 MAJOR, 4 MINOR). Documentation
no longer over-promises; the runtime no longer silently accepts
invalid tags.
**712 → 730 tests green, 1 skip.**

### Honest scoring + clearer errors

- **Search render drops misleading `score=`.** RRF fused scores are
  rank-based by construction (`1/(k+rank_lex) + 1/(k+rank_sem)`) and
  the same staircase appears for every query. List position is the
  only honest relevance signal, so we now render
  `## 1. <slug>~<pos>` — no numeric score.
- **`move` with no implementing kind** now returns
  `Unsupported: no active kind currently supports move` with a
  pointer to `put`. Previously the error looked like a per-kind
  quirk and prompted retry-loops on small models.
- **Cost trailer dedup.** The runtime no longer prepends `— cost: `
  to the handler's already-bracketed `[cost: ~$0.0020]`, so we no
  longer emit the double-`cost:` form
  (`— cost: [cost: ~$0.0020]` → `[cost: ~$0.0020]`).
- **Cross-kind search hint** now enumerates every kind whose spec
  has `supports_search=True` rather than the hard-coded `<one of:
  calc>` placeholder. Comma-list kinds (`paper,memory`) are caught
  with a precise `comma-list kind not supported` error.

### Paper handler

- **`view='cite/bib'` ⇄ `view='bibtex'` symmetric.** The id-path
  form (`id='slug/cite/bib'`) and the kwarg form
  (`view='cite/bib'`) now share an alias map. `cite/ris` and
  `cite/endnote` likewise.
- **`view='abstract'` strips `<jats:*>` namespace tags** before
  render, so abstracts read as clean prose instead of leaking
  `<jats:title>Abstract</jats:title><jats:p>…</jats:p>`.
- **Paper list view paginates and shows the total.** Caps at 50,
  appends `(50 of N)` for larger corpora, ends with a Next: trailer
  pointing to `search` and `get(id='<slug>')`.
- **Slug minter strips glued-initials prefix.** `A.Clark` no longer
  produces `aclark1998extended`; the leading run of single-letter
  dotted segments is treated as initials and dropped, so the slug
  is `clark1998extended`. `St.Pierre` and other multi-letter
  prefixes are preserved.
- **`Store.count_refs(kind=, provider=)`** primitive added so
  paginated list views can render `(N of M)` without a second
  unbounded `list_refs` call.

### Tag validation

- **`Tag.parse_strict`** rejects two classes of bad input loudly
  with a `BadInput` carrying the canonical alternative:
    - **Unknown closed-vocab values** (`STATUS:bogus`) — error
      lists the valid set.
    - **Bare flags that collide with closed-vocab values**
      (`'urgent'`) — error suggests `'PRIO:urgent'`.
- Closed vocabularies registered in `store.types`:
    - `STATUS`: `open / doing / blocked / done / won't-do`
    - `PRIO`: `low / normal / high / urgent`
    - `SRC`: `primary / secondary`
    - `CACHE`: `fresh / stale / pinned`
- `_numeric_ref._apply_tags` calls `parse_strict` for every
  user-supplied tag, so all NumericRef kinds (memory, todo, gripe,
  fc, etc.) enforce the discipline uniformly.
- The previous permissive `Tag.parse` is preserved for internal
  call sites that aren't agent-driven.

### Numeric-ref list trailers

- `_list_view('recent')` now emits a Next: trailer in both the
  populated and the empty case, so `get(kind='memory', id='/recent')`
  no longer trails off silently. Empty-state hint points at
  `put(kind=K, text='...')` to populate.

### Documentation

- **`precis-overview`** dropped the four fictional kinds
  (`clock`, `rng`, `plot`, `ask`); renamed `ask` to the actual
  three Perplexity kinds (`websearch`, `think`, `research`); fixed
  the `wang2020state` example to a slug that's actually ingested
  (`abazari2024design`); softened the `move` row to "reserved";
  rewrote the cross-kind search paragraph to match runtime
  behaviour; dropped the unsupported `due='friday'` put example.
- **`precis-todo-help`** STATUS vocabulary aligned with runtime
  (`open` not `active`); `view='today'` / `'overdue'` etc. removed
  (not implemented); `due=` / `untags=` / `rel=` examples replaced
  with the `link='target_id:relation'` form actually accepted by
  the schema.
- **`precis-tags`** STATUS vocabulary aligned; `DENSITY:` /
  `CONFIDENCE:` removed from the closed list (not registered);
  validation errors documented with example messages; "Not yet
  implemented" section flags `tags=` filter on search and
  `untags=` on put.
- **`precis-paper-help`** all `wang2020state` examples replaced
  with `abazari2024design`; symmetric view-alias semantics
  documented; honest description of search ordering (no numeric
  score); JATS strip noted.
- **`server.py`** `move` tool docstring says "No active kind in
  this build implements `move`" so the schema-level description
  matches the runtime behaviour.

### Tests

- New `tests/test_mcp_critic_regressions.py` (18 tests) — one per
  fix, named to match the critic's labels so future failures
  point straight at the relevant CHANGELOG line.

### Deferred (acknowledged but not implemented this pass)

- **Cross-kind / comma-list search.** Real feature, not just a
  doc fix; the runtime now rejects with a precise hint and the
  docs no longer claim the default behaviour exists.
- **`view='today'` / `'overdue'` etc. on todo.** Needs a real
  due-date field. Removed from the docs; users can still tag
  `due:2026-05-01` lowercase and post-filter.
- **`due=` / `untags=` / `rel=` parameters on put.** Removed from
  the docs; users compose state via `tags=['STATUS:done']` and
  `link='target:rel'` against the documented schema.
- **`tags=` filter on search.** Removed from the docs; fetch a
  list view and filter client-side until the parameter lands.
- **Paper TOC section detector improvements.** The critic flagged
  one paper where 357 of 460 blocks landed under a single
  "DOI: …" pseudo-section heading. Heuristic work; deferred.
- **Slug dedup tool** (`precis jobs dedup-paper-slugs`). The
  minter no longer produces the bug; existing duplicate slugs in
  production corpora need a separate one-shot pass.

## Perplexity polish (`/recent`, imported badge, bulk CLI, no-key usability)

Follow-up to the import flow. Imports are now first-class for Pro
subscribers who don't have an API key at all.
**699 → 712 tests green, 1 skip.**

- `get(kind=<perplexity>)` with no `id=` (or `id='/'` / `id='/recent'`)
  renders a newest-first listing of up to 20 cached refs of that
  kind, showing slug, title, provenance (`imported` vs `fetched`),
  and date. `id_required=False` on all three KindSpecs to reflect it.
- `requires_env=("PERPLEXITY_API_KEY",)` dropped from all three
  specs. Imports, `/recent`, and cache hits all work without a
  key; only a cache-miss `get` still raises `Upstream` when the
  key is absent. Previously users had to set a dummy
  `PERPLEXITY_API_KEY` just to unhide the kind.
- `_cost_str` override: cache hits whose `cache_state.meta.source`
  is `"imported"` render as `[cost: free — imported]` so agents
  can tell user-supplied bodies from API-cached ones at a glance.
- New CLI: `precis jobs import-perplexity <dir>`. Walks a directory
  of markdown reports and bulk-calls `put(mode='import')` for each.
  `--kind` selects the tier, `--query-from h1|filename` picks the
  id-derivation heuristic (H1 heading with filename fallback, or
  always filename), `--dry-run` previews the derived queries.
- Updated `precis-perplexity-help` skill with the new verbs,
  listing view, and CLI.
- 13 new tests across `tests/test_perplexity.py` and
  `tests/test_cli.py`: empty-state listing, mixed provenance
  listing, per-kind scope isolation, bad-view typo rejection,
  listing-without-API-key, imported-badge on hit, fetched keeps
  `cached` badge, CLI dry-run with H1 + filename-fallback, CLI
  filename-strategy override, CLI end-to-end write, CLI
  empty-file handling, CLI missing-dir handling.

## Perplexity import (`put(mode='import')`)

Pro subscribers can run deep research in the Perplexity web UI for
free, paste the result into precis, and have it land in the *same*
cache row a paid `get` would have produced. Future `get` on the same
query then returns the imported body for $0.
**555 → 565 tests green, 1 skip.**

- `_PerplexityBase.put(id=<query>, text=<report>, mode='import')` —
  validates inputs, parses the body via the existing `parse_markdown`
  splitter (so reports become per-heading / per-paragraph / per-list
  blocks with stable content-derived slugs), embeds the blocks via the
  active `Embedder` if one is configured, and calls
  `Store.put_cache_entry(provider='perplexity', cost_usd=0,
  ttl_seconds=None)`.
- The cache key matches what `get` would compute (`<model>:<query>`)
  so the import populates the row a future paid call would hit. Both
  `refs.meta.source` and `cache_state.meta.source` are set to
  `"imported"` for provenance.
- `WebsearchHandler` / `ThinkHandler` / `ResearchHandler` flip
  `supports_put=True` and advertise `modes=("import",)`. Each kind
  imports under its own model — so the same `id=` imported under
  `research` and `websearch` lives in two distinct cache rows by
  design.
- `_PerplexityBase.__init__` accepts an optional `embedder=`; the
  registry passes the active embedder through. With no embedder
  configured (e.g. stateless test runs) imports still land but
  without per-block vectors.
- New skill: `precis-perplexity-help` — documents the three Sonar
  tiers and the import flow side by side.
- 10 new tests in `tests/test_perplexity.py` cover: import → cache
  hit at $0; multi-block parsing; import without embedder; idempotent
  re-import (replace, not duplicate); per-kind cache isolation;
  `meta.source='imported'` provenance; mode/id/text validation;
  imported blocks are findable via fused block search.
- No DB schema change. No new kinds. No new env vars. No CLI changes.

## Phase 6a — Markdown file handler

The first file-backed kind. Read and edit `.md` files under a
configured root with the same four verbs every other kind uses.
**450 → 520 tests green, 1 skip.**

- `precis.utils.md_parse` — pure-logic markdown splitter. Recognizes
  ATX headings (1–6), fenced code (` ``` ` / `~~~`), pipe tables
  (with separator row), ordered + unordered lists, paragraphs.
  Thematic breaks are dropped; blank lines separate blocks.
  Per-block slugs are content-derived: heading slugs from the
  heading title (`# Hello World` → `hello-world`), other-kind slugs
  from `<5 leading words>-<6 hex>`. Stable across re-ingest.
- `precis.utils.md_parse.file_slug_from_path` — encodes a relative
  file path as a ref slug (`notes/meeting.md` → `notes--meeting`).
  `--` is the segment separator; segments are normalized to
  lowercase a–z 0–9 `_` `-`. `is_valid_file_slug` enforces this on
  every call (defence-in-depth against path traversal even though
  the handler also resolves+checks against the configured root).
- Migration `0004_file_kinds.sql` registers `markdown`, `plaintext`,
  `rmk`, `docx`, `tex` in the `kinds` table (only `markdown` has a
  handler in this session — others queue for phase 6b).
- `precis.config.PrecisConfig.markdown_root` (env:
  `PRECIS_MARKDOWN_ROOT`). The handler is hidden when unset.
- `MarkdownHandler` (slug-addressed, supports get/search/put):
  - `get(id='slug')` — overview + flat heading list (H1 + H2)
    + `Next:` hint trailer.
  - `get(id='slug~SLUG')` — one block by stable slug.
  - `get(id='slug~N')` — one block by 0-indexed pos.
  - `get(id='slug/toc')` — full hierarchical TOC (reuses
    `_paper_toc.build_toc` + `render_toc`).
  - `get(id='slug/raw')` — full source text.
  - `get()` / `get(id='/')` — index of every `.md` file under root.
  - `search(q='...', scope='slug')` — block-level fused-search
    (lexical + vector if embedder).
  - `put(mode='create', id='slug', text=...)` — create new file.
  - `put(mode='append', id='slug', text=...)` — append paragraph.
  - `put(mode='replace', id='slug~SLUG', text=...)` — rewrite one
    block in place.
  - `put(mode='delete', id='slug~SLUG')` — drop one block.
- **Lazy re-ingest**: every `get` first stats the file. If
  `meta.mtime_ns` matches, the cached blocks are served. If mtime
  differs but sha256 matches, only meta is bumped. If sha256
  differs, the file is re-parsed and blocks are atomically replaced.
  Block slugs survive across re-ingest (content-derived). Deleted
  files trigger soft-delete of the ref so the index stays clean.
- **Atomic writes**: every put writes via tmpfile + `os.replace`.
  After write the handler force-re-ingests so the next get sees
  the new state.
- **Path-traversal safety**: ref slugs are validated by
  `is_valid_file_slug`; the resolved path is checked to be under
  the configured root with `Path.relative_to`.
- CLI: `precis jobs ingest-md <root> [--force]` — pre-warm a
  directory (the handler ingests lazily on first `get` anyway, but
  pre-warming is useful before launching long-running searches).
- 70 new tests across 2 files: `test_md_parse.py` (37) covers
  the parser + slug helpers; `test_markdown_handler.py` (33)
  covers handler get/search/put/lazy-reingest end-to-end.
- Skill: `precis-markdown-help.md` documents address shapes,
  block kinds, put modes, CLI usage, and limits.
- Live verification: created `/tmp/precis-md-demo/` with two files,
  ingested, walked the TOC, edited a block via `put(mode=replace)`,
  appended a paragraph, created a new file. All atomic, all
  reflected on next `get`.

## Phase 5 — State kinds (todo, gripe, fc, quest, conv, oracle, skill)

The bulk of the agent-facing API for personal state. Six new kinds
plus the shared base that finally makes adding a new ref kind trivial.
**447 tests green, 1 skip.**

- `precis.handlers._numeric_ref.NumericRefHandler` — extracts the
  shared CRUD shape (get / search / put-create / put-update /
  delete / list-recent) that MemoryHandler had grown organically.
  Subclass contract is tiny: `spec`, `kind`, `sense`,
  `default_tags_on_create`, optional `_render_one` /
  `_render_search_hit` / `_list_view` / `_render_create_ack`.
- `precis.handlers.memory` — refactored to a 30-line subclass of
  the new base. All 20 memory tests still green.
- `TodoHandler` — STATUS:open default-on-create; status transitions
  via closed-prefix tag replacement (STATUS:doing supersedes
  STATUS:open atomically); `/open`, `/doing`, `/blocked`, `/done`,
  `/queue` list views; aligned Next: trailers on every view.
- `GripeHandler` — minimal numeric-ref kind. No default tags,
  free-form body. Lexical search.
- `FlashcardHandler` (`fc`) — knowledge statements with SM-2 review
  state in `ref.meta`. `/due` view surfaces cards whose
  `next_review` is in the past plus an "upcoming within 3 days"
  block. The actual SM-2 grader is deferred until the review-feedback
  agent surface lands.
- `QuestHandler` — slug-addressed work-queue kind with auto-mint:
  `put(text=...)` derives a slug via `slug_from_text`, appends
  `-2`/`-3` on collision. Same STATUS: vocabulary as todos. `/open`
  / `/doing` / `/blocked` / `/done` filters.
- `ConversationHandler` — read-only durable transcripts; one block
  per turn. Three views: overview (`slug`), full transcript
  (`slug/transcript`), single turn (`slug~N`). Block-level
  fused-search via `slug` scope.
- `OracleHandler` — slug-addressed authoritative reference nodes
  (e.g. saved rubrics, prompts). Read-only in phase 5; future `put`
  adds versioning.
- `SkillHandler` — markdown skills served from
  `precis.data.skills` package data via `importlib.resources` (so
  it works from a wheel). `get(kind='skill')` lists every skill with
  its title; `get(kind='skill', id='precis-overview')` returns the
  raw markdown; `search(kind='skill', q='...')` does case-insensitive
  full-text search across all skills. Front-matter `title:` is
  surfaced in the index. Read-only by design — skills are versioned
  with code.
- 51 new tests across 3 files: `test_todo.py` (16), `test_state_kinds.py`
  (24), `test_skill.py` (11).

## Phase 4b — Perplexity Sonar trio

Three new cache-backed kinds sharing one shared base. **396 tests
green, 1 skip.**

- `precis.handlers.perplexity._PerplexityBase` (subclass of
  `CacheBackedHandler`). Subclasses set `model`, `timeout`,
  `cost_per_call_usd`, `ttl_seconds`, and an attribution string.
- `WebsearchHandler` — `sonar`, 30s timeout, 7-day TTL,
  ~$0.001/call.
- `ThinkHandler` — `sonar-reasoning-pro`, 120s, 30-day TTL,
  ~$0.005/call.
- `ResearchHandler` — `sonar-deep-research`, 600s, **pinned**
  cache (these cost ~$0.50 each — never expire automatically),
  ~$0.50/call.
- Cache key is `<model>:<query>` so the same prompt under different
  tiers never collides on the `(provider='perplexity',
  request_hash)` unique index.
- Per-Perplexity-ToS attribution: every response carries a footer
  noting AI generation, model used, citations are not primary
  sources, and ToS disclosure requirements.
- Cache-hit Next: trailer suggests the next tier up
  (websearch → think → research) and a deep-link to fetch the
  first cited URL via `kind='web'` for primary-source verification.
- Migration `0003_perplexity_kinds.sql` registers the three kinds
  in the `kinds` table.
- 23 new tests with mocked httpx + env. All HTTP error cases
  (401/429/5xx/timeout/network) map to the correct `Upstream`
  variants.

## Phase 4a — Cache-backed kinds (math, youtube, web)

Three new kinds plus the shared infrastructure they need. 331 tests
green, 1 skip.

- Migration `0002_cache_providers.sql` adds the `web` provider row
  (others ship in 0001).
- `Store.get_cache_entry(provider, request_hash)` and
  `Store.put_cache_entry(...)` — atomic ref + `cache_state` upsert,
  hard-replaces existing refs with the same kind+slug so re-fetches
  cleanly cascade away stale blocks.
- `CacheBackedHandler` base in `handlers/_cache_base.py`. Shared
  cache flow: hash → lookup → freshness check → fetch-on-miss →
  attribution footer → cost trailer. Subclass contract is small:
  `provider`, `ttl_seconds`, `attribution`, `corpus_slug`,
  `_canonical_key`, `_fetch`. `FetchResult` dataclass wraps the
  upstream result.
- `MathHandler` (Wolfram Alpha): hand-rolled httpx GET to bypass two
  upstream `wolframalpha` library bugs (asyncio.run-in-loop, strict
  Content-Type assertion). Pod → markdown formatter ported from v1.
  Per-query deep-link + paste-ready academic citation appended to
  attribution. Cache pinned (results deterministic).
- `YouTubeHandler`: cache key is the bare 11-char video id, so URL
  variants (youtu.be / watch?v= / shorts / embed / live / mobile)
  collapse onto one row. Language preferences are part of the key
  (en/es cache separately). `view='languages'` side query lists
  available tracks. 30-day TTL.
- `WebHandler`: page-fetch mode. Canonical URL is the cache key
  (drops tracking params, default ports, fragments on non-SPA hosts).
  Article extracted with trafilatura → markdown body. 7-day TTL.
  Phase 4a ships fetch-mode only; bookmark mode + Wayback deferred
  to phase 4b.
- `precis.utils.url` ports v1's URL canonicalization
  (`canonical_url`, `slug_from_url`, `is_http_url`, `host_of`).
- All three kinds wire into the registry behind a try/ImportError
  guard: missing optional dep (`[external]` extra) silently hides
  the kind without breaking server startup.
- Skill drafts: `precis-math-help.md`, `precis-youtube-help.md`,
  `precis-web-help.md`.

## Phase 3.5 — Navigation parity

The user-facing navigation that made v1 distinctive, restored. **373
tests green, 1 skip.**

- `precis.utils.next_block` — `format_next_block` and
  `render_next_section` helpers. Column-aligned `(call, description)`
  pairs with em-dash separators; the formatter is shared across all
  handlers that emit `Next:` trailers.
- `precis.handlers._paper_toc` — heading detection (acatome
  `■ **NAME**` / `**Name**` / markdown `# Name` / `## Name`), section
  grouping, range-scoped clipping for drill-down, hierarchical
  rendering. Pure logic; no DB dependency.
- `PaperHandler.get(view='toc')` now produces a structured jump table
  with section/subsection ranges, block counts, indented children, and
  a "Next:" trailer pointing at the largest section to drill into.
  Replaces the flat "block 0 / block 1 / block 2 …" listing.
- `PaperHandler` accepts the combined drill-down id form
  `slug~A..B/toc` — TOC scoped to that range. Recursive: each child
  section is itself addressable.
- Aligned `Next:` trailers added to every PaperHandler view:
  - **overview**: TOC, first chunks, BibTeX, scoped search
  - **chunks**: next/previous range (sized to match the current
    range), full TOC, range-scoped TOC, BibTeX
  - **TOC**: drill into largest section, read largest section, BibTeX
- Live verified against the real `acheson2026automated` paper (177
  blocks → 20 detected sections; METHODS has 4 H2 children; RESULTS &
  DISCUSSION has 2). Drill-down to `~74..116/toc` correctly clips to
  just RESULTS & DISCUSSION + its children.

## Phase 3 — Paper kind + bundle ingest

End-to-end paper handling: ingest from `.acatome` bundles, hybrid block
search, citation views, CLI cutover commands. 216 tests green.

- `utils/slug.py`: deterministic `<surname><year><word>` minter with
  collision suffixing; pure logic, no DB
- `embedder.py`: `Embedder` Protocol, `MockEmbedder` (deterministic,
  used by all unit tests), shell `BgeM3Embedder` for the optional
  sentence-transformers backend
- `Store` block CRUD: `insert_blocks`, `get_block`,
  `list_blocks_for_ref`, `count_blocks`, `update_block_density`,
  `update_block_embedding`, `blocks_missing_embeddings`
- `Store` block search: `search_blocks_lexical` (tsvector +
  `ts_rank_cd`), `search_blocks_semantic` (pgvector cosine),
  `search_blocks_fused` (RRF, k=60, falls back to lex-only when no
  query vector supplied)
- `ingest.py`: bundle parsing, density classifier, embedding fill,
  slug minting glue
- `Store.ingest_bundle()`: idempotent on DOI; reuses bundle vectors
  when dim matches, re-embeds otherwise; applies `SRC:bundle` tag and
  density tags per block; one transaction per bundle
- `PaperHandler`: slug-addressed read-only kind. `get(id=slug)`
  overview, `id=slug~N` / `id=slug~N..M` chunk selectors,
  `id=slug/cite/bib`/`/abstract`/`/toc` view paths, `view='bibtex'`
  /`'ris'`/`'endnote'`/`'abstract'`/`'toc'` kwargs.
  `search(q=…, kind='paper', scope=slug)` block-level RRF search
- CLI: `precis migrate [--dry-run] [--database-url …]`,
  `precis jobs ingest-bundle <file>`,
  `precis jobs ingest-bundles <dir> [--dry-run] [--limit N]`
- `docs/v2-cutover.md`: ops runbook for the v1 → v2 switch

## Phase 2 — DB backbone (sync, psycopg 3) + memory handler

End-to-end ref-backed kind via local postgres. Sync top-to-bottom below
FastMCP. 88 tests green.

- `psycopg[binary,pool]` 3.2; pgvector codec via `pgvector.psycopg`
- `Store` (sync): corpus, ref CRUD, tag CRUD, system settings
- `Migrator`: forward-only SQL migrations with sha256 checksum guard
- `MemoryHandler`: first ref-backed kind. Numeric id, get/search/put,
  closed-prefix tag replacement
- Schema fixes: renamed `symmetric` → `is_symmetric` (postgres reserved
  word); `pos = -1` sentinel for ref-level (PK/UNIQUE without partial
  indexes)
- `tests/conftest.py` ephemeral-DB fixture (no docker, no testcontainers)

## Phase 1 — Walking skeleton (4 verbs + calc + HintBus)

End-to-end MCP server with one stateless kind. No DB. 39 tests green.

- `errors.py`: `PrecisError` hierarchy with `next=` breaking hint
- `hints.py`: `HintBus` contextvar collector, dedup with cooldown ring
- `runtime.py`: `PrecisRuntime` verb dispatch + error rendering
- `server.py`: FastMCP stdio server exposing `get/search/put/move`
- `cli.py`: `precis serve | migrate | jobs`
- `handlers/calc.py`: sympy-backed stateless calculator

## Design artefacts (pre-phase-1)

Ground-up rewrite. v1 history preserved in `main` branch upstream and on
the `v1-local` git remote. Breaking redesign — nothing wire-compatible
with v1.

- Schema: `src/precis/migrations/0001_initial.sql`
- Python store interface sketch: `docs/store_sketch.py`
- Paper-ingest spec: `docs/paper_ingest.md`
- Phase-3 plan: `docs/phase3-plan.md`
