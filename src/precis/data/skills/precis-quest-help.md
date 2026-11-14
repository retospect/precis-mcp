---
id: precis-quest-help
family: work
title: precis — the striving above the work
summary: quests — perpetual unachievable strivings that pull work + knowledge into their service; logbook, serves-graph, tree rollup
answers:
  - how do I mint a new perpetual striving (quest)?
  - how does PRIO: flow down from a quest to the work that serves it?
  - how do I log an entry in a quest's WORM logbook?
  - how is a quest different from a todo — why does it never go done?
  - how do I know a barrier number is trustworthy before I rank candidates or cite it?
applies-to: get/search/put/delete/tag/link (kind='quest')
tags: workflow
kinds: quest
status: active
---

# precis-quest-help — the striving above the work

A **quest is a perpetual, unachievable striving** — the medieval sense
(the Grail, not a milestone). *"A NO→NH₃ catalyst with no external
energy"*, *"heal the environment"*, *"a self-assembling molecular
computer"*: each is asymptotic. You never file it `done`; you **strive**
toward it, and it **drives** — it pulls subtasks and knowledge
acquisition into its service.

Quest is the **only** aim-layer kind. The achievable structure beneath
it is *not* a new thing: it's ordinary todos/projects (which own an
`open→done` lifecycle), marked as **serving** the quest by a `serves`
link. So the quest is the one un-completable node; the completable work
below it is the todo world you already have.

The canonical address is the **handle** `qu<id>` (e.g. `qu7`) — copy it
from search/get output. Logbook entries have their own handle `ql<id>`.

This doc is the **verb reference** — put/tag/link/get mechanics. For
the judgment call on *writing* a good striving (vision vs. BHAG vs.
SMART goal, keeping jargon and paper detail out, keeping quests few),
see [[precis-quest-writing-help]].

## Mint a striving

```python
put(
    kind="quest",
    text="A NO→NH₃ catalyst with no external energy\n\n"
    "Rubric: NH₃ selectivity · yield · stability",
)
# → created quest qu7 (STATUS:active).
# Create dormant atomically; avoid activating a parked test even briefly.
put(kind="quest", text="Synthetic parked striving", tags=["STATUS:dormant", "test-fixture"])

```

The **first line** is the striving statement; anything after a blank line
is criteria / rubric. Both embed (a quest *is a vector*). A quest defaults
to `STATUS:active`; an explicit `STATUS:` tag selects its initial state.

A striving is never a procedure — "how to evaluate a paper" is a
rubric/how-to and belongs in a `skill`, not a quest.

## Lifecycle — never `done`

A quest has **no achieved state**. It moves along a perpetual lifecycle:

```python
tag(kind="quest", id=7, add=["STATUS:dormant"])  # set aside (may reawaken)
tag(kind="quest", id=7, add=["STATUS:abandoned"])  # renounced
```

## Pause / resume — park the strategic roots, not just the quest tag

A plain `tag(..., add=["STATUS:dormant"])` sets the quest aside but leaves
its served work live. `edit(kind="quest", id=7, mode="pause")` goes
further — it also parks every **live strategic root** serving the quest
(a `serves`-linked todo with `meta.rotation_root` true, not already `done`
or `won't-do`): each gets `STATUS:paused` plus a `paused-by:quest` tag, the
quest moves to `STATUS:dormant`, and one logbook line names who paused it
and which roots. A root already `STATUS:paused` by hand is left alone and
never stamped `paused-by:quest`.

```python
edit(kind="quest", id=7, mode="pause")   # park the quest + its roots
edit(kind="quest", id=7, mode="resume")  # un-park only the roots it paused
```

`resume` lifts `STATUS:paused` and the stamp only from roots carrying
`paused-by:quest` — a root you paused by hand before the quest pause stays
paused — and moves the quest back to `STATUS:active`. Neither call cancels
a running job; `plan_dispatch`'s doable filter already walks
`STATUS:paused` up the tree, so a paused root's leaves leave
`view='doable'` within one tick. Both calls are idempotent: calling either
twice in a row writes no tag and no logbook line, and says so. This is the
MCP twin of the per-slice and per-quest Pause/Resume buttons on the `/work`
web page ([[precis-work-help]]).

`STATUS:done` (and every other workflow value) is **rejected** on a
quest — completing it would delete the "% done" axis as the wrong
measure. Progress is a **ledger of deeds**, not a percentage.

Tagging a quest `active` is also the affordance that **starts its
autonomous loop**: the `quest_loop_reconcile` pass ensures every active
quest has one live `quest_tick` coordinator loop and re-arms it if it
rests (gated on `PRECIS_QUEST_LOOP_ENABLED`, dark by default). Moving to
`dormant` / `abandoned` just stops new loops from being minted — the
current one winds down on its own. A tick resting with nothing runnable
raises `quest_tick/quest:dry-rest/<quest_id>` (warn, auto-resolves when
work appears); a live loop that sits queued or past its `at_time` wake
beyond the stall budget surfaces as `quest-tick-stalled:<quest_id>`
(watchdog `condition` group — see `precis-health-digest-help`).

