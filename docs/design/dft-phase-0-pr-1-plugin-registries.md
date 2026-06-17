# Phase 0 PR 1 — plugin registries for job_types and migrations

## Motivation

The `precis-dft` package (spec at
`~/.claude/plans/we-have-a-cluster-hidden-bird.md`) plugs into
precis-mcp via entry-point groups, mirroring how third-party
handler packages already plug in via `precis.handlers`. Two of the
groups `precis-dft` needs do not exist yet:

- `precis.job_types` — so a plugin can register a new `job_type`
  without modifying `src/precis/workers/job_types/__init__.py`
  or the executor's hard-coded dispatch switch.
- `precis.migrations` — so a plugin can ship its own forward-only
  SQL migrations alongside the core migrations.

Neither change has any behavioural effect on existing job_types or
migrations: built-ins keep their hard-imported registration path,
they just gain a sibling discovery path.

## Today's shape

- `src/precis/workers/job_types/__init__.py:84-101` —
  `_REGISTRY: dict[str, JobTypeSpec] = {}` populated lazily by a
  manual switch in `get_job_type(name)` (lines 87-96). `known_job_types()`
  returns the hard-coded list `["fix_gripe", "plan_tick"]`.
- `src/precis/workers/executors/claude_inproc.py:295-305` —
  `_run_one` dispatches via `if spec.name == "fix_gripe": ...
  elif spec.name == "plan_tick": ... else: _record_failure(...)`.
  Each branch calls a module-local `_run_<type>` helper that wraps
  `spec.run(...)` with status/chunk/cancel/parent boilerplate.
- `src/precis/store/migrate.py:68-157` — `Migrator(dsn,
  migrations_dir)` single-directory. `_applied_versions` reads
  `SELECT version, checksum FROM public._migrations`
  (`migrate.py:60`); `apply_all` writes
  `INSERT INTO public._migrations (version, checksum)` per file
  (`migrate.py:139-143`).
- `src/precis/migrations/0001_initial.sql:66-70` — `_migrations`
  has columns `(version, applied_at, checksum)`. No primary key,
  no plugin scoping.
- `src/precis/cli/migrate.py:42-44` — hard-coded
  `migrations_dir = Path(__file__).resolve().parent.parent /
  "migrations"`.
- `src/precis/dispatch.py:423,426-514` — `PLUGIN_GROUP =
  "precis.handlers"` and `_load_plugins(hub)` — the working
  template for both new entry-point groups.

## Design

### 1.1 `precis.job_types` entry-point group

The dispatch boilerplate around `spec.run(...)` in `_run_fix_gripe`
and `_run_plan_tick` (parent lookup, cancel polling, status tag
writes, summary chunk emission, gripe-rollback, job-failure
bubbling) is currently inside the executor module. To let a plugin
ship its own job_type, that boilerplate must be accessible without
re-implementing executor internals.

#### `JobTypeSpec.dispatch`

Add an optional `dispatch` callable field to `JobTypeSpec`
(`workers/job_types/__init__.py:29-47`):

```python
@dataclass(frozen=True)
class JobTypeSpec:
    name: str
    params_schema: dict[str, Any]
    compatible_executors: frozenset[str]
    requires: frozenset[str]
    description: str
    run: Callable[..., Any]
    validate_submit: Callable[..., str | None] | None = None
    dispatch: Callable[[DispatchContext, "JobTypeSpec"], None] | None = None
```

When present, the executor calls `spec.dispatch(ctx, spec)`
instead of going through the hard-coded `if/elif/else`. When
absent, the executor falls back to a default dispatcher that
simply invokes `spec.run(store=..., job_ref_id=...)`, records
success/failure, no parent or gripe handling. The default
dispatcher is enough for trivial test job_types but not for
fix_gripe / plan_tick / future precis-dft job_types — those declare
their own `dispatch`.

#### `DispatchContext`

A small dataclass (new file `workers/executors/_context.py`)
constructed by the executor and passed to each dispatcher:

```python
@dataclass(frozen=True)
class DispatchContext:
    store: Any
    ref_id: int
    title: str
    meta: dict[str, Any]
    # Helpers re-exported so dispatchers don't import executor internals:
    set_status: Callable[[str], None]
    append_chunk: Callable[[str, str], None]   # (chunk_kind, text)
    set_meta: Callable[..., None]              # **fields
    record_failure: Callable[..., None]        # (reason, *, gripe_rollback)
    is_cancel_requested: Callable[[], bool]
    linked_gripe_id: Callable[[], int | None]
```

The closures around the executor's existing helpers
(`_set_status`, `_append_chunk`, `_record_failure`, etc.) move
into `DispatchContext.from_executor(store, ref_id, title, meta)`.
The executor functions `_set_status`/`_append_chunk`/etc. remain
unchanged — they're now called via the ctx closures instead of
via direct module access.

#### Migration: move dispatch out of the executor

Move the bodies of `_run_fix_gripe` and `_run_plan_tick` (today in
`claude_inproc.py:308-399`) into their respective job_type
modules:

