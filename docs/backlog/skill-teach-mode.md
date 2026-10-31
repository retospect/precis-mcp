---
status: draft
title: "opt-in teach mode — specialist agents narrate the reasoning behind design choices as they work"
pillar: platform
prio: low
---

# Teach mode — narrated design reasoning

Source gripes: gr540148 (cluster `teach-mode`; `fix:reto`/`reto:ask`,
component `skills`; priority low per the gripe).

## Motivation / why

Specialist agents do the task and report the result but don't surface
*why* they chose one approach over another unless explicitly asked; for
a user building intuition, that reasoning is the valuable part. Mirrors
Matt Pocock's `/teach` skill (youtube:Z8lf5oWKfj0): explain tradeoffs
and reasoning as the work proceeds, not just the final diff/answer.

## In scope

- An opt-in flag/skill that, when invoked on a task, makes the
  responding agent narrate key decision points inline — why this
  library/pattern/structure over the alternative, what would break if
  done differently — scoped to the decisions that actually had
  alternatives worth mentioning, not a running commentary on every
  line.

## Explicitly NOT in scope

- A default-on behaviour — narration is opt-in per task.
- Long-form tutorials or post-hoc writeups; the narration is inline
  decision reasoning at the moment of the choice.

## Acceptance criteria

Draft — to be hardened at `ready`.

- With the mode invoked on a task, the agent's report names the
  decisions it faced and the rejected alternatives, inline where they
  happened.
- With it off, output is unchanged.
- Which surface carries the flag — TBD on the open question below.

## Target + blast radius

`src/precis/data/skills/` and/or the specialist prompts under
`.claude/agents/` — open question below. No handler changes.

## Open questions / decisions log

Reto's (`reto:ask`) — stays `draft`.

- **Build it at all** — the gripe's recommendation comment asks Reto to
  decide whether the opt-in teach mode is wanted.
- **Carrier.** A served skill (`precis-teach-help`) invoked per task,
  or a standing preference in agent definitions — and whether it
  composes with the shipped interrogation skill (`precis-grill-me-help`).
- **Narration scope.** Decisions-with-alternatives only, or also a
  short "why" on the final diff.