## Priority — how hard it steers

A quest's **striving weight** is its priority, set with a `PRIO:` tag
(synced to the canonical `prio` column, 1 = hottest … 10; reads render
the ladder name with it — `priority: 3 (high)`):

```python
tag(kind="quest", id=7, add=["PRIO:urgent"])  # prio 1 → weight 1.0
put(kind="quest", text="…", tags=["PRIO:high"])  # at birth → prio 3 → 0.8
```

Only **active** quests exert pull. This weight flows *down*
the `serves` DAG (max-aggregation on overlap, light decay per quest→quest
ladder hop) into three places work is chosen: the todo **rotation** (a
project serving a hot quest surfaces sooner in the doable view), paper
**acquisition** (a stub serving an active quest jumps the fetch queue),
and **reading** (daily concepts bias toward quest-servers). It's a
**no-op until you link real work to an active quest** — reweight, don't
mint.

## Put work in a quest's service

Any node — a project/todo, a concept, a paper, a draft, a structure, or
a **sub-quest** — can serve a quest. It's one relation:

```python
link(kind="todo", id=42, target="quest:7", rel="serves")
link(kind="concept", id=91, target="quest:7", rel="serves")
link(kind="draft", id="<slug>", target="quest:7", rel="serves")
link(kind="quest", id=12, target="quest:7", rel="serves")  # sub-quest → grand quest
```

Always link from the server's side with `rel='serves'` — the inverse
`link(kind='quest', ..., rel='served-by')` "succeeds" but writes the edge
backwards: the tree rollup's cycle-guard then renders the quest as its
*own* server while the real server never appears (gripe 161912).

A quest may serve a grander quest — a **DAG of strivings** above the
ordinary tree of deeds. One concept can serve several quests (m2m); the
shared spine floats up as the highest-value work. A concept and a quest
stay distinct graphs even when linked — a concept is something you
*know*, a quest is something you *strive* toward.

