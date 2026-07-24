# 0063 — Dark-factory governance: the done-contract, producer/verifier separation, liveness, and tiered verification

**Status:** proposed (2026-07-24). Design-of-record for a cluster of five
concerns; no code yet. Extends [0044](./0044-derived-job-lane.md) (the job
lane a task rides) and [0048](./0048-autonomous-backlog-execution.md) (the
autonomous fixer loop whose throughput makes these concerns bite).

Migrated from gripes 142884–142888, filed together 2026-07-11 off the
Dotta/Paperclip liveness-model talk (youtu.be/7P0elyLIxXo). The gripes are
retired; this ADR is their durable home.

## Context

The dark factory scales agent output past the point where a human can read
every result. Five governance gaps become load-bearing at that scale — all
variations on one theme: **"done" is currently an unverified self-assertion
with no evidence, no independent check, no liveness guarantee, and no
handoff.**

1. **`STATUS:done` is a boolean.** A done task actually asserts a bundle:
   artifact produced, evidence collected, rubric met, owner identified, next
   step known. We flatten all of it into one bit. The agent self-declares and
   the control plane accepts it with zero verification that any sub-claim
   holds. (142884)
2. **Author and verifier are the same agent.** The agent that does the work
   declares it done. Dotta's hard rule: verify with a *different* model
   (Claude codes, Codex verifies). We enforce no producer/reviewer split
   anywhere. (142885)
3. **No watchdog / liveness enforcement.** Nothing guarantees an in-progress
   task keeps moving. A blocked task just sits — no deadlock detection, no
   forward-progress guarantee, no escalation. (142886)
4. **Human review becomes verification theater at scale.** A flat human-gate
   on everything works at low volume and collapses at agent-scale: the queue
   grows faster than a human can honestly drain it, so sign-off degrades into
   rubber-stamping. (142887)
5. **No chain of custody.** A finishing agent isn't required to name who gets
   the work next. Work completes into a void; handoff is not part of the
   done-contract. (142888)

These are not five features — they are one control-plane model viewed from
five sides. Solving them piecemeal (five bolted-on checks) would re-create
the folksonomy-drift lesson of [0047](./0047-controlled-chunk-tagging.md) at
the process layer. This ADR fixes the *shape* of the answer; the build order
and per-slice specs are follow-on proposals.

## Decision (direction, not yet ratified)

Adopt a **structured done-contract** as the single primitive, and derive the
other four properties from it rather than adding four independent mechanisms.

### 1. Done is a contract, not a bit

A done-transition must carry, in the ref/job's `meta`, a structured
attestation instead of a bare `STATUS:done`:

- **artifact** — the produced ref/handle(s) the work claims to have made.
- **evidence** — a list of concrete, machine-checkable claims (a passing gate
  sha, a test id, a diff stat, a cited source chunk) — never free prose.
