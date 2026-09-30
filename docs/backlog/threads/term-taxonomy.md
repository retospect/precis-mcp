# term taxonomy and measurand list — the knowledge-mesh thread

**Status:** ends when the graph is the memory: a richly linked mesh of
small typed nodes (findings, measures, taxa, skills, memories, plans,
arguments, logbook entries) that an LLM navigates by walk and fisheye,
with typed links whose rules are data, and drafts rendered from it
(Reto's goal, stated 2026-09-30). Today the substrate (refs, chunks,
links, tags, an 81-relation registry) already is that mesh; the taxon kind
is ready to build and four items wait on it; the measurand-list pipeline
has its prompt, census and synonym fixes measured (stability 0.052 →
0.270 on the same 100 hubs, 0.55 of what that sample can show), per-call
metering in place, and a freeze that cannot land until the taxon kind
exists. Reviewed 2026-09-30 against the goal: the memory half (skills,
memory files, session history, context hierarchy) had no ordering owner
and is ranked here from now; the ordering rule is "unblocked and visible
first, then the measures chain, then the memory half behind the walk".
**Last reviewed:** 2026-09-30
**Worktree:** `term-taxonomy`

## Do next

1. **backlog/term-taxonomy.md** — status ready, Reto's two rulings recorded
   2026-09-30 (six seed axes, element symbols one node). Direct dependency
   of taxonomy-bootstrap's freeze, measures-substrate, graph-gardener and
   knowledge-mesh; nothing else in this thread can ship its output until
   the taxon kind exists.
2. **backlog/taxonomy-bootstrap.md §Resume (2026-09-30)** — the metered
   re-probe of the same 100 rows (~66 paid calls, Reto's go = td458388). Yields
   the cache-read counts that decide concurrency vs packing, the noise
   floor, and the first `probe criterion` verdict (≥0.60 of the unit-key
   ceiling; run 2 reads 0.55). The 1231-call full run waits on it.
3. **backlog/relation-constraints.md** — status ready, no blocker, small.
   Domain/range kinds, functional and acyclic as columns on `relations`,
   one validator in both link doors. Above the measures chain because it
   closes live holes now (quest `serves` has no cycle guard; the 1:1 draft
   family is bypassable through `link()`) and gives term-taxonomy's
   `instance-of` rule a row instead of a bespoke guard.
4. **backlog/fisheye-everywhere.md** — status ready, no blocker. The eye
   ladder on every kind, rings per relation group, a focus page for any
   handle. Split from knowledge-mesh because it depends on no measures work
   and is the goal's most visible surface; also the answer to "a viewer
   for the memory" once 6 lands.
5. **backlog/measures-substrate.md** — blocked-by term-taxonomy; identity =
   taxon + reference + convention, so it lands right after 1 and unblocks
   knowledge-mesh and the experiment loop. Reto's fold-in ruling =
   td458719.
6. **backlog/file-mirror.md** — status draft, no blocker. Skills and the
   Claude Code memory files as read-only `markdown` roots with links from
   `[[slug]]` and frontmatter; replaces the unfiled "memory/skills mesh
   pilot". Mirror-vs-native = td458720. Below 5 only because its recall AC
   is cheap to run at any time.
7. **backlog/norr-her-meta.md** — the consumer of list.v1.yaml (20-paper
   round, gold set, figure). Starts on a frozen list.

## Horizon

1. **backlog/taxonomy-bootstrap.md full run** (1231 calls) — waits on the
   re-probe clearing the probe criterion and the concurrency decision;
   delivers list.v1.yaml, the first frozen measurand list.
2. **backlog/term-taxonomy.md v1.5** (axis start node, meta.axis validated)
   — waits on v1 and gardener-promoted axes; link filters by axis. This is
   the thread's answer to "hierarchies over links": axis hierarchy, not
   relation specialisation.
3. **backlog/knowledge-mesh.md** (walk, numeric conflicts, quest_mesh,
   universal row) — waits on Do-next 5; its ladder item moved to Do-next 4.
4. **backlog/experiment-loop.md** — waits on Do-next 5; hypothesis → todo
   `tests` → measure → ruling → refuted, walked end to end through the
   verbs, plus the skill that teaches it.
5. **backlog/graph-gardener.md** — waits on Do-next 1 plus a populated mesh
   from Horizon 1; earned axes and merges without hand curation.
6. **backlog/session-history-into-precis.md** — the linear logbook: human
   sessions as `conv`, machine runs as `agentlog` (Reto's 2026-09-29
   split); waits on the shared redaction path, whichever item ships it
   first.
7. **backlog/curation-gate.md** — waits on eval-run-spine's verdict column
   (serving-programme thread); the guard that lets 5 run unattended.
8. **backlog/draft-linearization.md** — waits on 3; the graph as the
   truth and a draft as a rendered subgraph. Render-only vs two-way =
   td458721.
9. **backlog/context-memory-hierarchy.md** — the resident/discovered split
   for the harness memory; P0 is repo-only and can go any time, P1 after
   Do-next 6 gives the topic files a recall measurement, P2 = td458724.
10. **backlog/corpus-quantitative-extraction.md** — waits on Horizon 1 and
    Do-next 5; sourced numeric triples bound to list entries.
11. **backlog/norr-her-meta.md steps 3-5** (20-paper round, gold set,
    figure, draft) — waits on Horizon 1; the paper's spine.
12. **backlog/dreaming.md** — the consolidation pass over memory nodes;
    revisit once 5 and 7 exist, since both replace hand consolidation.

## Waiting on Reto

- td458719 fold `component_spec_values` into `measures` (Do-next 5).
- td458720 skills/memory mirror first or native (Do-next 6).
- td458721 draft render-only or two-way (Horizon 8).
- td458722 rename this thread file to `knowledge-mesh.md`.
- td458723 memory-lint extraction (decision date passed 2026-08-19); on
  answer `backlog/memory-lint-extraction-decision.md` is deleted.
- td458724 one resident identity + style block (Horizon 9, P2).

## No action needed

- taxonomy-bootstrap blockers 2, 3a, 3b and 4 — measured fixed (over-cap
  107 → 3, facet nodes 22 → 0, stranded unit borrowed, synonym families one
  node each); nothing further. The campaign vocabularies grow from node
  notes, not from code.
- the 0.80 stability threshold — not readable at 100 hubs (unit-key ceiling
  0.49); it is a full-run criterion, not a probe failure. The probe reads
  `min_probe_ratio` (0.60 of the ceiling) instead, stated 2026-09-30.
- per-call metering + raw-reply capture — `responses.jsonl` per discovery
  call, streamed as each call lands; nothing further until a paid run
  fills it.
- a relation hierarchy (sub-relations as rows) — rejected in
  `backlog/term-taxonomy.md`'s reconciliation table; Horizon 2 covers the
  need.
- a graph database or a closure table — rejected in knowledge-mesh and
  term-taxonomy with a stated revisit trigger.
