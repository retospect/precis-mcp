---
id: precis-work-help
family: work
title: precis — the work tree: what is in flight, what waits, how to claim a slice
summary: one hierarchy of the work — quest → served todo roots → slices, with leases, agents and jobs per row; the asks list; asking the owner a question (an ask-user todo with meta.ask) and answering it; claiming and releasing a slice; where gripes hang; what a pillar quest is
answers:
  - what is in flight right now and who is on it?
  - how do I read a row of the work view?
  - what blocks a lane, and what waits on the owner?
  - how do I ask the owner a question, and how is it answered?
  - how do I claim a slice before working it, and how do I release it?
  - where does a gripe land in the work tree, and what does its stage mean?
  - what is a pillar quest, and why is a paper a month todo and not a quest?
applies-to: search(kind='todo', view='work'); get(kind='todo', id=N, extent='fisheye+1hop'); tag(kind='todo', add=['claimed-by:…'])
tags: [workflow]
kinds: [todo, quest, gripe]
status: active
---

# precis-work-help — reading and working the work tree

All work hangs in one tree: a **quest** (science or pillar) at the top,
the todo roots that `serves` it below, their slices below those. A
**slice** is one todo leaf one agent builds in one session. Every row
starts with a handle, so any row is a `get` away.

Each section below answers one question; read one with
`get(kind='skill', id='precis-work-help~<n>')`, list them with
`get(kind='skill', id='precis-work-help/toc')`. The design behind the
tree, for when a row's shape surprises you:

```python
get(kind="md", id="main/docs/backlog/dark-factory.md~decisions-reto-2026-10-30")
```

## What is in flight right now, and who is on it?
## Show me the work tree with agents and leases

```python
search(kind="todo", view="work")
```

```
# work — 2 quests with work in service · 3 live agents

◆ qu540835 [dormant] prio=3 — Precis runs its own work: the platform under every agent lane …
  td540836 ○ prio=3 — Dark factory: precis runs the work
    td540842 ◀ claimed-by:slice-td540842 prio=2 agents=1 — Work view: gripes hang under the root …
    td540848 ◀ claimed-by:slice-td540848 prio=3 agents=1 jobs=2(1 live) — Skill precis-work-help …
    gr541510 open — precis-todo-tree-help~8 omits releasing a lease …

unassigned roots (4) — serve no quest
  td98 [open] — Write the nanotrans review.

unplaced gripes (37) — open, thread matches no root

hosts
  mac-a reported 3m ago
release: not reported by any host (dark-factory step 4)
```

Blocks, top to bottom:

| block | what it holds |
|---|---|
| `◆ qu<id> [status] prio=N — title` | one quest with at least one todo `serves` it; active quests first, then by prio |
| indented `td<id>` rows | each served root and its subtree, children ordered by prio; 40 rows per root, then `… +N more` with the `view='tree'` call |
| `unassigned roots (N)` | strategic roots that serve no quest — work nobody ranked against a quest |
| `unplaced gripes (N)` | open gripes whose `thread:` tag matches no root (see the gripe section) |
| `hosts` | each fleet host and when it last reported; `none reported` means no agent counts are real |

A todo that serves a quest directly and also sits inside another served
root renders once, inside the larger tree.

## How do I read one row of the work view?

`td<id> <icon> prio=N agents=N jobs=N(M live) — title`; a cell with
nothing to say is left out.

| cell | meaning |
|---|---|
| icon | `○` open · `▶` doing · `◀ claimed-by:<name>` leased · `⏸ waiting-for:<x>` / `⏸ ask-user` / `⏸ blocked` / `⏸` paused · `✓` done · `✗` won't-do |
| `prio=N` | 1..10, lower is hotter |
| `agents=N` | fleet agent rows whose purpose, tree, branch, `assigned` or `slice` field names this todo's handle or its lease holder |
| `jobs=N(M live)` | child jobs folded into one cell; live = queued or running |

`agents=0` on a leased row means the claimer has no fleet row naming the
slice. Put the handle in your fleet row so the count finds you:

```python
edit(kind="fleet", id="fl123", mode="replace", args={"slice": "td540848"})
```

