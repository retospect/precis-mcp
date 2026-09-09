---
status: idea
title: skills ship scripts, not just prose — stop agents re-deriving deterministic procedures
---

# Skill-bundled scripts

Observation (Reto, 2026-09-09): prod agents repeat the same multi-step
procedures — each run re-reasons a deterministic sequence from a prose skill,
burning tokens and occasionally diverging. The Agent Skills spec
(anthropics/skills) allows a skill to bundle `scripts/` the agent *runs*
instead of reasoning through; Anthropic's document skills (docx/pdf/xlsx)
work this way.

Precis equivalent: a skill's markdown says "run this", pointing at an
executable the agent invokes via kind='python' (or a registered op), instead
of ten prose steps the agent replays by hand. Candidates = the most-repeated
transcript procedures; the transcript mining `/whatneedsdoing` already does
can rank them.

Distinct from [composable-pipeline-kind](./composable-pipeline-kind.md):
that composes existing point ops into pipelines; this is about the *skill
layer* shipping the deterministic core of a procedure at all. If a bundled
script grows into a real op, it graduates into a handler and the pipeline
item takes over.

Guardrails: scripts run where the agent runs (executor container) — same
sandbox posture as any agent kind='python' call, nothing new; write-path
scripts stay out until the pattern is proven on read-only procedures.

test: take the top repeated procedure from prod transcripts, ship it as a
script-backed skill, and show the same task completing in materially fewer
turns/tokens with an identical end state.
