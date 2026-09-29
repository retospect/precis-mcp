---
status: draft
---

# Mcp surface economy

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Tool-ledger friction detector

_Grouped 2026-09-26; was `mcp-tool-ledger`, status idea._

**2026-09-29:** the *retrospective* half now exists — `scripts/mine-sessions/`
(committed extractor + detector catalogue + evidence cards) driven by the
14-day `/surface-review` pass (`docs/runbooks/surface-review.md`). That is a
human-cadence pass, NOT the automated threshold detector below; it supplies
the calibration the detector needs (what rate is abnormal) and the evidence
cards for judging a crossing. Measured while building it: the surface's error
rate is **0.8%** (609 / 73,472 calls, 10 d), so a threshold on
`(verb, kind, error_type)` alone will be quiet — the auto-detector should also
watch the `detour_census` and `retry_to_success` signals the miner computes.

The `tool_calls` ledger shipped (migration 0133, `src/precis/tool_ledger.py`,
written from `runtime/dispatch.py::dispatch_with_status`, sweeper GC; mining
queries in the module docstring). Remaining follow-on, cheap now the table
exists: a nursery friction-detector pass that auto-files a gripe when a
`(verb, kind, error_type)` rate crosses a threshold — the standing detector
for the next psql-detour-shaped coverage hole
(`mcp-aggregate-surface-gaps.md`). Also unblocks
`skill-question-targets-and-injection.md` §3 (ledger calibration).

## MCP aggregate surface gaps

_Grouped 2026-09-26; was `mcp-aggregate-surface-gaps`, status draft._

Observed 2026-08-19 during a claim-hub corpus audit. Every question worth
asking about the corpus turned out to be a `GROUP BY`, and none of them were
expressible through the MCP — so the whole audit ran through
`scripts/prod-psql`, which dumps raw rows into the main-loop context (the
`cluster-ops` PreToolUse hook nags about exactly this). The fix is not "use
bash less"; it is an aggregate surface, which is cheaper on **both** context
and tokens than either bash or paged `search`.

The MCP today is a per-ref retrieval API: `get` one, `search` top-N. That is
the right shape for reading. It is the wrong shape for *operating on a
cohort*, which is what every batch pass does.

> Corroborating measurement (2026-08-21, 62h local transcript mining): 1,063
> Bash psql calls in dev sessions, dominated by nanopub status polling (that
> slice → `nanopub-mcp-surface-gaps.md` §0) with corpus-audit GROUP BYs the
> rest. `mcp-tool-ledger.md` will make future detours continuously visible.

### Measured against a real window — surface-review pass #1, 2026-09-29

First `/surface-review` pass (5 days: 390,590 events across local transcripts,
the `tool_calls` ledger, `llm_call_log`, and job transcripts). Scorecard for
the six gaps below, so a future pass argues from evidence rather than re-
asserting the list:

- **#1 similarity scores — CONFIRMED, and the diagnosis is sharper than
  stated.** The score is not missing where it is exposed; it does not
  *discriminate*. Two `search(kind='gripe')` reformulation bursts
  (`job:450632`, `job:456022`; 5 and 4 consecutive re-queries) returned
  `rank=0.02` on **every** hit, for obviously different queries. The fix is
  "make the exposed rank mean something, or drop the column", not "expose a
  score".
- **#2 counts/facets — CONFIRMED, the largest single demand bucket.**
  `table:refs` n=151 / 27 sessions and `table:worker_logs` n=66 / 8 sessions,
  dominated by hand-rolled `GROUP BY` / `string_agg(...) FILTER` rollups.
  Sharpest single instance: `search(kind='ref', …)` fails, and the agent's
  very next call is `SELECT kind, count(*) FROM refs GROUP BY kind`.
- **#3 structural/graph-shape filters — NOT supported by this window.** The
  status/tag-join queries found are counts-and-facets shaped; nothing
  resembling "hubs with zero evidence edges" appeared. May still be real —
  do not re-cite it as confirmed off this pass.
- **#4 exhaustive cohort enumeration — CONFIRMED.** `table:chunks` n=38 / 8
  sessions plus `WHERE ref_id IN (…)` batch-existence checks.
- **#5 corpus-health view — CONFIRMED and WIDER than written.** This
  window's version is *fleet/ops* health, not the taproot corpus: one
  doctor-tick session family reconstructed "is the fleet healthy" through
  ~120 ad-hoc SELECTs (heartbeat staleness, `lease_until`, `child-failed*`
  and `alert-state:*` counts). The item should cover an ops-health view too.
