---
status: ready
title: dev-session context diet — settings, CLAUDE.md, MEMORY.md, agent defs
---

# Dev-session context diet

Baseline (`/context`, 2026-08-21): fixed ≈68k of 1M. System tools 39.5k ·
MCP 10.4k (deferred at t=0; loads on first use) · CLAUDE.md 5.1k ·
MEMORY.md 4.5k · skills 3.8k · agents 1.2k. Control: bare code-only repo
session ≈28k. Target: **~31k (~3%)**.

**User-only edits** (auto-mode classifier blocks agent writes to settings):
- `.claude/settings.json`: `"disableArtifact": true` (~4k; no repo dependents);
  `"disableWorkflows": true` (~5k) ONLY if losing `/coder-chain` is acceptable
  (`.claude/workflows/coder-chain.js` runs on the Workflow tool).
- `~/.claude/settings.json`: `"disableClaudeAiConnectors": true` (stops gmail
  connector auto-fetch).
- Optional `skillOverrides`: `"name-only"` for land/go/rebase/next (~0.2k).

**Agent-doable edits:**
- ~~CLAUDE.md 5.1k → ~2k~~ done 2026-08-21 (now ~1.1k tokens; READ-ONLY
  dogfood claim dropped, hook-enforced conventions are one-line pointers).
- MEMORY.md 4.5k → ~2k by trigger-vs-payload: recipe bodies (SQL, command
  sequences) → `docs/runbooks/`; index lines become symptom → pointer; delete
  resolved-incident entries. Rule: derivable-at-need → pointer; else keep, terse.
- 19 `.claude/agents/*.md` descriptions → one terse line each (~0.6k).
- Done in this worktree: `.mcp.json` pinned `claude-context-mcp@0.1.15`
  (was `@latest`; resolve-on-start broke the server while Milvus/embedder were
  healthy).

**Eviction (keeps it shrunk):** memory-lint gains a payload-smell check
(SQL/code fences in memory files → flag); `/whatneedsdoing` hygiene gains a
preamble token-count vs budget line (CLAUDE.md + MEMORY.md).

**Open verification (OQ-11, from the shipped command-profile item):** can MCP
2025-06-18 + FastMCP 1.x flag a `prompts/list` entry as render-at-session-start?
Decides whether the redundant `Pinned skills:` banner line drops. Owner:
`src/precis/mcp_modalities.py::register_skill_prompts`.

**Test:** fresh-session `/context` fixed total ≤ ~32k; memory-lint green.
