# graph memory consumers

**Status:** ends when an agent never leaves the graph to do its work — no
temp file for a proposal, no SQL for a write check, no guessing which
capability exists — and text-file memory it replaces is retired per
`docs/roadmap.md`. North-star: `backlog/draft-authoring-graph-affordances.md`
(the evidence) + `backlog/fisheye-level2.md` (the focus verb). The memory
half (file-mirror, context hierarchy, session history) and the surfaces
(fisheye-everywhere, draft-linearization) are ranked in `term-taxonomy.md`
since 2026-09-30; this thread ranks the agent-side affordances only, by
what a live consumer is already going without.
**Last reviewed:** 2026-09-30
**Worktree:** `graph-memory-consumers`
**Active:** no — opens at the next session restart if Reto names it.

## Do next

1. **backlog/draft-authoring-graph-affordances.md** — nanobuds-paper
   evidence: work leaves the graph today (a proposal in /tmp, write checks
   in SQL, history unreachable, dedup hiding live hubs), so a live
   consumer is already going without.
2. **backlog/fisheye-level2.md** — the focus verb and the render→act loop;
   the render side (every kind, the browser focus page) is
   `fisheye-everywhere.md`, term-taxonomy Do-next 4.
3. **backlog/server-side-session-context.md** — precondition td458385
   (sessions move to the shared MCP server); the SPACE-axis segregation
   `file-mirror.md` §"Pillar-review deltas" defines is what it selects on.
4. **backlog/unify-backlog-gripes-discoverable.md** — repo guidance and
   gripes as one searchable surface; consumes the mirror once it lands.

## Horizon

1. **backlog/source-code-ingest.md** → **backlog/retire-claude-context.md**
   — sequenced: the second removes the index the first replaces.
2. **backlog/capability-discovery-on-a-sprawling-surface.md**
3. **backlog/docs-and-skills-redesign.md**
4. **backlog/skill-eval-harness.md**
5. **backlog/skill-bundled-scripts.md**
6. **backlog/turn-routing-and-context-dsl.md**
7. **backlog/context-quality-eval.md** +
   **backlog/fisheye-context-eval-study.md** — the measurement tail.
8. **gr440078** — how LLM-driven paper access during live note-taking
   interacts with dreaming, request load and stats (front-pinning burst
   budget, per-session stub stats, the interactive-lane question).
9. **gr372794** — whether paper review notes should participate in dream
    consolidation; deferred by Reto 2026-09-20 pending a decision on
    anchor loss and the autoreviewer-volume question.

## Parked

- **backlog/dev-context-diet.md** — small enabler, parked until a
  consumer above needs it.
- **backlog/memory-lint-extraction-decision.md** — small enabler.
- **backlog/contextual-chunk-embeddings.md** — small enabler.
- **backlog/context-sentence-rag-over-body.md** — small enabler.
- **backlog/universal-short-codes.md** — small enabler.
- **gr343055** — `render_figure_chunk` has no production caller, so a
  draft graph-figure stays a placeholder; unparks with
  draft-authoring-graph-affordances.
- **gr454753**, **gr454749** — draft export gate inconsistency and the
  raw-identifier-in-prose guard; both are draft-authoring surface
  questions, park behind Do-next 1 rather than fixed independently here.
- **gr447365** — an agent routed a Précis URL through webfetch instead of
  the native handle: a discoverability failure on the agent surface;
  unparks with backlog/capability-discovery-on-a-sprawling-surface.md.
- **gr440083** — ERC panel-aware audience tracking over a draft: a
  human-surface feature ask on draft authoring; parks behind Do-next 1.
- **gr240051** — reference hover-preview may exceed the 200 ms
  hover-intent budget: human-surface latency on the web reader; parks
  behind backlog/fisheye-everywhere.md (term-taxonomy Do-next 4), whose
  browser focus page reuses that preview.

## No action needed

- (none yet)

## Seam

`term-taxonomy` owns the substrate (knowledge-mesh, measures-substrate,
graph-gardener), the memory half (file-mirror, context-memory-hierarchy,
session-history-into-precis) and the surfaces (fisheye-everywhere,
draft-linearization). This thread owns the agent-side affordances listed
above. Do not duplicate ranking across the two files.
