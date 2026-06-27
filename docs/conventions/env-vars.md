# Environment-variable policy

precis reads a lot of `PRECIS_*` environment variables. To keep that
surface discoverable and drift-free, every env var falls into exactly
**one of three tiers**. Pick the right tier when you add a new one.

## Tier 1 — core config → `PrecisConfig` (`src/precis/config.py`)

Durable, process-wide settings the server/worker reads at startup:
database URL, embedder selection, file roots, default tags, startup
skills, disabled kinds, …

- Add a typed field to `PrecisConfig` with a docstring naming the
  `PRECIS_*` var. pydantic-settings derives the env name from the field
  name + `env_prefix="PRECIS_"`, so a field `startup_skills` reads
  `PRECIS_STARTUP_SKILLS`.
- Read it via `load_config()` / an injected `PrecisConfig`, **not**
  `os.environ`. `PrecisConfig()` re-reads the environment on each
  instantiation, so call-time `load_config().field` has the same
  semantics as a raw `os.environ.get(...)` — there is no reason for
  deep handler/worker code to reach for `os.environ` for a Tier-1 var.
- **Guarded:** `tests/test_env_config_policy.py` fails if a Tier-1 var
  is read raw anywhere under `src/precis/` except the bootstrap zone
  (`src/precis/cli/**`, which legitimately reads env to *construct*
  config) and a small frozen grandfather list.

The separate `precis_web` service has its own `src/precis_web/config.py`
and is out of scope for this guard.

## Tier 2 — per-subsystem config (`<subsystem>.from_env()` / local reader)

Settings scoped to one subsystem that isn't loaded at server startup:
the dream agent (`PRECIS_DREAM_*`), the planner guardrails
(`PRECIS_MAX_TODO_USD`), the chase/summarize LLM endpoints, fetch-OA
tokens, etc.

- Group them in a small dataclass with a `from_env()` classmethod (see
  `precis.workers.dream.DreamConfig`) or a typed local reader
  (`precis.workers.planner_guardrails._env_float`). One reader per
  subsystem; don't scatter `os.environ.get("PRECIS_DREAM_*")` across the
  subsystem's modules.
- These are deliberately *not* on `PrecisConfig`: they belong to opt-in
  subsystems and keeping them local avoids bloating the always-loaded
  core config.

## Tier 3 — per-invocation IPC (read raw at the call site, on purpose)

Values that change per request / per subprocess and are passed as env
*because* they're a back-channel, not configuration:
`PRECIS_CURRENT_TODO`, `PRECIS_WORKSPACE`, `PRECIS_CURRENT_AGENTLOG`,
`PRECIS_CURRENT_MODEL`, `PRECIS_SOURCE`, `PRECIS_PROCESS`.

- These are set by a parent (the job runner, the MCP server) for a
  single child invocation. Reading them through a frozen startup config
  would be **wrong** — the whole point is that they vary per call.
- Read them raw via `os.environ.get(...)` at the call site, close to
  where the value is used. Keep the read small and commented.

## Adding a new env var — decision flow

1. Does it configure the whole process for its lifetime, set once at
   deploy time? → **Tier 1** (`PrecisConfig`).
2. Does it configure one opt-in subsystem? → **Tier 2** (a subsystem
   `from_env`).
3. Is it a per-request/per-subprocess hand-off from a parent? →
   **Tier 3** (raw read at the call site).

When unsure between 1 and 2, prefer Tier 2 unless the core server/worker
needs the value at boot.
