---
id: precis-quest-help
title: precis — the striving above the work
summary: quests — perpetual unachievable strivings that pull work + knowledge into their service; logbook, serves-graph, tree rollup
answers:
  - how do I mint a new perpetual striving (quest)?
  - how does PRIO: flow down from a quest to the work that serves it?
  - how do I log an entry in a quest's WORM logbook?
  - how is a quest different from a todo — why does it never go done?
  - how do I know a barrier number is trustworthy before I rank candidates or cite it?
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
```

The **first line** is the striving statement; anything after a blank line
is criteria / rubric. Both embed (a quest *is a vector*). A quest is born
`STATUS:active`.

## Lifecycle — never `done`

A quest has **no achieved state**. It moves along a perpetual lifecycle:

```python
tag(kind="quest", id=7, add=["STATUS:dormant"])  # set aside (may reawaken)
tag(kind="quest", id=7, add=["STATUS:abandoned"])  # renounced
```

`STATUS:done` (and every other workflow value) is **rejected** on a
quest — completing it would delete the "% done" axis as the wrong
measure. Progress is a **ledger of deeds**, not a percentage.

Tagging a quest `active` is also the affordance that **starts its
autonomous loop**: a reconciler pass ensures every active quest has one
live `quest_tick` coordinator loop and re-arms it if it rests (gated on
`PRECIS_QUEST_LOOP_ENABLED`, see Roadmap below). Moving to `dormant` /
`abandoned` just stops new loops from being minted — the current one
winds down on its own.

## Priority — how hard it steers

A quest's **striving weight** is its priority, set with a `PRIO:` tag
(synced to the canonical `prio` column, 1 = hottest … 10):

```python
tag(kind="quest", id=7, add=["PRIO:urgent"])  # prio 1 → weight 1.0
put(kind="quest", text="…", tags=["PRIO:high"])  # at birth → prio 3 → 0.8
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
link(kind="todo", id=42, target="quest:7", rel="serves")
link(kind="concept", id=91, target="quest:7", rel="serves")
link(kind="draft", id="<slug>", target="quest:7", rel="serves")
link(kind="quest", id=12, target="quest:7", rel="serves")  # sub-quest → grand quest
```

Always link from the server's side with `rel='serves'` — the inverse
`link(kind='quest', ..., rel='served-by')` "succeeds" but writes the edge
backwards: the tree rollup's cycle-guard then renders the quest as one of
its *own* servers (a bogus self-referencing leaf) while the real server
never appears at all (gripe 161912).

A quest may serve a grander quest — a **DAG of strivings** above the
ordinary tree of deeds. One concept can serve several quests (m2m); the
shared spine floats up as the highest-value work.

**Sub-quest vs. achievable goal — the rule of thumb:** open-ended
*"the best / a … "* → a **quest** (it can never be finished); a
completable deliverable (*"screen these 20 candidates", "write the
review"*) → an ordinary **project/todo that `serves`** the quest. On
*how* to phrase the striving itself, see [[precis-quest-writing-help]].

## The logbook — a WORM ledger of the journey

A quest keeps an **append-only, dated logbook** (like a lab notebook):
what happened, when, immutable. Append an entry with a type:

```python
put(kind="quest", id=7, text="Try Fe–N₄ single-atom sites", entry="hypothesis")
put(kind="quest", id=7, text="Second PCET barrier too high", entry="dead-end")
put(kind="quest", id=7, text="Dual-metal site clears both barriers", entry="milestone")
put(kind="quest", id=7, text="relax batch A", entry="result", by="agent", cost=1.5)
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
get(kind="quest", id=7)  # statement + tote + logbook TAIL (last 10 entries)
get(kind="quest", id=7, view="tree")  # rollup: servers + deed ledger + health + gaps
get(kind="quest", id=7, view="gaps")  # just this quest's exploration queue
get(kind="quest", id=7, view="dossier")  # the living research synthesis (slice 4)
get(kind="quest", id=7, view="frontier")  # Pareto frontier of candidate materials
get(kind="quest", id=7, view="leaderboard")  # ranked servers by deeds contributed
get(kind="quest", id=7, view="logbook")  # the FULL lab notebook, every entry
get(
    kind="quest", id=7, view="log"
)  # raw ref-events audit trail (generic, not logbook-specific)
get(kind="quest", id="/active")  # every active striving
get(kind="quest", id="/gaps")  # gaps across ALL active quests
```

**The complete `view=` set is** `tree · gaps · dossier · frontier ·
leaderboard · logbook` (quest-specific) plus the generic `links · log · raw`.
Note the trap: this doc says *deeds* constantly, but it isn't a view — a
*deed* is just the milestone-typed slice of the log. Bare `get(id=N)` shows a
digest with only the logbook **tail** (last 10 entries, cheap even on a quest
with thousands); `view='logbook'` is the complete append-only notebook;
`view='log'` is the raw ref-events ledger, a different (generic) thing.

`view='tree'` is the map: it walks who serves the quest (grouped by
kind), recurses into sub-quests, prints the deed ledger + tote, and — from
slice 3 — a **health** line and a **gaps** list at the foot.

All of the above is also visible on the web: `/refs/quest/<id>` is a
dedicated hub dashboard (header + momentum/tote, dossier + logbook tail,
frontier/gaps panels, servers-lite) rather than the generic ref-detail
render — a human can read a quest's state without calling `get(view=…)`
by hand.

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
get(kind="quest", id=7, view="dossier")  # read the synthesis
```