- **#6 post-write verification — not evidenced either way this window.**

**A seventh gap the list misses: schema/structure discoverability.**
`information_schema.columns` n=64 / 21 sessions + `information_schema.tables`
n=32 / 16 sessions = 96 calls, plus a ~79-call PCB-table long tail, agents
independently re-deriving the same table shapes. Cause is concrete and cheap:
`docs/reference/schema.md` says `Source: precis_prod @ 2026-07-05` — ~12
weeks stale — is missing `pcb_drc_findings` / `pcb_routes` / `pcb_copper` /
`pcb_local_footprints` entirely, and lists `part_footprints` with 9 columns
where prod has 11. And `grep -rn "schema.md" src/precis/data/skills/` returns
**zero** — nothing in the runtime surface points an agent at the doc before
it reaches for psql. This is orthogonal to #1–#6: those are about querying
*content*, this is about discovering *shape*. Fix: regenerate
(`scripts/gen-schema`), add a skill `answers:` line pointing at it, and for
prod agents with no checkout consider `get(kind='schema', id='<table>')`.

**An eighth: no batch form on singleton verbs.** Measured across the window:
**1,608 of 2,770 resolvable verb+kind calls (58%) sit inside a run of ≥4
consecutive identical-shape singleton calls**, over 52 of 99 sessions. Split
out into its own item — `singleton-id-no-batch-form.md` — with the ranked
`(verb, kind)` table and the `_coerce_id` crash it also uncovered.

**Instrumentation blocker for the next pass.** The biggest number this pass
produced is an *extrapolation*, not a measurement: fleet `get(kind='gripe')`
is 27,680 calls, but `tool_calls` carries no payload, so fleet byte volume
was estimated by applying the local mean (4,716 B, n=645) — ≈130 MB / ≈33M
tokens over 5 days. Adding a `result_bytes` INT to the ledger write path
(`runtime/dispatch.py::dispatch_with_status`) would make this directly
measurable without retaining any payload — a length is not content.

