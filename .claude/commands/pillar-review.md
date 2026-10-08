---
description: "Product-plan review: reconcile docs/roadmap.md with backlog, gripes and active threads; relink orphan gripes, prune closed pointers. Run each session-restart wave."
argument-hint: "[pillar to focus on — memory | 3d | local | personal | all (default all)]"
allowed-tools: Read, Grep, Glob, Edit, Write, Bash(scripts/inflight:*), Bash(scripts/docs-index:*), Bash(scripts/backlog-lint:*), Bash(scripts/ship:*), Bash(git log:*), Bash(git status:*), Bash(git add:*), Bash(git commit:*), Bash(git fetch:*), Bash(git merge:*), Bash(grep:*), Bash(ls:*), Bash(wc:*), Task, ListAgents, SendMessage, mcp__precis__get, mcp__precis__search, mcp__precis__put
---

Run the **pillar review**. Focus: `$ARGUMENTS` (default `all`).
The page under review is `docs/roadmap.md`; the layer under it is
`docs/backlog/threads/` (README there is the ranking rule). This command is
the driver; the first pass (2026-09-30) is the worked example and its
lessons are the traps below.

**Posture.** Reto is the customer, you are the product manager. Nothing is
written until the discussion is had: present findings and a numbered
decision list with a recommendation per line, get his answers, then write.
Peers are down in the weeds — one short question each, never a task.

## 1. Read the known set

- `docs/roadmap.md` — each pillar's end state, north-star items, threads,
  the active set, the retirement rules, the review log at the bottom.
- `docs/backlog/threads/INDEX.md` §Pillars and generated `PRIORITIES.md`
  (run `python3 scripts/docs-index` if missing or stale); activity comes
  only from `.claude/fleet/threads.tsv`.
- `scripts/inflight` — which threads actually have a session today; a
  mismatch with the fleet roster’s declared active set is a finding, not a fix.
- `get(kind='quest', id=459585, view='tree')` — the paper cadence: is this
  month's todo on track, and which decision blocks it.
- `search(kind='gripe', status='open', page_size=100)` paged to the end —
  **no `q=`** (a query turns the enumeration into a ranked filter).

## 2. Poll the owners (parallel, one message each)

`ListAgents`, then `SendMessage` to every session whose purpose names an
active thread. Two questions, five lines max, "do not stop your work for
this": (1) of the pillar's stated end state, what exists today, what is one
slice away, what is on no item; (2) the one thing outside your thread that
blocks you most. Replies arrive over the next half hour; do not wait on
them to start step 3.

## 3. Mine (fan out, read-only)

Dispatch one `Explore` agent per pillar in one message, each with the
pillar's end-state paragraph verbatim and this brief: enumerate the
backlog items relevant to the pillar (slug, status, prio, threaded or
not, spec/slice/small, one-line intent), list the runtime surface that
exists (skills, kinds, paths), and list the gaps between the end state
and any filed item, citing the closest existing item for each. Plus one
agent for gripes: open gripes versus every `gr<id>` in
`docs/backlog/threads/*.md`, orphans classified by pillar with a
suggested thread, clusters of three or more sharing a root cause,
staleness over 30 days.

## 4. Discuss

Present per pillar: what the backlog already commits to, what Reto's
statement adds that no item covers, where the threads cut against the
pillar split, then the decision list. Blocking questions only where the
readings lead to materially different work. Log every ruling as
`[decided YYYY-MM-DD, Reto]` in the item it belongs to — never re-ask.

## 5. Write (after the go)

Three commits, disjoint file ownership, agents in parallel:

- **You:** `docs/roadmap.md` (end states, "Where it stands", allocation priorities,
  the review-log line), `docs/mission.md` if a doctrine changed, and the
  items that carry the rulings.
- **A `documenter` agent:** the gap items, from one-paragraph briefs with
  fixed slugs, each naming its closest existing items and its evidence
  source. It must grep for an existing item before creating one and report
  skips.
- **A `documenter` agent:** thread files — new dormant files, inserts at
  rank with why-lines, orphan gripes placed, closed pointers pruned,
  `INDEX.md` seams. It must verify every `gr<id>` live before pruning and
  keep refuted/tombstone entries.

Then `scripts/docs-index`, `scripts/backlog-lint`, ship through the quick
lane (docs only), and tell each affected owner where their items landed and
what drift you found in their file — findings, never fixes to their
ranking.

## 6. Traps (each one cost a round on the first pass)

- **Siblings file from the same brief.** Reto restates a pillar to more
  than one session on the same day. Before filing, `git fetch` and read
  `origin/main`'s same-day items; on a merge conflict in a thread file,
  take the owner's ranking as the base and insert yours. Fold your
  duplicate into theirs as a dated "Pillar-review deltas" section and
  delete your file; never keep two.
- **Thread files are half stale on gripe ids.** Verify live status before
  pruning; a "soft-deleted, do not reopen" tombstone is not a closed
  pointer — keep it.
- **The fix_gripe reset re-surfaces landed fixes.** A gripe flipped back
  to open by an incident may already be fixed in main; ask "is the fix in
  main?" before ranking it (gr458087 was). A gripe's auto-diagnosis
  describes the code at filing time and is never re-run, so an open status
  plus a confident diagnosis is not a live defect: probe the scenario on
  prod before ranking it as the cause of anything (gr456213 was fixed on
  09-29, re-opened by an unrelated reset, and ranked as the live cause of a
  corruption for a day).
- **Rulings recorded only in a code comment are invisible to the review.**
  When an owner reports a question "queued for Reto" that he already
  answered, the answer is usually in a commit or a comment; the item must
  carry it as `[decided YYYY-MM-DD, Reto]` or the next pass re-asks.
- **Agents mis-cluster.** A pagination bug filed under "embed drain" is a
  read-surface bug; read the title, not the cluster label, before placing.
- **Do not rank inside another owner's thread beyond inserts.** Re-ranking
  is theirs; send the finding.
- **A rename request in a peer's queue may contradict a ruling Reto gave
  you.** Note it in the thread file; do not close the todo.
- **Guards on the build surface belong in the item.** When an owner names
  a test that will go red on the first run (a totality map, an export
  allow-list), put it in the item's blast radius before anyone starts.

## 7. Log

Append one dated line to `docs/roadmap.md` `## Review log`, newest first:
date, pillars covered, owners polled, items filed / folded, gripes
relinked / pruned, decisions still open on Reto. That line is the cadence
clock. Write the memory hook (`product_plan_review_<date>.md`) with the
rulings and what is still waiting on Reto.