One node with its place in the tree — parent, peers, children, each
with its icon — under a `— tree —` ring (`part-of:`, `peers (N):`,
`contains (N):`):

```python
get(kind="todo", id=540848, extent="fisheye+1hop")
get(kind="todo", id=540836, view="tree")  # one root in full
```

The web `/work` page renders the same hierarchy with columns status /
agents / stage / age, each row linking to `/eye/<handle>`; it lands with
td540843.

## What blocks a lane, and what waits on the owner?
## Which questions are waiting for a human decision?

The asks list is one view with three classes, grouped by the quest the
row serves, then by thread:

| class | the row is | sources |
|---|---|---|
| showstopper | a lane is stopped on it | `waiting-for:reto` on a claimed slice, an `ask-user` todo whose `meta.ask.blocks` names a slice, a tripped breaker, a red open release |
| decision | a next slice waits on it | `ask-user` todos, gripes tagged `needs:ruling` |
| discussion | a shape question, nothing waits yet | todos and gripes tagged `discussion` |

```python
search(kind="todo", view="asks")
```

The web `/asks` page shows the same list, but by default only the
showstoppers and the `ask-user` / `needs:ruling` decisions; a bare
`waiting-for:reto` row and the discussion rows sit behind `/asks?all=1`.

Argument on a row is comments on that ref; every user sees every row.
The sources one by one, when you want only one of them:

```python
search(kind="todo", view="ask-user")   # decisions parked on a human reply
search(kind="todo", view="waiting")    # every waiting-for:* leaf
search(kind="todo", view="attention")  # halted, asking, child-failed
search(kind="gripe", tags=["needs:ruling"])
```

To raise one yourself: write a question for the owner (next section), or
tag `waiting-for:reto` on the slice you hold when you cannot continue (a
showstopper). A question raised on a gripe stays a gripe: tag it
`needs:ruling` and comment the question.

## How do I ask the owner a question?
## Where does a question for Reto go, and how is it answered?

A question for the owner is an `ask-user` todo written through this MCP,
from any machine. Never a file, never a message in a pane.

```python
put(kind="todo", text="Should the pass cut on a red gate?", parent_id=540836,
    tags=["ask-user", "thread:factory"],
    meta={"ask": {"kind": "ruling", "options": ["yes, cut", "no, warn only"],
                  "blocks": "td540848", "context": "gate went red twice this week"}})
```

- `text` is the question, one sentence he can answer. Put the argument in
  `body=` or a later comment.
- `parent_id` is the slice or root it belongs to; omit it for a loose
  question. The row appears under that row's quest and thread.
- `meta.ask.kind` is `design-verdict` (a shape question), `ruling` (a
  choice between options) or `approval` (a yes to sign off). Anything else
  is refused with the accepted values.
- `meta.ask.options` is a list of short strings, the recommended one
  first. Optional; `context` is one short line of what he needs to know.
- `meta.ask.blocks` is `td<id>` of the slice that cannot proceed until
  answered, or `nothing`. A handle writes a `blocked-by` link from that
  slice to the question (it leaves `view='doable'` until the question
  closes) and files the row as a showstopper; `nothing` files a decision.
- The `ask-user` tag is required with `meta.ask`; add `thread:<slug>` so
  the row sits with its thread.

Change a question after filing with `edit(kind="todo", id=N,
meta={"ask": {...}})`; the new dict replaces the old one.

The owner answers on the web `/asks` page, which does both steps below.
Over the MCP, an answer is a comment starting `answer:` and closing the
row, which drops it from the asks view and releases the blocked slice:

```python
put(kind="todo", id=541990, text="answer: yes, cut; warn-only hides real reds")
tag(kind="todo", id=541990, add=["STATUS:done"], remove=["ask-user"])
```

Read the answer back with `get(kind="todo", id=541990)`: it is the
comment starting `answer:`. A question you filed and whose slice you
hold: after it closes, re-read the row and continue.

## How do I claim a slice before working it?
## Take the lease on a todo so no other agent builds it

```python
tag(kind="todo", id=540848, add=["claimed-by:slice-td540848"])
```