### The dossier is draft chunks — read it, don't edit it

A dossier is a real `draft`: a heading, a run of short narrative chunks
(one thought each, rewritten wholesale every tick), and pinned ledger
chunks the writer never overwrites. It is not markdown in
a text field, and block markdown (`##` headings, `-` bullets, fences,
tables) has **no renderer** — it shows up as literal characters. Only the
inline subset renders: `**bold**`, `*italic*`, backticks, `<sub>`/`<sup>`,
and `$…$` KaTeX for species (`$C_{60}$`). Cross-references use square
brackets, which linkify — `[st164913]`, `[pc1234]`, `[pa88]`, `[fi189542]`.
Parentheses do not linkify, so `(st164913)` is a dead reference.

**The quest tick owns this document.** `put`/`edit`/`delete` on a draft
linked `dossier-of` (or `paper-of`) is refused with `Unsupported` — a
generic draft-hygiene pass once "normalised" a live dossier into 13 chunks
and stranded 12 of them, feeding weeks-stale prose back to the loop under
the banner "the living synthesis". Read it however you like; to change it,
run a tick.

### The attempt ledger — why the proposer doesn't re-tread

Pinned alongside the narrative is the **attempt ledger**: one chunk per
attempt (`meta.pinned='ledger-node'`), nested through `parent_chunk_id` so
a refinement sits under the attempt it refines. Each carries its state on
the closed `ATTEMPT:` axis — `open` · `active` · `tried` · `ruled-out` ·
`idea` ([[precis-tags]]).

Because each attempt is its own chunk it is individually addressable,
taggable, and linkable, and because the ledger is pinned it survives every
narrative rewrite. That is what stops a tick from re-proposing something
already ruled out. Older dossiers still holding a single markdown ledger
blob convert on first read, so nothing needs a migration pass.

The writer **upserts**: a new attempt is near-dup-matched against the
whole ledger first — a rephrasing of an existing node transitions or
refines that node instead of appending a twin. When you search the
ledger (or the literature) for prior art, phrase the query as the thing
you expect to find, not as a question — *"subsurface H co-doping on
Pd(111) lowers NO dissociation barrier"*, not *"does H help?"*. Search
matches statements; [[precis-search-help]]'s `answers=` legs are the
same move for papers.

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
objective vector (override via `meta.rubric_objectives`; the catalyst
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
all). The loop dispatches **one proposal per tick**
(`PRECIS_QUEST_MAX_PROPOSALS`, default 1) and waits for its sims before
the next.

