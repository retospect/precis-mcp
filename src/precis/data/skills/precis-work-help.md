---
id: precis-work-help
family: work
title: precis — the work tree: what is in flight, what waits, how to claim a slice
summary: one hierarchy of the work — quest → served todo roots → slices, with leases, agents and jobs per row; the asks list; asking the owner a question (ask-user todo, meta.ask); claiming and releasing a slice; where gripes hang; the slice write scope; pillar quests
answers:
  - what is in flight right now and who is on it?
  - how do I read a row of the work view?
  - what blocks a lane, and what waits on the owner?
  - how do I ask the owner a question, and how is it answered?
  - how do I claim a slice before working it, and how do I release it?
  - where does a gripe land in the work tree, and what does its stage mean?
  - what does a rescue:<class> tag mean, and how do I read the rescue queue?
  - what may a dispatched slice write over the MCP?
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

Read one section with `get(kind='skill', id='precis-work-help~<n>')`;
list them with `get(kind='skill', id='precis-work-help/toc')`. The
design behind the tree, when a row's shape surprises you:

```python
get(kind="md", id="main/docs/backlog/dark-factory.md~decisions-reto-2026-10-30")
```

## What is in flight right now, and who is on it?
## Show me the work tree with agents and leases

```python
search(kind="todo", view="work")
```

```
# work — 3 quests: 1 running · 1 queued · 1 idle · 3 live agents

running (1)
◆ qu540835 running [dormant] prio=3 agents=2 jobs=1 next=td540850 3h ago — Precis runs its own work …
  td540836 ○ prio=3 — Dark factory: precis runs the work
    td540842 ◀ claimed-by:slice-td540842 prio=2 agents=1 — Work view: gripes hang under the root …
    td540848 ◀ claimed-by:slice-td540848 prio=3 agents=1 jobs=2(1 live) — Skill precis-work-help …
    gr541510 open — precis-todo-tree-help~8 omits releasing a lease …
  jo541880 [running] run_slice · under td540848
```

Blocks, top to bottom:

| block | what it holds |
|---|---|
| `running (N)` / `queued (N)` / `idle (N)` | the three run-state groups, in that order; a quest is **running** while a served todo holds a live `claimed-by:` lease or a child job is running, else **queued** while a doable leaf or a claimable (queued/yielded) job waits, else **idle** |
| `◆ qu<id> <run-state> [status] prio=N agents=N jobs=N next=td<id> <age> — title` | one live quest (active or dormant; abandoned stay off), ordered in its group by prio then id; `next=` is the first doable leaf; `<age>` is the newest row change |
| indented `td<id>` rows | under running and queued quests: each served root and its subtree, children by prio; 40 rows per root, then `… +N more` with the `view='tree'` call |
| indented `jo<id> [<STATUS>] <title> · under td<id>` | the quest's live jobs (running or claimable), one line each, after the rows |
| `unassigned roots (N)` | strategic roots that serve no quest, ranked against none |
| `unassigned jobs (N)` | live jobs with no quest path: the health signal, work no quest owns |
| `platform (N)` | live jobs with no todo parent, filed under qu540835: scheduled upkeep, one line by job type |
| `unplaced gripes (N)` | open gripes whose `thread:` tag matches no root |
| `hosts` | each fleet host and its last report; `none reported` means no agent count is real |

A job resolves to a quest two ways: its `parent_id` chain walks up to
the nearest ancestor that `serves` one, else its own meta answers
(`params.quest_id`, `coordinator_state.quest_id`, or its structure's
`quest:<id>` tag). A resolved job outside its quest's served trees still
joins that quest's job lines; a todo serving a quest directly and also
sitting inside another served root renders once, inside the larger tree.

Blocked-only quests leave the idle group and sort by who can unblock
them (gr573557): a quest whose live leaf rows are all blocked lands in
`needs you (N)` at the top (its line naming the unblocking items) when
Reto can unblock at least one leaf, else in `blocked by other work (N)`
at the bottom. Reto can unblock a leaf that carries
`waiting-for:reto`, is `blocked-by` an open `ask-user` todo, or is
itself an open ask; anything else (another open todo,
`waiting-for:fleet`, a closed gate, STATUS `blocked`) is other work.

