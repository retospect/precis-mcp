# Codebase orientation — the shape of precis

> **Altitude: orientation.** Read this *first* to build the mental model,
> then drop to reference. This file holds **invariants and seams** — the
> shape that survives refactors — NOT present-state status (that's
> the owning package's `__init__.py` docstring) and NOT rationale (also the
> `docs/decisions/`). If a line here reads like "the current status of X,"
> it's in the wrong file. **Keep true:** update in the same commit that
> changes the *shape*; terse per `docs/conventions/llm-facing-prose.md`.
>
> **Reader:** an agent (or human) about to *edit this repo*. Internals —
> table names, worker names, ADR numbers — are the payload here; name them.
>
> _Verified @ `c5d03fdc`._

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
  reserved `parent` relation, ADR 0027; `requested`→job, ADR 0044) — not
  raw foreign-key columns.
- **`chunks` — the body text, append-only.** Body rows (`ord >= 0`) are
  **never mutated in place**; only `ord < 0` card variants may be
  DELETE+INSERTed by a registered synthesis pass. Derived data cascades off
  chunks and must stay consistent with them: `chunk_embeddings` (bge-m3,
  **NULL at ingest** — worker-filled, ADR 0007), `chunk_summaries`, and
  `chunks.keywords` (KeyBERT, F20). To "update" a chunk's text you DELETE +
  INSERT so that cascade re-runs — an in-place UPDATE strands the derived
  rows. This is the single most load-bearing invariant in the codebase.

Schema evolves **forward-only** (ADR 0005/0031): new
`migrations/NNNN_<slug>.sql`, never edit a sealed file; a fresh DB loads the
`migrations/baseline/schema.sql` snapshot then applies the tail.

## The lifecycle, end to end

```
  precis add <input>
        │  ingest/{marker,pipeline,text_chunker,db_writer}.py
        ▼
   refs row + chunks (embedding IS NULL)
        │  derived queue — workers pick up work by SQL, no blocking jobs (ADR 0007/0017)
        ▼
   embed:bge-m3 fills chunk_embeddings ─┬─► discovery: chunks.keywords, view='toc'
                                        ├─► synthesis: cards (ord<0), findings, casts (audio)
                                        └─► search: hybrid lexical+semantic, rank-fused
        │
        ▼
   review tiers watch the whole thing (nursery SQL · structural · deep_review)
```

Autonomous work rides the **todo tree**: `kind='todo'` is a hierarchical
task graph (strategic/tactical gradient, `auto_check` leaves, `recurring`
watches, planner coroutines) with **jobs** hanging off owner refs in two
lanes — *intent* (parent is a `todo`) and *compute* (parent is a build
artifact: derived, idempotent, content-addressed). This is "the factory."

## Subsystems (where the code lives)

| Box | Code | One-line |
|---|---|---|
| **Ingest** | `src/precis/ingest/` | input → refs + chunks |
| **Storage / model** | `Store`, handlers, `migrations/` | refs + chunks + derived cascade |
| **Workers** | `src/precis/workers/` | derived-queue passes; `system` profile (every node) + `agent` profile (melchior only, `claude_inproc`) |
| **Discovery / search** | search verbs, F20 layer | keywords, `toc`, hybrid retrieval |
| **Task tree / factory** | `todo` handler, planner, jobs | intent vs compute lanes, dispatch |
| **Review tiers** | nursery / structural / deep | `nursery` = SQL/min, only `critical` alerts |

Surfaces on top: the **MCP server** (`precis serve`, the 7 verbs — the
product), the **CLI** (`precis …`), the **web UI** (`src/precis_web/`), the
**Discord bridge** (`src/asa_bot/`, `[asa]` extra, stdio to the server), and
the **Slack bridge** (`src/asa_slack/`, `[asa-slack]` extra) — a sibling that
routes chat turns through the ADR-0046 `dispatch()` seam (forced sonnet + a
hard per-turn kind-allowlist); see ADR 0062. Both bridges now route through
ADR-0046: Discord's `claude_invoke.invoke()` streams via `dispatch_async`
(`Tier.FRONTIER`, `on_event` driving the live Discord progress indicator,
router-migration Phase 3) where Slack's is one blocking `dispatch()` call.

## Package map (generated)

Subsystem architecture lives in each package's `__init__.py` module
docstring (docs/README.md); this map is its index — import path + the
docstring's first line, regenerated by `scripts/docs-index` at ship time.

<!-- docs-index:begin -->

