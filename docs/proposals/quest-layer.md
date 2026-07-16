# Quest layer — the striving above the work (proposal)

> **Status: model closed; slices 1–3 built.** Captures the design conversation
> of 2026-07-15 (Reto + session). The aim-layer that sits above projects/streams/
> concepts and gives the system **direction**: a legible answer to "what are we
> striving toward, and is this work/knowledge actually in its service?"
> **Slice 1** — the `quest` kind (`handlers/quest.py`, handle `qu`, migration
> 0065), the `serves` relation, the append-only logbook, the `view='tree'`
> rollup. **Slice 2 (reweighting)** — priority flows down the `serves` DAG into
> three sinks (`src/precis/quest/reweight.py`): rotation (the doable view +
> next-pick), acquisition (the OA fetch backlog), reading (meditation concept
> selection, quest-ready). **Slice 3 (gaps + health)** — the striving surfaces
> its own exploration queue (`src/precis/quest/gaps.py`: thin-support /
> no-literature / low-mastery / open-hypothesis) plus momentum + an embedding
> alignment floor on the rollup; `view='gaps'` + `id='/gaps'`. Each is a **no-op
> until quests + `serves` edges exist**, so they ship live safely. Skill:
> `precis-quest-help`. Related: `docs/design/reading-prep-loop.md` (the concept
> graph, which this consumes).

## What a quest *is*

A **quest is a perpetual, unachievable striving** — the medieval sense (the
Grail, not a milestone). *"Make the perfect NO→NH₃ catalyst"*, *"build a
self-assembling molecular computer"*, *"make a lighter-than-air brick"*: each is
asymptotic. You never file it `done`; you strive toward it, and it **drives** —
it pulls subtasks and knowledge acquisition toward itself.

Beneath a quest sit **achievable goals, explicitly in its service** — and those
are *not a new thing*: they are ordinary projects/todos, which already own an
`open→done` lifecycle and their own sub-tree. They just carry an edge marking
them as *serving* the quest. So the quest is the only un-completable node; the
completable structure below it is the todo world we already have.

This is the simplification that closed the model:

- **`quest` is the only new kind.** No `goal` sub-kind, no self-similar
  grand/goal/subgoal level schema. The achievable structure is plain todos.
- **A quest has no `achieved` state.** Lifecycle: `active` (we strive) →
  `dormant` (set aside) → `abandoned` (renounced). Never complete — that deletes
  the whole "% done" axis as the wrong measure.
- **Progress is a ledger of deeds, not a percentage.** When an in-service goal
  completes, the quest isn't "more done" — it has one more *feat accomplished in
  its service*. The quest accumulates a tally of deeds: the honest, medieval
  sense of progress toward the unreachable.

## Answered along the way

- *"Are quests just memories?"* — **No.** A `memory` is the stateless baseline
  node (no lifecycle, no typed edges). `quest` and `concept` are its structured
  cousins, adding state + typed edges + a lifecycle. precis is a **typed property
  graph**; quests earn a distinct kind because their *structure* (a striving that
  reweights work) is load-bearing.
- *"Could a quest be a concept?"* — **No.** Achieve vs know; striving-priority vs
  mastery. Bridged, not merged: `concept --serves--> quest`.

## The shape

```
quest  (perpetual, unachievable — the ONE new kind)
  ▲ serves          (a quest may serve a grander quest: a DAG of strivings)
quest
  ▲ serves
project / goal  (existing todo — achievable, has a done-state)
  ▲ (its normal parent tree)
subtasks · jobs · concepts · papers · drafts   (all may also serve directly)
```

- **A DAG of strivings above a tree of deeds.** Quests may ladder (*"advance human
  knowledge"* ⊃ *"self-assembling molecular computer"*) and overlap (one concept
  serves several quests — m2m). The completable work beneath each is the ordinary
  todo tree.
