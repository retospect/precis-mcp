# Codebase orientation — the shape of precis

> **Altitude: orientation.** Read this *first* to build the mental model,
> then drop to reference. This file holds **invariants and seams** — the
> shape that survives refactors — NOT present-state status (that's
> the owning package's `__init__.py` docstring) and NOT rationale (also the
> the owning package's docstring too). If a line here reads like "the current status of X,"
> it's in the wrong file. **Keep true:** update in the same commit that
> changes the *shape*; terse per `docs/conventions/llm-facing-prose.md`.
>
> **Reader:** an agent (or human) about to *edit this repo*. Internals —
> table names, worker names, ADR numbers — are the payload here; name them.
> Freshness is `git log docs/codebase.md` — no stamp to bump.

## What precis is

A **dark factory for research**: it ingests literature (papers, patents,
books), builds a queryable knowledge substrate, and runs perpetual
agent-driven work over it across a small Mac/Linux cluster. The repo *is*
an MCP server — `precis serve` exposes seven verbs over ~50 content kinds;
cluster agents operate the product through those verbs. (That product
surface is **not** a dev aid for this repo — see the "Two surfaces" note in
`CLAUDE.md`.)

## The data model in one picture

Everything is one Postgres DB (pgvector). Two ideas carry the whole model:

- **`refs` — one row per thing, discriminated by `kind`.** `todo`, `paper`,
  `patent`, `draft`, `quest`, `llm`, `gripe`, `concept`, … (~50 kinds). A
  kind is a *handler* (`src/precis/handlers/`), not a table. Relations
  between refs are typed **links** (`link` verb; e.g. reparenting via a
  reserved `parent` relation; `requested`→job) — not
  raw foreign-key columns.
- **`chunks` — the body text, append-only.** Body rows (`ord >= 0`) are
  **never mutated in place**; only `ord < 0` card variants may be
  DELETE+INSERTed by a registered synthesis pass. Derived data cascades off
  chunks and must stay consistent with them: `chunk_embeddings` (bge-m3,
  **NULL at ingest** — worker-filled), `chunk_summaries`, and
  `chunks.keywords` (KeyBERT, F20). To "update" a chunk's text you DELETE +
  INSERT so that cascade re-runs — an in-place UPDATE strands the derived
  rows. This is the single most load-bearing invariant in the codebase.
  (Body-less stubs have no chunks to cascade from; the `stub_rank` pass
  embeds title+abstract into `ref_embeddings`, keyed on `refs`, to rank
  the acquisition queue via `refs.prio` — 1=hottest.)

Schema evolves **forward-only** (dual-track baseline; see `store/migrate.py`): new
`migrations/NNNN_<slug>.sql`, never edit a sealed file; a fresh DB loads the
`migrations/baseline/schema.sql` snapshot then applies the tail.
`store/migrate.py::check_schema_at_startup` gates every long-running
entrypoint (serve/web/worker/ingest-watch/alertmanager) —
`PRECIS_SCHEMA_GATE` picks the mode: `refuse` on pending migrations
(default), `warn` for read-mostly clients of a DB another host migrates
(the session MCP sets it), `off`; warn-and-continue when the DB is
*ahead*, self-heal under `PRECIS_AUTO_MIGRATE` (advisory lock
`AUTO_MIGRATE_LOCK_KEY`), `PRECIS_SKIP_SCHEMA_CHECK` is the emergency
bypass.

## The lifecycle, end to end

```
  precis add <input>
        │  ingest/{marker,pipeline,text_chunker,db_writer}.py
        ▼
   refs row + chunks (embedding IS NULL)
        │  derived queue — workers pick up work by SQL, no blocking jobs
        ▼
   embed:bge-m3 fills chunk_embeddings ─┬─► discovery: chunks.keywords, view='toc'
                                        ├─► synthesis: cards (ord<0), findings, casts (audio)
                                        └─► search: hybrid lexical+semantic, rank-fused
        │
        ▼
   review tiers watch the whole thing (nursery SQL · structural · deep_review)
```

Autonomous work rides the **todo tree**: `kind='todo'` is a hierarchical
todo graph (strategic/tactical gradient, `auto_check` leaves, `recurring`
watches, planner coroutines) with **jobs** hanging off owner refs in two
lanes — *intent* (parent is a `todo`) and *compute* (parent is a build
artifact: derived, idempotent, content-addressed). This is "the factory."

## Subsystems (where the code lives)

| Box | Code | One-line |
|---|---|---|
| **Ingest** | `src/precis/ingest/` | input → refs + chunks |
| **Storage / model** | `Store`, handlers, `migrations/` | refs + chunks + derived cascade |
| **Supplier stock / sources** | `src/precis/supply/`, `component` handler | identity-only supplier links; single-component MCP reads live, bulk/stored reads offline; manufacturer datasheet evidence acquired by guarded worker |
| **Workers** | `src/precis/workers/` | derived-queue passes; `system` profile (every node) + `agent` profile (melchior only, `claude_inproc`) |
| **Discovery / search** | search verbs, F20 layer | keywords, `toc`, hybrid retrieval |
| **Todo tree / factory** | `todo` handler, planner, jobs | intent vs compute lanes, dispatch |
| **Review tiers** | nursery / structural / deep | `nursery` = SQL/min, only `critical` alerts |
| **Document export** | `src/precis/export/` | draft → LaTeX/PDF and docx: `walker.py` makes every shared decision (cite numbering and grouping, abbreviation first use, xref numbers, back-matter selection, the chunk walk); `latex.py` and `docx.py` are renderers that only format, checked byte-for-byte by `tests/test_export_golden.py` and kept decision-free by `tests/test_export_decisions.py` |
| **Nanopub publication** | `src/precis/nanopub/`, `nanopub_*` tables (0128) | reviewed claims → signed, OTS-anchored artifacts; append-only proof store; the ladder runs **both ways** — a `contradicts` edge demotes below the freeze line, alerts above it |
| **Flow kernel** | `src/precis_flow/` | dimension-free Hele-Shaw solve + Lagrangian tracer + regime card (`docs/backlog/se-flow-kernel.md`); numpy/scipy only, no first-party imports — se bindings stay outside. The DB-free cad → `Domain` adapter (`precis_geom.flow`) and `precis_geom.physics.evaluate(['flow'])` live in `precis_geom` (the precis-geom wheel must add `src/precis_flow` when its packaging lands); `precis_se.flow` keeps the tree-bound half (`patch:` ports, the `flow:` token, `view='flow'`) and its `FLOW_COMPUTERS` call `evaluate` |
| **Figure digitiser** | `src/precis/figdigit/` | raster crop → calibrated numbers: `axis.py` (spine/tick detection + pixel↔value fit, numpy only), `series.py` (colour-mask extraction, sub-pixel peak + sigma), `page.py` (image/PDF raster in), `propose.py` (checked vision-proposal seam + offline mode); a model only proposes, geometry verifies (`docs/backlog/figure-digitise.md`) |
| **Geom workspace kernel** | `src/precis_geom/` | DB-free builder for 3D-design workspace scripts (`docs/backlog/structure-workspaces.md`, `docs/backlog/pcb-workspace-code.md`); today hosts `precis_geom.trace`/`trace_schema` (the structure slice-2 construction tracer), `precis_geom.pcb` (the pcb builder, slice 0: `Board`/`Net`/`Part`/`Pin`/`Footprint`/`module`, `to_batch()` is the `pcb_apply`-shaped payload), `precis_geom.pcb_shape` (slice 0b: the board's 3-D shape + fit check — the ONE mechanical derivation, which `precis.pcb.export.mechanical_profile` and `precis_se.board` delegate to) and `precis_geom.flow`/`physics` (flow 4a — the Flow kernel row); trace/pcb/pcb_shape are pure stdlib, flow/physics pull numpy + `precis_flow` + the wheel's carved `precis` subset — the trusted-side importer and place/route services are later slices |

PCB selection binds raw board snapshots and instance IDs to optional Gerber
SVG targets. The shared web design-chat engine stores proposals in conversations;
PCB Apply reuses the handler and design history on one guarded connection.

Surfaces on top: the **MCP server** (`precis serve`, the 8 verbs — the
product; network transports gate on `PRECIS_MCP_TOKEN`; `PRECIS_MCP_PROFILE=command`
swaps the typed surface for one `precis(command, text=)` tool over the same
dispatcher — `tools/command_parser.py`; on this dev machine every Claude Code
session shares one streamable-http server whose PID 1 is
`mcp_supervisor.py`, which holds the port across restarts and runs
`precis serve --fd` as a child, with `mcp_liveness.py` killing a wedged one), the **CLI**
(`precis …`), the **web UI** (`src/precis_web/`, behind HTTP Basic against
`web_users` — `precis_web/auth.py`, roster via `precis users`, self-service
at `/account`: password, ORCID iD (the identity an attesting nanopub
signature is attributed to), podcast link, sign-out-by-fresh-challenge;
colours are light/dark CSS variables in `src/precis_web/static/tailwind.src.css`
behind semantic Tailwind classes, dark following the browser), the
**Discord bridge** (`src/asa_bot/`, `[asa]` extra, stdio to the server), and
the **Slack bridge** (`src/asa_slack/`, `[asa-slack]` extra) — a sibling that
routes chat turns through the ADR-0046 `dispatch()` seam (forced `Tier.BIG` +
a hard per-turn kind-allowlist). Both bridges now route through
the router seam: Discord's `claude_invoke.invoke()` has two lanes picked by
the effective `--model` — the `local` sentinel (deployed default) walks the
BIG placement chain via sync `dispatch()` in a worker thread (no event
stream, reply off the aggregated `LlmResult`), while a concrete claude id
(`/model opus`) streams via `dispatch_async` (`Tier.FRONTIER`, `on_event`
driving the live Discord progress indicator). Slack's is one blocking
`dispatch()` call.

The `/rxn` web form dispatches the rxn energetics handler and renders its
tables through the shared reader. Explicit keep stores equation inputs and
an archival ledger in an `rxn` ref; `/refs/rxn/<id>` derives the live ledger
from pinned sources. Catalyst `pathway` run records remain a separate kind.

## Package map

Subsystem architecture lives in each package's `__init__.py` module
docstring (docs/README.md); the index — import path + the docstring's
first line — is [`docs/codebase-map.md`](./codebase-map.md). Generated
locally and gitignored; if the link target is missing or stale, run
`scripts/docs-index` (stdlib-only, regenerated automatically at
session start).

## Seams — where changes concentrate

Most work lands at one of these. Each has a convention that bites:

- **Add a kind** → handler in `src/precis/handlers/` + forward migration +
  a `precis-*-help` skill + a row in the `precis-overview` kinds table.
- **Change schema** → forward migration only; regen the baseline snapshot
  at release time (`scripts/bump`), never hand-edit it.
- **Add a worker pass** → register on the derived queue; don't call
  `fill_embeddings` from the ingest path (workers own it).
- **Mutate chunk text** → DELETE + INSERT, never UPDATE (cascade).
- **Fetch an agent-supplied URL** → `safe_get`/`safe_stream`
  (`src/precis/utils/safe_fetch.py`); raw redirected `httpx` is an SSRF.

## Where to go deeper

- Present-state + why per subsystem → the owning package's `__init__.py` docstring (map above)
- Coined/overloaded terms → `docs/glossary.md`
- Why a decision is the way it is → the owning package docstring's "why" lines (history: `git log`)
- Schema → `docs/reference/schema.md` (generated from the live DB by
  `scripts/gen-schema`; conceptual sketch: `docs/reference/schema-v2.svg`)
- Conventions / workflow / DoD → `AGENTS.md`; ship workflow → `CLAUDE.md`
