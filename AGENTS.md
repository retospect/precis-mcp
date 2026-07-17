# precis-mcp — agent context

Source of truth for anyone (human or agent) evolving this package.
Read this before substantive changes.

> **Getting acquainted — read in this order:** (1) **this file** — the
> contract: conventions, workflow, definition-of-done. (2)
> [`docs/architecture.md`](docs/architecture.md) — a thin, link-heavy
> system map + source-tree map; orient once, follow links for depth.
> (3) [`CLAUDE.md`](CLAUDE.md) — the lean session router (ship workflow +
> conventions + pointers), then
> [`docs/architecture/state-map.md`](docs/architecture/state-map.md) — the
> present-tense map of the live subsystems you're about to touch. Deep
> per-kind reference is on demand via skills
> (`get(kind='skill', id='precis-overview')` for the master kinds table +
> index). Backlog: [`OPEN-ITEMS.md`](OPEN-ITEMS.md).

## Identity

- **Project**: `precis-mcp`
- **Purpose**: PostgreSQL + pgvector backed knowledge platform exposing a
  seven-verb MCP API over papers, documents, code, personal state, patents,
  and cached web/Wolfram/YouTube tool calls.
- **Audience**: LLM agents (Claude Code, Cursor, Windsurf) and humans
  using the `precis` CLI directly for ingest, search, and curation.
- **Constraints**:
  - Forward-only SQL migrations; sealed once applied.
  - Single canonical schema (no per-tenant divergence).
  - Idempotent ingest paths (re-running an `add` skips not duplicates).
  - All tooling via `uv run`. No bare `pip` / `pytest` / `mypy`.

## Stack / toolkit

- **Language**: Python 3.11+ (CI on 3.11, 3.12, 3.13).
- **Build**: `hatchling`, dependencies via `uv` + `uv.lock`.
- **Lint/format**: `ruff` (replaces black/isort/flake8). `mypy` for types.
- **DB**: PostgreSQL ≥ 16 with `pgvector` extension. Driver: `psycopg[binary,pool]` v3.
- **Models**: BGE-M3 embedder via `sentence-transformers`, downloaded on first use.
- **MCP server**: `FastMCP` (mcp[cli] ≥ 1.0).
- **Migrations**: numbered `*.sql` files in `src/precis/migrations/`, applied
  by `precis migrate` (see `src/precis/store/migrate.py`). A fresh DB
  bootstraps from the generated `migrations/baseline/schema.sql` snapshot
  (the chain compiled to one file) and applies only the tail; existing
  DBs migrate forward as always. Regenerate the snapshot with
  `scripts/bump` / `precis db dump-schema` — never hand-edit it. ADR 0031.
- **CLI entry-point**: `precis = precis.cli:main` (subcommand-driven).

## Repository shape

```
precis-mcp/
  AGENTS.md                  # this file (read first)
  CLAUDE.md                  # present-tense map of the live subsystems
  README.md                  # user-facing intro
  OPEN-ITEMS.md              # active backlog + planned workstreams
  BACKLOG/history            # git log — there is no CHANGELOG file
  pyproject.toml             # build, deps, ruff, mypy
  uv.lock                    # pinned dependency graph
  src/precis/                # (fuller module map: docs/architecture.md)
    server.py                # MCP stdio entry — thin FastMCP wrapper
    runtime.py               # server runtime (verb dispatch)
    dispatch.py              # handler registration + flat dispatch table + hub
    protocol.py              # Handler ABC + KindSpec (what a kind implements)
    handlers/                # one per-kind adapter (~70 kinds)
    store/                   # DB pool, mixins, migrations runner
    migrations/*.sql         # schema source of truth (forward-only)
    ingest/                  # Marker → chunks pipeline
    workers/                 # background passes (embed, dispatch, nursery, …)
    jobs/                    # job executors (fix_gripe, plan_tick, …)
    embedder*.py             # BGE-M3 wrapper + HTTP service (ADR 0020)
    cad/ pcb/ structure/     # keystone-kind IR + export
    cli/                     # subcommand modules
    utils/                   # safe_fetch, toc, cluster_map, …
    data/skills/             # on-demand agent docs (precis-*-help)
  tests/                     # pytest suite (mirrors src/ layout)
  docs/
    conventions/             # how-to rules (thresholds, naming, …)
    decisions/               # ADR-style log of substantive choices
    design/                  # plan artefacts; one per non-trivial change
    user-facing/             # external docs (kind specs, ingest paths)
```

