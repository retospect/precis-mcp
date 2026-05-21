# precis-mcp — agent context

Source of truth for anyone (human or agent) evolving this package.
Read this before substantive changes.

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
  by `precis migrate` (see `src/precis/store/migrate.py`).
- **CLI entry-point**: `precis = precis.cli:main` (subcommand-driven).

## Repository shape

```
precis-mcp/
  AGENTS.md                  # this file (read first)
  README.md                  # user-facing intro
  CHANGELOG.md               # release log
  OPEN-ITEMS.md              # active backlog (gripes that survived triage)
  pyproject.toml             # build, deps, ruff, mypy
  uv.lock                    # pinned dependency graph
  src/precis/
    cli/                     # subcommand modules
    store/                   # DB pool, mixins, migrations runner
    migrations/*.sql         # schema source of truth
    handlers/                # per-kind ingest/search adapters
    ingest.py                # bundle parsing & dispatch
    embedder.py              # BGE-M3 wrapper + dim probe
    server.py                # MCP entry-point
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
6. If you altered the schema, run `precis migrate --dry-run` against a
   throwaway DB; confirm only the new file is pending and apply
   succeeds.
7. Bump version (`uv version X.Y.Z`) and add a `CHANGELOG.md` entry for
   any user-visible change.
8. Update `docs/decisions/` with a new ADR if you made a substantive
   trade-off (one new file per decision; never edit a sealed ADR).

## Definition of done (any user-visible PR)

- [ ] Plan in `docs/design/<slug>.md` exists and was reviewed.
- [ ] Decision log entry in `docs/decisions/` if a non-obvious trade-off
      was made.
- [ ] Version bumped (`uv version`) and `CHANGELOG.md` entry written.
- [ ] `uv run ruff check .` passes.
- [ ] `uv run ruff format --check .` passes.
- [ ] `uv run mypy src tests` passes.
- [ ] `uv run pytest` passes (coverage on touched modules ≥ existing
      level).
- [ ] If schema changed: a new `migrations/NNNN_<slug>.sql` exists; old
      migrations are unmodified; the migration applies cleanly to a
      fresh DB.
- [ ] If CLI surface changed: subcommand has `--help`, an integration
      test, and a line in the README.

## Don'ts

- **Don't edit sealed migrations.** Forward-only; a new file overrides.
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

- A `refs` row with `slug`, `paper_id`, `title`, `authors`, `year`.
- All known external IDs in `ref_identifiers` (DOI, arXiv, S2, PubMed,
  pdf_hash) so future ingests of the same paper via any identifier
  collapse to the same `ref_id`.
- One row per Marker block in `blocks` with `section_path`,
  `block_type`, and `text`.
- (Forward-looking) one row per chunk in `chunks` once that schema
  lands; embeddings populated lazily via the worker queue.

Idempotency: re-running `precis add` against the same input MUST NOT
duplicate rows. Conflicts are detected via `ref_identifiers` lookup; a
hit short-circuits to `inserted=False` and updates only mutable fields.

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
