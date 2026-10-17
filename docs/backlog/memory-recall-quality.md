---
status: in-progress
pillar: memory-graph
prio: normal
---

# Memory recall quality (context-memory-hierarchy P4)

Part of [context-memory-hierarchy](./context-memory-hierarchy.md): its
caveat names "never found" as the failure mode once memory is discovered
rather than preloaded. This item measures that for the repo-dev graph and
fixes the misses. Not [improve-hnsw-recall-check](./improve-hnsw-recall-check.md),
which is `chunk_embeddings` ANN recall.

## Question set

`src/precis/data/memory_recall_set.json`: one entry per real case, each a
task query a session would type at task start, the prod answer node
(`want`, a `me…` handle), that node's title + hook as a snapshot, where the
case came from (`source`: a gripe, the fleet ledgers, or a fact sessions
re-derive) and the mistake it prevents (`why`). The snapshot carries no
body text, hostnames or addresses: the repo is public, and
`tests/test_deploy_tree_no_secrets.py` gates the file.

36 entries across the seven hubs (Ship/CI/tests 12, Agent conduct & fleet
6, Session MCP & cluster 5, Product core 4, Drafts/citation 3, CAD/pcb 3,
Chemistry 3). The seed miss "label timestamps with Z" answers to me481712
(the prod node; `timestamps_utc` is the AC5 fixture's synthetic topic).

Bias: the entries were written by an agent that read the answer nodes, so
query wording leaks node vocabulary and the score is an upper bound. Grow
the set from observed misses: when a session finds a memory only after the
mistake, add its first recall query and the node.

## Recall score

Three scorers, one entry format:

- **Prod (the number that counts).** `scripts/prod-precis memory index --set`
  prints `HIT <rank>` / `MISS -` per entry with the answer node's origin
  (native write or file-mirror import), then `RECALL h/n at k=5 (native …,
  mirror …)` — bare `--set` scores the packaged set, `--set <path>` a JSON
  file. Same path as a session's recall: `render_memory_index` →
  `MemoryHandler.search(view='index')`, hybrid with the remote embedder.
- **Weekly (the durable check).** The `memory_recall` scheduler cadence
  (`workers/scheduler.py` → `workers/memory_recall.py`) scores the same set
  weekly, records the k=5 score (total + native/mirror split) in
  `app_settings['memory_recall:last']`, and raises a WARN
  `memory_recall/recall-dropped` alert when the score drops below the last
  recorded one. The set lives in `precis.data` so the deployed worker reads
  the same file the CLI scores.
- **Test (the floor).** `tests/test_memory_recall_native_vs_mirror.py::
  test_recall_set_holds_its_floor` seeds each answer node's snapshot (title +
  hook; the hook doubles as body) plus the 24 AC5 topics into a test DB,
  once native and once through `import_mirror`, and scores the set at k=5
  with the hash test embedder (lexical in effect). `SET_FLOOR` pins the
  measured hits per mode; raise it when a fix lands, never lower it. A fix
  that edits a prod title or hook updates the entry's snapshot in the same
  commit.

## Baseline (2026-10-17)

| scorer | total | native | mirror |
|---|---|---|---|
| prod, k=5 | 33/36 | 12/14 | 21/22 |
| test, k=5 | 3/36 | 3/36 | 3/36 |

The test leg is lexical in effect: the hash embedder has no semantics and
the lexical leg is a term-AND, so only the three queries whose every word
is in the snapshot hit. It guards the scorer and seeding path, not wording;
prod is the only signal for a title or hook fix. The same gap shows what
recall does when the embedder is down: natural-language queries stop
matching at all.

Prod misses:

| query | want | what ranked instead |
|---|---|---|
| compare behaviour before and after my change in a worktree | me494842 (git stash shared across worktrees) | worktree traps (cwd, Edit path, sibling audit) |
| feature is merged but nothing happens in prod | me478008 (dark feature activation) | ship/gate env-pointer nodes |
| how should draft prose cite a paper claim | me474338 (cite-findings-only policy) | draft thread nodes |

Two near-misses: me478000 at rank 4, me477690 and me477196 at rank 2.

## Fixes

Each fix is a meta-only `edit` (title and/or `meta.hook`; no body rewrite,
so the `memory_body` chunk and its embedding stay), which re-syncs the
header card (title + hook) that search reads. Before/after is the prod
scorer's total.

An edit re-mints the header card with no embedding until the embed worker
fills it; measure after the card has a vector (fix 1b waited about an
hour). Prod total 33/36 → 36/36 (native 12/14 → 14/14, mirror 21/22 →
22/22); no other entry changed rank. With every entry hit, the set no
longer discriminates: grow it from observed misses (above) before the next
fix pass.

| # | node | change | entry rank before → after |
|---|---|---|---|
| 1 | me474338 | slug title → "Draft prose cites finding hubs only, never paper chunks (…)" | miss → miss (rank 8 at k=10) |
| 1b | me474338 | hook leads with "How draft prose cites a paper claim" | miss → 1 (once its card had a vector; the lexical leg alone missed) |
| 2 | me478008 | slug title → "Merged is not live in prod: dark feature activation order (…)" | miss → 1 |
| 3 | me494842 | hook adds "to set work aside or compare before and after a change, use a WIP commit or a patch file" | miss → 5 |

The retitle alone left me474338 at rank 8, behind draft thread nodes
whose long bodies are dense with draft/paper/claim words. A hook that
opens with the question's own words beat them; in this case the hook
moved the rank and the title did not.

Slug titles (mirror imports titled with the topic filename) caused two of
the three misses. A follow-up pass can retitle the remaining slug-titled
mirror nodes by the same meta-only edit, scored against this set.

## Appendix: prod writes

Coordinator-authorized for this task: edits to existing `SPACE:repo-dev`
nodes (title, hook, tags inside the space) and new links only. No deletes,
retires, imports or exports.

All edits are meta-only (`edit(mode='replace', args={'title', 'meta'})`,
no `text=`), so bodies are untouched and each `ref_events` row carries a
`reason=`. The write path was tested first on a test DB
(`test_meta_only_title_hook_edit_moves_recall`); the dev DB was not
provisioned.

| when (UTC) | node | field | before → after |
|---|---|---|---|
| 2026-10-17 ~12:56 | me474338 | title | `cite-findings-only-policy` → `Draft prose cites finding hubs only, never paper chunks (cite-findings-only-policy)` |
| 2026-10-17 ~12:56 | me474338 | hook | unchanged (re-sent; the handler refuses a title-only edit) |
| 2026-10-17 ~13:00 | me478008 | title | `dark_feature_activation_order` → `Merged is not live in prod: dark feature activation order (dark_feature_activation_order)` |
| 2026-10-17 ~13:00 | me478008 | hook | unchanged (re-sent) |
| 2026-10-17 ~13:00 | me494842 | hook | `… Use a patch file in .scratch instead.` → `… To set work aside or compare before and after a change, use a WIP commit or a patch file in .scratch.` |
| 2026-10-17 13:10 | me474338 | hook | `Reto 2026-09-17 — prose cites are finding hubs [fi<id>] only, never direct [pc<id>]/[pa<id>]; …` → `How draft prose cites a paper claim (Reto 2026-09-17): cite the finding hub [fi<id>] only, never the paper chunk [pc<id>]/[pa<id>]; …` (rest unchanged) |

No links were added and no tags changed.