## Workflow — plan first, always

1. Read this file and any pointer in `docs/decisions/` relevant to the
   area you are touching.
2. For any non-trivial change (schema change, new CLI subcommand, new
   handler, multi-package coordination): produce
   `docs/design/<slug>.md` first. The plan is the artefact reviewers
   react to before code lands.
3. Apply `docs/conventions/thresholds.md`. If a threshold trips, stop
   and ask the user; do not push past it silently.
4. Implement. Keep edits minimal and scoped to the plan.
5. Run the full check before claiming done:
   `uv run ruff check . && uv run ruff format --check . && uv run mypy src tests && uv run pytest`
   Run the **full pytest suite in the dev container**, not the host:
   `scripts/dev pytest` (or `scripts/dev bash -lc '<full check>'`). The
   host venv is torch-free, so a host `uv run pytest` reports dozens of
   spurious missing-extra failures (`sympy`, `marker`, `lxml`, …); the
   `precis-mcp:dev` image bakes all extras and wires `PRECIS_TEST_PG_URL`.
   Host `uv run pytest` is for targeted, extra-free subsets only.
6. If you altered the schema, run `precis migrate --dry-run` against a
   throwaway DB; confirm only the new file is pending and apply
   succeeds.
7. Bump version (`uv version X.Y.Z`) for any user-visible change. The
   dated change story is the git history — there is no CHANGELOG file;
   write a clear conventional-commit message instead.
8. Update `docs/decisions/` with a new ADR if you made a substantive
   trade-off (one new file per decision; never edit a sealed ADR).

## Definition of done (any user-visible PR)

- [ ] Plan in `docs/design/<slug>.md` exists and was reviewed.
- [ ] Decision log entry in `docs/decisions/` if a non-obvious trade-off
      was made.
- [ ] Version bumped (`uv version`) and a clear commit message written
      (no CHANGELOG file — git history is the record).
- [ ] `uv run ruff check .` passes.
- [ ] `uv run ruff format --check .` passes.
- [ ] `uv run mypy src tests` passes.
- [ ] The full suite passes in the dev container (`scripts/dev pytest`;
      the torch-free host can only run extra-free subsets) with
      coverage on touched modules ≥ existing level.
- [ ] If schema changed: a new `migrations/NNNN_<slug>.sql` exists; old
      migrations are unmodified; the migration applies cleanly to a
      fresh DB.
- [ ] If CLI surface changed: subcommand has `--help`, an integration
      test, and a line in the README.

## Don'ts

- **Don't edit sealed migrations.** Forward-only; a new file overrides.
- **Don't mutate body chunks.** ``chunks`` is append-only for body
  rows (``ord >= 0``). Only ``ord < 0`` card variants
  (``card_combined`` and siblings) may be DELETEd and re-INSERTed
  by a registered synthesis pass (today: the finding-chase
  chain-snapshot pass in ``precis.workers.chase``). New code that
  needs to "update" a chunk's text must DELETE the row and INSERT
  a fresh one so the embedding/summary cascade re-runs cleanly;
  in-place UPDATE of ``chunks.text`` leaves stale ``chunk_embeddings``
  and ``chunk_summaries`` rows by construction.
- **Don't bypass `uv`.** Bare `pip`, `pytest`, `mypy` invocations are
  not reproducible.
- **Don't introduce a new top-level dependency** without an ADR
  explaining why an existing dep is insufficient.
