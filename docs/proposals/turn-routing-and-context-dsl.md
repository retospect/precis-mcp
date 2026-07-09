---
status: draft (WIP — design conversation; decisions recorded in ADR 0051)
title: Turn-as-job routing (delegate-on-confidence) + context curation
---

# Turn-as-job routing + context curation

> **WIP design conversation.** The settled decisions are recorded in
> [ADR 0051](../decisions/0051-turn-taking-persona-threads-and-blackboard-convergence.md);
> this doc keeps the fuller reasoning and the still-open issues. Parts
> that turn out to be co-dependent: **(0)** who the thread *is* (persona)
> and how its context is ordered for the cache, **(1)** how a turn is
> routed across model tiers, and **(2)** how the model curates its own
> context window. All ride the existing `kind='job'` substrate and the
> ADR 0036 handle grammar — mostly composition of primitives we already
> have, plus a small tool-call surface.
>
> **Note (2026-07-09):** the inline context-DSL framing below (magic-text
> `pc1234:+` ops) is **superseded by ADR 0051 §6** — curation is now
> *structured tool calls* (`resticky`, `spawn`), not prose syntax. The
> `:±` grammar is retained here only as shorthand for the *fidelity
> levels* (full/summary/keywords/drop); it is not a wire format.

## Motivation / why

Today model selection is a patchwork (ADR 0046 router half-adopted;
see the opus-4.8 consolidation) and context assembly for a turn is
either hand-built (planner prompt layers) or implicit. Two things we
want:

- **Spend the big model only where it earns its keep**, but decide
  that with a *reliable* signal rather than a weak model grading its
  own homework.
- **Let the model manage its own working set** — it knows what it
  needs better than a retrieval heuristic — with a compact, stateful,
  addressable syntax.

The unifying frame: **each turn is a job.** It persists in the DB
(prompt + route + eval + result), which generalizes today's
`plan_tick` (a tick is already a `kind='job'`) to *every* turn —
interactive web, reviewers, planner — under one substrate, one router,
one context DSL. Persisting every turn is also what makes the routing
tunable: the stored prompts/routes/outcomes are the training corpus.

## Part 0 — thread persona + the cache-ordering gradient

Two things ride *ahead* of routing and the DSL, because they shape the
prompt every turn assembles: **who the thread is** (a pinned persona)
and **in what order its blocks are laid down** (so the prompt cache
stays hot). Both are ADR 0038 assembler concerns — this is the layer
`workers/planner_prompt.py` already half-implements
(`_PINNED_SKILL_ID` at the head of `_CACHED_MODULES`; the
`has_review`-gated `_m_reviewer_persona`), generalized.

### The persona is a pinned charter, first block

Each **thread type** (`write-document`, `dream`, `review`, `triage`,
…) is fronted by a persona skill — "you are a document-writer; you are
doing X" — pinned as the leading block and **exempt from the demotion
clock** (Part 2). It is the finer-grained successor to the coarse
`Profile` enum (`AGENT`/`HELPER`): a registry `thread_type →
persona_skill_id`, resolved once per thread and cached.

The persona is a *floor*, not a sticky item: it never ages out, never
demotes, and its cache segment only changes when the thread type does
(i.e. never, within a thread). Everything else in the window rides the
fidelity ladder; the persona sits under it.

### The ordering gradient — stable before volatile

Prompt caching is a **strict prefix match**: only a contiguous run
from position 0 is reused. So block order *is* the cache policy. Lay
blocks down in a monotonic volatility gradient — never a
more-volatile block before a less-volatile one, or you truncate every
downstream prefix:

1. **Immutable-within-session** — the persona + skills, the
   fleet-invariant mechanics (tools/kinds/skill-menu/MCP contract),
   **and static resources**: papers, paper chunks, held figures. None of
   these can change within a session → the deepest cache prefix.
2. **Static-ish snapshots** — indexes, search results, glossaries. These
   *can* change, but only if re-run: treat a fetched search result as a
   **frozen snapshot** (cacheable); **re-running is the explicit
   invalidation act**. This is also how the liveness/cache tension
   resolves — snapshot the things that don't have to be live, live-resolve
   only what must be (Part 2).
