# Codebase orientation — the shape of precis

> **Altitude: orientation.** Read this *first* to build the mental model,
> then drop to reference. This file holds **invariants and seams** — the
> shape that survives refactors — NOT present-state status (that's
> `docs/architecture/state-map.md`) and NOT rationale (that's
> `docs/decisions/`). If a line here reads like "the current status of X,"
> it's in the wrong file. **Keep true:** update in the same commit that
> changes the *shape*; terse per `docs/conventions/llm-facing-prose.md`.
>
> **Reader:** an agent (or human) about to *edit this repo*. Internals —
> table names, worker names, ADR numbers — are the payload here; name them.
>
> _Verified @ `c491c9a6`._

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
product), the **CLI** (`precis …`), the **web UI** (`src/precis_web/`), and
the **Discord bridge** (`src/asa_bot/`, `[asa]` extra, stdio to the server).

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

- Present-state per subsystem → `docs/architecture/state-map.md`
- Coined/overloaded terms → `docs/architecture/glossary.md`
- Why a decision is the way it is → `docs/decisions/` (ADR index in README)
- Full schema → `docs/design/storage-v2.md`
- Conventions / workflow / DoD → `AGENTS.md`; ship workflow → `CLAUDE.md`
