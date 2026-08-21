---
status: draft
title: question-shaped skill retrieval targets + bimodal skill injection
---

# Skill question targets + bimodal injection

Decided direction (Reto, 2026-08-21): retrieval-as-infrastructure — the
harness runs the first hop of skill RAG; the model never has to *decide* to
search (measured: 5 of ~19,853 prod jobs used discovery). And no almost-right
middle tier: inject the perfect skill or inject nothing.

## 1. Question targets — SHIPPED 2026-08-21

`question_only` (front-matter `summary:` + new `answers:` list) and
`heading_only` chunk variants now embed alongside structural/body_only
(`skill_index/chunker.py`, `variant` field); ~10 high-traffic skills carry
seeded `answers:`. Remaining authoring: the coverage sweep over the rest of
the corpus (dreaming/build-time LLM pass) — questions in task language. Embed `summary:` the same way as a freebie. The same
targets sharpen pull-based `search(kind='skill')` too.

## 2. Bimodal injection (no cue tier)

At harness-controlled context-assembly points — tick prompt build
(`workers/executors/claude_inproc.py`, `quest/tick.py`,
`workers/planner_prompt.py`; dev-side later via a prompt-submit hook) — embed
the task text against the question targets:

- score ≥ high threshold → inject the **whole matched skill** (the perfect
  one, payload included);
- otherwise → inject **nothing**. Fallbacks stay the existing pull channels:
  executable error hints and `Next:` blocks
  (`tick-tool-lists-and-discovery-reflex.md`; hints are parse-verified against
  the shipped command profile, `tests/test_command_parser.py`).

No cue-line middle tier — that was a hedge for a fuzzy matcher; sharp
question targets make scores bimodal, and an almost-right skill in context is
worse than silence.

## 3. Ledger calibration (self-improving, cheapest possible edit)

`mcp-tool-ledger.md` closes the loop: injected-skill-never-used → threshold
too permissive; detour-despite-silence (psql, paging workarounds) → a
coverage hole whose fix is *authoring one more question* on an existing
skill — not new machinery.

Sequencing: 1 ships alone (index + authoring + search win); 2 needs 1; 3
needs the ledger. Interacts with per-job tool lists (injection is per-job
too) and `unify-backlog-gripes-discoverable.md` (same architecture over dev
knowledge).