The autonomous *scheduling* of ticks (a perpetual per-quest coordinator loop,
not a single step) is a later rung — see Roadmap below. Dark by default:
nothing mints a loop automatically, and compute is off unless you pass
`--compute` (`PRECIS_QUEST_LOOP_ENABLED` gates the autonomous loop; the
manual CLI runs regardless).

## Trust a barrier before you rank on it

`barrier_trusted` (behind the confirmed/provisional split above) goes
`False` on: adsorbate detached, wrong binding site, NEB not converged,
`|barrier| > 8 eV` (nonphysical), relax converged in 0 steps (the geometry
never actually moved), and symmetry-identical structures disagreeing
(re-measure both — neither is trusted until they agree). **A barrier near
0 eV carries none of these flags** — it's usually a broken/degenerate NEB
(both endpoints collapsing to the same state), not a record-low. Treat any
~0 eV read as invalid until checked, never as a leaderboard entry.

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

## What this is *not*

- **Not a todo.** A todo is completable and has a parent tree; a quest is
  perpetual and sits *above* the todo tree via `serves`.
- **Not a concept.** Achieve vs. know. A concept can *serve* a quest
  (`concept --serves--> quest`) but they're distinct graphs.
- **Not a memory.** A memory is the stateless baseline node; a quest adds
  a lifecycle, the serves-DAG, and the logbook.
- **Not a skill.** A quest is a striving, not a procedure — "how to
  evaluate a paper" is a rubric/how-to, filed as a **`skill`**, not a quest.
- **Not a paper-specific finding.** A best-practice result or evaluation
  tied to one paper is a **`finding`** (citation-linked, kind='finding'),
  not a quest — a quest is generic and never paper-specific.

## Roadmap (what's live vs. coming)

Slices 1–3 + rungs **4a–4d** are **live**: the kind + `serves` + logbook +
tree rollup (slice 1); **reweighting** (slice 2); **gaps + health** (slice 3,
`view='gaps'`, `id='/gaps'`); the **research tick + dossier** (slice 4a); the
**compute dispatch + Pareto frontier** (slice 4b, `precis quest tick --compute`,
`view='frontier'`); the **local↔frontier cascade** (slice 4c) — a tick runs
cheap+local and *escalates to a frontier review* on a signal; and the
**allocator** (slice 4d) — `precis quest run` picks the highest-scoring active
quest by an EWMA bandit (priority × momentum × promise + exploration) under a
weekly budget, ticks it once, and cools cold quests to `dormant` (manual /
CLI-only now — see below); and **graduation** (slice 4e) — a candidate that
crosses the quest's declared ceiling (`meta.graduation = {key, sense,
threshold}`) is tagged `needs-experiment`, logged as a `milestone` deed, and
surfaced as a `needs-experiment` gap (★ in `view='frontier'`) — the in-silico
ceiling, a call to a human/lab.

The **background** autonomy is no longer the allocator picking one quest per
pass — it's a **reconciler** (`precis.quest.loop`) that runs every agent-worker
cycle and ensures each active quest has one live `quest_tick` **coordinator
loop** (not a single scored step): the loop harvests finished sims,
reviews+proposes via the local model, dispatches the next batch, and yields
until they land — self-paced by sim completion, not a timer. A loop that rests
(a bounded run of dry/unproductive slices) is re-armed after a short cooldown;
consecutive dry rests escalate to a ~daily retry cadence plus a
`quest:dry-rest/<id>` alert, self-healing on frontier improvement or a
non-dry rest (thresholds/cooldowns env-tunable, see `precis.quest.loop`). Runs on the melchior agent worker **only when
`PRECIS_QUEST_LOOP_ENABLED` is set** (dark by default); `precis quest run
--force` still runs one manual allocator tick by hand, independent of the
loop. The quest layer is complete. Design of record:
`quest-layer` (git-only).
