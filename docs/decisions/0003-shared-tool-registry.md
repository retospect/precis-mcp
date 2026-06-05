# ADR 0003 — Shared tool registry for MCP + CLI

- **Status**: accepted (2026-05-21)
- **Deciders**: Reto + agent
- **Supersedes**: nothing

## Context

The seven-verb surface (`get`, `search`, `put`, `edit`, `delete`,
`tag`, `link`) is exposed twice in `precis-mcp`:

1. By the MCP server in `src/precis/server.py`, which registers each
   verb as a `FastMCP` tool.
2. By the CLI in `src/precis/cli/`, which currently ships verb-specific
   modules (`ingest.py`, `migrate.py`, …) that re-implement parameter
   handling.

The two surfaces drifted: a parameter added to the server-side `search`
implementation didn't always reach the CLI, and adding a new verb
required parallel edits in two places. Tests pinned each side
separately; nothing pinned that they stayed in sync.

## Decision

Introduce `precis/tools/` as the single registration point:

- `precis/tools/core.py` holds the verb implementations as plain
  Python functions.
- `precis/tools/__init__.py` registers each function into
  `TOOL_REGISTRY: dict[str, dict]` keyed by verb name. Each entry
  carries the function, docstring, signature, and parameter
  metadata extracted via `inspect`.
- `precis/tools/cli_adapter.py` consumes the registry to
  auto-generate argparse sub-parsers and to translate parsed
  arguments back into the kwargs each verb expects.
- `precis/server.py` consumes the registry to register MCP tools.
- `precis/cli/tools.py` is the CLI subcommand entry point that
  dispatches via `cli_adapter.run_tool_from_cli`.

The verb list is enforced by `tests/test_tool_registry.py` — adding
or renaming a verb fails CI loudly until both ends are updated.

## Consequences

### Positive

- One canonical list of verbs. A new verb is one entry in `core.py`
  plus one line in `__init__.py`'s registration block; both surfaces
  pick it up automatically.
- Parameter changes propagate to the CLI without manual sync.
- The CLI's argument names mirror the function signatures, so help
  text reads naturally (`--kind paper --id 123`).
- The MCP server gets a free per-tool docstring → `description` map.

### Negative

- argparse synthesis is opinionated: list parameters accept either
  comma- or space-separated values, but more exotic types (nested
  dicts, file handles) need explicit handling. The registry's
  `_extract_parameters` is a focal point that grows with each new
  type.
- The seven-verb surface is constitutional — see ADR 0002 and the
  `seven-verb-surface-migration.md` design — so additions are rare
  by design. The registry assumes that scarcity; it is *not* a
  generic plugin system.
- Future CLI-only commands (e.g., `precis add`, `precis verify`,
  `precis health` from `docs/design/storage-v2.md`) live outside
  this registry. They are not MCP verbs; they are administrative
  CLI surface. The boundary is documented in §"Scope" below.

### Scope

`precis/tools/` registers the seven MCP-level verbs **only**. CLI
subcommands that have no MCP equivalent — `migrate`, `add`, `watch`,
`worker`, `verify`, `health`, `stats` — register themselves in
`precis/cli/main.py`'s top-level dispatch as before.

The mental model:

- **Verbs** live in `tools/core.py` and are exposed via *both* MCP
  and `precis tools <verb>` CLI.
- **Commands** live in `cli/<name>.py` and are exposed via the CLI
  only. They do administrative work that would not make sense over
  MCP.

### Tests

`tests/test_tool_registry.py` pins:

- The registry exposes exactly the seven canonical verbs.
- Every verb has a callable, signature, and parameter dict.
- The CLI adapter can synthesize a parser for at least `get` and can
  translate args to a payload.
- `add_tool_parsers` registers every verb as a CLI sub-command.

The tests deliberately do *not* exercise the verb implementations
themselves — those have their own per-handler tests already
(`test_block_ingest.py`, `test_block_search.py`, etc.). The
registry tests pin only the wiring.

## Follow-ups

- Once `docs/design/storage-v2.md` lands its CLI surface (`precis
  add`, `precis watch`, `precis worker`, `precis verify`,
  `precis health`), update §"Scope" if any of those should migrate
  into the registry as MCP verbs.
- The smoke script `test_cli.py` at the repo root (the predecessor
  of this ADR's pytest suite) is removed; equivalent coverage now
  lives in `tests/test_tool_registry.py`.
