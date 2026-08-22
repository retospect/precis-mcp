---
status: draft
title: coder-chain / Workflow-tool context diet — keep the chain, drop the ~5k standing cost
---

# Coder-chain context diet

Decision context (Reto, 2026-08-22): `disableWorkflows` would reclaim ~5k
startup tokens but kills `/coder-chain` (`.claude/workflows/coder-chain.js`,
3.8KB, runs on the Workflow tool) — declined. This item is the third option:
keep the capability, shed the standing cost.

The ~5k is dominated by the built-in Workflow tool's schema/description
(orchestration doc), not the 3.8KB script — the script only loads when
invoked. So the diet targets the *tool*, not the workflow.

Candidate paths, cheapest-first; measure before building (fresh-session
`/context` with each variant):

1. **Defer the Workflow tool.** `ENABLE_TOOL_SEARCH` already defers ~29
   tools whose schemas load on first use at the prompt tail (no cache bust).
   Workflow isn't in the deferred set — find out whether the deferral roster
   is configurable (settings/env) or usage-frequency-driven, and whether a
   built-in can be forced into it. If yes: zero capability loss, full ~5k
   win, one config line. Verify /coder-chain still invokes (skill → Workflow
   via ToolSearch load).
2. **Re-platform /coder-chain off the Workflow tool.** The chain
   (coder → test-runner → reviewer sequencing) is expressible as a skill that
   drives the already-loaded Agent tool sequentially. Then `disableWorkflows`
   becomes safe and the ~5k is reclaimed. Cost: lose Workflow's deterministic
   script/resume semantics — acceptable for a 3-stage linear chain, but
   check the .js for logic that doesn't port (schemas, retries).
3. **Accept partial:** if neither works, document the ~5k as the price of
   /coder-chain and close this item.

**Test:** fresh-session `/context` shows the Workflow tool absent from
loaded System tools (variant 1: deferred; variant 2: disabled), and a
/coder-chain invocation completes a real 3-stage run.

Related: `dev-context-diet.md` (parent effort; its settings-lines section
records the declined `disableWorkflows`).
