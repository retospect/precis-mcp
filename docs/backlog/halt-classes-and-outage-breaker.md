---
status: draft
title: Halt classes + outage circuit-breaker — agent-declared halts stop being terminal-silent
---

# Halt classes + outage circuit-breaker

**The problem, from a live incident (2026-09-01/03, dr287265 "Organoid
constraint stress"):** two plan ticks (jb287266, jb301715) each ran
during the migration-0151 UndefinedColumn outage, correctly declared
`verdict: halt` ("infrastructure blocker, not a task-design issue" — the
agents even said so in their conclusions), and the runner tagged both
parents `halt:agent-declared`. That tag is in the doable-exclusion
registry, so both todos left dispatch *forever*. The outage was fixed
the next day; nothing reconsidered the halted set — the user found the
dead smartdraft by staring at it. Root defects:

1. `verdict: halt` has **no reason class** — "the DB is down" and "this
   task is misconceived" collapse into the same terminal tag, so
   retryability is undecidable.
2. Halt is **defined as working-as-designed**: the health digest's
   stuck-doable detector explicitly excludes halted leaves
   (`workers/nursery.py`, exclusion-registry gate), no nursery detector
   covers them, no TTL, no alert.
3. A **systemic** failure was recorded as **per-todo** state. N todos
   ticking during one outage yield N individually-poisoned todos and no
   single thing to clear when it ends. (The 2026-08-15 outage was
   cleaned up by a *manual* cohort tag `halt:env-outage-20260815` + a
   hand un-halt sweep — see
   `docs/backlog/plan-tick-deploy-window-mcp-outage-watch.md`. This
   item mechanizes exactly that.)

**Research backing:** perplexity-research ref
`best-practices-for-failure-handling-and-escalation-in-autono`
(2026-09-04, cached/pinned). Key alignments: Temporal's
transient/intermittent/permanent split (only *permanent* may fail
fast; everything environmental must stay retryable); SHIELDA
(arXiv 2508.07935) — classify agent exceptions by artifact + phase
before choosing a handler, and give misconceived tasks one
repair attempt ("Clarify Prompt" / "Plan Repair") before terminal
halt; the closed→open→half-open circuit-breaker with **probe-based
auto-close**; "parole" = release-on-known-fix, not blind elapsed time;
"log the reasoning, not just the outcome" for dead-lettered work;
never auto-retry an uncertain-outcome (non-idempotent) operation.

## Present state (what this builds on — do not duplicate)

- Tick conclusions: `utils/tick_conclusion.py` parses
  `verdict: done|continue|yield|halt`; the runner
  (`workers/executors/claude_inproc.py`, "runner-side honoring" block)
  maps halt → `halt:agent-declared` on the parent.
- The infra-vs-content axis **already exists at the job layer**:
  `handlers/_job_bubble.py::_is_infra_failure` reads
  `INFRA_FAILURE_TAGS`; infra-class job failures get bounded
  `orphan_retry_count` retries, content-class latches
  `child-failed:<jobid>`. This spec extends that same axis to
  *agent-declared* halts. (Known gaps in that layer:
  `docs/backlog/infra-failure-tag-classification-gaps.md` — fix
  compatibly, don't fork the vocabulary.)
- Park machinery with revisit semantics already exists:
  `waiting-for:*` / `ask-user:*` cooldown release in
  `workers/dispatch.py` (`_parked_child_still_blocks_sql`), long-wait
  alert at 7d.
- Ad-hoc halt reasons already in the wild: `halt:planner-stuck`,
  `halt:bad-dispatch`, `halt:cost-cap`, the manual
  `halt:env-outage-20260815`. The class vocabulary below subsumes
  these, it doesn't rename them.
- Escalation-ladder unification is owned by
  `docs/backlog/self-healing-spine.md`; this item is a compatible
  slice (its detector + breaker become condition-registry rows there),
  not a competing ladder.

## Design

### A. Halt classes — the foundation

Extend the tick-conclusion block with an optional
`halt-class: infra | blocked | misconceived | cost-cap`
(absent/unknown → treated as `infra`, the conservative-for-liveness
default: a wrongly-parked misconceived task wastes one probe tick; a
wrongly-terminal infra halt is this incident). Runner maps per class:

- `infra` — environment broken (tool errors, DB down, auth).
  **Never terminal.** Tag `waiting-for:infra` (a park, inheriting
  cooldown-release + long-wait alerting) instead of `halt:*`, and
  enroll in the breaker (B).
- `blocked` — a named external dependency
  (`waiting-for:<what>` if the agent can name it). Park, existing
  semantics.
- `misconceived` — task-design failure. Gets **one repair attempt**:
  the next re-tick's prompt carries the halt summary + an explicit
  "clarify or re-plan; if still misconceived, halt again" instruction;
  a second misconceived halt is terminal → `halt:misconceived` +
  severity-`info` alert. (SHIELDA's Plan Repair, bounded to one
  round.)
- `cost-cap` — terminal until a human raises the budget:
  `halt:cost-cap` (already exists), no parole.

Terminal halts must capture context, not just the tag: the runner
already stores the conclusion summary on the job's `job_summary`
chunk — additionally stamp the parent's `meta.halt = {class, job_ref,
at, summary_first_line}` so the attention view / web can show *why*
without a join hunt.

**Retry-safety gate:** before any automatic re-tick of a previously
halted todo (parole, probe, repair round), the runner must check the
prior tick's side-effect record (tool-call audit over `worker_logs` —
the same query `_build_job_result_text` uses). If the halted tick made
non-idempotent writes with uncertain outcome, don't auto-retry —
escalate to `ask-user:` instead. (Report: uncertain-outcome operations
never auto-retry.)

### B. Outage circuit-breaker — de-personalize systemic failure

A dispatch-side breaker per failure signature (start with one global
`infra` signature; refine later if needed):

- **Trip:** ≥N `halt-class: infra` (or classless halts) across ≥K
  *distinct* todos within window W (suggest N=3, K=2, W=30min — the
  09-01 outage would have tripped on its second victim). On trip:
  raise ONE `critical` alert (`alert-source:breaker:infra-outage`,
  fingerprint = window id), tag subsequent victims
  `waiting-for:outage-<alert_id>` instead of individual parks, and
  **stop dispatching the claude_inproc todo lane** while open (don't
  feed more todos into a known-broken environment; scheduled
  non-agent lanes unaffected).
- **Half-open probe:** after cooldown (suggest 15min, doubling), tick
  ONE sampled member of the cohort. Success → close: resolve the
  alert and sweep the cohort's `waiting-for:outage-*` tags in one
  transaction (members resume normal dispatch). Failure → stay open,
  back off.
- Manual override: resolving the alert by hand also sweeps the cohort
  (the 2026-08-15 recovery, one click).

### C. Parole — subsumed into A+B

No standalone 48h timer. `infra` halts are parked (A) and released by
the breaker probe (B) or by the ordinary park cooldown when the
breaker never tripped (single-victim infra flake). `misconceived`
gets its one repair round (A) and is then genuinely terminal.
Rationale: the literature's "parole" is release-on-known-fix; a blind
timer re-runs permanent failures.

### D. Nursery backstop detector

New row in the nursery catalogue: `stale-halt` — leaf with
`halt:agent-declared` (the legacy classless tag, or any terminal halt
older than 14d whose `meta.halt` is missing) → `info` alert. Catches
pre-existing debris (there is live debris *today*: td287264 still
carries it, deliberately) and anything A/B misses. Cheap SQL, same
dedup/auto-resolve as the other detectors.

### E. Surface tree state on the artifact page

The smartdraft/draft web view renders a state banner when a todo
linked `draft-of` to the draft is halted/parked/child-failed —
class, since-when, one-line reason from `meta.halt`, and the existing
▶ resume affordance (`routes/todo.py::_halt_tags_for` already powers
subtree-clearing resume). The user was watching the exact page that
hid the state.

## Slices (each independently shippable)

1. **A-core:** `halt-class` parsing in `tick_conclusion.py` + runner
   mapping (infra/blocked → park; misconceived → repair-round state in
   parent meta, terminal on second; cost-cap → terminal) +
   `meta.halt` context stamp. Prompt-side: document the field in the
   planner prompt (`workers/planner_prompt.py`).
2. **D:** `stale-halt` nursery detector (also the migration story for
   legacy `halt:agent-declared` debris — surfaces it for triage
   instead of a big-bang sweep).
3. **B:** breaker (trip/half-open/probe/cohort sweep) + critical
   alert. Depends on slice 1's classes.
4. **A-retry-safety:** uncertain-outcome check gating every automatic
   re-tick path added above.
5. **E:** draft-page state banner.

## Tests (contract level)

- A halt conclusion with `halt-class: infra` parks
  (`waiting-for:infra`), does NOT tag `halt:*`, and the todo re-enters
  dispatch candidacy after cooldown.
- `halt-class: misconceived` → first occurrence re-ticks with a
  repair prompt; second → terminal `halt:misconceived` + alert +
  `meta.halt` populated.
- Classless `verdict: halt` behaves as `infra` (backward compat with
  deployed agents that predate the field).
- Breaker: 3 infra halts / 2 todos / 30min trips once (one alert, no
  dupes); probe success sweeps the whole cohort's parks in one tx;
  probe failure keeps it open.
- A halted tick with a non-idempotent uncertain-outcome tool call is
  excluded from auto-retry and gets `ask-user:`.
- Nursery `stale-halt` fires on aged legacy tags and auto-resolves
  when the tag clears.
