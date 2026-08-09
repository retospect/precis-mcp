# Dreaming — agentic memory consolidation + synthesis

Shipped portion: see the `precis.workers` package docstring and
`src/precis/workers/dream_agent.py` module docstring; full design in
git history. Live: migration `0007_dreaming.sql` (salience columns,
`bump_salience()`, `supersedes` relation, `dream_log` +
`dream_transcripts`), the full-agentic dream loop
(claude-over-MCP, turn/cost-bounded, transcript capture), the
`angle`/`n`/`like` cone-sampling spray on `search`,
`view='dreamable'`, the guarded `supersede` tool, `DREAM:` fencing,
external reach (`PRECIS_DREAM_SEARCH`), and the dream throttle
(`workers/dream_throttle.py`). The `acquire` tool was later retired
(see `handlers/paper.py` note).

Owner anchors: `src/precis/workers/dream_agent.py`,
`src/precis/tools/core.py` (angle spray / dreamable view).

## Open scope

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