- **rubric** — which acceptance criteria (the [0048] `ready`-gate criteria, or
  a gripe's implied fix) this transition claims to satisfy, each mapped to its
  evidence item.
- **next-owner** — the named agent/human/lane the work hands to (or the
  explicit terminal marker `custody:closed` with a reason). Empty is a
  contract violation, not a default.

The control plane validates the *shape* (every rubric line has an evidence
item; next-owner is present) deterministically; it does not judge the
*substance* — that is the verifier's job (§2).

### 2. Producer ≠ verifier (a different model)

The evidence in a done-contract is checked by a **separate verification pass
run under a different model than produced it**, before the transition is
accepted. This reuses the existing seams rather than inventing a lane:

- The verifier runs on the [0044] job lane as a derived job parented on the
  artifact, dispatched through [0046]'s router with an explicitly *different*
  `Tier`/model than the producer used (recorded in the route log, so
  same-model self-verification is detectable and rejectable).
- Adversarial framing: the verifier is prompted to *refute* the evidence, not
  confirm it — default-to-fail on any unbacked rubric line.
- A verified done-contract flips the transition; a refuted one bounces the
  task back to its producer (or to the next-owner as a rework handoff), never
  to a human queue by default.

This is the structural fix for §4 (theater): humans stop being the flat gate.

### 3. Liveness = a watchdog over the custody graph

Because every task now names a next-owner (§1) and every done-transition is
timestamped, a stalled task is *detectable*: a task in-progress past its
expected-progress window with no forward event is a liveness fault. A
watchdog pass (nursery-tier / SQL where possible, escalating to an agent tick
only on a real stall) re-triggers or escalates — the goal-oriented maximizer
Dotta describes, but grounded in the custody graph rather than a heuristic
stall counter. This subsumes the current generic 12-tick stall heuristic
(cf. gripe 170252, still open — the quest-loop instance of the same gap).

### 4. Tiered verification (theater is a routing problem)

Human approval is reserved for **high-stakes transitions only**; everything
else is gated by automated evidence checks (§1 shape validation) plus
peer-agent adversarial review (§2). Stakes are a property of the transition
(a prod deploy, a schema migration, an outward-facing publish → human; an
internal draft revision, a card synthesis → agent-only). The human gate
becomes selective and therefore honest.

## Explicitly NOT in scope

- **A new kind or a new lane.** The done-contract is `meta` on the existing
  ref/job; the verifier is a [0044] derived job; the watchdog is a review-tier
  pass. Nothing here mints `kind='custody'` or a parallel control plane.
- **A proof engine.** Evidence is *checked* (a gate sha is real, a test id
  passed), not *proven*. This mirrors [0054]'s "operator is a label, not a
  validity engine" ruling — no defeasible-logic discharge, no computed trust
  scalar.
- **Retrofitting every existing `STATUS:done`.** The contract applies at the
  dark-factory throughput frontier (fixer-loop work items, quest ticks,
  dispatched jobs) first; hand-driven todos keep the boolean until a slice
  explicitly migrates them.
- **The human-review UX.** Which surface shows the high-stakes queue is a
  precis-web concern for a later slice.

## Consequences

- **Positive.** Done becomes auditable; self-declared-done stops being
  load-bearing; a different-model check catches the class of "plausible but
  wrong" self-reports the [0048] loop is most exposed to; stalls are
  detectable instead of silent; the human gate shrinks to where it's real.
- **Cost.** Every done-transition now spends a verifier pass (a second model
  call) — acceptable at the router's budget breaker, but it is a real
  per-task cost that must be tier-gated (don't verify a trivial mechanical
  transition with an opus refuter).
- **Migration risk.** The `meta` contract shape is a new convention every
  producer must emit; a partial rollout where some producers emit it and some
  don't needs the validator to treat "no contract" as "legacy boolean," not a
  fault, until a slice flips enforcement.

## Open questions / decisions log

- **Contract storage.** `meta` envelope on the ref vs. a dedicated
  `done_attestations` table (queryable custody graph). Lean `meta` first per
  the no-new-lane principle; promote if the watchdog needs SQL-speed graph
  queries.
- **Verifier model policy.** Fixed "different tier" or an explicit
  producer→verifier model map? The route log already records model, so
  same-model detection is cheap; the *policy* (which model verifies which
  tier) is unset.
- **Watchdog home.** Nursery SQL pass (per [nursery tiers]) vs. a dedicated
  liveness tick. The custody graph makes the SQL path viable for detection;
  escalation still wants an agent.
- **Stakes classification.** Who declares a transition high-stakes — the
  producer's rubric, a static per-lane policy, or a classifier? A
  producer-declared stake is gameable; a static policy is coarse.
- **Relationship to quest-loop stall (170252).** The watchdog here should
  subsume the quest-loop's separate stall heuristic rather than run beside it;
  confirm one mechanism, not two, when this graduates.

## Slicing (follow-on proposals, not built)

1. **Done-contract shape + deterministic validator** (the primitive; enables
   everything else).
2. **Producer≠verifier pass** on the [0044] lane via [0046] router.
3. **Watchdog over the custody graph** (subsuming 170252's heuristic).
4. **Tiered/stakes-gated human queue** in precis-web.

Each is separately shippable; 2–4 are `blocked-by` slice 1.
