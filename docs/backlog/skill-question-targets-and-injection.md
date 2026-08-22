---
status: draft
title: skill-injection ledger calibration (§3 of question-shaped retrieval)
---

# Skill-injection ledger calibration

Decided direction (Reto, 2026-08-21): retrieval-as-infrastructure — the
harness runs the first hop of skill RAG; no almost-right middle tier.

§1 (question targets) and §2 (bimodal injection) are SHIPPED: `answers:` +
`summary:` front-matter cover the full skill corpus (question_only /
heading_only variants, `skill_index/chunker.py`), and
`skill_index/injection.py` injects the whole top-matching skill — or
nothing — into planner prompts (`workers/planner_prompt.py`) and quest tick
prompts (`quest/tick.py`) when the score clears
`PRECIS_SKILL_INJECT_THRESHOLD` (default 0.85; `PRECIS_SKILL_INJECT=off`
kills it). Injection decisions log skill id + score; near-misses within
0.05 log at debug.

## Remaining: §3 — ledger calibration (blocked on `mcp-tool-ledger.md`)

The ledger closes the loop, cheapest-possible-edit style:

- injected-skill-never-used → threshold too permissive (raise it);
- detour-despite-silence (psql detours, paging workarounds) → a coverage
  hole whose fix is *authoring one more question* on an existing skill —
  not new machinery.

Inputs already exist: the injection log lines above, plus prod job
transcripts. Blocked until the ledger lands; then calibration is a
read-the-ledger-and-edit-front-matter loop, no code.

Interacts with per-job tool lists (`tick-tool-lists-and-discovery-reflex.md`)
and `unify-backlog-gripes-discoverable.md` (same architecture over dev
knowledge).
