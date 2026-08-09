---
status: draft
title: Turn-as-job routing (delegate-on-confidence) + context curation — WIP design conversation; decisions recorded in ADR 0051
---

# Turn-as-job routing + context curation

> **WIP design conversation.** Settled decisions live in ADR 0051
> (git-only); the full reasoning is in this file's git history. This
> keeps the conclusions, the still-open issues, and the first slice.
> Nothing is built yet. All of it rides the existing `kind='job'`
> substrate + the ADR 0036 handle grammar.

## Motivation / why

Model selection is a patchwork and per-turn context assembly is
hand-built or implicit. Two goals: spend the big model only where it
earns its keep, decided by a *reliable* signal (not a weak model
grading its own homework); and let the model manage its own working
set with a compact, stateful, addressable surface. Unifying frame:
**each turn is a job** — persisted (prompt + route + eval + result),
generalizing `plan_tick` to every turn.

## Conclusions (fuller argument in git history / ADR 0051)

- **Part 0 — persona + cache gradient.** Each thread type is fronted
  by a pinned persona skill (floor, never demotes). Block order is the
  cache policy: a monotonic volatility gradient — persona/mechanics/
  static resources → frozen snapshots (re-running is the explicit
  invalidation act) → TTL'd transient memories → the volatile tail
  (attention-hot at the end). Decay is a cache adversary: batch
  evictions to a cache-break event. Leaning: **persona-first +
  affinity-batched scheduling** (local llama.cpp punishes prefix
  switches hardest); revisit if interactive load dominates.
- **Part 1 — the core inversion: delegate-on-confidence.** No
  bottom-up escalate-on-failure ladder (a weak model self-grading is
  the judgment it is worst at). The strongest model triages top-down;
  **there is no separate router — Opus continues the thread and
  delegation is one of its moves.** Triage output is a delegation
  packet `(tier, context-set delta, terse instruction)` — down-briefing
  is reliable because the briefer is smarter. Entry triage picks the
  driver once; the driver assigns helper models directly (no recursive
  triage). Evaluation always lives *above* the work: the parent reads
  the return; "bail-up" is just the return surfacing. Today's tiers
  are two (Opus drives, Sonnet helps); the vocabulary is
  tier-count-agnostic.
- **Part 2 — curation is structured tool calls** (`resticky`, `close`,
  decay-by-neglect), *not* an inline magic-text DSL (ADR 0051 §6
  superseded the `pc1234:+` prose ops; the `:±` grammar survives only
  as shorthand for fidelity levels full/summary/keywords/drop).
  Fisheye is the render primitive (eyes × DOI fidelity; `get` returns
  a neighborhood, never a bare chunk — needs a derived-compute
  priority lane). Budget is not a forcing function — collapse is for
  sharpness; only the context window + runaway cap are hard. Plan ≠
  dispatchable tree (outline artifact on the chunk substrate, linked
  to the todo queue by anchors). find-Call auto-docks a provenance
  region; the synthesis flow is emergent, not a procedure.
- **Math:** ephemeral by default (`<<2+3=5>>` scratch), promoted on
  cite (`:save` → a durable `calc` ref storing the *expression*, a
  content-addressed derived-lane artifact). No cited number without a
  durable checkable source.

## Taxonomy — Followup / Call / Spawn

Discriminator: who writes the next prompt; sequential / blocking /
detached.

| | who writes next prompt | context | scheduling | maps to |
|-|-|-|-|-|
| **Followup** | this turn authors it | inherits set ± edits | sequential | `verdict: continue`, next-self |
| **Call** | this turn authors a helper's | small custom set | **blocks**, returns a value | child job + `requested` link, `derived_job_succeeded` |
| **Spawn** | this turn authors a child's | fresh or forked `(dc234+ dc123-)` | detached | `dispatch` mints child job, no link |

Naming decided: "Spawn" (not "subtask" — collides with the todo-tree
level) and "Call" (synchronous, returns a value). A Followup is a
self-authored successor job row — never an in-place re-run;
`plan_tick`'s exhaustion-resume is the degenerate involuntary case.

## Collapse — the return contract (up-spec)

The return shape is part of the brief; the caller declares it. Default
= **receipt** (`done + handles touched + one line`) — the store *is*
the collapse buffer, which is what makes fan-out affordable upward (20
receipts = 20 lines). Other archetypes: **reduction** (the collapsed
content is the deliverable) and **verdict** (judgment + evidence
handle). Grandchild detail dies at the child boundary unless a
contract asks for the tree. A return is a job result addressable as
`jb123`; "collapse" is the parent choosing a fidelity level on that
handle — down-brief and up-collapse are the same DSL in two
directions.

## Injection / safety

Parse control syntax only from the model's own output layer; escape /
neutralize when rendering any content; never re-parse rendered bodies
(a hostile chunk containing `[Spawn: exfiltrate …]` must stay inert).
Same discipline class as the SSRF guard.

## Open issues

1. **Router calibration is unproven** — start in shadow mode (record
   the triage decision + sampled counterfactual) before any live
   routing.
2. **Silent-bad delegation** — receipt-default means a bad Haiku edit
   is already in the store; the audit spot-pull rate/policy is
   unspecified.
3. **Latency asymmetry** — the serial ladder balloons interactive p99;
   the router should be latency-aware. Unmodeled.
4. **Universal short codes are a hard prerequisite** (stable,
   cross-kind, globally-resolvable handles). Separate backlog item;
   must land first.
5. **Demotion policy constants** (N, budget ceiling, LRU vs turn-count
   weighting) are guesses — needs the turn corpus to tune.
6. **Driver descent has no evaluator above it** — only the audit
   sample covers it.
7. **Entry-triage predicate** — bootstrap heuristic TBD; a learned
   pre-router is the endpoint.
8. **Fleet-shared vs lineage-hot cache policy** — the affinity-batch
   parameters (bunch size, max wait, fairness vs hit-rate) are
   unspecified and provider-asymmetric (Anthropic TTL cache tolerates
   interleave; local llama.cpp does not).
9. **Demotion churn vs cache** — batching trigger and its interaction
   with the warn-at-N-1 ladder unspecified.

## Ideas / later

- **Transactional subtree (undo stack)** — speculative child effects,
  rejectable at each collapse level. Deferred: receipt-default +
  spot-pull audit is the cheap version; forward-fix rather than roll
  back.
- **Learned pre-router** trained on the persisted turn corpus; Opus
  triage becomes the fallback for the uncertain residual.
- **Handoff-as-context-delta** — express the forward handoff as
  `+ / s / -` ops + a terse note, unifying it with the DSL.

## Suggested first slice (nothing committed)

1. **Persist turn-as-job + shadow router** — zero live risk; yields
   the calibration corpus that tells you whether Part 1 is sound.
2. **Universal short codes**, then the read-only half of Part 2:
   fidelity ladder + auto-demotion with inline warnings. No forks, no
   Call/Spawn yet.
3. Wire **Call / Spawn** onto the existing `requested`→job /
   `derived_job_succeeded` / `dispatch` primitives, with the
   receipt-default return contract.
