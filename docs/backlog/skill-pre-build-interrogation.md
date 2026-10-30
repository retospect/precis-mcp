---
status: draft
title: "pre-build interrogation skill — an opt-in 'grill-me' pass that surfaces unknown unknowns before agents execute, grounded in the precis corpus"
pillar: platform
prio: normal
---

# Pre-build interrogation skill ("grill-me")

Source gripes: gr540146, gr540147 (cluster `grill-me-skill`; both
`fix:reto`/`reto:ask`, component `skills`).

## Motivation / why

When a build/implementation request lands, specialist agents (coder,
science, writer) go straight to execution; unstated assumptions —
security model, deployment target, who else touches this, untested edge
cases — get silently filled in, and the cost surfaces later (Matt
Pocock's framing, youtube:Z8lf5oWKfj0: the clarity of the ask determines
the quality of the output).

And a questioning pass alone would still guess at context cold: precis
already holds ~17k papers, prior conversations and memories that often
contain the relevant prior decision, constraint or prior attempt —
nothing forces a "check the corpus before asking" pass (Pocock's
`/grill-with-docs`; gr540147).

## In scope

- A skill (`precis-grill-me-help` or similar), opt-in per task and
  invokable as a step before coder/writer/science agents run, that asks
  a short round of targeted clarifying questions before any files are
  touched — focused on the gaps most likely to bite (security, deploy
  target, back-compat, scope boundary), not generic "what do you want"
  filler.
- A corpus pass before (or interleaved with) the questions
  (gr540147): `search(kind='memory'|'paper'|'conv', q=…)` over the
  task's keywords; hits that bear on the task are surfaced ("we decided
  X on 2026-xx-xx", "paper Y already covers this") and questions the
  corpus already answers are never asked. Folded into the same opt-in
  step, not a separate gate.

## Explicitly NOT in scope

- A blanket gate on every request — opt-in per task, by design.
- Changes to agent dispatch itself — this is a skill, not wiring.

## Acceptance criteria

Draft — to be hardened at `ready`.

- Invoking the skill on a flagged task produces a short, targeted
  question round before edits begin.
- The corpus pass runs first; a question the memory/paper/conv corpus
  already answers is not asked, and the surfaced hit is cited.
- An unflagged task runs exactly as today.

## Target + blast radius

`src/precis/data/skills/` (a new served skill) and possibly the
specialist agent definitions in `.claude/agents/` if the pass is meant
to trigger from their side rather than the user's. Docs/prompt surface
only; no handlers.

## Open questions / decisions log

All Reto's (`reto:ask` on both gripes) — stays `draft`.

- **Build it at all** — the gripe's recommendation comment asks Reto to
  decide whether the opt-in interrogation skill is wanted.
- **Trigger shape.** A `/grill-me`-style invocation the user calls on a
  task, or a flag the specialist agents honour when set?
- **Unattended runs.** What does a clarifying-question pass do when no
  human is in the loop (fleet builder, autonomous fixer) — skip-and-log,
  or file the questions into the item's decisions log and proceed on
  defaults?
- **Corpus scope** (gr540147): which kinds the pass searches and the
  bar for "already answered".
