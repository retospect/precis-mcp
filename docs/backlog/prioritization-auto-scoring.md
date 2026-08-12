---
status: idea
prio: low
title: Auto-score priority (severity × frequency) + human triage loop
---

# Auto-score priority + human triage loop

The plain priority axis shipped (a `prio:` front-matter on backlog items,
sorted high-first by `scripts/docs-index` + `fixer/intake.py`; the groomer
carries a gripe's `prio` onto its minted `fix_gripe` todo). Priority is set
**by hand** today. These follow-ons make it self-populating — deferred on
purpose so the model-tier decision isn't made under time pressure.

## Motivation / why
3/69 open gripes carry any priority; manual scoring won't scale. The value
of a priority axis is only realised if most items carry a *meaningful*
score, which means deriving one automatically.

## In scope
- **LLM auto-scorer at groom time.** `backlog_groom.py` reads a gripe body →
  severity (1–3) × frequency (1–3) → `prio`, stamped on both the gripe and
  its minted todo. **Open decision: model tier.** A tiny model is cheap but
  mis-ranks the fixer's queue on nuanced bugs; a smart model is accurate but
  bills ~Opus per gripe every groom cycle. Pick deliberately — this orders
  what the autonomous fixer builds first.
- **Auto-escalate the obvious.** A `scripts/backlog-lint` heuristic bumps an
  item to `prio: high` on hard signals (past-due `snooze-until`, a referenced
  `high`/`critical` Dependabot alert, `P0`/`data loss`/`security` keywords),
  printing what it escalated so a human can veto.
- **`/whatneedsdoing` score-and-dismiss loop.** After reading INDEX, surface
  the unscored (`normal`, untouched) items in impact order and drive an
  interactive score/snooze/delete pass, writing `prio:` / `snooze-until:`
  into the item file.

## Explicitly NOT in scope
- The plain `prio:` field and gripe-prio inheritance (already shipped).
- A real frequency signal wired from the LLM-confusion mining histogram
  (`/whatneedsdoing` step 6) — a later refinement once gripes carry a stable
  dedup key to join on.

## Acceptance criteria
- Groomed gripes land with a non-default `prio` derived from their body.
- The model-tier choice is recorded (a decision line here or in the groomer
  docstring), with its per-cycle cost estimate.
- The escalation heuristic and the triage loop each have a test.

## Target + blast radius
`src/precis/workers/backlog_groom.py`, `scripts/backlog-lint`,
`.claude/commands/whatneedsdoing.md`, and whichever `llm.chain.*` tier the
scorer routes to.

## Open questions / decisions log
- Model tier for the scorer (see In scope) — unresolved; the reason this
  item exists rather than shipping with the mechanism.