## How do I read one row of the work view?

`td<id> <icon> prio=N agents=N jobs=N(M live) — title`; a cell with
nothing to say is left out.

| cell | meaning |
|---|---|
| icon | `○` open · `▶` doing · `◀ claimed-by:<name>` leased · `⏸ waiting-for:<x>` / `⏸ ask-user` / `⏸ blocked` / `⏸` paused · `✓` done · `✗` won't-do |
| `prio=N` | 1..10, lower is hotter |
| `agents=N` | fleet agent rows whose purpose, tree, branch, `assigned` or `slice` field names this todo's handle or its lease holder |
| `jobs=N(M live)` | child jobs folded into one cell; live = queued or running |

`agents=0` on a leased row means no fleet row names the slice; put the
handle in yours so the count finds you:

```python
edit(kind="fleet", id="fl123", mode="replace", args={"slice": "td540848"})
```

One node with its place in the tree (parent, peers, children, each with
its icon) sits under a `— tree —` ring (`part-of:`, `peers (N):`,
`contains (N):`):

```python
get(kind="todo", id=540848, extent="fisheye+1hop")
get(kind="todo", id=540836, view="tree")  # one root in full
```

The web `/work` page renders the same board, one collapsed line per
quest; a signed-in caller sees a **Pause**/**Resume** button per
slice row and quest line (the twin of
`tag(kind="todo", ..., add=["STATUS:paused"])` and
`edit(kind="quest", ..., mode="pause"|"resume")`
([[precis-quest-help]]). Pausing a quest parks every live strategic root
serving it; neither button cancels a job.

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

The web `/asks` page shows only the showstoppers and the `ask-user` /
`needs:ruling` decisions; the rest sit behind `/asks?all=1`.

Argument on a row is comments on that ref. One source at a time:

```python
search(kind="todo", view="ask-user")   # decisions parked on a human reply
search(kind="todo", view="waiting")    # every waiting-for:* leaf
search(kind="todo", view="attention")  # halted, asking, child-failed
search(kind="gripe", tags=["needs:ruling"])
```

To raise one: write a question for the owner (next section), or tag
`waiting-for:reto` on the slice you hold when you cannot continue (a
showstopper). On a gripe, tag it `needs:ruling` and comment the question.

Triage marks an item that waits on Reto specifically with
`for-reto:<ruling|spec|review|other>` (agents write plain needs-spec;
this tag is Reto's alone). `search(kind="todo", view="reto-queue")` lists
every such row plus the asks and `waiting-for:reto` rows across todos,
gripes and alerts, most blocked first then oldest, each with context,
options (recommended first) and impact.

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

- `text` is the question, one sentence he can answer; put the argument
  in `body=` or a later comment.
- `parent_id` is the slice or root it belongs to; omit it for a loose
  question. The row appears under that row's quest and thread.
- `meta.ask.kind` is `design-verdict` (a shape question), `ruling` (a
  choice between options) or `approval` (a yes to sign off); anything
  else is refused with the accepted values.
- `meta.ask.options` is a list of short strings, the recommended one
  first (optional); `context` is one short line he needs.
- `meta.ask.blocks` is `td<id>` of the slice that cannot proceed until
  answered, or `nothing`. A handle writes a `blocked-by` link from that
  slice to the question (it leaves `view='doable'` until the question
  closes) and files a showstopper; `nothing` files a decision.
- The `ask-user` tag is required with `meta.ask`; add `thread:<slug>`.

Change a question with `edit(kind="todo", id=N, meta={"ask": {...}})`;
the new dict replaces the old one.

The owner answers on the web `/asks` page, which does both steps below.
Over the MCP, an answer is a comment starting `answer:` plus closing the
row, which drops it from the asks view and releases the blocked slice:

```python
put(kind="todo", id=541990, text="answer: yes, cut; warn-only hides real reds")
tag(kind="todo", id=541990, add=["STATUS:done"], remove=["ask-user"])
```

Read the answer back with `get(kind="todo", id=541990)`: it is the
comment starting `answer:`.

## How do I claim a slice before working it?
## Take the lease on a todo so no other agent builds it

```python
tag(kind="todo", id=540848, add=["claimed-by:slice-td540848"])
```

The lease is a compare-and-set tag: if another name holds a live lease
the call refuses naming the holder; pick another leaf from
`search(kind='todo', view='doable')`. It lasts 4 h from the last claim
and re-issuing the same tag refreshes it. A dispatched slice's lease name
is `slice-td<id>`. Full rules: `precis-todo-tree-help~8`.

Release it one of three ways:

```python
tag(kind="todo", id=540848, add=["STATUS:done"], remove=["claimed-by:slice-td540848"])
tag(kind="todo", id=540848, add=["STATUS:won't-do"])
tag(kind="todo", id=540848, remove=["claimed-by:slice-td540848"])  # hand it back unfinished
```

An unrefreshed lease expires after 4 h and the leaf returns to
`view='doable'`. When the work lands, comment it on the todo:

```python
put(kind="todo", id=540848, text="landed: a84e0a0d0 — skill served; block lint green")
```

## Where does a gripe land in the work tree?
## What does a gripe's stage mean on a work-view row?

A gripe hangs under the todo root that carries the same `thread:<slug>`
tag (td540836 carries `thread:factory`). Place one:

```python
tag(kind="gripe", id=540900, add=["thread:factory"])
```

After the root's todo rows, one line per open gripe (15, then a
`… +N more gripes` line with the search call):

```
    gr541348 fixing [claude] — Work view: tree ring for a todo eye
```

`gr<id> <stage> [lane] — title`: the stage is the gripe's `STATUS` tag;
the lane is the one recorded on the newest job under the gripe, absent
with no job. A gripe with two `thread:` tags hangs under both roots.
Gripes with no `thread:` tag, or one no rendered root carries, go in
`unplaced gripes (N)` at the end of the view: the count plus the five
oldest.

The stage is the `STATUS` ladder; a row leaves it only when the status
changes:

| stage | the gripe is |
|---|---|
| `open` | reported, not yet looked at |
| `triaged` | read and judged worth fixing |
| `fixing` | claimed by a builder who holds a lease |
| `auto_verify` (legacy `verify`) | fixed on main; the deploy check closes it once the sha is on prod |
| `user_verify` | fixed on main; a human checks it via the per-deploy ask todo |
| `needs_spec` | ill-specified; parked until a docs/backlog spec names it |
| `parked` | waiting on a `meta.until` condition; `gripe_parked` wakes it to `open` |
| `done` | fixed and verified (leaves the view) |
| `wontfix` | decided against (leaves the view) |

Older `ready_for_fix` and `in_review` values still show as stored; the
fix loop is in [[precis-gripe-loop-help]].

## How does a gripe reach a build lane by itself?
## What does the gripe_dispatch pass mint, and which gripes does it skip?

The `gripe_dispatch` worker pass (every 10 minutes, dark until
`service_config` lights it for a host) mints one `gripe-fix-direct`
chain per free code-lane slot (summed cap of the enabled `code` lanes,
minus open chains whose build has not landed). The build carries the
gripe text verbatim and picks its lane at dispatch (cheapest fit, free
before paid); the land writes `resolved: <sha>` on the gripe.
It picks the hottest `prio` then the oldest among gripes that are `open`
or `triaged`, tagged `fix:` code/skill/docs/web/mcp (a `code` build),
15+ minutes past the latest `STATUS:triaged` or fix tag, with no live
todo `related-to` them. Setting `factory.gripe_trust`: `all` (default)
takes any filer; `filer` only Claude Code, human or ci_watch filers.
A `release-waiver:r<N>` gripe is a broken build: minted at once, first.
A `hold:no-autoclose` gripe never enters a lane. Keep a gripe out with any of `fix:reto`,
`reto:ask`, `disagreement`, `flood`, or

```python
tag(kind="gripe", id=564261, add=["hold:no-dispatch"])
```

A hand-minted chain also counts (the `related-to` link is the marker).
Each mint leaves a `dispatch: minted gripe-fix-direct td<root>` comment.
A `project:` tag must name a landable project (`precis`, the default;
`precis-mcp`, `dark-factory`); others get a `dispatch: skipped unsupported project:` comment, no slot.

## What does a rescue:<class> tag mean?
## How do I read the rescue queue?

Every factory failure ends in done or rescue. A node that burned its
retry budget — a build that kept failing, a mint that kept raising —
carries the open tag `rescue:<class>` and waits for diagnosis instead of
retrying forever. The class says what failed: `builder`, `land`,
`review`, `release`, `intake`, `lane`, or an engine class (`stalled`,
`cancelled`, `tick-raises`, `post-land-write`, `wait-deadline`, `infra`)
when the factory's own machinery broke rather than the task.

One gripe collects every node the same failure defeated: the signature
(class plus a normalised failing test, error line, or last log line)
tags the gripe `rescue-sig:<hash>`, and each rescued todo links
`related-to` it. An engine-class signature gripe also carries
`cluster:dark-factory` (a plain cluster label since gr591017) but no
`fix:code` and no agent/human `filed_by`, so it never re-enters a build
lane. A rescued gripe (three failed mints → `rescue:intake`) is its own
signature gripe. The failure's evidence (job, step, branch, head sha,
log tail, diff stat) sits as a comment on the rescued node; later nodes
on one signature leave a line each on the shared gripe.

```python
search(kind="todo", view="rescue")
```

The work view carries the same queue as a `rescue (N)` group after the
quest groups (twenty rows, then `… +K more`); the asks view files every
rescued node as a showstopper with `rescue:<class>` as its reason.

Rescue is a machine verdict, not a human hold: a node parked on a human
(`waiting-for:`, an open blocking `ask-user`, `halt`, `STATUS:paused`,
`won't-do`) is not in rescue, and a `rescue-sig:<hash>` tag marks a
signature gripe, never a rescued node. A node in rescue keeps its
worktree and branch, named on the evidence comment.

## What happens to a node in rescue?

The `factory_rescue_tick` job's second pass: a task-class rescue gets
ONE read-only diagnose task (lane `claude-sub`); the node is `blocked-by`
it meanwhile. The session replies with one first line:

- `rescue: REARM — <corrected brief>` — the tag comes off, a
  `STATUS:doing` node returns to `open` (the tick only dispatches
  `open`), `meta.rescue` (`n`, `at`, `diagnose`, `cls`) and the re-arm
  floor are stamped, and the corrected `brief:` comment goes on the
  node; the rebuilt run resumes on `slice/<id>`, reusing worktree and
  branch.
- `rescue: NOFIX fixed <sha> — <why>` — fixed by `<sha>`; verified on
  `origin/main`, the node closes and the gripe goes to verify.
- `rescue: NOFIX — <why>` — the cause is outside the brief.

## When does a rescued node get a diagnose task, and when an ask?

A task-class rescue with `meta.rescue.n < 1` gets the diagnose task.
Anything else (a `rescue: NOFIX`, a failed or unparsable diagnose, a
second rescue, every engine class) files one `ask-user` + `showstopper`
ruling todo blocking the node; closing it re-arms it.

## What does the transcript_audit pass sample, and how is it lit?

The `transcript_audit` worker pass (every 30 minutes, dark until
`service_config` lights it for a host, the same switch as
`gripe_dispatch`) samples closed factory chains: plan roots `done` or
`won't-do` within seven days that ran a `run_slice` or `land_slice` job,
taken at ten percent by root id (`PRECIS_TRANSCRIPT_AUDIT_PERCENT`
overrides), one mint per fire. Each gets an audit plan: a root tagged
`audit:transcript` and `thread:factory`, linked `related-to` the chain
root, and one task node pinned to lane `claude-sub` whose brief lists the
chain's job handles, the reading rule (read the job cards; an unreachable
transcript is one gripe and a stop) and the bar for a gripe: a blatant
skill- or MCP-rooted waste of five or more calls or two identical
retries, at most three per audit. The pass calls no model; the chain
root is never sampled twice.

## Can a review or a validate run on a lane without the precis MCP?
## Which plan nodes still need a Claude lane?

Yes for both. A reviewer mints nothing: its one verdict is the first line
of its final message (`review: LAND`, `review: FIX — <findings>`,
`review: REVERT — <why>`), which `run_slice` posts as the `handoff:`
comment and `plan_dispatch` reads there; it is read-only and ends the
verdict with `(read-only, tests not run)`. The reviewed todo's `landed:`
sha and brief are inlined, so `git show <sha>` in the slice worktree is
all it needs. A validate's
`validated: <why>` first line closes it the same way; a gripe chain's
validate body carries the gripe report verbatim. Devin fits `code`,
`review` and `validate`; codex takes `review` and `validate` only
(gr573563); the review is picked off the builder's vendor.
Nodes that mint or post through the MCP stay on a Claude lane: a triage
or slicer, a brief writer (posts `brief:`), a pcb fix; `run_slice`
refuses a child-minting generator on an MCP-less lane.

If no enabled lane offers a task's class, dispatch blocks the node on one
`ask-coordinator` ask. Enable an offering lane or retarget
`meta.task_class`, then close the ask. Full lanes just wait.

## What may a dispatched slice write over the MCP?

A dispatched slice authenticates with its own grant token; every write
stamps `slice:td<grant>:<role>` as its principal, server-set. Reads are
unrestricted. Writes are bounded by the role the slice's own todo
declares, under `mcp.slice_scope` (default `observe`: an out-of-scope
write still runs but its call row is marked `SliceScopeWouldRefuse`;
under `enforce` it refuses with Denied):

| role | may write |
|---|---|
| every role | comment on its own todo (`put(kind='todo', id=<own>, text=…)`); file a gripe |
| `builder` (no `generate` block) | + tags on its own todo; `SLICE_TAG_ALLOW` is empty until the observe readout fills it, so every tag refuses now |
| `generator` (slicer / spec-writer / triage) | + `put(kind='todo')` children under its own todo, meta limited to `{llm_tier, op}` with `op={'type':'run_slice','params':{'model','effort'}}`; + `link(rel='blocked-by')` between those children |
| `reviewer`, `analyst`, `brief`, `security-reviewer`, `diagnose` (a `meta.rescue_for` todo) | comment only |
| any other `generate.role`; an unreadable grant todo | comment — fail closed |

Everything else refuses: `edit`/`delete`, tags on foreign refs or with
`meta`/`prio`, other link rels, jobs, memory, settings, unknown verbs. A
refused write is not lost: put it in your final message and the worker
records it (the Denied's `next=` says so). A comment on your own todo is
also how a slice answers a question or reports a halt. The would-refuse
ledger is `get(kind='agentlog', id='/scope?…')`
([[precis-agentlog-help]]); the observe → `SLICE_TAG_ALLOW` → `enforce`
ladder is an operator runbook (`docs/runbooks/dark-factory-arming.md`).

## What is a pillar quest?
## Why is a paper a month todo and not a quest?

A pillar quest is a quest whose striving is one product pillar (tag
`pillar:<name>`); qu540835 is the platform pillar. It is the parent for
`serves` links from thread roots, so platform work ranks against science
quests on one board with one PRIO. It stays `STATUS:dormant` until the
build tick lands (a quest tick whose roles are slice / build / review
instead of research); dormant here means waiting, not stalled. Hang a
thread root under it:

```python
link(kind="todo", id=540836, rel="serves", target="quest:540835")
```

A paper is not a quest: a quest never finishes, a draft gets posted. The
cadence quest qu459585 holds one month todo per paper (td459586 is
October's); the paper's slices (citation pass, export, sign) are its
children; the draft is `draft-of` the month todo and `serves` every
science quest it draws on.

## See also

- [[precis-todo-tree-help]] — tiers, doable leaves, leases, ask-user
- [[precis-quest-help]] — quests, `serves`, PRIO flow-down, logbook
- [[precis-grill-me-help]] — opt-in question round before a build
- [[precis-gripe-loop-help]] — brief / handoff / review / landed
- [[precis-fleet-help]] — agent rows, hosts, the `slice` field
- [[precis-job-help]] — the jobs behind a row's `jobs=` cell
