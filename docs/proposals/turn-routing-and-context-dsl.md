---
status: draft (WIP — design conversation, not yet sliced)
title: Turn-as-job routing (delegate-on-confidence) + a stateful context DSL
---

# Turn-as-job routing + a stateful context DSL

> **WIP.** This is a captured design conversation, not a committed
> plan. The shape has stabilized enough to write down; the open
> issues below are real and unresolved. Two parts that turn out to be
> co-dependent: **(1)** how a turn is routed across model tiers, and
> **(2)** how the model curates its own context window. Both ride the
> existing `kind='job'` substrate and the ADR 0036 handle grammar —
> almost no new mechanism, mostly a surface syntax over primitives we
> already have.

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

## Part 1 — routing: the driver continues the thread

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

The model is the operator of its own window — a small register machine
over the context set. MCP does the document mutations; the rest of the
turn's output carries **context ops** that mutate a per-thread sticky
set (state, persisted on the thread; survives until changed).

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

### Demotion — a fidelity ladder with warnings

Full → summary → keywords → gone. Hybrid clock (not a pure global
timer, which mass-demotes and punishes a chunk you cite every turn):

- per-item **age** = turns since last touched; a bare mention resets it
- **warn at N-1, demote at N** (N≈3), rendered inline:
  `pc1234 [full · demotes next turn]`
- **budget pressure overrides the clock**: over the token ceiling,
  demote the oldest immediately regardless of age
- **dropped items stay marked** so the model can `+` them back

### Liveness — always live, never snapshot

References resolve at render time (live). The sticky *set* is **also
live** (decision: no snapshot) — working off data that moved on is the
cardinal sin for a grounding system. Consequences:

- a live handle can be soft-deleted out from under the set → render it
  `pc1234 [gone]`, don't crash, let the model react
- **but the audit/tuning log snapshots what was actually rendered** at
  turn time. The *set* is live pointers; the *log* is the immutable
  render. Otherwise liveness corrupts the Part-1 tuning corpus (you'd
  replay a prompt that no longer reproduces). No conflict — the set
  holds pointers, the log holds the render.

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
