---
id: precis-quest-help
title: precis — the striving above the work
summary: quests — perpetual unachievable strivings that pull work + knowledge into their service; logbook, serves-graph, tree rollup
applies-to: get/search/put/delete/tag/link (kind='quest')
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

## Mint a striving

```python
put(kind='quest',
    text="A NO→NH₃ catalyst with no external energy\n\n"
         "Rubric: NH₃ selectivity · yield · stability")
# → created quest qu7 (STATUS:active).
```

The **first line** is the striving statement; anything after a blank line
is criteria / rubric. Both embed (a quest *is a vector*). A quest is born
`STATUS:active`.

## Lifecycle — never `done`

A quest has **no achieved state**. It moves along a perpetual lifecycle:

```python
tag(kind='quest', id=7, add=['STATUS:dormant'])    # set aside (may reawaken)
tag(kind='quest', id=7, add=['STATUS:abandoned'])  # renounced
```

`STATUS:done` (and every other workflow value) is **rejected** on a
quest — completing it would delete the "% done" axis as the wrong
measure. Progress is a **ledger of deeds**, not a percentage.

## Priority — how hard it steers

A quest's **striving weight** is its priority, set with a `PRIO:` tag
(synced to the canonical `prio` column, 1 = hottest … 10):

```python
tag(kind='quest', id=7, add=['PRIO:urgent'])   # prio 1 → weight 1.0
put(kind='quest', text='…', tags=['PRIO:high'])  # at birth → prio 3 → 0.8
```