- **Don't claim done without the smoke test in §Definition-of-done.**
- **Don't edit a decision log entry retroactively** — supersede with a
  new entry that names the predecessor.
- **Don't commit secrets.** `.env`, `*.pem`, DSNs with passwords belong
  outside the tree (use `~/.secrets/` or a secret manager).

## Ingest guarantees (consumer-facing)

A successful `precis add <input>` MUST result in:

- A `refs` row with `paper_id`, `title`, `authors`, `year` and a
  `cite_key` row in `ref_identifiers`.
- All known external IDs in `ref_identifiers` (DOI, arXiv, S2, PubMed,
  pdf_hash) so future ingests of the same paper via any identifier
  collapse to the same `ref_id`.
- One row per Marker block in `chunks` with `section_path`,
  `chunk_kind` (propagated from the Marker classifier, including
  `'references'` for bibliography blocks), `text`, and a populated
  `numerics TEXT[]` array of `<number><unit>` tokens (eV/V/A/Hz/%/…).
- Embeddings (`chunk_embeddings`) and RAKE summaries
  (`chunk_summaries`) populated lazily via the derived queue worker
  (`precis worker`). `chunk_kind='references'` rows are skipped from
  both queues via the worker's `skip_chunk_kinds` filter.
- Discovery-layer keywords (`chunks.keywords TEXT[]` +
  `chunks.keywords_meta JSONB`) populated per-chunk by the
  `chunk_keywords` worker (`precis worker --only chunk_keywords`, or
  the default round-robin). This is the F20 successor to the dropped
  `ref_segments` / `ref_segment_sentences` tables (see ADR 0018
  status note): the paper TOC view (`view='toc'`) now DP-clusters
  these keyword arrays at request time
  (`src/precis/utils/toc_db.py`) — no precomputed segment rows.

Idempotency: re-running `precis add` against the same input MUST NOT
duplicate rows. Conflicts are detected via `ref_identifiers` lookup; a
hit short-circuits to `inserted=False` and updates only mutable fields.
The `chunk_keywords` worker re-claims any chunk whose
`keywords_meta->>'version'` differs from the current
`KEYWORDS_VERSION`, so bumping that constant lazily re-derives the
whole corpus without a manual backfill.

### Watcher routing (papers / books / presentations)

`precis watch` routes by the first path component under `inbox/`:

- `inbox/papers/...` → paper pipeline (current behaviour).
- `inbox/books/...` → paper pipeline, plus `subtype:book` and
  `topic:book` open tags. (A real `book` kind may follow once
  page/chapter ToC and ISBN cascade are needed; books-as-paper
  stays the supported route until then.)
- `inbox/presentations/...` → `PresInput` → `kind='pres'` via
  `src/precis/ingest/pres.py`: one chunk per slide
  (`chunk_kind='pres_slide'`), `subtype:slides` on creation. Slide
  decks land in a separate `corpus_pres/` root so an `ls
  corpus_pres/<letter>/` listing stays useful as the paper corpus
  grows.
- Components under any `tagging/` segment become `topic:<kebab-slug>`
  open tags applied additively on both fresh-ingest and `pdf_sha256`-
  hit branches; re-dropping the same PDF under a new tagging dir
  merges tags rather than silently no-op'ing.

Flat-inbox files (no kind dir) still ingest as paper, preserving
back-compat with any files staged before the routing landed.

## On-demand pointers

- **Conventions**: `docs/conventions/`
  (start with `thresholds.md`)
- **Decisions** (ADR log): `docs/decisions/`
  (sorted by number; never delete, only supersede)
- **Plans**: `docs/design/`
  (one file per non-trivial change; obsolete plans stay for context)
- **External-facing specs**: `docs/user-facing/`
  (`paper_ingest.md`, kind-spec docs, edit-protocol-spec)
- **Active backlog**: `OPEN-ITEMS.md`
- **Historical critic review**: `docs/mcp-critic-review-2026-05-02.md`