3. **Transient memories** — the thread's self-authored worldview
   carryover (Part 2), TTL'd; re-surfaced from the store rather than kept
   cache-warm.
4. **Active work** — the tail: the brief, ancestry, `doc_context`,
   children's returns, the sibling roster. Volatile *and* attention-hot
   (front-and-tail placement counteracts lost-in-the-middle — a
   mid-context block at full fidelity is not necessarily *attended* to,
   so put the live work at the end).

The subtlety for Part 2: **decay is a cache adversary.** If fidelity
flips every turn, the tier-3 segment churns and every downstream prefix
resets. Batch evictions to a cache-break event — only re-render the
sticky region when it *actually* changes, in a burst — so most turns
keep tiers 1–3 stable and only append a fresh tail.

### The fleet-shared vs. lineage-hot tradeoff (→ scheduling)

Here is the crux the persona-first instinct collides with. Because
caching is prefix-only:

- **Persona at position 0** → each thread type is its own cache
  island; **cross-type sharing is exactly zero** (the shared mechanics
  now sit *behind* a per-type prefix and can't be reused across
  types). Strong personality framing (the model attends hardest to the
  head); worst cross-fleet economics.
- **Mechanics at position 0, persona second** → the big fleet-invariant
  prefix (~5k tokens of tools/kinds/skill-menu) is hot across *every*
  turn fleetwide; the persona forks a smaller tail that's hot
  per-lineage. Best economics for an interleaved, heterogeneous fleet;
  the persona is no longer the literal first token.

You cannot have both from ordering alone — that is the real tradeoff:
**one hot fleet-wide prefix, or one hot prefix per thread-type
lineage.**

**Scheduling is what rescues persona-first.** If the dispatcher
*batches ready turns by cache affinity* — bunch all the same-persona
(and, within that, same-lineage) turns and run them consecutively,
then move to the next batch — each per-type island stays hot for the
duration of its batch, so persona-first stops costing sharing *in
practice*. This trades a little fairness/latency (a turn waits for its
batch) for hit-rate. The pressure is **provider-dependent**:

- **Anthropic (Opus/Sonnet)** caches many prefixes with a ~5-min TTL,
  so mild interleaving survives — batching is an optimization.
- **Local llama.cpp** (the litellm helper alias) holds ~one hot KV
  slot, so a prefix switch is a cold reload — batching by affinity is
  close to mandatory there.

Decision leaning (per this session): **persona-first + affinity-batched
scheduling**, accepting per-lineage islands, because the fleet does
long runs of same-type work and the local models punish prefix
switching hardest. Revisit if interactive/interleaved load dominates.

## Part 1 — routing: the driver continues the thread

> **Tiers (current vs. future).** Concretely today there are **two**
> tiers: **Opus** drives, **Sonnet** helps (map "the strongest model"
> / "Haiku" in the text below onto Opus/Sonnet for now). The
> high/medium/low tier *vocabulary* — and any third cheap tier — is a
> later refactor once the two-tier loop is proven; the routing shape
> (driver assigns helpers directly, evaluation lives above the work)
> is tier-count-agnostic.

### The core inversion: delegate-on-confidence, not escalate-on-failure

The original sketch was a bottom-up ladder: run Haiku, have it
self-grade quality/complexity, escalate on "medium/bad." The problem
is calibration — a *weak* model honestly reporting "I did badly" is
exactly the judgment weak models are worst at; an overconfident Haiku
`good/easy` on wrong work is a silent, undetectable false-negative.

The inversion: **the strongest model triages, top-down.** "Can
something dumber do this?" is a far more reliable judgment than "did I
do this well?", and it's the judgment the biggest model is *best* at.

Collapsed to its final form: **there is no separate router. We just
ask Opus to continue the thread, however it needs to.** The router
dissolves into the thread-driver. Delegation is one of the moves the
driver makes.

### Triage *is* briefing

The key insight: Opus's triage output isn't a bare tier label — it's a
**delegation packet** `(tier, context-set delta, terse instruction)`.
The strong model curating context for the weak one is the *opposite* of
error-laundering (the old scheme had the weak model summarizing *up*,
which launders its mistakes into "facts"). Down-briefing is reliable
because the briefer is the smarter party.

