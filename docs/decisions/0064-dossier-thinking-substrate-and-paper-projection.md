# 0064 — The dossier as thinking substrate: the preserved ledger and the paper-as-export

- **Status**: proposed (2026-07-24). Extends the *shipped* quest dossier
  (`src/precis/quest/{dossier,logbook,tick,cascade}.py`; `view='dossier'` in
  `handlers/quest.py`) — it does **not** re-decide dossier=draft, which already
  ships. Committed core is **A** (preserve the ledger across the rewrite). **B**
  (paper-as-export) and **C** (sim-failure handling) are named, separable
  follow-ons, not decided here.
- **Deciders**: Reto + agent
- **Builds on**:
  - [ADR 0033 — drafts as editable chunk-native documents](./0033-draft-chunks-editable-document.md) — the dossier IS a `draft` with all draft features (edit / export / tex-compile); this stays true.
  - the `quest` layer — the two-memory model (append-only `logbook` = what
    happened, immutable; whole-rewritten `dossier` draft = current synthesis +
    rolling context) and the Pareto `frontier`, all injected into every tick
    (`tick.py` `_PROMPT_TEMPLATE`).
  - [ADR 0060 — topic dossiers](./0060-topic-dossiers.md) — reuses the quest
    dossier/log loop for lit-synthesis, and owns the periodic **"bundle up and
    share"** digest cast that B explicitly defers to.
  - relates to [ADR 0048](./0048-autonomous-backlog-execution.md) (the loop this
    keeps productive) and [ADR 0051](./0051-turn-taking-persona-threads-and-blackboard-convergence.md) (its plan status/belief markers are the migration-free chunk-`meta` pin precedent A reuses).

## Context

The quest already keeps two records — a WORM `logbook` (episodic, immutable)
and a `dossier` `draft` (semantic synthesis, whole-rewritten each tick via
`rewrite_dossier`, injected as the tick's bounded rolling context) — plus the
`frontier` as injected ground truth. The tick prompt already says "progress
means new external evidence, not more restating," and `cascade.py` already
scores a bare dossier-rewrite as spin. The *shape* is right and shipped.

The gap is structural: **the dossier is whole-rewritten prose with no pinned
state.** `rewrite_dossier` (`dossier.py:85-101`) replaces the single body chunk
in place every tick, so:

1. **The ruled-out ledger is prose the model regenerates, not memory it
   keeps.** "What's been tried / ruled out" lives in the free text the model
   rewrites wholesale. Nothing pins it. A rewrite that drops "ruled out Pt"
   silently re-opens ruled-out ground → re-propose → dry/recap ticks → the loop
   rests as "out of ideas" when it really "lost its own trail." This is the
   autocatpath loop's dead-3-days failure mode.
2. **Fabrication has no structural check in the dossier body.** The logbook path
   already clamps unverified model claims (`_sanitize_model_entry`, `tick.py`);
   the *dossier prose* has no equivalent — nothing forces a synthesis claim to
   trace to a real sim (the dossier-prose half of gripes 171148/171149).

(Human edits being overwritten by the rewrite is *not* a gap — see peek/poke/
seed below: a free-form human idea is meant to be absorbed and adapted, not
preserved; the only thing that must survive is the ledger, which the human can
write into directly.)

## Decision

**Framing — four layers, one ownership rule.** `logbook` (what happened) →
`dossier` (what I make of it + what to try next) → `frontier` (measured best) →
**paper** (a compiled/exported snapshot, for a human). Refinement rises, rawness
falls, left to right. The rule that removes the "do papers get dossiers?"
confusion: **a dossier belongs to a *process*, never an *artifact*.** A paper is
a *render* of a process's dossier + frontier. Some outputs *are* processes (a
living review) → they own a dossier by the same rule; a one-shot summary is an
artifact with no process → no dossier. **Author/meta notes are links** from
paper chunks to dossier chunks, never a parallel notes document. (Today
`dossier.py` hardcodes a quest owner; generalizing the owner is part of **B**.)

