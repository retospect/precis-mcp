---
id: reconsolidate
family: memory
title: precis — bring repo-dev memory back to lint-clean
summary: the periodic cleanup pass for the repo-dev memory graph — lint, fix mechanical findings in the main loop, delegate per-hub verify-and-recommend for judgment findings, then verify and record the pass
answers:
  - how do I run the reconsolidation pass the SessionStart hook says is due?
  - how do I fix memory-lint findings like orphan, no hub or flat hub?
  - which memory-lint findings need a delegated agent vs a direct fix?
  - where do I record a completed reconsolidation pass?
applies-to: the repo-dev memory graph (memory nodes, their links and tags)
status: active
tags: [workflow]
kinds: [memory]
governs: factory
---

# reconsolidate — bring the repo-dev graph back to lint-clean

Writes from the [[remember]] skill keep the graph in shape; this pass catches
what slipped. A finding that recurs means the write path has a gap — fix
the `Next:` hint or the [[remember]] skill, not just the node.

## The pass

1. **Lint.** `scripts/memory-lint` (graph mode reads the cached node export
   at `~/.cache/precis/memory-nodes/`), then `scripts/memory-lint --currency`
   for the per-claim ledger. A stale snapshot's findings are candidates, not
   verdicts: recheck each live before correcting it. Refreshing the cache is
   an explicit operator export (see Export vs refresh), never a reflex.
2. **Mechanical findings — fix in the main loop:**
   - `no hub` → `link(…, rel='part-of')` to the hub the hint names.
   - `orphan` / `dead links` → link to neighbours; `link(…, mode='remove')`
     works on retired targets.
   - `unqualified thread` → link the hub's relevant gotchas with
     `rel='qualifies'` — find them via `search(kind='memory',
     tags=['SPACE:repo-dev', 'section:gotchas'], args={'under': 'me<hub>'},
     q=…)`; if none applies, tag the thread `gotchas:none` (a `part-of`
     child of a thread inherits its parent's qualification).
   - `stalled review` (a `memory-review` todo open > 7 days) → read the todo
     via `get(kind='todo', id='td…')`, settle the memory yourself (edit with
     `reason='misled: …'`, caveat / `qualifies` gotcha, or retire), mark the
     todo done; if you cannot, `tag(kind='todo', id='td…',
     add=['waiting-for:reto'])`. `under review` lines are informational.
   - `flat hub` on a node with > 8 `part-of` children → tag the node
     `section:index`, do not prune children (until gr548270 changes the
     hint).
   - `big hub` (> 40 direct members) → propose sub-hubs to Reto first.
3. **Judgment findings — delegate verify-and-recommend, one read-only
   Sonnet agent per hub; the main loop applies the writes:**
   `eye-truncated` (trim landed history, split the rest into `part-of`
   children), `landed thread` / `retire candidate` / `cold` (verify, move
   keepers, delete), `stale thread` (14+ days untouched: retire, or move
   what matters to its file under the backlog threads dir / a gotcha / a
   todo), `--currency` suspects (adjust, kill or promote-to-doc). The agent
   only reads: it verifies each Left:/claim against git and prod via the MCP
   and returns a table (node → retire/rewrite/keep → evidence), the exact
   text to move and its destination per retire, the exact replacement
   Left: line per rewrite, promote-to-doc candidates, and what it could
   not verify with the missing affordance named. Brief each agent with:
   never drop a still-true claim without moving it; check the repo before
   calling something history; cite `path.py::Qual.name` and bare shas; no
   repo doc edits. The main loop then applies, in order: `edit`/`put` the
   moves, `delete` the retires (`dry_run=True` first; a node with live
   mentions needs `superseded_by=`), rewrite the Left: lines.
   (No subagent runtime? Do the verify-and-recommend yourself, one hub at a
   time, with the same read-only remit. Sonnet unavailable? Use the best
   available read-only model, never a writing lane, and name the model in
   the pass record; if none is available, do the review yourself rather than
   skipping it.)
4. **Verify** by live readback of every touched node (below); for a
   graph-wide claim, a fresh export and `scripts/memory-lint` (aim: no
   findings), and read one hub's fisheye.
5. **Record** a dated entry at the top of memory_consolidation_log (me474383):
   date, what changed, counts, and the scope — the reviewed handles or
   subtree and the coverage still unreviewed. Keep that node under the fisheye cap —
   move the oldest entries verbatim to an archive child under me489908;
   do not summarise them.
6. **Promote-to-doc candidates** the agents listed go to Reto as one list;
   moving them is a repo change on a branch. Missing MCP affordances the
   pass surfaced go into one gripe.

Cadence: the SessionStart hook reports DUE; at most once a day.

## Bounded live-MCP pass

For a scoped request ("garden this hub"), skip the export and work live:

1. **Scope.** Name the handles or the subtree (`args={'under': 'me<hub>'}`)
   up front.
2. **Review.** Read-only agents (step 3's remit and fallback) verify each
   node's claims against git and prod and return evidence-backed
   recommendations.
3. **Correct.** The main loop applies only corrections the evidence
   supports — anchored `edit` with `reason=`, links, retire after
   preservation. Hold and ask before changing a human-origin node.
4. **Read back live.** `get` each touched node and its fisheye; replay the
   recall route that exposed the problem (the original `search` or link
   walk) and confirm it now answers right.
5. **Record.** Log entry per step 5: reviewed handles, changes, what is left
   unreviewed.

"The graph is clean" is reserved for a complete fresh audit: a fresh export
and a full-graph `scripts/memory-lint` with no findings. A bounded pass
claims only its scope.

## Export vs refresh

The authority is AGENTS.md "Repo-dev graph memory". Native graph edits and
operator CLI mirror commands are distinct. Never import or export at task
start, to answer a question, or automatically on an MCP outage. An export the
operator asked for as part of a gardening pass is explicit and allowed:

```
scripts/prod-precis memory index --budget-tok 20000 --export-dir <dir>
```

A cache refresh a hook or a habit triggers is not. When in doubt, read live.

## See also

- [[remember]] — the write recipe whose misses this pass catches.
- [[precis-memory-help]] — graph semantics and the retirement contract.
- [[precis-fisheye-help]] — the fisheye read used in step 4's verify.