This is why Part 1 and Part 2 are the same object: a delegation packet
*is* a context-set (Part 2) plus a prompt.

### Two lanes, and where evaluation lives

- **Entry triage picks the *driver* once.** If Opus decides the whole
  task is Haiku-work, Haiku drives (see audit sampling). This is the
  only delegate-on-confidence decision for the thread *itself*.
- **The driver assigns its helpers' models directly** — `call sonnet:
  …`, `spawn haiku: …`. **No recursive triage on helpers**: the driver
  *is* the dispatcher and already made the routing call when it wrote
  the brief. A sonnet mid-tier can fan its own haikus the same way.

Evaluation always lives *above* the work:

- A **helper** is always safe to run cheap because its parent is the
  gate — the helper's return surfaces in the parent's *next* turn as an
  addressable handle (`jb123 said …`) and the parent evaluates it
  there. **"Bail-up" is not a signal — it's just the return
  surfacing.** The reliable gate is the parent reading the return, not
  the child self-grading.
- Dropping the **driver** tier (letting Haiku drive the thread's own
  followups) is the one move with no evaluator above it. That is the
  risky decision, and the one that needs **audit sampling**: re-check a
  sample of driver-delegated turns with Opus, feed the labels into the
  tuning corpus. Adaptive rate — heavy while the router is young,
  tapering as a learned pre-router earns trust (same shape as the ADR
  0047 gold-set eval; the learned router eventually replaces the Opus
  triage call for the obvious cases, Opus as fallback for the
  residual).

This makes wide fan-out natural: 20 haikus out, evaluate the 20 returns
next turn.

## Part 2 — a stateful context DSL

The model is the operator of its own window. It curates the per-thread
sticky set via **structured MCP tool calls** — *not* an inline
magic-text DSL (ADR 0051 §6). Tool-calling is on-distribution
(higher adherence than a novel `pc1234:+` grammar), validated, logged as
the structured data the routing corpus needs, and a clean trust boundary
(Injection, below). The surface:

- **`resticky(handles=[…])`** — keep/add the named handles for the next
  turn and reset their TTL; everything else decays.
- **fidelity** (full / summary / keywords / drop) is a structured
  argument, not a `:±` suffix.
- **delegation** (`spawn(...)` / a synchronous helper call) is likewise
  a tool call, reusing the `put(kind='todo')` + `requested`→job
  primitives `plan_tick` already uses.

A bare handle mention in prose may still act as "keep warm" (free — the
output is scanned for handles regardless), but every *explicit* change
is structured. The `:±` notation below is retained only as shorthand for
the fidelity *levels*.

### Addressing — reuse ADR 0036, add a fidelity suffix

The address grammar already exists (`utils/handle_registry.py`,
`Store.resolve_relative`) and resolves against *current structure*
(reading-order-correct, never raw `ord`):

- `pc1234+1` / `pc1234-3` — signed sibling step; `++` / `--` — step one
- `pc1234^` / `pc1234^2` — ancestor, n levels up
- `pc1234-0..4` — signed sibling **span** (this + next 4);
  `pc1234-2..2` — two before + two after

The DSL adds **only the fidelity suffix** — `address:level`:

| op | meaning |
|----|---------|
| *(bare mention in prose)* | **reference** — keep warm, reset demotion clock, no level change |
| `pc1234:+` | pin at **full** |
| `pc1234:s` | **summary** level |
| `pc1234:k` | **keywords** level |
| `pc1234:-` | **drop** from context |
| `pc1234-0..4:s` | apply summary to the span |

Uniform across handle kinds: `dc890:+`, `jb123:s`, skill `:+`/`:-`.
(Depends on universal short codes — one uniform terse-pointer handle
per kind, the backlogged base-62 anchor generalization. The DSL is
unusable without stable cross-kind handles.)

Rendered compactly (TOON-ish). Skills, plan (local plan shown in the
context of the larger plan), and previous job outputs are all
stickyable the same way. Default: the last *n* jobs stickied
(input+output), older jobs demoted to summary.

### Decay — one TTL, one warn, batched eviction

Each stickied item has a **TTL** (turns or wallclock) and passes through
a **single `Warn: will remove` state**, then drops. Eviction is **not**
run per-item on a clock (that churns the cache, §Part 0) — it is
**batched to a cache-break event**: let items reach the warn state,
render the "about to expire" list in the *tail*, let the model
`resticky` what it wants to keep, then apply all drops in **one** batch
(one cache break at the earliest dropped position, not one per item).
Segment order stays stable; TTL is metadata, not a sort key.

**Eviction is quality-driven, not cost-driven.** Cached reads bill only
~10% (Anthropic) / free-but-single-slot (local), so the dollar incentive
to evict is weak; the real reasons are context *quality*
(lost-in-the-middle / context rot) and *churn avoidance* (the cache-write
premium). **LRU-by-usage is rejected**: you cannot measure whether a
present chunk was used or washed over, so recency ≠ usefulness. The
model's `resticky` call is the authoritative "still needed" signal
instead — the model is the oracle of what it actually used.

### Liveness — live in the tail only

The earlier "sticky set is always live" stance contradicts a stable
cache prefix: a live handle whose ref is edited changes the rendered
bytes and busts the whole downstream prefix. Resolution (ADR 0051 §4):
**live-resolved handles appear only in the active tail; the cached
prefix holds immutable resources or frozen snapshots.** A soft-deleted
handle renders `pc1234 [gone]` rather than crashing. The audit/tuning
log still snapshots the *rendered* prompt at turn time (the set holds
live pointers; the log holds the immutable render), so liveness never
corrupts the Part-1 corpus.

### Handoff notes — thin, store-first, never load-bearing

Continuity is **store-first, note-second** (decision: ADR 0051 §7). The
bulk of what a turn knows is reconstructible from durable artifacts
(plan outline, draft chunks, findings), re-read losslessly each turn, so
the handoff note is **not a worldview dump** (that is a telephone-game
ratchet needing a super-capable instance every turn) — it is a **thin
intent delta carrying handles, not content** (`continue dc2323; next
check pc998`). It is **never load-bearing**: a turn killed before writing
it degrades (next turn rebuilds from the store, as `plan_tick` already
does on exhaustion), it does not lobotomize. Protect the write with a
**soft-cap handoff reserve** + **incremental checkpointing**. Persisted
as a `kind='memory'` / `MEM:transient` ref (GC-swept, out of default
search); two subtypes — **self-handoff** (consume-on-next-own-turn) and
**directed message** (until recipient reads) — the latter being the
best-effort sibling mailbox substrate (Part 3).

### Newer decisions folded into ADR 0051 (2026-07-09)

The conversation past the original Parts converged several mechanisms;
the precise decisions live in the ADR, summarized here so this doc is
not misleading:

- **Fisheye is the render primitive** (ADR 0051 §6) — two axes: *eyes*
  (model-placed: big = full, small = TOC-path bookmark, none =
  collapsible) and *DOI fidelity* (auto, by distance to nearest eye).
  `get` returns a **neighborhood, never a bare chunk** (`view='fisheye'`:
  target full + pre/post summary/keywords + ancestor branch), which
  needs a **derived-compute priority lane** for touched refs.
- **Curation verbs** = `resticky` (keep) / `close` (actively clear,
  reversible via handle receipt) / decay (neglect default), on a
  **big→small→gone** ladder with one warn each; the turn is
  **curate→work→handoff** (curation is deferred-effect on the next turn).
- **Budget is not a forcing function** (§5) — the driver right-sizes via
  eyes; collapse is for *sharpness*, not cost; only the context window +
  runaway cap are hard.
- **Plan ≠ dispatchable tree** (§2b) — the plan is an outline artifact on
  the chunk substrate (rendered whole each turn), distinct from the
  `kind='todo'` dispatch queue, linked by anchors; this also fixes the
  runaway planner.
- **find-Call auto-docks a provenance region** in the parent (§9); the
  child absorbs the search noise, only the attributed region returns.
- **The synthesis flow is emergent, not a procedure** (§13).

### Math — ephemeral by default, promote on cite

- **scratch**: `<<2+3=5>>` inline, ages out on the demotion clock,
  never persisted. Fine for intermediates nobody cites.
- **persisted**: the moment a result becomes *evidence* a
  draft/citation leans on, it gets a handle (a `calc`/`math` ref) —
  auditable, addressable, re-runnable. No cited number without a
  durable checkable source (grounding pillar). One gesture promotes
  (`<<expr>>:save`). Store the **expression, not just the value**,
  which makes a persisted calc a natural derived-lane artifact (ADR
  0044): content-addressed, idempotent, same-expression → cache hit.

## Taxonomy — followup / call / spawn

Each is authored by the current turn; the discriminator is **who
writes the next prompt, and is it sequential / detached / blocking.**

| | who writes next prompt | context | scheduling | maps to |
|-|-|-|-|-|
| **Followup** | *this turn* authors it | inherits set ± this turn's edits + tool summary | sequential (thread's next turn) | `verdict: continue` where the "child" is the next-self |
| **Call** (agent) | *this turn* authors a *helper's* | small custom (`[Call sonnet: pc1234 dc23423, prompt]`) | **blocks**, returns a value | mint child job + `requested`→job link, block on `derived_job_succeeded` |
| **Spawn** (subtask) | *this turn* authors a *child's* | fresh or forked (`(dc234+ dc123-)`) | detached, runs free | `dispatch` mints child job, no link, no block |

Notes:

- **Naming**: "subtask" collides with the todo-tree hierarchy level
  (strategic/tactical/subtask). Rename the detached primitive to
  **Spawn** (or Detach) to avoid the clash. "Agent" → **Call** (it's
  synchronous and returns a value, like a function call).
- **Followup ≠ re-mint self.** Every turn is its own job row (never an
  in-place re-run — Part 1's corpus needs each turn stored
  independently). A Followup is a *self-authored successor*: new prompt
  that *this* turn wrote ("next, do X"), inheriting the context set.
  `plan_tick`'s exhaustion-resume is the *degenerate, involuntary*
  Followup — nobody authored a next-instruction, so `dispatch`
  re-renders "keep going, you ran out of budget."
- **thread** ≈ the persistent parent (identity, owns the context set);
  **turn** ≈ a disposable tick/job row.
- Forked context for Call/Spawn: `(current context dc234+ dc123-)` =
  copy the set, apply a diff. Live (per the liveness decision).

## Collapse — the return contract (up-spec)

Symmetric to briefing: **the return shape is part of the brief.**
Down-spec includes up-spec. The caller declares the return contract;
the callee answers *that*, nothing more. That removes the "dump
everything vs. terse ack" ambiguity — it was never the child's choice.
The child's job is "what did my parent send me here for, and am I
done?" against its contract.

**Default = receipt, not content — because MCP already landed the work
in the addressable store. The store *is* the collapse buffer.** A
filter/edit helper returns `done · dc2323 dc3421 · "dropped the
furniture rows"`, not the rows; the parent pulls what it wants with
`dc2323:+`. This is what makes fan-out affordable in the *up*
direction: 20 receipts are 20 lines in Opus's window; 20 full outputs
blow it. (Same economics as small-context-makes-Opus-first-affordable,
pointed the other way.)