### A — Preserve the ledger across the rewrite (committed)

Split the dossier body into a **pinned ledger** (tried / ruled-out / open) and a
**model-rewritten narrative** (the prose synthesis, plus any free-form human
edit). `rewrite_dossier` touches only the narrative; the ledger survives every
rewrite losslessly, mutated only by explicit append/resolve ops (and by a human
who wants something to stick). The ledger is injected into the propose step as
an explicit "do not re-propose these" constraint.

Payoffs: a **dry tick means the ledger says the declared space is genuinely
exhausted** (a real rest signal — see the definition below), not "the model
forgot"; a frontier point with no ledger entry and no backing sim is a **visible
fabrication** (the claim → ledger → log-sim anti-cheat spine becomes queryable).

**Pin mechanism (settles "no migration"):** reuse the generic `paragraph`
chunk_kind + a chunk-`meta` marker (`meta.pinned='ledger'`), exactly as ADR
0051's plan markers do via `store/_draft_ops.py` `patch_chunk_meta`; the rewrite
skips pinned chunks. **No new chunk_kind, no schema migration.**

### Definition — what a "dry" tick is

A tick is **dry** iff it (a) ran to completion with no LLM failure/pause, (b)
was a genuine reasoning pass — **not an empty/degenerate punt**, and (c) given
the ruled-out ledger, proposed no new experiment worth running. Exclusions are
load-bearing: a **punt** (model returns nothing useful) is *not* dry → retry
(the "local 80B punted once → rested forever" bug); an **infra-blocked**
proposal (a real candidate that failed to simulate for infra reasons) is *not*
dry → that's **C**. Only genuine dry ticks accumulate toward the exhausted-rest;
the ledger lets the model further mark "space *exhausted*" (terminal) vs
"nothing *this round*" (keep trying).

### Definition — peek / poke / seed

The dossier is an ordinary editable draft, so: **peek** = read it
(`view='dossier'`); **poke** = edit/annotate in place; **seed** = drop in a new
idea or candidate. No new verbs. The next tick reads the whole dossier as
rolling context, so a poke/seed **enters the reasoning and is absorbed/adapted**
— not preserved verbatim (a human idea should be munged and evolved as the work
develops). To make something **stick** (a hard constraint, a ruled-out you don't
want re-tried), write it into the **ledger** (pinned); free ideas live in the
narrative and stay malleable.

### B — Paper as an on-demand export of the dossier (follow-on)

The dossier is a `draft`, so it already **exports and compiles at will**
(draft → tex/docx via existing export; "update with data as we go" = the dossier
is continuously fed frontier results). A shareable "paper" is therefore a
**rendered snapshot of the dossier on demand**, *not* a separately-maintained
second artifact. The periodic **"bundle up and share"** (weekly/monthly
digest/newsletter) is a **separate process** — 0060's digest cast — out of scope
here.

**Built (2026-07-24).** The one enabling generalization — `dossier.py`'s owner
widened from a quest to any process — is done, migration-free: every public
function takes `owner_id` (any ref) instead of `quest_id`; the owner title seed
reads `refs` directly (a kind-agnostic `_owner_title`, replacing the
`get_ref(kind="quest", …)` coupling); the back-pointer meta key is
`dossier_of_owner`, with resolution via the owner-agnostic `dossier-of` edge so
legacy `dossier_of_quest` dossiers resolve with no backfill. A non-quest
living-review process can now own — and therefore export — a dossier. See
`docs/proposals/dossier-owner-generalization.md` (its own proposal;
`ready`-gated 2026-07-24, blockers resolved in-body) +
`src/precis/quest/dossier.py`.

### C — Sim-level infra failures must not read as "dry" (follow-on)

