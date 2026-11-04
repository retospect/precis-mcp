---
name: reconsolidate
description: "Use when the SessionStart hook says reconsolidation is DUE, or memory-lint reports findings: lint the repo-dev memory graph, fix findings per hub with delegated agents, record the pass."
---

# reconsolidate — bring the repo-dev graph back to lint-clean

Writes from the `remember` skill keep the graph in shape; this pass catches
what slipped. A finding that recurs means the write path has a gap — fix
the `Next:` hint or the `remember` skill, not just the node.

1. **Lint.** `scripts/memory-lint` (graph mode reads the SessionStart export
   at `~/.cache/precis/memory-nodes/`; refresh it with
   `scripts/prod-precis memory index --budget-tok 20000 --export-dir <dir>`
   when stale), then `scripts/memory-lint --currency` for the per-claim
   ledger.
2. **Mechanical findings — fix in the main loop:**
   - `no hub` → `link(…, rel='part-of')` to the hub the hint names.
   - `orphan` / `dead links` → link to neighbours; `link(…, mode='remove')`
     works on retired targets.
   - `unqualified thread` → link the hub's relevant gotchas with
     `rel='qualifies'` (`search(kind='memory', tags=['SPACE:repo-dev',
     'section:gotchas'], args={'under': 'me<hub>'}, q=…)`); if none
     applies, tag the thread `gotchas:none` (a `part-of` child of a thread
     inherits its parent's qualification).
   - `stalled review` (a `memory-review` todo open > 7 days) → read
     `get(kind='todo', id='td…')`, settle the memory yourself (edit with
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
   what matters to its `docs/backlog/threads` file / a gotcha / a todo),
   `--currency` suspects (adjust, kill or promote-to-doc). The agent only
   reads: it verifies each Left:/claim against git and prod via the MCP
   and returns a table (node → retire/rewrite/keep → evidence), the exact
   text to move and its destination per retire, the exact replacement
   Left: line per rewrite, promote-to-doc candidates, and what it could
   not verify with the missing affordance named. Brief each agent with:
   never drop a still-true claim without moving it; check the repo before
   calling something history; cite `path.py::Qual.name` and bare shas; no
   repo doc edits. The main loop then applies, in order: `edit`/`put` the
   moves, `delete` the retires (`dry_run=True` first; a node with live
   mentions needs `superseded_by=`), rewrite the Left: lines.
4. **Verify** with a fresh export and `scripts/memory-lint` (aim: no
   findings), and read one hub's fisheye.
5. **Record** a dated entry at the top of memory_consolidation_log (me474383):
   date, what changed, counts. Keep that node under the fisheye cap —
   move the oldest entries verbatim to an archive child under me489908;
   do not summarise them.
6. **Promote-to-doc candidates** the agents listed go to Reto as one list;
   moving them is a repo change on a branch. Missing MCP affordances the
   pass surfaced go into one gripe.

Cadence: the SessionStart hook reports DUE; at most once a day.
