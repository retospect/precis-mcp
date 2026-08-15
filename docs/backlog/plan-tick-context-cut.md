---
status: draft
title: plan_tick context cut — pre-fetch to shrink turns, exponential re-tick cooldown
model: opus
due: 2026-08-24
---

# plan_tick context cut (P2 of the 2026-08-15 zombie-loop plan)

**Do not start before ~2026-08-24** — the criterion is one healthy baseline
week of plan_tick cost/turn data *after* the credential fix deployed
(2026-08-15), so the savings are measured against a real baseline instead of
the outage's zombie numbers. Reto approved this as a filed item, not
immediate work ("p2 write an openissue and add a duedate").

Cost shape at filing (healthy ticks, pre-outage): ~53–57k-char assembled
prompt × 12–22 agent turns ≈ 0.6–1.2M cache-read tokens ≈ $0.55–0.70/tick on
the sonnet lane. Two levers, in order of expected yield:

1. **Turn-count reduction via pre-fetch.** Most early turns are the agent
   `get`ting context the assembler could have included: extend the
   draft-blocks pattern (`planner_prompt._render_draft_sources` pre-fetches
   sources for draft ticks) to plain planner ticks — pre-render the
   likely-needed reads (children's latest job_summary bodies, the workspace
   files it always opens, the top search hits for the todo title) into the
   VARIABLE user layer. Every pre-fetched read is one fewer round-trip at
   full-context cache-read cost. Measure: median turns/tick before vs after
   (llm_call_log.turns_used, source='plan_tick').

2. **Exponential re-tick cooldown.** A todo that ticks without producing
   children/state changes re-enters the rotation on a fixed cooldown; make
   the cooldown grow per consecutive resultless tick (e.g. 1h → 4h → 16h,
   reset on any store write) so a healthy-but-stuck leaf costs
   asymptotically less. Complements — does not replace — the
   `no-precis-tools` parking shipped with P1 (that handles the *env-broken*
   flavor; this handles "env fine, task going nowhere").

Keep the CACHED system layer untouched (already slimmed 22.4KB→2.8KB on
2026-08-07); the win is in the variable layer and the turn count, not the
persona floor.

test: a planner tick whose parent has completed children receives their
summaries in the assembled prompt without a `get` round-trip; a todo with N
consecutive resultless ticks waits longer than one with N-1 before its next
tick is minted.
