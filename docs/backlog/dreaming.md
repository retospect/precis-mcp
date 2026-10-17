---
status: idea
pillar: memory-graph
---

# Dreaming — agentic memory consolidation + synthesis

Shipped: see the `precis.workers` package docstring and
`src/precis/workers/dream_agent.py` module docstring; design in `git log`.
Owner anchors: `src/precis/workers/dream_agent.py`,
`src/precis/tools/core.py` (angle spray / dreamable view),
`handlers/_finding_hypothesis.py` (Step 6d/6e hypothesis proposals).

## Open scope

- **Proposed hypotheses have no lane in the `/nanopub` forest.** They
  land there as `unminted` alongside ~1,300 ordinary hubs that simply
  never got a publish row, so the forest cannot tell "an agent prepared
  this for you" from "nobody ever touched this". Discovery works today
  via `/needs-you` (counted lane, linked rows) and the top-bar badge —
  that is the queue a human actually works. Giving the forest its own
  `hypothesis-proposed` grouping means a new field on
  `nanopub/overview.py::HubOverviewRow` threaded through `hub_tree` and
  the state strip, whose axis is publish state — which a proposal, by
  construction, does not have. Worth doing only if the forest becomes
  the place people triage proposals.
- **Dream mode rotation (Part B).** Rotate the cycle's *deliverable*
  (connection / library-gap / open-question / consolidation /
  analogy), not just the lens (`PRECIS_DREAM_LENS`). Needs surgery on
  `dream-prompt.md` — the connection shape is hardcoded into Step 6.
- **Active dreams (wanted).** An active-build mode that kicks a
  derived-lane job (DFT relax, `cad_propose`, structure relax) on a
  surfaced subject, then connects the result back into a memory.
  Gate behind the load ceiling + a budget cap.
- **Tuning knobs still on defaults** (revisit from `dream_log`
  telemetry, not preemptively): access-event weights
  (cite / read / search-impression), cluster-frontier size and
  `angle`/`n` cone defaults, `--max-turns` / `--max-budget-usd`
  starting values.

## Decided constraints

- Full-agentic loop only — no separate per-mode worker path.
- "No action" is a first-class outcome; bias toward doing nothing.
- Agent writes are additive (`put`/`link`/`tag`); destructive merges
  go only through the guarded `supersede` tool — no raw delete.
- Dreams are fenced in search (`DREAM:` tag), never boosted.
- Provenance relation reuses `derived-from` (no new `summarises`).
- Cluster ids stay ephemeral (computed per call); persist a
  `clusters` table only if the agent must reference a cluster seen
  several turns earlier (`like=<member>` is the cheaper pin).
- `dream_log` retention: keep everything forever (incl. no-ops),
  analysis-only.