- **One relation: `serves` / `served-by`** (X serves quest; quest served-by X) —
  a plain `links` row (`relation='serves'`), the same edge machinery as
  `cites`/`related-to`, auto-mirrored to `served-by`. It covers
  project/stream/concept/paper/job/draft/**sub-quest** → quest. Reuse everything
  below: projects stay strategic-root todos, streams stay recurring todos,
  concepts stay the epistemic peer-graph.

## The kind

- **`quest`** — numeric-ref, handle `qu`, `emits_card=True` so the
  statement+criteria embed (a quest *is a vector*, which the alignment floor and
  reading calibration consume for free). `refs.title` = the statement;
  `refs.meta` carries `priority` (striving weight), `horizon`, and the deed
  ledger; `STATUS:` tag carries `active|dormant|abandoned`. `corpus_role='none'`
  (never cited as evidence).
- **Reads** — `get(kind='quest', id, view='tree')` (the quest + what serves it,
  grouped by kind, recursing into sub-quests + the deed ledger + rolled-up
  health) and a quests dashboard (`/quests`).

## The direction it gives — reweighting (v1's steering lever)

The quest layer's job is to **give the system direction**: priority is a *field*
that flows **down** the `serves` DAG — from a quest, and from any higher goal —
into the three places work is actually chosen. Aggregation on overlap = **max**
(a node serving two quests inherits the stronger pull), with light decay per hop.

1. **Rotation** — the strategic 1/N rotation stops being uniform. A project's
   pick-weight = base × (1 + its served striving-weight), so projects serving hot
   quests surface more often.
2. **Reading** — the reading-prep loop biases daily concepts toward those that
   serve active quests (and their prerequisites). "What matters to learn" = what
   serves the striving (this *is* the concept-calibration signal, seen from the
   quest end).
3. **Dream / acquisition** — the dream's nominations (pursue a thread, acquire a
   paper) tilt toward active quests; a paper that would serve a high-priority
   quest jumps the acquisition backlog.

**Reweight, don't mint** — v1 tilts the existing loops; it does not create work.
The active planner that finds gaps and *mints* the deeds is a later rung.

## Two memories — the logbook and the dossier

A quest keeps *two* records, and precis's append-only-body rule forces the split
cleanly:

- **Logbook** — append-only `quest_log` chunks (the `gripe` body+comment pattern;
  they embed + keyword-index for free). A **WORM, dated** log — *episodic*: what
  happened, when, immutable. Lightly typed entries — `note · observation ·
  hypothesis · result · decision · dead-end · milestone · reflection · cost` + a
  `by` field (human · agent · dream). A **deed is just a `milestone` entry**
  auto-appended on an in-service goal completing, so the deed ledger is a
  *filtered view* of the log, not a separate store. Append idiom mirrors gripe:
  `put(kind='quest', id=N, text=…, entry='hypothesis')`. Dead-ends are
  first-class (recording *what failed and why* stops the whole system re-treading
  it); an un-answered `hypothesis` is a gap.
  - **The log is also the ledger.** Because it's dated and write-once, spend
    lives *in* it: sim-dispatch / result / `cost` entries carry a cost, so the
    **tote** — lifetime ("what's been sunk into the Grail") and the windowed
    weekly-budget figure — is just a *query over the dated log*, no separate cost
    store. One append-only ledger for narrative and accounting.
- **Understanding dossier** — a **`draft` the quest owns** (via a `dossier-of`
  relation, like a project's `draft-of`). *Semantic*: the living synthesis,
  **rewritten every cycle** — current understanding, the best materials so far,
  what's ruled out, open questions — woven with handles to the `structure`
  materials tried + new, the `job`/`pathway` experiments, the `paper` literature,
  the `concept` knowledge. A draft because it is *made* to be rewritten, embeds,
  renders in the virtual-scroll reader, exports, and already weaves handles in its
  References panel. **Dual purpose:** the dossier is also the loop's *rolling
  context* — each step reads the compact dossier instead of the whole logbook, so
  context stays bounded. The living summary serves the human and the agent with
  one artifact.

## Health — momentum + alignment (no completion axis)

A quest is measured by *striving*, not finishing:

- **momentum** — mechanical, computed on read: are deeds and knowledge flowing in
  (recent activity on servers, open-todos moving, no `child-failed`, streams live
  and recent)?
- **alignment** — is the serving work *still in the quest's service*? Free
  **mechanical floor**: quest and every server are embeddable, so cosine
  proximity is a first-pass "still on-aim?" score. The judgment layer refines only
  the ambiguous middle. **Lean:** an autonomous **dream re-review** re-scores the
  low-proximity / stale `serves` edges on a cadence and writes a verdict onto each
  edge; a **human override** on the dashboard always wins. Alignment is a value
  *on the edge*, not a global scalar.

## The autonomous research loop (slice 4) — the quest runs itself

Once the structure holds, a quest stops waiting to be worked and **runs itself** —
a continuous slow burn, not a clock:

- **Two-speed — the cascade (ADR 0047) applied to inquiry.** Local "free" models
  grind the bulk continuously: propose the next candidate materials, interpret
  each sim result, keep the running notes current. The **frontier model is the
  escalation tier**, firing on a *signal* (enough new evidence · a stalled
  frontier · a surprising result), **not a schedule** — it reads the digested
  state + cited papers + dossier, renders the verdict, rewrites the dossier, and
  **sets the next line of inquiry**. Cheap does the legwork; expensive does the
  steering. Routes through the LLM router (ADR 0046) + the local/cloud-super
  tiering already in place.
- **Evidence-triggered, not timed.** The coroutine already advances when results
  land (`derived_job_succeeded`). The only clock-driven part — kicking off fresh
  inquiry when idle — is replaced by: *quest idle + it won a compute slot → start
  the next line of inquiry.* No hourly tick.
- **The compute is the existing lane.** Each inquiry mints catpath `pathway` +
  `structure` DFT-relax jobs on spark (the derived-job lane, ADR 0044),
  content-addressed so a re-proposed candidate is a cache hit, not a re-run.
- **Materials are `structure` servers; the graph is the memory of explored
  space.** Every candidate tried is a `structure` that `serves` the quest,
  carrying its measures; failed ones stay linked + `ruled-out`-tagged so the
  proposer never re-treads. Promising ones sit on the current **Pareto frontier**
  (the objective vector = the quest's rubric). "Do better" = push the frontier.
- **Ceiling awareness.** In-silico only goes so far; a candidate that looks great
  *graduates to "needs a real-world experiment"* — a gap surfaced for a
  human/lab, not something the loop pretends to close.

### Scheduling — emergent bursts, then a narrow choice

Burstiness isn't a scheduler policy — it's **emergent from the shape of the
work**. A quest-thread is naturally I/O-bound: a short active phase (local
collect + think + *dispatch* a sim) then a long **block** while a DFT runs for an
hour. While it's blocked the rest of the system is free, so many quests' threads
interleave to keep the hardware busy — cooperative multitasking over long-running
jobs (exactly the `plan_tick` yield-on-`derived_job_succeeded` shape). No
round-robin, no deliberate burst.

So the allocator's *only* real decision is narrow: **when a compute slot frees,
which idle / ready-to-resume quest advances next?** That's the competition —
scored by a **long-running average** (EWMA, so it damps on the smoothed trend, not
a single result):

- baseline = **priority** (the striving weight you set — the field flowing down);
- earned = **momentum × promise** (smoothed recent progress × expected remaining
  improvement — an active-learning acquisition term);
- + **exploration** so a low-priority quest isn't starved, and a stalled one
  **cools to `dormant`** on its own.

A **weekly proportional budget** — each quest a compute share ∝ priority × bid,
metered against the tote in the log — bounds total draw over the week; the
emergent bursts fill the idle time within it. *(Deferred: coalescing similar sims
/ batching frontier reviews across quests to hit the sim + prompt caches — a real
efficiency win, but a later optimization that only pays with several hot quests.)*

## Worked example — heal the environment

```
quest: Heal the environment                                    (grand striving)
  ▲ serves
quest: A NO→NH₃ catalyst   (kinetics problem — the reaction is downhill;
       rubric: NH₃ selectivity · yield · stability · stretch: no external energy)
  ▲ serves                         ▲ serves
quest: Solid-catalyst route        quest: Bio/o-chem route      (rival strategies, hedged)
  ▲ serves                           ▲ serves
project/todos · relax+pathway jobs   project/todos · papers · fold/seq jobs
  · structures (materials) ──shared concepts (PCET · N–H formation · NO binding · selectivity)──
```

- **Rivals, not a fork primitive.** Both routes `serve` the parent; you hedge, and
  as evidence accrues dial one up and the other to `dormant` — a priority knob + a
  `decision` logbook entry, no special edge. (An explicit `alternative-to` edge
  between substitutes is a later refinement, only if reweighting needs to know.)
- **Shared concepts float up.** PCET / N–H formation / selectivity serve *both*
  routes → max-aggregation makes the shared spine the highest-value reading.
- **The loop, concretely.** Local proposes Fe–N₄ / dual-metal sites → mints
  relax+pathway sims on spark → local reads the barriers as they land → on a
  batch, the frontier model reviews results + the NO-reduction literature,
  rewrites the solid-route dossier, and sets the next candidates. A `dead-end`
  entry ("cNOR reduces NO→N₂O, not →NH₃ — wrong enzyme; try a cytochrome-c nitrite
  reductase") keeps the bio route from re-treading.

## Slice ladder

1. **Read-only structure** *(built)* — `quest` kind + the `serves` m2m relation
   + the rollup `view='tree'` (servers by kind + deed ledger). **Link the
   existing projects up to 3–4 quests** derived from `docs/mission.md` + the real
   research programs (NO→NH₃ catalyst, …). Read-only, so the model is inspectable
   before anything steers.
2. **Reweighting** *(built — `src/precis/quest/reweight.py`)* — priority-as-a-
   field down the `serves` DAG (max-aggregation, `DECAY` per ladder hop; only
   **active** quests pull) into three sinks. **Rotation**: the doable view /
   next-pick discounts a strategic's 7-day picks by the striving weight it serves
   (`(picks+1)/(1+w)`). **Acquisition**: a paper stub serving an active quest
   jumps the OA fetch backlog. **Reading**: `build_meditation(bias_active_quests=)`
   biases concept selection toward quest-servers (dark until the daily-release
   cron, slice 3 of reading-prep). Priority is the canonical `refs.prio` column,
   set on a quest via a `PRIO:` tag. A **no-op until quests + servers exist**, so
   it's live on the hot path without a flag. *This is where it starts giving
   direction.* (Deferred: the dream's own nomination *prompt* — the dream is
   gated off, so tilting `fetch_oa` covers the live acquisition half.)
3. **Gaps** *(built — `src/precis/quest/gaps.py`)* — the striving surfaces its
   own exploration queue: **thin-support** (little serves it), **no-literature**
   (work under way with no `paper` grounding), **low-mastery** (a served
   `concept` below the mastery floor), **open-hypothesis** (a `hypothesis`
   logbook entry with no later `result`/`dead-end`). Plus **health** on the tree
   rollup — *momentum* (recent logbook + server activity, open todos moving, no
   `child-failed`) and an *alignment floor* (cosine of the quest's card vector
   vs. each server's; the dream re-review that refines the middle is slice 4).
   Surfaced in `view='tree'`, `view='gaps'` (per quest), and `id='/gaps'`
   (corpus-wide). Read-time + mechanical, a **no-op until servers exist**. Gaps
   *are* the exploration queue the slice-4 loop consumes.
4. **Active quest-planner** — the continuous slow-burn research loop (see *The
   autonomous research loop* above): local grind + frontier steering,
   evidence-triggered, materials as `structure` servers, quests competing for
   scarce attention. The self-driving research program.

## Open questions (resolve as the steering rungs land)

The **structure is closed** and buildable (slice 1); everything below is about
*how hard it steers and how it runs itself*, and none of it blocks slice 1.
Tagged by the slice that forces the answer:

1. **Cost & credit attribution under overlap** *(slice 4, the sharp one)*.
   Priority *pulls* down `serves` and aggregates by **max** (a weight). But cost
   and earned momentum are conserved-ish: a sim that serves two quests burned one
   sum of GPU — attributing it fully to both double-counts the weekly budget.
   **Pull aggregates by max; cost/credit need a conservation rule** (split, or a
   shared pool). Does a shared breakthrough boost *both* quests' EWMA? (Likely yes
   for credit, no for cost.)
2. **"Promise" is the softest term** *(slice 4)*. The bid is `priority × momentum
   × promise`; *promise* (expected remaining improvement — an acquisition function
   over an ill-defined space) needs a concrete proxy: frontier-improvement rate,
   result variance, count of untried candidates near the front. Momentum is
   measurable-ish; promise is a guess until made one.
3. **Prose rubric → machine-measurable objective** *(slice 4)*. Turning a quest's
   success criteria ("NH₃ selectivity · stability") into a computed score vector
   the loop optimizes is unspecified. Realistic: the frontier model judges
   qualitatively first; hard numbers as sim outputs get parsed.
4. **The proposer is the crux and least-specified** *(slice 4)*. The loop is only
   as good as "propose the next candidate"; a weak local proposer just burns sims.
   Needs grounding (dossier + literature + neighbours of the frontier) with the
   frontier model seeding *directions*. The local-propose / frontier-steer split
   at the *idea* level is open.
5. **Sub-quest vs achievable-goal boundary** *(slice 1's craft skill)*. The model
   allows both; authors + the LLM need a rule of thumb — *open-ended "best/a …" →
   quest; a completable deliverable → a project that `serves`.* Belongs in
   `precis-quest-help`.

**Standing leans** (decided-enough, easy to flip): dossier = a `draft` the quest
owns (arrives with the loop); alignment judge = embedding-proximity floor + dream
re-review, human override wins (cadence/storage → slice 2–3).

## Why this is the right frame

It unifies the subsystems under one legible apex: reading-prep (concepts),
compute (jobs/sims), writing (drafts), acquisition (papers) all ladder up to
quests; priority flows down as direction. The quest is the one un-completable
thing that gives the completable work its aim — non-destructive (nothing
migrates; you mint quests and add `serves` edges), and honest about strivings you
pursue forever without ever arriving.