**Sub-quest vs. achievable goal — the rule of thumb:** open-ended
*"the best / a … "* → a **quest** (it can never be finished); a
completable deliverable (*"screen these 20 candidates", "write the
review"*) → an ordinary **project/todo that `serves`** the quest. On
*how* to phrase the striving itself, see [[precis-quest-writing-help]].

## Candidate reasoning from a structure

New logbook entries mentioning a live `st<N>` handle automatically `cites`
that structure from the entry's chunk. The append receipt counts linked,
unresolved/skipped and failed targets; a failed link does not lose the entry.
Repeated mentions in one entry create one edge per structure.

The structure's `fisheye+1hop` groups these entries under **Notes & links**
by their stored section, falling back to entry type, then one plain group.
Each row names the logbook block (`qu<N>~<ord>`). New candidate mints also
log their structure handle and originating proposal block, carrying its
rationale into the candidate's neighbourhood. Existing entries are not
backfilled.

## The logbook — a WORM ledger of the journey

A quest keeps an **append-only, dated logbook** (like a lab notebook):
what happened, when, immutable. Append an entry with a type:

```python
put(kind="quest", id=7, text="Try Fe–N₄ single-atom sites", args={"entry": "hypothesis"})
put(kind="quest", id=7, text="Second PCET barrier too high", args={"entry": "dead-end"})
put(kind="quest", id=7, text="Dual-metal site clears both barriers", args={"entry": "milestone"})
put(kind="quest", id=7, text="relax batch A", args={"entry": "result", "by": "agent", "cost": 1.5})
```

- `args={'entry': …}` is one of **note · observation · hypothesis ·
  result · decision · dead-end · milestone · reflection · cost**
  (default `note`).
- A **`milestone` is a deed** — the honest, medieval sense of progress.
  The deed ledger is a filtered view of the log, not a separate store.
- **`dead-end` is first-class** — recording *what failed and why* stops
  the whole system re-treading it.
- `args={'by': …}` is **human · agent · dream** (default `human`).
- `args={'cost': …}` records spend; the **tote** (lifetime spend sunk
  into the quest) is just a sum over the dated log — no separate cost
  store.

The append path takes only `text` plus the `args` keys
`entry`/`by`/`cost` (top-level `entry=`/`by=`/`cost=` still run but
answer with a deprecation note); use `tag()` / `link()` on the quest
itself for status and the serves-graph.

## Read a quest

```python
get(kind="quest", id=7)  # statement + tote + logbook TAIL (last 10 entries)
get(kind="quest", id=7, view="tree")  # rollup: servers + deed ledger + health + gaps
get(kind="quest", id=7, view="gaps")  # just this quest's exploration queue
get(kind="quest", id=7, view="dossier")  # deprecated — the retired synthesis draft
get(kind="quest", id=7, view="frontier")  # Pareto frontier of candidate materials
get(kind="quest", id=7, view="leaderboard")  # ranked servers by deeds contributed
get(kind="quest", id=7, view="results")  # lineage-ordered results table (all bands)
get(kind="quest", id=7, view="series")  # controlled series: one axis varied per block
get(kind="quest", id=7, view="measures")  # best live measured value per measurand
get(kind="quest", id=7, view="logbook")  # lab notebook — newest 50 entries + next: older page
get(
    kind="quest", id=7, view="logbook",
    args={"since": "7d", "limit": 20}
)  # bounded window: ISO or relative ('7d','12h','2w') since=/until=, entry limit
get(
    kind="quest", id=7, view="logbook",
    args={"q": "trusted anchor"}
)  # ranked matching entries (ql handles), composes with since=/until=/limit
search(kind="quest", q="trusted anchor", scope="qu7")  # same retrieval
# rendered as a hit list — also args={"within": "qu7"}
get(kind="quest", id=7, view="pourbaix")  # per-candidate surface-Pourbaix validity maps
get(kind="quest", id=7, view="considerations")  # scoped verdict ledger — rel='considered' judgments, reopen queue + history
get(
    kind="quest", id=7, view="log"
)  # raw ref-events audit trail (generic, not logbook-specific)
get(kind="quest", id="/active")  # every active striving
get(kind="quest", id="/gaps")  # gaps across ALL active quests
search(kind="quest", view="roots")  # top-level strivings + serves DAG (id="/roots" too)
get(kind="quest", id=7, q="perovskite")  # passages inside this quest (find-in-ref)
```

**The complete `view=` set is** `tree · gaps · dossier · frontier ·
leaderboard · results · series · measures · logbook · pourbaix · status ·
rubric · served · meta · replay · kinetics-summary · considerations`
(quest-specific — `dossier` is deprecated, kept only for
quests not yet converted) plus the generic
`links · log · raw`, plus the fisheye ladder (`kwd · summary · verbatim ·
fisheye · fisheye+1hop · fisheye+2hop`, any rung with `+recall`):
`view='fisheye+1hop'` shows the quests this one serves and the quests
serving it under `Roadmap:`, with the findings, papers and structures
that serve it (`served-by`, sub-quests then findings first) capped at 8
per group, see
`precis-fisheye-help`. The table views (`results`, `frontier`, `series`,
`measures`) are in the next section.
A *deed* is not a view — it is the milestone-typed slice of the log. Bare
`get(id=N)` digests the logbook **tail** (last 10 entries, cheap even on a
quest with thousands); `view='logbook'` is the append-only
notebook paged newest-first (default `limit` 50 — `args={'since': '7d',
'until': '2026-10-01', 'limit': 100}` bounds the window, and each page's
`next:` names the call for the older page); `view='log'` is the raw
ref-events ledger, a different
(generic) thing.
`view='status'` is the one-call orientation read (~1 kB): the lifecycle
line (striving status · priority · horizon) plus the frontier counts —
candidates by band and fidelity tier, trusted-per-axis coverage
(including declared axes at zero coverage), the frontier handles — then
momentum, the open gaps, the last `quest_tick` coordinator tick (real
tick activity, `never` while every minted job is still queued), and the
operator holds in force (`fidelity_restale_cap=0`,
`compute_lane`/`lit_lane` `off`). It replaces a frontier+gaps+results
round-trip for "where does this quest stand". `view='rubric'` prints just
the measured axes (`rubric_objectives` + demand/supply). `view='raw'` shows
scalars + meta + a capped links section (`view='links'` is the full graph).

## Read a quest — served refs, the tree and the web hub

`view='served'` lists every live ref with an inbound `serves` edge, one
row each with a `fulltext` flag (body chunks held), plus a count line.
`args={'has_fulltext': True|False}` filters it:

```python
get(kind="quest", id=164903, view="served", args={"has_fulltext": True})
```

`view='tree'` is the map: it walks who serves the quest (grouped by
kind), recurses into sub-quests, prints the deed ledger + tote, and ends
with a **health** line and a **gaps** list at the foot. On a `"roadmap"`-
body root it also renders the capability ledger table — see
[[precis-roadmap-help]].

All of this is on the web too: `/refs/quest/<id>` is a dedicated hub
dashboard (header + momentum/tote, the graph neighbourhood + logbook
tail, frontier/gaps panels, servers-lite) — readable without calling
`get(view=…)`.

## Read a quest's numbers — results, frontier, series, measures

`view='results'` is one row per candidate across
*every* band (not just the frontier), grouped by lineage (dopant, then
what varies) rather than band. It renders the compact agent table
`precis-toon` documents by default;
`args={'format': 'text'}` swaps in the fixed-width copy the tick
prompt embeds — "what the next tick would see now". `view='results'`
and `view='frontier'` are **budgeted** like the tick's copy (2500
tokens; rows drop from the tail, never the newest ten; the body says
`(+K … rows omitted)`); `args={'budget': 8000}` widens it. The measure
columns are the pathway axes plus the quest's declared **objective**
columns — for a quest with no `meta.rubric_objectives` that is `energy`,
so a completed structure relax shows its converged result instead of an
empty row. On an
`inquiry`-body quest (see "Patch quest meta" below) `view='frontier'`
says the body has no Pareto frontier — it never proposes, so nothing
converges.
`results`, `leaderboard` and `frontier` also take
`args={'trusted': true}` — only candidates carrying an affirmative
trust verdict (the rows the `trusted` column reads `yes`: no flipped
`*_trusted` flag, no `*_untrusted_value` stash, no network-stale mark)
— and `args={'sort': '<measure>'}`, ranking rows by any carried
measure on its declared objective sense (`min` when undeclared,
unmeasured last). "Which candidate is trusted AND best" is
`args={'trusted': true, 'sort': 'barrier'}`. When the confirmed
frontier is empty because everything measured is unconfirmed, the
headline's per-axis counts name the ≤10 handles that DO carry it
trusted (`barrier ×252 (trusted: st123)`).
`view='series'` re-cuts the same rows as **controlled comparisons**: one
block per set of candidates that share a base and differ in exactly one
axis (`dopant · n_dopant · site · coads`), so a difference down a block is
attributable to that axis. It recognises series already in the data —
never enumerates a grid, says plainly when there are none. A
level's numbers are comparable only where `trusted=yes`. The axes come from
each candidate's `meta.params`, stamped at proposal time from its structure
ops (a co-adsorbate carries the named site it was placed on, e.g.
`H2@hollow`); a candidate that predates the stamp falls back to counting
its atoms, which cannot see a site.
`view='measures'` is the quest's number table: from the `measures` rows of
everything that serves the quest (at any depth), the best live value of each
measurand, one line per `(measurand, reference, normalization)` group, shown
in the measurand's display unit with the run's conditions, the tier and the
paper. "Best" follows the taxon's `higher_is_better`; a group without it
reports its row count and no best. Rows that are ambiguous, flagged
(`meta.escalation`), anchor-lost or distrusted are left out — never compared
across groups. See `precis-measure-help`.