- `asa_bot` — asa-bot — Discord bridge to Claude Code + precis-mcp.
- `asa_slack` — asa-slack — Slack bridge to Asa, routed through the ADR-0046 LLM router.
- `precis.anki` — Anki integration — headless AnkiWeb sync for the `anki` cloze kind.
- `precis.backfill` — ``source-backfill`` — find corpus sources a draft *should* cite but doesn't,
- `precis.budget` — Budget guardrails — a lightweight spend backstop.
- `precis.cad` — Analytic-IR CAD kernel (ADR 0041).
- `precis.cli` — Precis CLI — ``precis serve | migrate | jobs ...``.
- `precis.data` — Read-only data files shipped with `precis` (skills, etc.).
- `precis.diagram` — The shared diagram-editing core (ADR 0057, slice 3).
- `precis.draft` — Draft-document machinery shared by the store, handlers, and web reader.
- `precis.draftimport` — Import external LaTeX documents into the ``draft`` kind.
- `precis.export` — Document export engines (LaTeX → Tier-B). ADR 0033.
- `precis.figure` — The ``figure`` kind — an interactive SVG canvas you draw *with* the model.
- `precis.fixer` — The laptop fixer loop (ADR 0048).
- `precis.format` — Output format registry — TOON, JSON, ASCII table.
- `precis.handlers` — Handlers — one adapter per kind (~70 kinds).
- `precis.ingest` — Ingest pipeline: PDF → refs → chunks → derived queue.
- `precis.jobs` — ``precis.jobs`` — single-shot maintenance entry points.
- `precis.llm_eval` — LLM golden-eval harness (slice 11) — measure a model on precis's own tasks.
- `precis.mail` — ``precis.mail`` — the email kind's IMAP/SMTP machinery.
- `precis.mermaid` — The ``mermaid`` diagram kind — a second instance of the shared diagram core
- `precis.pcb` — The PCB *eyes* (ADR 0042 §8) — pure-Python analysis over the netlist +
- `precis.python_index` — AST-based Python code indexer.
- `precis.quest` — Quest layer runtime — the striving's autonomous research loop.
- `precis.reading` — Reading-prep loop — the adaptive concept-graph study system.
- `precis.render` — Figure rendering — execute a chunk's render recipe and capture an image.
- `precis.runtime` — Server runtime — public surface.
- `precis.sim` — ``precis.sim`` — slice 1 of the sim-harness (``docs/backlog/sim-harness.md``).
- `precis.skill_index` — Embedded index over a directory of files.
- `precis.store` — Async postgres-backed store for precis V2.
- `precis.structure.importers` — The adapter registry — ADR 0053 §2's ETL seam for external DFT catalyst DBs.
- `precis.structure` — The ``structure`` kind — a legible atomistic cell + bond-graph IR (ADR 0043).
- `precis.taproot` — Taproot — the evidence-grounded claim graph.
- `precis.tools` — Shared tool registry for MCP server and CLI interface.
- `precis.tts` — Text-to-speech engines behind the :class:`precis.export.audio.Synthesizer`
- `precis.utils.llm` — The LLM routing layer (ADR 0046) — tiers, chains, catalog.
- `precis.utils.prompt` — Prompt assembler + module library (ADR 0038).
- `precis.utils` — Small pure utilities used across handlers and the store.
- `precis.workers.auto_check_evaluators` — Evaluator registry for the auto-check worker (Slice 1b).
- `precis.workers.executors` — Executors — runner classes for `job` work.
- `precis.workers.job_types` — job_type registry — what kinds of work the `job` substrate runs.
- `precis.workers.schedule` — Slice 4 schedule worker package.
- `precis.workers` — Background work: worker passes, scheduler cadences, and job executors.
- `precis_bio.migrations` — precis-bio plugin migrations.
- `precis_bio` — precis-bio — the protein / structure-prediction tool-pack (ADR 0056).
- `precis_chem.migrations` — precis-chem plugin migrations.
- `precis_chem` — precis-chem — the chemistry / protein tool-pack (ADR 0056).
- `precis_pathway.migrations` — Plugin migration root for the autocatpath `pathway` kind.
- `precis_pathway` — precis-pathway — the reaction-pathway tool-pack (bundle-pathway-in-tree
- `precis_web.routes` — Route modules — one per tab (tasks, papers, console, status).
- `precis_web` — precis_web — the cluster web surface for precis-mcp.
- `precis` — precis-mcp v8 — MCP server for paper / document / state / tool access.

<!-- docs-index:end -->

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
- Coined/overloaded terms → `docs/architecture/glossary.md`
- Why a decision is the way it is → `docs/decisions/` (ADR index in README)
- Full schema → `storage-v2` (git-only)
- Conventions / workflow / DoD → `AGENTS.md`; ship workflow → `CLAUDE.md`