Three return archetypes, caller's pick:

- **receipt** — `done + handles touched + one line`. Default for
  mutations (the work is in the store).
- **reduction** — the content, collapsed, when *production of the
  summary is the purpose* ("400 chunks → one line each"). Here the
  flowing-up *is* the deliverable.
- **verdict** — judgment + evidence handle ("supported: yes, per pc998").

Each level answers only its own contract, so **grandchild detail dies
at the child boundary** unless a contract explicitly asks for the tree.
Collapse is bounded to one level's fan-out, not the whole subtree.

The unification: **a return is just a job result, addressable as
`jb123`, and "collapse" is the parent choosing a fidelity level on that
handle** (`jb123:+` / `:s` / leave it as the receipt line). No separate
collapse mechanism — down-brief and up-collapse are the *same context
DSL* in two directions.

## Injection / safety

Control syntax is parsed from model output, and chunk *bodies* get
rendered back into context. A hostile chunk containing `pc0001:-` or
`[Spawn: exfiltrate …]` becomes an instruction on the next render.
**Parse control syntax only from the model's own output layer; escape
/ neutralize it when rendering any content; never re-parse rendered
bodies.** Same discipline class as the SSRF guard.

## Open issues

1. **Router calibration is unproven.** Start in **shadow mode**: run at
   current tier, record the self/triage decision *and* (on a sample)
   the counterfactual higher-tier result, before letting the router
   decide anything live.