## Read a quest's numbers — Pourbaix maps and staircase points

`view='pourbaix'` is the resting-state surface-Pourbaix validity map per
candidate structure serving the quest: stored `surface_coverage_scan`
sweeps folded into validity regions per U interval (two bands per boundary —
propagated and model-form, never summed), the bulk Pourbaix verdict as a
labelled uncertain overlay, one map per footing (partitioned, never
blended), the `needs_anchor` ranking, and a `dropped:` line per cause for
anchors the sweep left out (detached configurations, uncorrected or
other-footing models). Candidates without scans list as `no anchors`.
Same map on a structure (`view='pourbaix'` on `kind='structure'`) and as
an SVG at `/pourbaix/<handle>`.

To judge a candidate's reaction staircase at one point on that map, pass
the point to `view='staircase'` on the structure or its pathway:

```python
get(kind="structure", id="pd111", view="staircase", args={"U": -0.3, "pH": 7})
get(kind="pathway", id="pw483780", view="staircase", args={"point": "-0.3,7"})
```

Each rung line gains a state; a merged proton-electron step is judged
once and both halves show it.

- `off_map`: the map rests on a different surface at U than the one the
  pathway was computed on.
- `thermally_blocked`: a U-flat step more than 0.3 eV uphill.
- `electro_uphill`: a proton-electron step uphill at U; the line says
  where it opens.
- `viable`.

A point block follows the rungs: `window inside | outside | outside
(HER)`, the resting termination, and the worst rung with its reason. On
the web page, click, drag or arrow-key the map to pick the point. Rung
rows recolour from `/pourbaix/<handle>/states?U=&pH=` (the same
verdict); overlay toggles show where each rung is viable.

## Health + gaps — the exploration queue

A quest is measured by *striving*, not finishing, so the tree rollup ends
with two read-time, mechanical reads (no `% done`):

- **health** — *momentum* (`quiet` / `stalled` / `warming` / `active`,
  from recent logbook entries, server activity, open todos moving and
  any `child-failed` bubble) and an *alignment* floor (cosine proximity
  of the quest's card vector to each server's — a best-effort on-aim
  flag; unembedded servers are skipped).
