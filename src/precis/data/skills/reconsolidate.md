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

The default pass runs live over MCP and never reads the node export. The
procedure is [[precis-gardener-memory-help]] § "An interactive gardening
pass"; this skill adds the repo-dev routing.

1. **Survey live.** Scope the repo-dev graph or the hub you were given, then
   read `search(kind='memory', view='smells', args={'under': 'me474312'})`
   and `view='shape'` the same way, the pending proposals
   (`get(kind='job', id='/garden/proposals')`), the open memory-review todos
   (`search(kind='todo', tags=['memory-review', 'STATUS:open'])`) and the open
   `project:memory` gripes. Every finding is a candidate: recheck it live
   before correcting it.
2. **Mechanical findings — fix in the main loop:**
   - `no-section` / `multi-parent` / orphan → `link(…, rel='part-of')` to the
     one right hub; `link(…, mode='remove')` works on retired targets.
   - `unqualified-thread` → link the hub's relevant gotchas with
     `rel='qualifies'` — find them via `search(kind='memory',
     tags=['SPACE:repo-dev', 'section:gotchas'], args={'under': 'me<hub>'},
     q=…)`; if none applies, tag the thread `gotchas:none`.
   - A memory-review todo open > 7 days → read it (`get(kind='todo',
     id='td…')`), settle the memory yourself (edit with `reason='misled: …'`,
     caveat / `qualifies` gotcha, or retire), mark the todo done; if you
     cannot, `tag(kind='todo', id='td…', add=['waiting-for:reto'])`.
   - A hub with > 40 direct members → propose sub-hubs to Reto first.
3. **Judgment findings — delegate verify-and-recommend, one read-only
   Sonnet agent per hub; the main loop applies the writes:** `grab-bag`,
   `history-dense`, `thread_shape`, `compact`, `stale-hot`, `dangling_mention`,
   `griped` and pending proposals (verify; retire, rewrite, split or keep).
   The agent only reads: it verifies each claim against git and prod via the
   MCP and returns a table (node → retire/rewrite/keep → evidence), the exact
   text to move and its destination per retire, the exact replacement per
   rewrite, promote-to-doc candidates, and what it could not verify with the
   missing affordance named. Brief each agent: never drop a still-true claim
   without moving it; check the repo before calling something history; cite
   `path.py::Qual.name` and bare shas; no repo doc edits. The main loop then
   applies, in order: `edit`/`put` the moves, `delete` the retires
   (`dry_run=True` first; a node with live mentions needs `superseded_by=`),
   the rewrites; never a pending proposal's payload by hand. (No subagent
   runtime? Do it yourself, one hub at a time, same read-only remit. Sonnet
   unavailable? Use the best available read-only model, never a writing
   lane, and name it in the pass record; else review it yourself.)
4. **Verify** by live readback of every touched node (below), one hub's
   fisheye, and the same smells read over the scope. Claim only that scope.
5. **Record** a dated entry at the top of the graph pass log,
   memory_consolidation_log (me474383): date, what changed, counts, and the
   scope — reviewed handles or subtree and the coverage still unreviewed.
   Keep that node under the fisheye cap — move the oldest entries verbatim to
   an archive child under me489908; do not summarise them.
6. **Promote-to-doc candidates** the agents listed go to Reto as one list;
   moving them is a repo change on a branch. Missing MCP affordances the
   pass surfaced go into one gripe.

Diagnostic, not the gardening pass: when an operator separately asks for an
offline snapshot check, `scripts/memory-lint` over an explicit export
(see Export vs refresh) — every finding re-verified live, never a reflex.

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