The lease is a compare-and-set tag: if another name holds a live lease
the call refuses naming the holder — pick another leaf from
`search(kind='todo', view='doable')`. It lasts 4 h from the last claim;
re-issuing the same tag refreshes it. A dispatched slice's lease name is
`slice-td<id>`. Full rules: `precis-todo-tree-help~8`.

Release it one of three ways:

```python
tag(kind="todo", id=540848, add=["STATUS:done"], remove=["claimed-by:slice-td540848"])
tag(kind="todo", id=540848, add=["STATUS:won't-do"])
tag(kind="todo", id=540848, remove=["claimed-by:slice-td540848"])  # hand it back unfinished
```

A lease left to expire releases itself after 4 h and the leaf returns to
`view='doable'`. When the work lands, record it on the todo as a comment:

```python
put(kind="todo", id=540848, text="landed: a84e0a0d0 — skill served; block lint green")
```

## Where does a gripe land in the work tree?
## What does a gripe's stage mean on a work-view row?

A gripe hangs under the todo root that carries the same `thread:<slug>`
tag. td540836 carries `thread:factory`, so a gripe tagged
`thread:factory` sits under it. Place one:

```python
tag(kind="gripe", id=540900, add=["thread:factory"])
```

Under the root, after its todo rows, one line per open gripe (15, then
a `… +N more gripes` line with the search call):

```
    gr541348 fixing [claude] — Work view: tree ring for a todo eye
```

`gr<id> <stage> [lane] — title`. The stage is the gripe's `STATUS` tag.
The lane is the lane recorded on the newest job under the gripe; absent
when the gripe has no job. A gripe with two `thread:` tags hangs under
both roots. Gripes with no `thread:` tag, or one no rendered root
carries, go in `unplaced gripes (N) — open, thread matches no root` at
the end of the view: the count plus the five oldest.

The stage is the `STATUS` ladder; a row leaves it only when the status
changes:

| stage | the gripe is |
|---|---|
| `open` | reported, not yet looked at |
| `triaged` | read and judged worth fixing |
| `fixing` | claimed by a builder who holds a lease |
| `verify` | fixed on main, waiting for the release check that closes it (legacy spelling of `auto_verify`) |
| `auto_verify` | fixed on main; the deploy check closes it once the sha is on prod |
| `user_verify` | fixed on main; a human checks it via the per-deploy ask todo |
| `needs_spec` | ill-specified; parked until a docs/backlog spec names it |
| `done` | fixed and verified (leaves the view) |
| `wontfix` | decided against (leaves the view) |

The older `ready_for_fix` and `in_review` values still show as stored.
The fix loop's own queues (brief, handoff, review, landed sha) are in
[[precis-gripe-loop-help]].

## What is a pillar quest?
## Why is a paper a month todo and not a quest?

A pillar quest is a quest whose striving is one product pillar (tag
`pillar:<name>`); qu540835 is the platform pillar. It is the parent for
`serves` links from thread roots, so platform work ranks against science
quests on one board with one PRIO. It stays `STATUS:dormant` until the
build tick lands — a quest tick whose roles are slice / build / review
instead of research. Dormant here means waiting for that tick, not
stalled. Hang a thread root under it:

```python
link(kind="todo", id=540836, rel="serves", target="quest:540835")
```

A paper is not a quest: a quest never finishes, a draft gets posted. The
cadence quest qu459585 holds one month todo per paper (td459586 is
October's); the paper's slices — citation pass, export, sign — are
children of that month todo; the draft is `draft-of` the month todo and
`serves` every science quest it draws on. Quest mechanics (PRIO
flow-down, logbook, ticks) are in [[precis-quest-help]].

## See also

- [[precis-todo-tree-help]] — tiers, doable leaves, leases, ask-user
- [[precis-quest-help]] — quests, `serves`, PRIO flow-down, logbook
- [[precis-grill-me-help]] — opt-in question round before a flagged build
- [[precis-gripe-loop-help]] — brief / handoff / review / landed on the gripe
- [[precis-fleet-help]] — agent rows, hosts, the `slice` field
- [[precis-job-help]] — the jobs folded into a row's `jobs=` cell
