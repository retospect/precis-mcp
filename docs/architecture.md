# precis-mcp — system manual

A single narrative map of the system. It is deliberately **thin and
link-heavy**: it ties the pieces together and points at the
authoritative source for each, rather than restating them (that copy
would just drift). Read this once to orient; follow the links for
depth.

> **Audience.** Humans and agents working on or with precis-mcp. For
> the *contributor* contract (conventions, workflow, definition of
> done) read [`AGENTS.md`](../AGENTS.md) first — it is canonical. For
> the present-tense map of the live subsystems read
> [`CLAUDE.md`](../CLAUDE.md).

## What it is

An [MCP](https://modelcontextprotocol.io/) server that gives
language-model agents one small, uniform API over papers, documents,
code, personal state, patents, and cached tool calls — backed by
PostgreSQL + `pgvector`, designed for small (7B-class) models. The
pitch and install live in the top-level [`README.md`](../README.md).

## The surface: seven verbs, one `kind=`

The entire tool surface is **`get` · `search` · `put` · `edit` ·
`delete` · `tag` · `link`**, discriminated by a single `kind=`
argument. There are no per-kind bespoke tools — capability unfolds
through *progressive disclosure*: each verb/kind has a help skill the
agent pulls only when it reaches for it.

- Verb mechanics + the address grammar (`slug~SELECTOR`):
  [`precis-overview`](../src/precis/data/skills/precis-overview.md),
  [`precis-get-help`](../src/precis/data/skills/precis-get-help.md) and
  siblings in [`src/precis/data/skills/`](../src/precis/data/skills/).
- Verb-surface design rationale:
  [`docs/user-facing/seven-verb-surface-migration.md`](./user-facing/seven-verb-surface-migration.md).
- Anchored edits across file kinds:
  [`docs/user-facing/edit-protocol-spec.md`](./user-facing/edit-protocol-spec.md).

## The kinds

`precis-overview` (and the synthesised `precis-help` skill, which
introspects the live registry) is the **authoritative kinds
catalogue** — the README lists only a representative sample. Broadly:

- **Ref kinds** (addressed by slug/id): `paper`, `skill`, `oracle`,
  `conv`, `markdown` / `plaintext` / `tex`, `python`, `todo`,
  `memory`, `gripe`, `flashcard`, `citation`, `finding`, `job`,
  `provenance`, `pres`.
- **Tool kinds** (stateless / cache-backed): `calc`, `math`,
  `youtube`, `web`, `websearch` / `perplexity-reasoning` /
  `perplexity-research`, `patent`.
- **Discovery kind**: `random`.

When to enable a new kind:
[`docs/conventions/kind-enablement.md`](./conventions/kind-enablement.md).
Plugin (third-party) kinds:
[`docs/user-facing/plugin-authoring.md`](./user-facing/plugin-authoring.md).

## Storage

PostgreSQL + `pgvector` is the system of record (ADR
[0010](./decisions/0010-postgres-pgvector-system-of-record.md)); raw
`psycopg 3`, no ORM. The substrate is `refs` (one row per addressable
thing) + append-only `chunks` (body blocks) + a unified `tags` /
`links` graph, with derived artifacts (`chunk_embeddings`,
`chunk_summaries`, `chunks.keywords`) filled lazily by workers.

- **Generated, always-current diagram:**
  [`docs/design/schema.md`](./design/schema.md) — a Mermaid ER diagram
  produced from the **live database** by `precis schema-doc`
  (`scripts/gen-schema`). It can't drift; regenerate rather than
  hand-edit. (The older [`schema-v2.puml`](./design/schema-v2.puml) is
  the hand-drawn *conceptual* sketch and carries a drift note.)
- **Prose + rationale:**
  [`docs/design/storage-v2.md`](./design/storage-v2.md).
- **Discovery layer (F20):** per-chunk KeyBERT keywords on
  `chunks.keywords` / `keywords_meta` (the dropped `ref_segments`
  tables' successor). Policy:
  [`docs/conventions/discovery-layer-policy.md`](./conventions/discovery-layer-policy.md).

## Intent, execution, review — the todo tree

`kind='todo'` is a hierarchical task graph (strategic → tactical →
subtask), with `meta.auto_check` wait-leaves, recurring schedules,
`PRIO`, **projects** (a strategic root owning a `meta.workspace`), and
**jobs as children of todos** (the offline-LLM substrate). Three
review tiers (`nursery` / `structural` / `deep_review`) write memory
digests. The present-tense detail lives in
[`CLAUDE.md`](../CLAUDE.md); the deep designs are
[`todo-tree-plan.md`](./design/todo-tree-plan.md),
[`finding-chase.md`](./design/finding-chase.md), and
[`dreaming.md`](./design/dreaming.md). Agent-facing:
[`precis-tasks-help`](../src/precis/data/skills/precis-tasks-help.md),
[`precis-job-help`](../src/precis/data/skills/precis-job-help.md).

## Workers

Two `precis worker` profiles (`--profile=system` everywhere,
`--profile=agent` for the LLM reviewers) plus the dream + cron
daemons drive every derived-artifact pass (embed, summarize,
chunk_keywords, chase, fetch, dispatch, sweeper, nursery, …). The
derived-queue pattern is ADRs
[0007](./decisions/0007-derived-queue-no-block-jobs.md) /
[0017](./decisions/0017-derived-queue-family.md); the live pass list
is in [`CLAUDE.md`](../CLAUDE.md).

## The web surface

`precis web` is a browsable UI (Tasks / Papers / Console /
Conversations / Status) over the handler layer — ADR
[0026](./decisions/0026-precis-web-surface.md). Source under
[`src/precis_web/`](../src/precis_web/).

## Decisions & history

Substantive trade-offs are recorded as ADRs, never deleted (obsolete
ones are superseded). Start at the index:
[`docs/decisions/README.md`](./decisions/README.md). Design plans (one
per non-trivial change, kept for history) live in
[`docs/design/`](./design/). The dated change story is the **git
history** (`git log` — there is no CHANGELOG file); the active backlog
is [`OPEN-ITEMS.md`](../OPEN-ITEMS.md).

## Map of the source

Where things live under `src/precis/` — the spine is the **seven-verb /
one-`kind=`** surface (see above), so most work is "find the handler for
a kind, or the worker for a pass."

| Path | What lives there |
|------|------------------|
| `server.py` | MCP stdio entry — a thin FastMCP wrapper around the runtime |
| `runtime.py` | server runtime; renders handler `Response`s to text |
| `dispatch.py` | handler registration + the flat verb dispatch table + service hub |
| `protocol.py` | `Handler` ABC + `KindSpec` — the contract every kind implements |
| `handlers/` | one adapter per kind (~70): get/search/put/edit/delete/tag/link |
| `store/` | DB pool, query mixins, the migrations runner |
| `migrations/*.sql` | schema source of truth — **forward-only, sealed once applied** |
| `ingest/` | Marker → chunks pipeline (papers/cfp/books/…) |
| `workers/` | background passes — `embed`, `dispatch`, `nursery`, `review`, `sweeper`, … |
| `jobs/` | job executors (`fix_gripe`, `plan_tick`, propose jobs) |
| `embedder*.py` | BGE-M3 wrapper + its HTTP service (ADR 0020) |
| `cad/` `pcb/` `structure/` | keystone-kind IR + export (ADR 0041/0042/0043) |
| `cli/` | `precis` subcommand modules |
| `utils/` | leaf helpers — `safe_fetch`, `toc`, `cluster_map`, … |
| `data/skills/` | the on-demand agent docs (`precis-*-help`) the MCP serves |
| `config.py` · `kind_gate.py` · `errors.py` · `alerts.py` · `agentlog.py` | frozen config · kind-enablement gate · exception hierarchy · alert/agentlog write sides |

`tests/` mirrors this layout.

## Map of the docs

See [`docs/README.md`](./README.md) for the directory-by-directory
index.
