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
(synced to the canonical `prio` column, 1 = hottest … 10):

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
get(kind="quest", id=7, view="dossier")  # the living research synthesis
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
get(kind="quest", id=7, view="pourbaix")  # per-candidate surface-Pourbaix validity maps
get(
    kind="quest", id=7, view="log"
)  # raw ref-events audit trail (generic, not logbook-specific)
get(kind="quest", id="/active")  # every active striving
get(kind="quest", id="/gaps")  # gaps across ALL active quests
get(kind="quest", id=7, q="perovskite")  # passages inside this quest (find-in-ref)
```

**The complete `view=` set is** `tree · gaps · dossier · frontier ·
leaderboard · results · series · measures · logbook · pourbaix · status ·
rubric` (quest-specific) plus the generic
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
`view='status'` is the one-line lifecycle read (striving status · priority ·
horizon) for "is this quest still live"; `view='rubric'` prints just the
measured axes (`rubric_objectives` + demand/supply). `view='raw'` shows
scalars + meta + a capped links section (`view='links'` is the full graph).

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
dashboard (header + momentum/tote, dossier + logbook tail, frontier/gaps
panels, servers-lite) — readable without calling `get(view=…)`.

## Read a quest's numbers — results, frontier, series, measures

`view='results'` is one row per candidate across
*every* band (not just the frontier), grouped by lineage (dopant, then
what varies) rather than band — the same table the tick prompt embeds,
so it doubles as "what the next tick would see now". `view='results'`
and `view='frontier'` are **budgeted** like the tick's copy (2500
tokens; rows drop from the tail, never the newest ten; the body says
`(+K … rows omitted)`); `args={'budget': 8000}` widens it. On an
`inquiry`-body quest (see "Patch quest meta" below) `view='frontier'`
says the body has no Pareto frontier — it never proposes, so nothing
converges.
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

## The dossier + a research tick

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
the banner "the living synthesis". Read it freely; to change it, run a
tick.

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
refines that node instead of appending a twin. A refinement whose parent
the writer cannot find is kept as a top-level node with
`meta.parent_unresolved`, not dropped, and the next tick sees it marked
unplaced. When you search the
ledger (or the literature) for prior art, phrase the query as the thing
you expect to find, not as a question — *"subsurface H co-doping on
Pd(111) lowers NO dissociation barrier"*, not *"does H help?"*. Search
matches statements; [[precis-search-help]]'s `answers=` legs are the
same move for papers.

## A research tick

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

## Patch quest meta — compute_lane / lit_lane / quest_body / rubric_objectives / reaction_config

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
  than searching a materials space — same logbook/dossier/ledger/dialectic/
  lit-search machinery, but **no proposal menu and no Pareto frontier**
  (`view='frontier'` says so plainly). Never inferred from other meta (a
  live materials quest can carry no `reaction_config`/`compute_lane`/
  `rubric_objectives` at all) — set it explicitly for a striving with no
  candidate materials to propose (literature synthesis, landscape
  mapping).
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
- **`reaction_config`** — the autocatpath config the compute lane
  dispatches. A dict of engine `Config` field names only (unknown keys
  refused); `network_her` must be a bool. The patch **shallow-merges**
  into the stored config — `meta={'reaction_config': {'network_her':
  true}}` flips the HER-competitor branch without touching
  `slab`/`mlip`/`search` — so re-dispatched pathways run with it on.
- **`"roadmap"`** — a fourth body: grows a pathway/capability/rung graph
  from measured gaps instead of proposing materials or writing prose.
  Only the root quest ticks; see [[precis-roadmap-help]] for the rung
  meta contract and the three roles.

`edit(kind='quest', id=N, text=...)` (the founding-statement rewrite) and
`meta=` may be combined in one call, or `meta=` passed alone — no `text=`
required for a meta-only patch.

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
frontier/top provisional band). Seeds must agree:
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
