---
status: ready
title: dev-session context diet — user-only settings edits + OQ-11
---

# Dev-session context diet

Baseline (`/context`, 2026-08-21): fixed ≈68k of 1M. Target: **~31k (~3%)**.
Agent-doable edits are done: CLAUDE.md ~1.1k (2026-08-21), agent
descriptions one-line (2026-08-21), `.mcp.json` pinned, MEMORY.md
trigger-vs-payload diet (2026-08-22 — recipe bodies live in
`docs/runbooks/reset-test-db.md` / `prod-one-off-cli.md`), and the eviction
checks shipped in `scripts/memory-lint` (payload-smell flag on code fences in
memory files + a preamble token-vs-budget line in the hygiene output).

**Remaining — user-only edits** (auto-mode classifier blocks agent writes to
settings):
- `.claude/settings.json`: `"disableArtifact": true` (~4k; no repo dependents);
  `"disableWorkflows": true` (~5k) — the `/coder-chain` caveat is now moot:
  `coder-chain` re-platformed onto the Agent tool (86808d8b,
  `.claude/skills/coder-chain/SKILL.md`; old `.claude/workflows/coder-chain.js`
  deleted), so disabling the Workflow tool no longer costs it. Still a
  user-only edit — apply it whenever convenient.
- `~/.claude/settings.json`: `"disableClaudeAiConnectors": true` (stops gmail
  connector auto-fetch).
- Optional `skillOverrides`: `"name-only"` for land/go/rebase/next (~0.2k).

**Remaining — open verification (OQ-11,** from the shipped command-profile
item): can MCP 2025-06-18 + FastMCP 1.x flag a `prompts/list` entry as
render-at-session-start? Decides whether the redundant `Pinned skills:`
banner line drops. Owner: `src/precis/mcp_modalities.py::register_skill_prompts`.

**Test:** fresh-session `/context` fixed total ≤ ~32k; memory-lint green.