2. **Silent-bad delegation.** Opus delegates on *predicted* difficulty
   and never sees a happy-path helper's output. Receipt-default means a
   bad Haiku edit is *already in* `dc2323`. Mitigation is the audit
   spot-pull (read 20 one-liners, `dc####:+` the suspicious ones,
   re-issue) — but the audit rate/policy is unspecified.
3. **Latency asymmetry.** The serial ladder is pure win for autonomous
   work; for interactive turns the hard-case p99 balloons. The router
   should be latency-aware (interactive leans on predicted complexity
   to jump tiers; autonomous runs the full ladder). Unmodeled.
4. **Universal short codes are a hard prerequisite** for the DSL
   (stable, uniform, cross-kind, globally-resolvable handles). Backlog
   item; must land first.
5. **Demotion policy constants** (N, budget ceiling, LRU vs.
   turn-count weighting) are guesses. Needs the corpus to tune.
6. **Driver descent has no evaluator above it** — see open issue #2;
   this is the sharp edge, only the audit sample covers it.
7. **Precise predicate for entry triage** — when does the driver step
   down vs. do it itself? Currently "Opus decides"; a learned
   pre-router is the endpoint but the bootstrap heuristic is TBD.
8. **Fleet-shared vs. lineage-hot cache is unresolved policy** (Part
   0). Persona-first gives per-type islands + zero cross-fleet sharing;
   mechanics-first gives one fleet-wide hot prefix but demotes the
   personality framing. The lean is persona-first + affinity-batched
   scheduling, but the batch policy (bunch size, max wait, fairness vs.
   hit-rate) is unspecified and provider-asymmetric (Anthropic TTL
   cache tolerates interleave; local llama.cpp does not).