- **gaps** — the exploration queue: **thin-support** (almost nothing
  serves it), **no-literature** (work under way with no `paper`
  grounding), **low-mastery** (a served `concept` you don't understand
  yet), **open-hypothesis** (a `hypothesis` logbook entry with no later
  `result`/`dead-end`), **needs-experiment** (a graduated candidate
  waiting on a real-world experiment), **needs-tool** (an open gripe the
  tick filed for a capability that doesn't exist — see the next section).
  Gaps *are* where to look next.

`view='gaps'` focuses one quest; `id='/gaps'` rolls the queue up across
every active quest, hottest first. All degrade to empty until quests +
servers exist.

## A tick can ask for a tool that doesn't exist — `needs`

Every quest body's tick output accepts an optional `needs` list —
`{"tool", "why", "would_unblock"}` — for a tool, data source, sim lane or
catalog entry the tick needs but does not have. The **executor** (code,
not the model) turns it into a gripe: one per `(quest, tool)`, a repeat
sighting bumping the open gripe with a comment rather than filing a
second; tagged `quest:<id>` + `needs-tool:<slug>`; linked `blocks` → the
quest, so the gap list reads `blocked on gr<id>: <tool>` and the dry-rest
alert cites the handles. Closing the gripe clears the gap.

What a tick already has, before it declares a need: `searches` (graph +
Semantic Scholar lit acquisition), `read` (full text of a held
`fi`/`pa`/`pc`/`td` item or a `web:<id>` fetched page, next tick's context),
`fetch` (`{"paper": …}` pulls a served stub's full text; `{"url": …}`
fetches a live page through the `web` cache — vendor pricing, changelogs,
announcements — serves-linked, rendered next tick), `proposals` /
`reruns` (sim lanes), `dialectic_ops` + `ledger_ops` (structure).

## Directions — where attempts and ideas live

A quest keeps *two* records. The **logbook** is episodic (what happened,
when — WORM). The **synthesis is the graph itself** — hypotheses,
findings, papers and structures linked `serves`, plus the direction
tree below, read through the fisheye / `tree` / `status` views.

**Attempts and ideas are direction todos.** A *direction* is a `todo`
`part-of` the quest (a root), or nested under a direction by
`parent_id` (a *strand* is a direction with children) — the old pinned
attempt ledger as real, linkable refs. State rides the ordinary todo
`STATUS` axis ([[precis-tags]]); the `ATTEMPT:` vocabulary maps
exactly — `open`→`open`, `active`→`doing`, `tried`→`done`,
`ruled-out`→`won't-do` (`blocked`/`paused` still read as open).
`done`/`won't-do` are the do-not-repropose set — a ruled-out branch
reads as a subtree. **Pending integration:** the tick still takes its
do-not-repropose list from the dossier's pinned ledger chunks on
unconverted quests — `quest/tick.py` reads and writes dossier chunks
only, not direction todos — so the direction tree stops *human and
graph readers* re-treading a branch today, and stops the tick only
once the tick's ledger reads switch to it (see "The dossier is
deprecated" below).
Directions are **not work**: the
rotation fence keeps them out of `view='doable'`/`'queue'`, the nursery
and the momentum counts, so minting one never queues a task.

```python
put(kind="todo", text="Pd(111) subsurface-H co-doping")
link(kind="todo", id=<td>, target="quest:7", rel="part-of")  # a root direction
put(kind="todo", text="…", parent_id=<strand td>)            # refine under a strand
tag(kind="todo", id=<td>, add=["STATUS:won't-do"])           # rule it out
```

**`ATTEMPT:` never goes on a ref** — it was chunk-level only, valid on
the dossier's pinned ledger chunks, and retires with it. Attempt state
lives only on a direction todo's `STATUS`; a standalone memory, finding
or todo linked to the quest carries none. The convention for what hangs
where (Reto 2026-11-13):

| thing | lives as | its state |
|---|---|---|
| an idea / an approach | direction `todo` `part-of` the quest | `STATUS` open→doing→`done`/`won't-do` |
| the idea's narrative | `memory` `part-of` the direction todo | prose, no state |
| a check or research step — lit search, acquisition, test run, exploration | `todo` `part-of` the direction | its own `STATUS`; stays ordinary `doable` work |
| an open question | a `hypothesis` logbook entry | the `open-hypothesis` gap tracks it |
| evidence — finding, paper, measure, structure | `serves` the quest | — |

An idea's state never rides a tag — not `ATTEMPT:`, not
`DREAM:speculative`: a try that ran its course marks `done`, a
never-again `won't-do`, and the *why* is a `hypothesis`/`dead-end`
entry. **Open questions stay out of the striving statement** — the first
line keeps the one asymptotic sentence ([[precis-quest-writing-help]]); a
"what if…" is a `hypothesis` entry where the gap list sees it. Nest a
check with `part-of`, never `parent_id` — a `parent_id` child joins the
direction tree itself and leaves `doable` with it. Link evidence
`serves` the **quest**, and the direction too when you want the answer
pinned to that branch: the quest readers (gaps, momentum, alignment, the
frontier) still collect `serves` one hop from the quest
(`gaps._live_servers`), so a direction-only link is invisible to them
until retire-dossier slice 5 wires `directions.quest_evidence` through.
When you search the tree (or the literature) for prior art, phrase the
query as the thing you expect to find, not as a question — *"subsurface
H co-doping on Pd(111) lowers NO dissociation barrier"*, not *"does H
help?"*. Search matches statements; [[precis-search-help]]'s `answers=`
legs are the same move for papers.

## The dossier is deprecated

The `dossier-of` `draft` — a narrative **rewritten each tick** plus
pinned `ATTEMPT:`-tagged ledger chunks — is being retired (Reto
2026-11-11): everything it held lives in the graph above.
`view='dossier'` and `precis quest dossier` still render it, and a tick
still rewrites it on quests not yet converted, but it is no longer
where open questions or synthesis live — read it as history.
`put`/`edit`/`delete` on a `dossier-of` (or `paper-of`) draft remains
refused with `Unsupported`; the tick owns its writes while it lasts.

## A research tick

A **research tick** is one bounded step of the (future) autonomous loop:
it reads the quest's rolling context (statement + synthesis + gaps +
momentum + logbook tail), does one increment of reasoning, appends 1–4
logbook entries, updates its synthesis state (still written to the
deprecated dossier on quests not yet converted — see above), and may
**propose candidate materials**. Run one by hand:

```
precis quest tick 7            # one reasoning tick against quest 7
precis quest tick 7 --dry-run  # print the assembled context, no LLM call
precis quest tick 7 --compute  # ALSO simulate proposed candidates (GPU relax)
precis quest dossier 7         # the deprecated dossier (see above)
precis quest frontier 7        # the Pareto frontier of candidate materials
precis quest redispatch 7      # re-score every candidate on the deployed engine
precis quest backfill-network-basis 7          # dry run: count stampable measures
precis quest backfill-network-basis 7 --apply  # write the stamps
```

A tick does not edit repo files. To land prose in a repo file (docs, a
skill, a backlog spec), file a gripe carrying the text; a session applies it.

`redispatch` re-runs each candidate at its **own highest completed rung**,
not a blanket neb — a verify-tier candidate costs a full verify pass
(3+ seeds, ~2 h each/GPU), so use it by hand after an engine deploy,
not in a loop. It collapses onto existing jobs when the engine token is
unchanged.
`reset-compute` nulls derived stamps but keeps `meta.neb_edges`; next
tick re-stamps off it.

To re-measure ONE named candidate on demand — a deliberate rerun to test a
credited pass's reproducibility — call
`put(kind='structure', id=<st<id> or slug>, mode='rerun')`
(`args={'tier': 'screening'|'neb'|'verify'}` overrides the rung; default is
the candidate's highest completed rung; `args={'seeds': [<ints>]}`
names the dispatched seed set — at most 8, default the reaction config's
pinned seeds). The rerun mints a fresh seed+aggregate tree under a
`rerun:` nonce so it never dedups onto the earlier result; the new
`pathway` carries `meta.rerun_of` plus a `derived-from` link to the
prior one, which stays `ready` (not superseded) so both results
compare. Bounded: one in-flight evaluation per candidate — a second
request while one runs is refused — and it respects the quest's compute
lane (`meta.compute_lane='off'` refuses). The same door is on the
tick's tool surface: a tick emits
`"reruns": [{"structure": "<handle>", "why": "…", "seeds": [<ints>]}]`
(cap 2/tick); the compute stage runs each through the same path,
recording per-entry refusals as logbook observations. Fresh seeds give
the `endpoint_agreement` trust check new basins to compare;
`get(kind='pathway', view='trust')` shows the whole trust gate — each
check's verdict, input and threshold — plus per-state `delta_meV`
beside the `tol_eV` it was judged against (see `precis-pathway-help`).

New seeds round-robin over the live gpu nodes by default. `--node
<name>` pins them all to that node; `--spread` gives each to the
least-loaded live node (queued + running gpu jobs, ties by name).
Neither picks a retired ssh_node lane; the ack names the per-node
counts. Already-queued seeds stay put (`job` `mode='retarget'` moves
those).

A margin measured on an older reaction network (another catpath template or
version) does not rank — it shows provisional with reason "measured on an
older reaction network (template@version)" until re-measured.
`backfill-network-basis` stamps the network on pre-stamp candidates, only
where a completed pathway reproduces the stored value.

## A research tick — searches and compute

**Searches go local first.** A tick's `searches` hit our own graph (papers,
findings, drafts, concepts, memories) before anything outside. Semantic
Scholar is queried and papers acquired only under 3 relevant local hits;
an S2 failure keeps them. Local papers, findings
and concepts are linked `serves` (max 3 per query); drafts and memories
count as hits but are not linked. The logbook line per query reads
`[local N, acquired M; …]`.

