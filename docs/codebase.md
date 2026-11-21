# Codebase orientation — the shape of precis

> **Altitude: orientation.** Read this *first* to build the mental model,
> then drop to reference. This file holds **invariants and seams** — the
> shape that survives refactors — NOT present-state status (that's
> the owning package's `__init__.py` docstring) and NOT rationale (also the
> owning package's docstring too). If a line here reads like "the current status of X,"
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
  `patent`, `draft`, `quest`, `llm`, `gripe`, `concept`, `machine`, …
  (~50 kinds). A
  kind is a *handler* (`src/precis/handlers/`), not a table. The primary
  key is **`ref_id`** (`bigint`); every satellite table FKs to it, so
  grepping `ref_id` floods — the concept's home is this bullet and
  `docs/reference/schema.md`, not the hit list. Relations
  between refs are typed **links** (`link` verb; e.g. reparenting via a
  reserved `parent` relation; `requested`→job) — not
  raw foreign-key columns. An edge's *why* rides `links.meta.reason`; a
  `considered` edge (scope memory/quest → a judged candidate source)
  carries a verdict block instead, and every judgment also lands an
  append-only `considerations` row (`store/_consideration_ops.py`) — the
  edge shows the latest verdict, the table keeps them all.
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

Private study activity is a non-ref exception to the corpus model:
`precis.learning` stores owner-keyed settings, append-only self-reports,
study answers and synced Anki review timestamps in dedicated tables, outside chunks, embeddings and public graph
search. `user_context` binds stable authenticated account IDs; `mcp_identity`
stores revocable per-user bearer digests. The `learning` handler and web page
share that store. `runtime.private` routes the kinds in the `private_kinds`
registry (learning and gtd today) around shared payload ledgers and continuation
caches; learning events record engagement, never concept mastery.

`eye_visits` stores each authenticated `/eye/<handle>` focus with the web
user ID, handle, kind, previous handle and UTC timestamp. The owner alone
can see the latest steps on `/learning`, which shows a 90-day window. Each
append prunes expired rows for that owner. `POST /learning/walk/clear`
deletes the owner's entire walk.

`precis.oauth` adds independently revocable user/app grants backed by the same
account IDs. Web routes own consent and token exchange; `oauth_transport`
publishes MCP resource discovery. Request identity and dispatch revalidation
keep OAuth grants separate from personal-token IDs and infrastructure callers.
Capabilities distinguish explicit full MCP access (including writes and shared
services) from private-kind-scoped connections. User identity does not create
per-document ACLs for the shared corpus.

`mcp_identity` also stores the third bearer class (gr595554): client
capability tokens (`precis_cap_`), per-consumer credentials on
`web_user_capability_tokens` — digest-only, labelled, minted and revoked at
`/account`. A presented token stamps a `CapabilityIdentity` (a consumer, never
a personal `Principal`) gated at the request boundary and again in dispatch by
its own scopes: `read` (the whole get/search surface), `read:<kind>`,
`write:<kind>`, and `feed`. The podcast `?t=` credential folded into the
class: feed mints are `feed`-scoped rows (`precis_feed_` prefix on new tokens,
legacy bare digests still authenticate), so `/podcast/*` keeps URL carriage
while MCP keeps `Authorization: Bearer` only. Dispatch re-judges the allow-list
on the final kind after every rewrite: converted `pa<id>` handles and a
retired memory's successor (`md`, `python`, another kind) alike.

A network MCP gate accepts five bearer classes: `install`, `slice`,
`personal`, `capability` and `oauth`. `PRECIS_MCP_CREDENTIAL_CLASSES`
(`mcp_identity.credential_classes`) narrows them per deployment; unset means
all. A refused class gets the same 401 as a wrong token with no lookup, and the
web account page stops offering to mint the personal and capability classes the
deployment would refuse.

Exact-handle write substitution uses one-hour ``read_receipts`` keyed on the
authenticated credential, surviving transport restarts. Unauthenticated MCP
receipts and hint suppression remain in process memory. Opt-in
``PRECIS_MCP_STATELESS_HTTP`` authenticates every request and groups ledger
calls by credential/hour; persisted ordinals are allocated under a database
lock so restart cannot reorder recall routes. Public rollout is separate.
The word-handle switch uses the live session where available. Stateless
HTTP uses a bounded process-local cache keyed by the authenticated
connector credential. Claude OAuth connections default to spoken handles
because the connector registration identifies the app across requests.


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
Headless slice jobs delegate vendor launch/output protocols to
`workers/job_types/_slice_adapters.py`; `run_slice` owns leases, credentials
and the branch check before handing work to the lander.
The detached executor records a submit intent before launching either job.
Recovery uses launch evidence from that attempt and host to resume poll,
including a ship that finished before publishing its mirror squash.
Plan dispatch queues every doable node with its score and release band.
The gateway's `20d-precis-worker-drain.yml` provisions a release-only
`job_inproc` claimant so bulk jobs cannot consume its worker slot.
`release_confirm` attests the workers' heartbeat sha and, from the
`release.mcp_served` stamp the shared session MCP writes itself
(`mcp_served_stamp.py`; store only, no token), the sha that server serves:
no live stamp is a note, a lagging one parks the probe and then files an ask.
A pre-land review job's and every `land_slice` job's score floors at
`FINISH_FIRST_SCORE`, so the review and the land of a finished build are
claimed ahead of fresh builds; at the host claim an already-queued land
without the stamp floors at the same value, so pre-rollout lands keep
their place.
Host executor claims enforce capacity and skip file overlaps with started,
unlanded builds; gripe intake bounds minting by queued builds per free
executor slot (`factory.gripe_dispatch.queue_depth`, default 2).
A todo tagged `factory:build` is the factory's second source of work:
`workers/todo_dispatch.py` (switch `factory.todo_dispatch.enabled`, off by
default; runs after the gripe pass in its scheduler fire) mints its chain
through `plans/templates.py::mint_todo_build`; `plans/todo_chain.py` holds
the tags, brief and refusal parsing and the source todo's close.
`reports.weekly` collects factory tables for the deterministic `weekly_report`
job. It shares gripe closure clocks with the flow view and replaces direct
draft-section table children atomically, preserving authored prose. Its weekly
scheduler cadence is opt-in; Git history requires a configured checkout.

Builder drift reports schedule ``task_class='skill'`` children after land;
their live-tool checks run on MCP lanes with writes confined to skills and
skill evals. Skill ``rulings:`` metadata exposes provenance through the
skill handler's ``rulings`` view.
Devin builds leave worktree edits for the worker to commit. Available
acceptance tests supplied before launch gate that commit; absent tests
fall back to the checked land. Worker commits always retain the lander's
impacted-test gate. Sandboxing is opt-in until its write scopes are verified.
Planner-declared halts carry a reason class and a full-stream retry receipt; dispatch
releases audited parks and probes shared infra outages through one alert
cohort (`workers/halt_policy.py`, `workers/halt_recovery.py`).

Memory capture (`precis-make-memory`) and graph-gardener judgments share the
packaged memory-help reconciliation policy. The interactive agent resolves new
claims before writing; the gardener evaluates existing maintenance candidates
through its reviewed apply paths. The opt-in gardener grounding pass records
claim classifications, evidence judgments and premise lineage in SHA-pinned
review rows (target, cited sources, consulted passages and premise
judgments) outside the apply queue. The pass
sends repair proposals through the existing garden ledger. It tracks
unresolved checks in bounded research or human-review todos and reads
pinned returns as candidate input on re-audit; reads fold the latest row
into claim-level coverage — the `get` footer, the fisheye header, index
bullets and `view='grounding'` — where a claim stays supported only while
its pins still match (`graph/garden/grounding.py`). Labelled memory search batches
reuse ordinary search dispatch and preserve each query's results and errors
separately.

Skill discovery fuses up to five phrasings into attributed section matches.
Planner and quest prompts use the same ranking and table after matching task
sentences against question and heading embeddings. They include the selected
section bodies. See `skill_index/__init__.py` for the index and prompt assembly.

Explicit memory processing pins current body/header chunks in a bounded
`embed_batch` job (`handlers/_memory_process.py::process_memory`). Its
opt-in worker claim path shares chunk leases with bulk embedding; default
queue order and model routes stay unchanged. Readiness requires matching
artifacts. Other enrichment stages refuse targeted requests where the worker
cannot provide a revision-specific receipt.

Paper cards draw on the publisher abstract, a labelled generated abstract,
or leading body prose. New nanopub approvals use chunk-anchored passages.
RDF and LaTeX preserve context literals already frozen into publish rows.

Learning and Anki review share `precis.reading.known` for card dismissal and
undo. `anki_known_undo` records the owner's retirement set and the prior
shared mastery fields. Undo restores the original card refs and queues their
notes for the next sync.

Anki card creation can snapshot a public image with the ref. The per-owner
Anki sync worker adds the snapshot to the mirror's media collection, embeds
its filename in the cloze note, and waits for incremental media sync.
An authenticated caller can queue the same sync for their own cards through
the `anki_sync` job type. The worker claim gate requires a configured Anki
mirror, and the job shares the daily cadence's failure backoff.

The jobs settings surface reports effective allowlisted flags. Flag updates
and reverts take a per-key transaction lock before reading the predecessor,
and commit the override and audit together. Inbound chase uses this setting
for both paper-read activation and worker admission.

## Subsystems (where the code lives)

Checklist definitions retain their registry names and allocate a graph ref
on first link. Provenance edges use the shared link graph, so paper and
memory reads expose backlinks to the checklist definition.

| Box | Code | One-line |
|---|---|---|
| **Ingest** | `src/precis/ingest/` | input → refs + chunks |
| **Storage / model** | `Store`, handlers, `migrations/` | refs + chunks + derived cascade |
| **Supplier stock / sources** | `src/precis/supply/`, `component` handler | identity-only supplier links; single-component MCP reads live, bulk/stored reads offline; manufacturer datasheet evidence acquired by guarded worker |
| **Workers** | `src/precis/workers/` | derived-queue passes; `system` profile (every node) + `agent` profile (the agent host only, `claude_inproc`) |
| **Discovery / search** | search verbs, F20 layer | keywords, `toc`, hybrid retrieval |
| **Todo tree / factory** | `todo` handler, planner, jobs | intent vs compute lanes, dispatch |
| **Review tiers** | nursery / structural / deep | `nursery` = SQL/min, only `critical` alerts |
| **Document export** | `src/precis/export/` | draft → LaTeX/PDF and docx: `walker.py` owns cite numbering/grouping, abbreviation first use, xref numbers, back-matter selection and the chunk walk; `audience.py` owns collaborator links, external record numbering and the private-URL guard, with stored evidence packaged by `supplement.py`; `latex.py` and `docx.py` format the shared decisions, checked by `tests/test_export_golden.py` and `tests/test_export_decisions.py` |
| **Nanopub publication** | `src/precis/nanopub/`, `nanopub_*` tables (0128) | reviewed claims → signed, OTS-anchored artifacts; append-only proof store; the ladder runs **both ways** — a `contradicts` edge demotes below the freeze line, alerts above it |
| **Flow kernel** | `src/precis_flow/` | dimension-free Hele-Shaw solve + Lagrangian tracer + regime card (me695207); numpy/scipy only, no first-party imports — se bindings stay outside. The DB-free cad → `Domain` adapter (`precis_geom.flow`) and `precis_geom.physics.evaluate(['flow'])` live in `precis_geom` (the precis-geom wheel must add `src/precis_flow` when its packaging lands); `precis_se.flow` keeps the tree-bound half (`patch:` ports, the `flow:` token, `view='flow'`) and its `FLOW_COMPUTERS` call `evaluate` |
| **Figure digitiser** | `src/precis/figdigit/` | raster crop → calibrated numbers: `axis.py` (spine/tick detection + pixel↔value fit, numpy only), `series.py` (colour-mask extraction, sub-pixel peak + sigma), `page.py` (image/PDF raster in), `propose.py` (checked vision-proposal seam + offline mode); a model only proposes, geometry verifies (me695362) |
| **Diagnostics core** | `src/precis_diag/` | domain-free `Diag`/`RunResult`/`Anchor`/`FrameSite`/`Status` + `render` (shared-core-layers S1a): the structured failure/check record every domain adapter emits — stdlib-only, ships in the precis-geom wheel; `precis_geom.run_diag` anchors CAD failures at the user-facing op and script call site, retaining inner frames in `anchor.chain`; a report-carrying exception (hexfold `BuildError`) yields one Diag per finding instead of the single `cad.run-failed`; `hexfold.diag` owns the reverse Finding conversion |
| **Geom workspace kernel** | `src/precis_geom/` | DB-free builder for 3D-design workspace scripts (me693174, me693071); today hosts `precis_geom.trace`/`trace_schema` (the structure slice-2 construction tracer), `precis_geom.pcb` (the pcb builder, slice 0: `Board`/`Net`/`Part`/`Pin`/`Footprint`/`module`, `to_batch()` is the `pcb_apply`-shaped payload), `precis_geom.pcb_shape` (slice 0b: the board's 3-D shape + fit check — the ONE mechanical derivation, which `precis.pcb.export.mechanical_profile` and `precis_se.board` delegate to) and `precis_geom.flow`/`physics` (flow 4a — the Flow kernel row); trace/pcb/pcb_shape are pure stdlib, flow/physics pull numpy + `precis_flow` + the wheel's carved `precis` subset — the trusted-side importer and place/route services are later slices |

Editable geometry feature graphs live in `precis_geom.design.Recipe`, with
stable feature IDs and isolated edit, snapshot and restore boundaries.
`precis_flow.slip` supplies Navier-slip gap profiles and Taylor dispersion
coefficients through the species solver and tracer hooks.

Thin-gap extensions declare their token keys, evaluate keys, input measures
and outputs in `src/precis_flow/physics.py::PHYSICS`; `TESTED` gates declared
combinations before evaluation. Card builders register through
`src/precis_flow/regime.py::REGIME_GROUPS`, and se computer import targets
through `src/precis_se/flow_registry.py::COMPUTER_GROUPS`. The latter stays
solver-free for property discovery; species computations live separately in
`src/precis_se/flow_species.py::_species_run`.

PCB contract details: [kernel reference](reference/pcb-kernel-contracts.md).

PCB selection binds raw board snapshots and instance IDs to optional Gerber
SVG targets. The shared web design-chat engine stores proposals in conversations;
PCB Apply reuses the handler and design history on one guarded connection.
The script-side ``Board.check()`` runs the stdlib ``precis_geom.pcb_motifs``
registry on declared netlist topology; motif checks share ``CheckFinding``
with the builder's pin checks and require no server or copper geometry.

Surfaces on top: the **MCP server** (`precis serve`, the 7 verbs — the
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

Shared context lives in `src/precis/chat/context.py::build`, behind a
`ToolCaller` protocol and section/budget profiles. The bridge adapter
`src/asa_bot/preamble.py::build` preserves its existing prompt; `WEB_CHAT`
omits soul and inner life and bounds hot turns and the complete prompt.
Core imports neither frontend, so web callers can reuse context independently.

The `/rxn` web form dispatches the rxn energetics handler and renders its
tables through the shared reader. Explicit keep stores equation inputs and
an archival ledger in an `rxn` ref; `/refs/rxn/<id>` derives the live ledger
from pinned sources. Catalyst `pathway` run records remain a separate kind.
Pathway environment points cross MCP reads, explicit re-solves and web links
through `src/precis_pathway/environment.py::Environment`; stored-result lookup
reuses `src/precis/quest/environment_basis.py::environment_basis` without
loading the compute engine or scheduling work.
Draft pathway figures store a lazy recipe in `meta.render`; export and web
refresh share `src/precis/render/figure.py::render_figure_chunk`. Selection
options live in `precis.utils.pathway_figure`, and the quest renderer freezes
stored energies and kinetics alongside the PNG without running a solve.
Draft quest-frontier figures use the same lazy dispatch. Their cache key
includes the serving candidates' frontier data, and the renderer uses the
same frontier result for the PNG and its frozen snapshot.

## Package map

Subsystem architecture lives in each package's `__init__.py` module
docstring (docs/README.md); the index — import path + the docstring's
first line — is [`docs/codebase-map.md`](./codebase-map.md). Generated
locally and gitignored; if the link target is missing or stale, run
`scripts/docs-index` (stdlib-only, regenerated automatically at
session start).

## Seams — where changes concentrate

Handlers return `Response`; runtime appends hints and applies result budgets.
Indivisible content such as slides supplies `Response.budget_fit`, allowing
the final fitter to reserve runtime suffix bytes before choosing boundaries.
`Response.images` carries binary payloads (the figure render check's PNG);
the tool surface emits them as MCP `ImageContent` blocks via the per-call
`_RESPONSE_IMAGES` sink in `runtime/dispatch.py` — text-only surfaces keep
just `body`.

Most work lands at one of these. Each has a convention that bites:

- **Add a kind** → handler in `src/precis/handlers/` + forward migration +
  a `precis-*-help` skill + a row in the `precis-overview` kinds table.
- **Change schema** → forward migration only; regen the baseline snapshot
  at release time (`scripts/bump`), never hand-edit it.
- **Add a worker pass** → register on the derived queue; don't call
  `fill_embeddings` from the ingest path (workers own it).
- **Journal names** → `ingest/journal_record.py` caches Crossref journal
  records by ISSN in `app_state`; metadata enrichment materializes the title
  in `meta.journal`. The worker repairs old papers and refreshes every 30 days;
  substantive name disagreements create deduplicated review todos. Citation
  exporters prefer that journal over a provider's raw `meta.venue`.
- **Mutate chunk text** → DELETE + INSERT, never UPDATE (cascade).
- **Ingest needs worker-side follow-up** → `precis.ingest` may not
  import `precis.workers` (import-linter contract), so an ingest path
  that must trigger a worker pass stamps a flag on `refs.meta` in the
  same transaction and a worker ref pass drains it — the pattern
  `db_writer.py`'s `markup_refetch` body REPLACE uses for
  `meta.survey_requeue_pending` → `workers/survey_requeue.py`
  (re-running `survey_extract` after a body swap, gr632959: a live pass
  gets a durable `meta.survey_revision_request` its next slice
  atomically swaps for a `coordinator_state.restart` checkpoint — a
  checkpoint clear would race the in-flight slice's persist, a bare
  request delete would strand the stale checkpoint on a crash; a `Done`
  return discharges the marker —
  and the same pass sweeps either marker on a terminal job — an
  unconsumed request (`Done` raced the stamp) or a restart checkpoint
  whose slice never returned — into an idem-keyed re-mint, or
  transfers the request to a live sibling whose state cannot see
  the swap — parked at `await_accept` over the outgoing body, or a
  `running` slice already past its `body_mark` check that can still
  finish into `await_accept`/`Done` without re-checking; the sibling's
  coverage is judged on `meta` + STATUS read under its `refs` row lock
  so a concurrent `_persist_dispatch_result` can't tear the pair).
- **Fetch an agent-supplied URL** → `safe_get`/`safe_stream`
  (`src/precis/utils/safe_fetch.py`); raw redirected `httpx` is an SSRF.

## Where to go deeper

- Present-state + why per subsystem → the owning package's `__init__.py` docstring (map above)
- Coined/overloaded terms → the repo-dev vocabulary taxonomy, `search(kind='taxon', q='<term>', args={'under': 'tn559460'})`
- Why a decision is the way it is → the owning package docstring's "why" lines (history: `git log`)
- Schema → `docs/reference/schema.md` (generated from the live DB by
  `scripts/gen-schema`; conceptual sketch: `docs/reference/schema-v2.svg`)
- Conventions / workflow / DoD → `AGENTS.md`; ship workflow → `docs/conventions/shipping.md` (short form in `CLAUDE.md`)
