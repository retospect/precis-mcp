# graph memory consumers

**Status:** ends when an agent never leaves the graph to do its work — no
temp file for a proposal, no SQL for a write check, no guessing which
capability exists — and text-file memory it replaces is retired per
`docs/roadmap.md`. North-star: `backlog/fisheye-level2.md` (the focus verb); the
nanobuds draft evidence (proposal in /tmp, history by SQL, write checks by
SQL) was closed 2026-10-02 by draft `view='history'`/`'proposals'` and the
landed-sha edit ack. Both views were dogfooded 2026-10-03 on the live
server (63301c5c) against dr173020, and both rendered. The edit ack was
not exercised on that live review draft. The memory
half (file-mirror, context hierarchy, session history) and the surfaces
(fisheye-everywhere, draft-linearization) are ranked in `knowledge-mesh.md`
since 2026-09-30; this thread ranks the agent-side affordances only, by
what a live consumer is already going without.
**Last reviewed:** 2026-10-04
**Worktree:** `graph-memory-consumers`
**Active:** yes — Reto 2026-10-01: "graph memory we want soon".

## Resume (2026-10-04 08:58Z)

- **Landed, all live on prod 727728cc:**
  - memory slice 2, the cutover and memory-lint graph mode (31fc2a3c, fef5f1d5);
  - the coerced-composite marker (a8c706b6).
  - The branch holds nothing unshipped.
- **Open:**
  - The marker is unverified. No finding has been minted on prod since
    2026-10-03 16:59Z, so no composite hub carries the key yet.
  - The `--sync` guard has not been exercised against the live pointer.
    Auto mode denies that run; unit tests cover the guard.
  - The currency ledger re-flags the same 5 provenance suspects every run.
- **In flight (08:58Z):** a coder agent is adding `find-replace` /
  `insert` to memory `edit` (Do-next 1) on branch
  `worktree-agent-a92adc8b6b7627afd`, worktree
  `.claude/worktrees/agent-a92adc8b6b7627afd`. It is not landed and its
  tests are unreviewed.
- **Next step:** review that branch's diff. Run its memory handler test
  file with `scripts/test`, merge it into this worktree and qland. If the
  branch is gone or empty, redo the change from the
  `memory-native-authoring.md` decisions-log entry dated 2026-10-03.
- **Reconsolidation:** 2026-10-04 pass logged; no node edits.

## Do next

1. **backlog/memory-native-authoring.md** — Reto 2026-10-01: top priority,
   ahead of td458720's sequencing. First slice built 2026-10-02 (`SPACE:`
   axis, `precis memory import`/`index`, the hook script, test 4a).
   - **Dogfooded on prod 2026-10-03:** the import ran, and the graph render
     matched `MEMORY.md`.
   - **The dogfood exposed gaps:** no verb writes an index line,
     memory-lint is file-only, and the import is a snapshot.
   - **Slice 2 closes them:** `meta={'hook':…}`, handle-form index,
     `import --sync`, the hook's last-good cache, memory-lint graph mode,
     and the hook wired but silent until the marker.
   - **Round 4 dogfooded on prod (727728cc), 2026-10-04 04:45Z.**
     Harness memory is in the graph: `MEMORY.md` is the pointer, and the
     old index is kept as `MEMORY.md.pre-cutover`.
     - **The hook:** printed 150 lines and exported 137 node bodies plus
       `_sections.tsv` (41 threads, 65 gotchas, 13 runbooks, 18 workflow).
     - **memory-lint graph mode:** clean, 19.1 KB index, preamble ≈ 7455
       tok of 8000. No landed threads.
     - **Currency ledger:** lists 5 suspects. They are the provenance
       worktree names judged on 10-03, and the ledger has no way to mark
       a suspect resolved, so they re-flag on every run.
     - **The sync guard (31fc2a3c):** not exercised on prod. Auto mode
       denied the live `--sync` against the pointer as a possible mass
       delete; the unit tests cover it.
   - **memory-lint graph mode now lints node bodies.** Bodies come from the
     cache that `memory index --export-dir` writes, and a stray-write check
     is added. The first graph reconsolidation ran on 2026-10-03 and is
     logged in `memory_consolidation_log.md`. The landed scan is now
     scoped to thread nodes.
   - **Next on this item:** memory `edit` needs `find-replace` (decisions
     log, 2026-10-03).
   - **Blocked:** tests 4b/4c, which need `backlog/file-mirror.md`
     (knowledge-mesh Do-next 7).