9. **Demotion churn is a cache adversary** (Part 0). A per-turn fidelity
   flip resets the downstream prefix. Batching demotions to a cache
   boundary is the mitigation, but the batching trigger (only on real
   change? every N turns?) and its interaction with the warn-at-N-1
   ladder are unspecified.

## Ideas / later

- **Transactional subtree (undo stack).** Let a subtree do its work
  *speculatively* and reject/undo it at each collapse level — the
  parent can reject a child's whole effect rather than fix it forward.
  Lisp-like (a `dynamic-wind` / continuation flavor over the job tree).
  Powerful, but a lot of machinery (every mutation needs to be
  reversible + staged per-level). **Deferred** — receipt-default +
  spot-pull-audit is the cheap version for now; forward-fix a bad
  delegate rather than roll back.
- **Learned pre-router** replacing the Opus triage call for obvious
  cases, trained on the persisted turn corpus. Opus triage becomes the
  fallback for the uncertain residual (cascade shape, again).
- **Handoff-as-context-delta.** The forward "what the next model needs"
  handoff should be expressed as `+ / s / -` ops + a terse note, not
  free prose — unifying it with the DSL and making it tunable data.

## Suggested first slice

Shape has stabilized enough to slice, but nothing is committed. Lowest
risk / highest learning first:

1. **Persist turn-as-job + shadow router.** Everything runs at current
   tier; record the triage decision and, on a sample, the
   counterfactual. Zero live risk; yields the calibration corpus that
   tells you whether Part 1 is even sound.
2. **Universal short codes** (prerequisite), then the *read-only* half
   of Part 2: `+ / - / s / k`, the fidelity-ladder auto-demotion with
   inline warnings, and the "dropped, `+` to restore" marker. No forks,
   no Call/Spawn yet.
3. Wire **Call / Spawn** onto the existing `requested`→job /
   `derived_job_succeeded` / `dispatch` primitives, with the return
   contract (receipt default).
