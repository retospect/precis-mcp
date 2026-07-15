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
get(kind='quest', id=7, view='tree')  # rollup: servers by kind + sub-quests + deed ledger
get(kind='quest', id='/active')       # every active striving
```

`view='tree'` is the map: it walks who serves the quest (grouped by
kind), recurses into sub-quests, and prints the deed ledger + tote at the
foot.

## What this is *not*

- **Not a todo.** A todo is completable and has a parent tree; a quest is
  perpetual and sits *above* the todo tree via `serves`.
- **Not a concept.** Achieve vs. know. A concept can *serve* a quest
  (`concept --serves--> quest`) but they're distinct graphs.
- **Not a memory.** A memory is the stateless baseline node; a quest adds
  a lifecycle, the serves-DAG, and the logbook.

## Roadmap (what's live vs. coming)

Slice 1 (**live**) is read-only structure: the kind, the `serves`
relation, the logbook, the tree rollup. It does **not** steer yet.
Coming: **reweighting** (priority flows down the `serves` DAG into
rotation / reading / dream — slice 2), **gap surfacing** (slice 3), and
the **autonomous research loop** (local grind + frontier steering,
materials as `structure` servers — slice 4). Design of record:
`docs/proposals/quest-layer.md`.