2. **backlog/vocab-align-to-literature.md** — ruled 2026-10-01 (both
   tiers, throughout code and comments, no compatibility path); gates the
   January paper (td459587), so it lands before January even though
   item 1 outranks it on value. Tier 1 glosses shipped 2026-10-02. The
   `envelope` row moved to tier 2: it is not an outer bound.
   - **Tier 2, the taproot half (89fff2fe):** live; the prompt A/B is
     closed (orchestrator 2026-10-03 21:39Z). The shipped prompt stays,
     and `_coerce_extraction` is the floor for a dropped composite.
     Coerced composites carry `meta.composite_source` (live in 727728cc).
     No finding had been minted by 04:45Z, so check the first new
     composite hub for the key.
   - **Tier 2, the nanopub half (0181, round 2):** live and checked on
     prod 2026-10-03. The CHECK is validated and allows claim, composite
     and hypothesis; the one `compound` row is now `composite`.
     `nanopub_artifacts` is append-only and was not rewritten.
3. **backlog/fisheye-level2.md** — the focus verb and the render→act loop;
   the render side (every kind, the browser focus page) is
   `fisheye-everywhere.md`, knowledge-mesh Do-next 4.
4. **backlog/server-side-session-context.md** — precondition td458385
   (sessions move to the shared MCP server); the SPACE-axis segregation
   `file-mirror.md` §"Pillar-review deltas" defines is what it selects on.
5. **backlog/unify-backlog-gripes-discoverable.md** — repo guidance and
   gripes as one searchable surface; consumes the mirror once it lands.

## Horizon

Surface metric for this thread's end state: the tool-ledger friction detector
in `backlog/mcp-surface-economy.md` — an agent that leaves the graph for SQL
or a temp file shows up there as friction (no retag; that item stays put).

Search cluster, ranked here as the consumer side of navigation (retrieval is
how an agent reaches the graph; `fisheye-*` is how it moves within it):

- **backlog/paper-search-unique-per-paper.md** and
  **backlog/paper-evidence-selection-fisher-rao.md** — what a search returns
  shapes every consumer's first read.
- **backlog/embed-freshness.md** · **backlog/improve-hnsw-recall-check.md** —
  silent recall loss is invisible to the agent that suffers it.
- **backlog/uncited-facet-patent-edgar-wiring.md** ·
  **backlog/patent-search-parity.md** — kinds the facet cannot see.
- **backlog/search-notation-symmetric-fold-index.md** ·
  **backlog/search-future-filters.md** · **backlog/good-search-coordinator.md**
- **backlog/skill-index-build-once-vs-shed.md** · **backlog/vocab-compaction.md**
  — triage first; order inside the group is provisional.

1. **backlog/quest-graph-as-dossier.md** — Reto 2026-10-01: a quest's
   graph "is" the dossier and a writer agent linearises it; settles the
   membership relation and the render trigger before any quest-document
   work. Same principle as Do-next 1 (graph is truth, text is a render).
2. **backlog/source-code-ingest.md** → **backlog/retire-claude-context.md**
   — sequenced: the second removes the index the first replaces.
3. **backlog/capability-discovery-on-a-sprawling-surface.md**
4. **backlog/docs-and-skills-redesign.md**
5. **backlog/skill-eval-harness.md**
6. **backlog/skill-bundled-scripts.md**
7. **backlog/turn-routing-and-context-dsl.md**
8. **backlog/context-quality-eval.md** +
   **backlog/fisheye-context-eval-study.md** — the measurement tail.
9. **gr440078** — how LLM-driven paper access during live note-taking
   interacts with dreaming, request load and stats (front-pinning burst
   budget, per-session stub stats, the interactive-lane question).
10. **gr372794** — whether paper review notes should participate in dream
    consolidation; deferred by Reto 2026-09-20 pending a decision on
    anchor loss and the autoreviewer-volume question.

## Parked

- **backlog/dev-context-diet.md** — small enabler, parked until a
  consumer above needs it.
- **backlog/contextual-chunk-embeddings.md** — small enabler.
- **backlog/context-sentence-rag-over-body.md** — small enabler.
- **backlog/universal-short-codes.md** — small enabler.
- **gr343055** — `render_figure_chunk` has no production caller, so a
  draft graph-figure stays a placeholder; parks behind Do-next 1 with
  the other draft-authoring surface gripes.
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
  behind backlog/fisheye-everywhere.md (knowledge-mesh Do-next 4), whose
  browser focus page reuses that preview.

## No action needed

- (none yet)

## Seam

Search cluster (13 items) joined 2026-10-01; `classify-scope-and-400-watch`
left for `platform`.

`knowledge-mesh` owns the substrate (knowledge-mesh, measures-substrate,
graph-gardener), the memory half (file-mirror, context-memory-hierarchy,
session-history-into-precis) and the surfaces (fisheye-everywhere,
draft-linearization). This thread owns the agent-side affordances listed
above. Do not duplicate ranking across the two files.