The coordinator tick *already* separates a failed/paused tick (own budget
`_max_tick_failures`, rests `success=False`) from a dry tick — that layer is
fine. The unaddressed layer is **compute**: a sim that failed for infra reasons
(`struct_relax`/autocatpath `failure_class='infra'`, `compute.py:444-451`) produces
*dispatched=0* and currently reads as a dry tick, laundering "autocatpath didn't
run" into "out of ideas." Fix: on a sim-level infra failure, **retry the sim
once; if it still fails, file a gripe** (bounded + visible) and do not let its
absence count toward the dry/exhausted budget. Distinct from **honest
no-improvement** (a sim that *ran* and lost to the front) → record the dominated
point **truthfully**, no retry, no fabricated number. Its own proposal.

**Built (2026-07-24).** Both lanes now implement the retry-once-then-gripe in
`harvest_measures` (`quest/compute.py`): the **relax** lane keys on
`failure_class='infra'` (a non-convergence failure stays a physical rule-out),
tracked by `meta.quest_infra_retries`; the **barrier** lane treats *every* failed
`autocatpath_explore` as retry-eligible (a crashed NEB is never a physical "no
pathway" verdict, so it never rules out), tracked by
`meta.quest_autocatpath_infra_retries`. Both re-dispatch the sim so it goes
non-terminal and the loop *awaits* it instead of drifting dry; a second failure
files a bounded `quest-infra-failure` gripe (`lane=` names which sim) and stops.

## Alternatives considered

- **Keep the pure whole-rewrite; rely on model discipline to carry
  ruled-out.** Rejected — that discipline is exactly what fails (the dead-3-days
  loop). The loss is silent and self-reinforcing.
- **Pin human seeds too, preserving their text across rewrites.** Rejected
  (Reto): a seed is an input to the thinking, not a record — it should be munged
  and adapted as the idea develops. The only human input that must survive is a
  deliberate ledger entry, which the pin already covers.
- **A maintained evergreen paper as a second document.** Rejected — the draft
  already exports/compiles on demand, so the paper is a *render*, not a new
  artifact; periodic sharing is a separate process (0060).
- **A new kind for the dossier, or a new chunk_kind for the ledger.** Rejected —
  the dossier is already a `draft` (0033), and the pin rides generic
  `paragraph` + `meta` (0051 precedent).

## Consequences

- **Positive**: the dry-tick spin is retired *structurally*, not by
  prompt-nagging; the ledger is a queryable anti-cheat spine; peek/poke/seed is
  a real two-way channel with no new machinery; the paper (B) is free from
  existing draft export.
- **Negative**: the dossier gains internal structure (pinned ledger vs
  narrative) — `rewrite_dossier` and the propose prompt must respect the pin. C
  adds a compute-layer retry + gripe; B generalizes the dossier owner.
- **Neutral**: A ships with **no migration** (generic `paragraph` + chunk
  `meta`). Extends 0033/0060; relates 0048/0051.

## Open questions for implementation (not decided here)

- **Ledger representation** — lean: distinct pinned `paragraph` chunks carrying
  `meta.pinned`, so pins are per-item and machine-readable for the propose
  constraint (not a fenced section). Confirm on build.
- **Punt detection (dry definition)** — how the tick distinguishes a genuine
  "no new move" from a degenerate punt (empty/low-content proposal set). The
  shipped dry-retry budget is a blunt version; a cleaner signal may read the
  model's own "exhausted vs nothing-this-round" mark against the ledger.
- **Paper export (B)** — whether an export wants light reader-facing
  restructuring beyond the raw (already tight) dossior, or ships the dossier
  as-is.
- **The view** (`view='dossier'` exists) — add frontier + log-tail panels;
  surface last-modified dossier chunks as "where the action is." Composition
  over existing views.

## See also

- `src/precis/quest/{dossier,logbook,tick,cascade}.py` — the shipped mechanism.
- `src/precis/store/_draft_ops.py` `patch_chunk_meta` — the migration-free pin precedent (0051).
- the RC1/RC2 residual thread (reboot-reap shipped `8815d396`); A is the RC1
  root-cause fix, one level deeper than the re-mint guard —
  [0065](./0065-quest-loop-failed-rest-backoff-and-surfacing.md) is that
  re-mint guard (failed-rest backoff + nursery surfacing).
