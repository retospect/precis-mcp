---
status: draft
---

# Mcp surface economy

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Tool-ledger friction detector

_Grouped 2026-09-26; was `mcp-tool-ledger`, status idea._

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
