# ADR 0026 — precis-web as a sibling package consuming the handler layer

- **Status**: accepted (2026-06-13)
- **Deciders**: Reto + agent
- **Builds on**:
  - ADR 0003 — shared tool registry
  - ADR 0013 — MCP session context as env vars (`PRECIS_SOURCE`)
- **Plan artefacts**: `docs/design/precis-web-plan.md`,
  `docs/design/precis-web-build.md`

## Context

precis needed a browser surface with four aspects: managing the
hierarchical todo tree, reading corpus PDFs in-browser, calling the
seven verbs interactively, and a status dashboard. The
`precis-web-plan.md` established the architecture (FastAPI + Jinja +
HTMX, server-rendered, importing the `precis` package directly). This
ADR records the build-time decisions for the first shipped cut.

## Decision

### Sibling package + optional extra

The web surface lives in `src/precis_web/` (a second top-level package
in the same repo), pulled in by the `precis-mcp[web]` extra
(`fastapi`, `uvicorn[standard]`, `jinja2`, `python-multipart`). The
`precis web` CLI subcommand imports it lazily so a base install
without FastAPI keeps the rest of the CLI working. Rationale: single
repo for tightly-coupled edits (one PR touches handler + web), but the
heavy ASGI deps stay out of the default install and the torch-free
worker/serve images.

### Reads off the DB, writes through the handler

Page renders read structured data straight from the `Store`
(`list_refs`, `search_refs_lexical`, `fetch_refs_by_ids`, ad-hoc
summary SQL). Every mutation routes through the in-process
`PrecisRuntime.dispatch_with_status` — the same path the MCP server
uses — so the todo handler's level-gradient guard, depth check, and
STATUS vocabulary are single-sourced. No tree SQL is duplicated in the
web layer; the no-surface-drift principle from the plan holds.

### Authority via the existing `PRECIS_SOURCE` (no new mechanism)

The level-gradient guard (`handlers/_todo_guards.py`) already reads
`PRECIS_SOURCE` (ADR 0013) and classifies `web:*` as owner. The web
process sets `PRECIS_SOURCE=web:reto`; no `Hub.actor` / `PRECIS_ACTOR`
parallel mechanism was introduced (an earlier draft of this work added
one and was reverted to avoid two competing identity sources).

### No auth in cut 1

The service binds `127.0.0.1` and is reached over Tailscale; the
network boundary is the only control. An optional `auth_token` field
exists (unset = open) so a bearer check can land later without a code
change. This diverges from `precis-web-plan.md`'s bearer-token default
per Reto's explicit "show the whole thing without authentication".

### PDF streaming + `corpus_dir`

`PrecisConfig.corpus_dir` (env `PRECIS_CORPUS_DIR`, default
`~/work/corpus`) names the PDF corpus root, laid out as
`<corpus_dir>/<letter>/<cite_key>.pdf` by `precis watch`. The viewer
streams the file through `/papers/{id}/pdf` (resolved via the ref's
cite_key = `Ref.slug`), so the browser never touches the NFS mount
directly and the app stays the single network ingress.

## Consequences

- New top-level package `precis_web`; wheel ships it + its templates
  via `force-include`.
- `precis web` subcommand launches uvicorn; documented in the README.
- The web tab tests (`tests/precis_web/`) run without Postgres via a
  fake runtime/store, so CI doesn't need a DB for the surface.
- The todo-tree handler (built in parallel: `todo.py`,
  `_todo_views.py`, `_todo_guards.py`) is consumed, not extended.
- Re-parenting ("Move…") is deferred — it's the one tree mutation
  without a verb yet; cut 1's tasks tab omits drag/move per the plan.

## Alternatives considered

1. **Separate `precis-web` repo.** Rejected for cut 1: tightly-coupled
   handler/web edits would span two PRs and double the CI overhead.
   Revisit if the surface grows its own release cadence.
2. **Go through MCP instead of importing precis.** Rejected: adds a
   JSON-RPC translation hop for no gain since both surfaces ultimately
   call the same handlers (plan §"Why import precis directly").
3. **New `Hub.actor` / `PRECIS_ACTOR` identity field.** Rejected:
   `PRECIS_SOURCE` (ADR 0013) already carries caller identity; a second
   field would be a competing source of truth.
4. **SPA (React) frontend.** Rejected: the plan deliberately chose
   server-rendered HTML + HTMX to avoid a Node toolchain for a
   single-user LAN tool.