- `workers/job_types/fix_gripe.py` gains
  `def dispatch(ctx: DispatchContext, spec: JobTypeSpec) -> None`.
- `workers/job_types/plan_tick.py` gains the same.

Their loaders (`_load_fix_gripe` / `_load_plan_tick` at
`workers/job_types/__init__.py:50-79`) attach `dispatch=fix_gripe.dispatch`
when building the spec.

#### Entry-point discovery

Add `JOB_TYPE_PLUGIN_GROUP = "precis.job_types"` and a
discovery function modelled on `_load_plugins`:

```python
def _discover_job_type_plugins() -> dict[str, JobTypeSpec]:
    """Load JobTypeSpecs declared by third-party packages via the
    ``precis.job_types`` entry-point group. Failures are logged,
    never raised — one buggy plugin must not brick the worker."""
    out: dict[str, JobTypeSpec] = {}
    try:
        eps = _entry_points(group=JOB_TYPE_PLUGIN_GROUP)
    except Exception as exc:
        log.warning("precis.job_types discovery failed: %s", exc)
        return out
    for ep in eps:
        name = getattr(ep, "name", "<unknown>")
        try:
            obj = ep.load()
        except Exception as exc:
            log.warning("job_type %r failed to load: %s", name, exc)
            continue
        spec = obj() if callable(obj) and not isinstance(obj, JobTypeSpec) else obj
        if not isinstance(spec, JobTypeSpec):
            log.warning("job_type %r did not produce a JobTypeSpec", name)
            continue
        out[spec.name] = spec
    return out
```

Cached on first call via a module-level
`_plugin_specs: dict[str, JobTypeSpec] | None = None`.

`get_job_type(name)` checks: (a) `_REGISTRY`, (b) the built-in
switch (fix_gripe / plan_tick), (c) the plugin cache.

`known_job_types()` returns `["fix_gripe", "plan_tick"] +
sorted(_get_plugin_specs())`. The cache is populated lazily so the
import graph stays cheap.

#### Executor change

`workers/executors/claude_inproc.py:295-305` becomes:

```python
dispatcher = spec.dispatch or _default_dispatch
ctx = DispatchContext.from_executor(store, ref_id, title, meta)
dispatcher(ctx, spec)
```

`_default_dispatch` is a small fallback that calls
`spec.run(store=store, job_ref_id=ref_id)` and records
success/failure. Useful for tests and trivial plugin job_types.

### 1.2 `precis.migrations` entry-point group

#### Schema

New migration `src/precis/migrations/0023_migrations_plugin.sql`:

```sql
ALTER TABLE public._migrations
    ADD COLUMN plugin text NOT NULL DEFAULT 'precis';

ALTER TABLE public._migrations
    ADD CONSTRAINT _migrations_pkey PRIMARY KEY (plugin, version);
```

`DEFAULT 'precis'` backfills every existing row to the precis
plugin. The new primary key on `(plugin, version)` cannot collide
with existing data because today's `version` values are unique
within precis. Plugin migrations can reuse version numbers
(`0001_dft_kinds.sql` and `0001_initial.sql` no longer conflict).

The default is dropped immediately after the migration applies
(plugin specifies explicitly on every INSERT going forward):

```sql
ALTER TABLE public._migrations ALTER COLUMN plugin DROP DEFAULT;
```

#### `MigrationSource`

In `src/precis/store/migrate.py`:

```python
class MigrationSource(NamedTuple):
    plugin: str
    dir: Path
```

`Migrator.__init__` signature changes from
`(dsn, migrations_dir)` to `(dsn, sources: list[MigrationSource])`.
A `Migrator.discover_sources(builtin_dir: Path) -> list[MigrationSource]`
class method returns the built-in source first, then iterates
`importlib.metadata.entry_points(group="precis.migrations")`. Each
EP yields either:

- a string `"my_pkg.migrations"` (resolved to that package's
  `migrations/` directory), or
- a callable returning a `Path`.

Failure semantics mirror `_load_plugins`: a broken EP is logged
and skipped, never raised.

#### `_load_migrations` updates

`_load_migrations(source: MigrationSource) -> list[MigrationFile]`
now tags each loaded file with `source.plugin`. The
`MigrationFile` dataclass gains a `plugin: str` field.

`_applied_versions(conn) -> dict[tuple[str, str], str]` keys by
`(plugin, version)` so the integrity check at
`migrate.py:112-119` and the INSERT at `migrate.py:139-143` carry
the plugin column through.

```sql
SELECT plugin, version, checksum FROM public._migrations;
INSERT INTO public._migrations (plugin, version, checksum) VALUES (%s, %s, %s);
```

#### Apply order

Built-in source applies first (preserving existing ordering of
`0001_initial.sql` through `0022_kind_provider.sql` →
`0023_migrations_plugin.sql`), then plugin sources in alphabetical
order by `plugin`, then by `version` within each plugin. Plugin
authors guarantee their own migrations are forward-only and
self-contained (no cross-plugin dependencies).

#### CLI

`src/precis/cli/migrate.py:42-44`:

```python
sources = Migrator.discover_sources(builtin_dir)
migrator = Migrator(dsn, sources)
```