## A research tick: compute and the frontier

**Compute.** With `--compute`, each proposal that carries a
concrete atomistic `structure` (a periodic cell + atoms) becomes a
`structure` that `serves` the quest (the graph *is* the memory of
explored space), content-addressed so re-proposing a material is a cache
hit. Its relax is dispatched on the GPU node (the derived compute lane);
a later tick **harvests** the result into a `result` logbook entry (with
an energy + step-count cost that feeds the tote). A candidate whose relax
fails is `ruled-out:`-tagged so the proposer never re-treads it. The
converged candidates form a **Pareto frontier** over the quest's
objective vector (override via `meta.rubric_objectives` — a list of
`{key, sense}` items; add `"optional": true` to an axis a lower ladder
tier cannot yet measure, e.g. `P_side`, so a candidate lacking it is still
evaluated and scored worst there instead of parked as unevaluated; the catalyst
default ranks `log_tof` max (activity, from the kinetics model —
`barrier` demotes to a context scalar it's derived from) ·
`atom_cost` min (mass-weighted $/kg — a soft economic axis: a dear-but-
active composition still competes) · `selectivity_margin` max
(side-product selectivity) · `poison_margin` max (site-competition
resistance — needs `reaction_config.poisons`, e.g. `["CO"]`)), shown by
`view='frontier'`. The view splits three ways: the **confirmed frontier**
(trusted, converged measurements — the only citable barriers),
**provisional** candidates (measured but unconfirmed — an untrusted
barrier or a missing relax; values shown with their exclusion reason and
Pareto-ranked separately), and **awaiting a sim** (no measurement at
all). A `★` marks a graduated candidate (earned the `needs-experiment`
milestone); `★ would lead` on a provisional row means it would top the
frontier if its measurement were trusted. The loop dispatches **one
proposal per tick**
(`PRECIS_QUEST_MAX_PROPOSALS`, default 1) and waits for its sims before
the next.

## Skip the guesswork — opt a quest into a structure search

Set the quest's `meta.search = {seed, box, add, algo?, model?, budget?,
top_k?, timeout_s?}` (`seed` a structure that already `serves` this
quest — see `precis-structure-help`'s `struct_search`) to spend the
tick's one proposal slot on a surrogate search instead of an
LLM-authored candidate. One search per `(seed, box, add, algo, model,
budget)` — changing any of them re-arms it; a failed search is not
retried automatically, edit `meta.search` to try again. The returned
candidates land as ordinary `serves`-linked `structure` rows with an
`ml` energy already measured, so the next tick's proposer sees and
argues them like any other candidate — it never sees the search
internals.

The autonomous *scheduling* of ticks (a perpetual per-quest coordinator
loop, not a single step) is dark by default: `PRECIS_QUEST_LOOP_ENABLED`
gates it, the manual CLI runs regardless, and compute stays off without
`--compute`.

## Patch quest meta — lanes and bodies

`edit(kind='quest', id=N, meta={...})` patch-merges an **allowlisted** key
into `refs.meta` (siblings untouched — never a whole-dict clobber); an
unknown key is refused, naming the allowlist. The write lands as a
`meta_patched` row in `view='log'`. Same allowlist over the CLI:
`precis quest set <id> <key> <value>` (`value` is JSON-decoded when it
parses, so `true`/`[...]` land typed — `off` stays the plain string
`"off"`) — the lever to reach for when the MCP itself is the thing
that's wedged.

- **`compute_lane`** — `"off"` makes every armed tick of *this* quest
  reason-only: no `--compute` dispatch. Any other value, or the key's
  absence, keeps compute on. Lit-search does NOT gate on this switch
  (gr500282) — it is network, not GPU compute; use `lit_lane` for that.
- **`lit_lane`** — `"off"` skips the tick's lit-search stage and the
  guaranteed-acquisition fallback (a quiet propose step's force-fired
  lit-search — that fallback used to key on `compute_lane`; it keys on
  this switch since gr500282, and before that fix `compute_lane=off`
  silently starved a lit-only quest's searches). Any other value, or
  the key's absence, keeps lit-search on.
- **`quest_body`** — which tick body runs the loop. Unset (the default) is
  the catalyst/materials-discovery body above, unchanged. `"weave"` routes
  to the paper-writing body instead; see `mark_weave_quest`. `"inquiry"` is
  a third body for a striving that reads, reasons, and synthesises rather
  than searching a materials space — same logbook/dialectic/lit-search
  machinery (and the deprecated dossier until the quest converts), but
  **no proposal menu and no Pareto frontier**
  (`view='frontier'` says so plainly). Never inferred from other meta (a
  live materials quest can carry no `reaction_config`/`compute_lane`/
  `rubric_objectives` at all) — set it explicitly for a striving with no
  candidate materials to propose (literature synthesis, landscape
  mapping).
- **`"roadmap"`** — a fourth body: grows a pathway/capability/rung graph
  from measured gaps instead of proposing materials or writing prose.
  Only the root quest ticks; see [[precis-roadmap-help]] for the rung
  meta contract and the three roles.

`edit(kind='quest', id=N, text=...)` (the founding-statement rewrite) and
`meta=` may be combined in one call, or `meta=` passed alone — no `text=`
required for a meta-only patch.

## Patch quest meta — rubric, operating conditions and reaction config

- **`rubric_objectives`** — the frontier's measured-axis override, see
  "A research tick" above. On a catalyst quest with
  `surface_coverage_scan` anchors the **window/Shapley fields** stamp on
  each scanned candidate whether or not they are declared:
  `window_area_VpH` / `window_fraction` (the operating window — resting
  surface ∩ every staircase rung ∩ HER), `shapley_sumsq_raw` /
  `shapley_ipr` / `ridge_churn` (which rung owns the lost area),
  `her_margin_at_Uopt` (limiting HER rung minus the route's at the
  window's U centroid — needs the pathway run with `network.her`),
  `reset_window_area` / `reset_window_fraction` (the guaranteed-reset
  window beside the thermodynamic one), plus the diagnostics
  `bottleneck_rung`, `window_notes` and `validity_region_at_point`.
  Declaring one in `rubric_objectives` (`{"key": "window_area_VpH",
  "sense": "max"}`) promotes it to a Pareto axis; `meta.window_objectives`
  tunes the knobs (`eps_eV`, `her_margin_eV`, `reset_eta_eV`,
  `reset_ea_floor_eV`).
- **`operating_conditions`** — human-set bulk-Pourbaix point:
  `{"U_RHE": -0.2, "pH": 7, "ion_conc_M": 1e-6,
  "window": {"U_RHE": [-0.4, 0], "pH": [7, 10]}}`. U_RHE and pH
  are required finite numbers; ion_conc_M is optional in (0, 10];
  window optionally bounds U_RHE and/or pH with ordered `[lo, hi]` spans.
- **`reaction_config`** — the autocatpath config the compute lane
  dispatches. A dict of engine `Config` field names only (unknown keys
  refused); `network_her` must be a bool. The patch **shallow-merges**
  into the stored config — `meta={'reaction_config': {'network_her':
  true}}` flips the HER-competitor branch without touching
  `slab`/`mlip`/`search` — so re-dispatched pathways run with it on.

## Patch quest meta — environment space

- **`environment_space`** — human-set bounds and goals for the per-slab
  environment optimizer. Set with the same meta-edit door:
  `edit(kind='quest', id=N, meta={'environment_space': {'axes':
  {'T': [350, 900], 'p_total': [0.1, 100, 'log'],
  'U_RHE': [-0.4, 0], 'pH': [7, 10]}, 'TOF_floor': 0.001}})`.
  `axes` and `TOF_floor` are required. Known axes are T (K), p_total,
  NO and H2 (bar), U_RHE (V), pH, and ion_conc_M (M). Each span is
  finite `[lo, hi]` with lo <= hi (equal fixes an axis); a third
  `'log'` entry requests logarithmic sampling and requires lo > 0.
  T, p_total and ion_conc_M must be positive; NO/H2 may start at zero
  on a linear axis. Optional `selectivity_floor` in (0, 1] adds a mask
  constraint. TOF_floor (/s) and optional `tau_s` (default 1 s, barrier
  equivalent) must be finite and positive. Optional `weights` selects
  the weighted form instead of the bottleneck: a map of objective terms
  `m_th`, `m_kin`, `m_HER`, `s`, `r`, `m_Ea` to finite nonnegative
  weights, at least one positive; omitted terms have zero weight.
  Each edit replaces the whole environment_space block. At optimizer
  read time, missing axes use T [350, 900], p_total [0.1, 100, 'log'],
  and operating_conditions windows or fixed points (including declared
  ion_conc_M). Each declared axis replaces that axis's default individually,
  including its spacing: a p_total range without `'log'` loses the default
  log spacing and samples linearly. Missing coordinates stay absent.
  With no environment_space,
  TOF_floor defaults to 1e-3 /s and tau_s to 1 s; no selectivity floor
  or weights are inferred. Defaults are never persisted by a tick.
  Ticks cannot change either human-set conditions block. Default
  `get(kind='quest', id=N)` and `view='meta'` show the declared space.

## Trust a barrier before you rank on it

`barrier_trusted` (behind the confirmed/provisional split above) goes
`False` on: adsorbate detached, wrong binding site, NEB not converged,
`|barrier| > 8 eV` (nonphysical), relax converged in 0 steps (the geometry
never actually moved), symmetry-twin disagreement (re-measure both),
fewer than 2 independent seeds at neb tier (`barrier_seed_blocker`:
`seeds:<n>-independent` — pooled across the run and its ready siblings
so a rerun re-using a seed re-verifies — or `seeds:unknown` without
evidence; `barrier_independent_seeds` carries the count), and an older
engine stamp (`engine:stale`; `engine:unknown` on an unstamped trusted
edge). A stale edge re-runs on current — struct_neb self-queues,
autocatpath via restale (opt-in ladder, `fidelity_restale_cap` > 0,
frontier/top provisional band; `=0` is the master hold on every automatic
pathway re-mint — promotes and repair re-dispatches included — while
`put(mode='rerun')` stays the deliberate door). Seeds must agree:
`endpoint:union-disagree` / `endpoint:union-not-run`. **A barrier near
0 eV carries none of these flags** — a broken/degenerate NEB, not a
record-low — treat as invalid until checked.

A refused gate names its blockers (`barrier_seed_blocker`,
`barrier_blocked_by` check + subject) and points at
`view='neb' args={'run': N}` / `view='trust'`; logbook results carry
the same digest.

A single **low** trusted barrier is strong evidence — an upper bound, since
a path that cheap exists. A single **high** barrier rules nothing out — the
seed may have missed the easy path. Ruling a direction out needs several
seeded attempts, all high; a single read only ever supports a lead, not a
rule-out.

Same-crystal repeats have disagreed by ~1 eV between two otherwise-identical
runs (one or both invalid, or genuinely different paths found) — never rank
two candidates on a margin under ~1 eV without a replicate agreeing.

A proposal rejected with `'NoneType' object has no attribute 'get'` is
infrastructure noise — resubmit the identical proposal unchanged. A
rejection naming a real geometry problem ("structure preflight rejected
this edit") is a genuine veto — fix the edit, don't resubmit it.

`view='frontier'` lags the system log by hours to days — a barrier logged
as a `result` logbook entry can sit unpropagated for a day before it shows
on the frontier. The frontier is authoritative but late; read the logbook
tail before concluding a quest has stalled.