**`result_bytes` alone is not enough** (Reto asked; answering here so the next
pass doesn't re-derive it). Three columns, none retaining payload:

- `result_bytes INT` — the volume question.
- `result_count INT` — **already declared in migration 0133 as "reserved:
  populated once Response carries a structured hit count" and still NULL.**
  Without it you cannot separate one huge render from a hundred small hits,
  which is exactly the render-diet-vs-pagination distinction.
- **A correlation key that is actually set.** `agentlog_id` exists but is
  populated on 25 of 83,721 rows (0.03%) and `source` is NULL throughout, so
  the ledger cannot group a burst of calls into one agent's run. This is an
  instrumentation bug, not a schema change, and it is the highest-leverage of
  the three: it is why the 58%-singleton-runs measurement had to come from
  local transcripts instead of the fleet-wide ledger.

**2026-09-29: the seventh gap's doc half is fixed.** `scripts/gen-schema` was
*broken*, not merely unrun — `information_schema`'s `_pg_expandarray` blew the
server statement_timeout, and `PGOPTIONS` cannot fix it because pgbouncer
rejects unproxied startup parameters; it needs `SET LOCAL` inside an explicit
transaction. Fixed, and `docs/reference/schema.md` regenerated: 58 → 130
tables, 65 → 186 FKs, snapshot now current. The skill pointer is in
`precis-status-help`. What remains of that gap is the prod-agent case (no
checkout ⇒ no doc), i.e. `get(kind='schema', id='<table>')`.

### Gaps, most valuable first

1. **Similarity scores on search results.** Sharpest gap, because a shipped
   skill rule already depends on it: `precis-taproot-mint-help`'s "Search before
   you mint" gate tells the agent to search for a proximate hub before
   minting. But `search` returns ranked hits with **no score**, so the agent
   cannot separate "0.93 — this is the same claim, attach to it" from
   "0.55 — merely related, mint a new one". The mandated gate is therefore a
   judgment call where it should be mechanical. Expose the score, or add
   `view='dupes'` returning trigram + embedding neighbours with both numbers.

2. **Counts and facets.** A `view='count'` on any filtered `search`, plus
   facet counts by tag/state/kind. "How many claim hubs have no evidence
   edge" needed SQL; it should be one call.

3. **Structural (graph-shape) filters.** Filter by `has_inbound_edges`,
   `edge_count < N`, `source_kind='paper'`, drift state. The single most
   valuable query of the audit — hubs with zero evidence — is one SQL line
   and impossible via MCP. `search` filters by tag/status/kind but never by
   the shape of the graph around a ref.

4. **Exhaustive cohort enumeration.** `search` is relevance-ranked top-N,
   `page_size <= 100`, with no stable cursor over a filtered set. A batch
   pass cannot *prove* it covered every member. This is why the notation
   normalization partitioned by `ref_id % 3` in SQL rather than paging the
   MCP. Needs a deterministic, order-stable cursor (`sort='ref_id'`) that
   walks a filter to exhaustion.

5. **A corpus-health view.** `get(kind='finding', view='taproot-health')`
   returning the audit table directly: total hubs, orphan count, single-edge
   count, groundable count, near-duplicate pair count, per-rule lint
   violation counts. Small in context, and it alone would have made this
   session's bash unnecessary. Pairs with the lint work in
   `nanopub-corpus-remediation.md` — the lint codes are the natural columns.

6. **Post-write verification.** After editing 127 titles the question was
   "did every one round-trip untruncated" — a `length()` aggregate. The MCP
   can only re-`get` one at a time, so verification also fell to SQL.

### Reliability

`search(kind='skill', q=...)` exceeded 120 s and was backgrounded during this
session — the second recorded occurrence (see the `precis_search_hang_no_progress`
note; a prior instance ran 1800 s before being force-aborted). A verb that may
hang is one agents learn to route around, which undercuts every gap fixed
above. Worth a timeout + a cheap fast path before the semantic leg.

### Note on write ergonomics

All three notation subagents tripped a `[Modify Shared Resources]` security
warning for `mcp__precis__edit` against prod, because the authorization lived
in the parent conversation and subagents cannot see it. Not an MCP defect as
such, but if batch prod mutation becomes routine the dispatch prompt needs to
carry the authorization explicitly, or the batch path needs a distinct verb
that records its warrant.

## Skill-injection ledger calibration

_Grouped 2026-09-26; was `skill-question-targets-and-injection`, status draft._

Decided direction (Reto, 2026-08-21): retrieval-as-infrastructure — the
harness runs the first hop of skill RAG; no almost-right middle tier.

§1 (question targets) and §2 (bimodal injection) are SHIPPED: `answers:` +
`summary:` front-matter cover the full skill corpus (question_only /
heading_only variants, `skill_index/chunker.py`), and
`skill_index/injection.py` injects the whole top-matching skill — or
nothing — into planner prompts (`workers/planner_prompt.py`) and quest tick
prompts (`quest/tick.py`) when the score clears
`PRECIS_SKILL_INJECT_THRESHOLD` (default 0.85; `PRECIS_SKILL_INJECT=off`
kills it). Injection decisions log skill id + score; near-misses within
0.05 log at debug.

### Remaining: §3 — ledger calibration (blocked on `mcp-tool-ledger.md`)

The ledger closes the loop, cheapest-possible-edit style:

- injected-skill-never-used → threshold too permissive (raise it);
- detour-despite-silence (psql detours, paging workarounds) → a coverage
  hole whose fix is *authoring one more question* on an existing skill —
  not new machinery.

Inputs already exist: the injection log lines above, plus prod job
transcripts. Blocked until the ledger lands; then calibration is a
read-the-ledger-and-edit-front-matter loop, no code.

Interacts with per-job tool lists (`tick-tool-lists-and-discovery-reflex.md`)
and `unify-backlog-gripes-discoverable.md` (same architecture over dev
knowledge).

## Demand confirmed 2026-09-29 — and §1 is unblocked

Reto, independently and without having read this item, asked for "a griper
that checks the logs for stupid MCP issues". That is §"Tool-ledger friction
detector" above, arrived at twice from opposite directions — decent evidence
the shape is right.

It is also no longer blocked: the `tool_calls` ledger shipped (migration
0133, `src/precis/tool_ledger.py`), which was the dependency for both §1 and
§3. `eval-run-spine.md` adds a sequence number and an args hash to the same
table, so the detector gains two further signals for free — repeated
identical calls within one run (a loop), and a `(verb, kind)` whose callers
keep retrying with the same arguments (a surface that is not saying what it
wants).

§3's calibration reading gains a second consumer: `friction-reflection-enable.md`
wants the ledger as the behavioural check on end-of-run self-report.