`precis migrate --dry-run` continues to work, now listing pending
migrations per plugin.

## What does not change

- Built-in handlers (`JobHandler`, etc.) still hard-import as today.
- `fix_gripe` and `plan_tick` keep their semantics, parent rules,
  failure-bubble behaviour, and gripe-rollback path. Only the
  *location* of the dispatch logic moves (executor → job_type
  module).
- `_run_one` keeps the cancel-poll-before-run invariant.
- The 30-min lease, `FOR UPDATE OF r SKIP LOCKED`, and the
  STATUS:queued/running/succeeded/failed/cancelled vocabulary are
  unchanged.
- `JobHandler.put` — same validation order (parent_id check,
  executor compat, REQUIRES coverage, params_schema). The plugin
  spec just appears in the registry the validation code already
  reads from.

## Risk and rollback

- **Refactor surface area**: moving `_run_fix_gripe` /
  `_run_plan_tick` into their job_type modules is a body-shuffle,
  not a logic change. Test coverage on these dispatchers
  exercises both code paths.
- **Plugin failure isolation**: the plugin discovery loops mirror
  `_load_plugins`'s `Exception`-swallowing contract. A broken EP
  logs a warning and never crashes boot.
- **Schema migration is additive**: `0023_migrations_plugin.sql`
  adds a column with a default + a primary key. Rollback would
  be a hand-written forward migration that drops the constraint
  and the column — kept in mind, not pre-shipped.
- **`get_job_type` cache**: lazy, populated on first call. If a
  plugin is installed after the worker starts, it will not appear
  until restart. Acceptable for v1 (plugins ship with the worker
  image).

## Tests

- `tests/workers/test_job_type_plugins.py` (new): synthesizes a
  fake entry point via `importlib.metadata` monkeypatch, asserts
  `get_job_type` resolves it and `known_job_types` includes it.
- `tests/workers/test_claude_inproc_dispatch.py` (extend): asserts
  the executor calls `spec.dispatch` when present, falls through
  to default dispatch when absent, and that `fix_gripe` /
  `plan_tick` dispatchers continue to behave as before (moved-but-
  unchanged).
- `tests/store/test_migrate.py` (extend): asserts
  `MigrationSource` ordering applies builtins first, then plugins
  alphabetically; the `plugin` column is populated correctly;
  checksum mismatch detection still fires per (plugin, version).
- `tests/store/test_migrate_plugin.py` (new): synthesizes a fake
  `precis.migrations` entry point pointing at a temp dir with one
  `0001_test.sql` file; asserts it applies after builtins and
  records as `(plugin='fake', version='0001_test')`.

## Files touched

| File | Change |
|---|---|
| `src/precis/workers/job_types/__init__.py` | Add `dispatch` to `JobTypeSpec`; add `JOB_TYPE_PLUGIN_GROUP` + `_discover_job_type_plugins`; generalize `get_job_type` and `known_job_types`. |
| `src/precis/workers/job_types/fix_gripe.py` | Add `dispatch(ctx, spec)` (body moved from `claude_inproc._run_fix_gripe`). |
| `src/precis/workers/job_types/plan_tick.py` | Add `dispatch(ctx, spec)` (body moved from `claude_inproc._run_plan_tick`). |
| `src/precis/workers/executors/_context.py` | New file: `DispatchContext` dataclass + `from_executor` builder. |
| `src/precis/workers/executors/claude_inproc.py` | Replace `if/elif/else` at 295-305 with `spec.dispatch(ctx, spec)`. Move `_run_fix_gripe`/`_run_plan_tick` bodies out (keep `_default_dispatch` as fallback). |
| `src/precis/store/migrate.py` | `MigrationSource` NamedTuple; `Migrator(dsn, sources)`; `discover_sources(builtin)`; key by `(plugin, version)` throughout. |
| `src/precis/cli/migrate.py` | Use `Migrator.discover_sources`. |
| `src/precis/migrations/0023_migrations_plugin.sql` | New: add `plugin` column + `(plugin, version)` PK. |
| `tests/workers/test_job_type_plugins.py` | New. |
| `tests/workers/test_claude_inproc_dispatch.py` | Extend. |
| `tests/store/test_migrate.py` | Extend. |
| `tests/store/test_migrate_plugin.py` | New. |
| `CHANGELOG.md` | Entry under `## Unreleased`. |
| `pyproject.toml` | Version bump (`uv version <next>`). |

## Out of scope (separate PRs)

- The `coordinator` executor and `wake_runner` (Phase 0 PR 3).
- `precis.ref_passes` entry-point group (Phase 0 PR 4).
- Idempotency advisory-lock hardening, MCP frame chunking,
  `meta.no_index` chunk filter (Phase 0 PR 2).

## Open questions

- Should `_discover_job_type_plugins` honour a `disabled_kinds`-
  style suppression list at boot, mirroring `kind_gate` in
  `dispatch.py:_try`? Not strictly required for v1; defer until a
  plugin job_type collides with an operator's policy.
- Migration ordinal `0023` is the next free slot today; confirm at
  PR time in case another change has landed since.