Only **active** quests exert pull. From slice 2 this weight flows *down*
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
link(kind='todo', id=42, target='quest:7', rel='serves')
link(kind='concept', id=91, target='quest:7', rel='serves')
link(kind='quest', id=12, target='quest:7', rel='serves')  # sub-quest → grand quest
```

A quest may serve a grander quest — a **DAG of strivings** above the
ordinary tree of deeds. One concept can serve several quests (m2m); the
shared spine floats up as the highest-value work.

**Sub-quest vs. achievable goal — the rule of thumb:** open-ended
*"the best / a … "* → a **quest** (it can never be finished); a
completable deliverable (*"screen these 20 candidates", "write the
review"*) → an ordinary **project/todo that `serves`** the quest.

## The logbook — a WORM ledger of the journey

A quest keeps an **append-only, dated logbook** (like a lab notebook):
what happened, when, immutable. Append an entry with a type:

```python
put(kind='quest', id=7, text="Try Fe–N₄ single-atom sites", entry='hypothesis')
put(kind='quest', id=7, text="Second PCET barrier too high", entry='dead-end')
put(kind='quest', id=7, text="Dual-metal site clears both barriers", entry='milestone')
put(kind='quest', id=7, text="relax batch A", entry='result', by='agent', cost=1.5)
```

- `entry=` is one of **note · observation · hypothesis · result ·
  decision · dead-end · milestone · reflection · cost** (default
  `note`).
- A **`milestone` is a deed** — the honest, medieval sense of progress.
  The deed ledger is a filtered view of the log, not a separate store.
- **`dead-end` is first-class** — recording *what failed and why* stops
  the whole system re-treading it.
- `by=` is **human · agent · dream** (default `human`).
- `cost=` records spend; the **tote** (lifetime spend sunk into the
  quest) is just a sum over the dated log — no separate cost store.

The append path takes only `text`/`entry`/`by`/`cost`; use `tag()` /
`link()` on the quest itself for status and the serves-graph.

## Read a quest

```python
get(kind='quest', id=7)               # statement + logbook timeline + tote
get(kind='quest', id=7, view='tree')  # rollup: servers + deed ledger + health + gaps
get(kind='quest', id=7, view='gaps')  # just this quest's exploration queue
get(kind='quest', id=7, view='dossier')  # the living research synthesis (slice 4)
get(kind='quest', id=7, view='frontier') # Pareto frontier of candidate materials
get(kind='quest', id='/active')       # every active striving
get(kind='quest', id='/gaps')         # gaps across ALL active quests
```

`view='tree'` is the map: it walks who serves the quest (grouped by
kind), recurses into sub-quests, prints the deed ledger + tote, and — from
slice 3 — a **health** line and a **gaps** list at the foot.

## Health + gaps — the exploration queue (slice 3)

A quest is measured by *striving*, not finishing, so the tree rollup ends
with two read-time, mechanical reads (no `% done`):

- **health** — *momentum* (`quiet` / `stalled` / `warming` / `active`,
  from recent logbook entries + recent server activity + open todos
  moving − any `child-failed` bubble) and an *alignment* floor (cosine
  proximity between the quest's card vector and each server's — a
  best-effort "is this still on-aim?" flag; servers not yet embedded are
  skipped).
- **gaps** — the exploration queue: **thin-support** (almost nothing
  serves it), **no-literature** (work under way with no `paper`
  grounding), **low-mastery** (a served `concept` you don't understand
  yet), **open-hypothesis** (a `hypothesis` logbook entry with no later
  `result`/`dead-end`). Gaps *are* where to look next.

`view='gaps'` focuses one quest; `id='/gaps'` rolls the queue up across
every active quest, hottest first. All degrade to empty until quests +
servers exist.

## The dossier + a research tick (slice 4a)

A quest keeps *two* records. The **logbook** is episodic (what happened,
when — WORM). The **dossier** is semantic: a `draft` the quest owns
(`dossier-of`), the *living synthesis* — current understanding, best
leads, what's ruled out, open questions — **rewritten each cycle**. It
doubles as the loop's rolling context.

```python
get(kind='quest', id=7, view='dossier')   # read the synthesis
```

A **research tick** is one bounded step of the (future) autonomous loop:
it reads the quest's rolling context (statement + dossier + gaps +
momentum + logbook tail), does one increment of reasoning, appends 1–4
logbook entries, rewrites the dossier, and may **propose candidate
materials**. Run one by hand:

```
precis quest tick 7            # one reasoning tick against quest 7
precis quest tick 7 --dry-run  # print the assembled context, no LLM call
precis quest tick 7 --compute  # ALSO simulate proposed candidates (GPU relax)
precis quest dossier 7         # print the dossier
precis quest frontier 7        # the Pareto frontier of candidate materials
```

**Compute (slice 4b).** With `--compute`, each proposal that carries a
concrete atomistic `structure` (a periodic cell + atoms) becomes a
`structure` that `serves` the quest (the graph *is* the memory of
explored space), content-addressed so re-proposing a material is a cache
hit. Its relax is dispatched on the GPU node (the derived compute lane);
a later tick **harvests** the result into a `result` logbook entry (with
an energy + step-count cost that feeds the tote). A candidate whose relax
fails is `ruled-out:`-tagged so the proposer never re-treads it. The
converged candidates form a **Pareto frontier** over the quest's
objective vector (default: minimise energy; override via
`meta.rubric_objectives`), shown by `view='frontier'`.

The autonomous *scheduling* of ticks (which quest advances when compute
frees) is a later rung. Dark by default: nothing mints a tick
automatically, and compute is off unless you pass `--compute`
(`PRECIS_QUEST_LOOP_ENABLED` gates the future auto-dispatcher; the manual
CLI runs regardless).

## What this is *not*

- **Not a todo.** A todo is completable and has a parent tree; a quest is
  perpetual and sits *above* the todo tree via `serves`.
- **Not a concept.** Achieve vs. know. A concept can *serve* a quest
  (`concept --serves--> quest`) but they're distinct graphs.
- **Not a memory.** A memory is the stateless baseline node; a quest adds
  a lifecycle, the serves-DAG, and the logbook.

## Roadmap (what's live vs. coming)

Slices 1–3 + rungs **4a–4b** are **live**: the kind + `serves` + logbook +
tree rollup (slice 1); **reweighting** (slice 2) — priority flows down the
`serves` DAG into the todo rotation, paper acquisition, and reading (a no-op
until you link work to an active quest); **gaps + health** (slice 3) — the
striving surfaces its own exploration queue + a momentum/alignment read
(`view='gaps'`, `id='/gaps'`); the **research tick + dossier** (slice 4a) — one
bounded reasoning step reads the rolling context and rewrites the dossier; and
**compute dispatch + Pareto frontier** (slice 4b) — proposals become candidate
`structure` sims, harvested into the logbook and ranked on a frontier
(`precis quest tick --compute`, `view='frontier'`). Coming: the local↔frontier
cascade (frontier-model escalation on a signal, 4c) and the scheduler that
decides which quest advances when compute frees (4d). Design of record:
`docs/proposals/quest-layer.md`.
