---
status: ready
pillar: platform
---

# Retire claude-context (Milvus + embed shim + node stack)

Agent-facing half shipped 2026-10-08: Reto disabled the `claude-context` MCP,
`.mcp.json` no longer registers it, the SessionStart hook no longer boots
Milvus or the embed shim, and CLAUDE.md, the agent definitions, the `bug`
skill and `bash-reflex-nudge` point at the precis python kind
(`get(kind='python', id='main::<qualname>')`, `search(kind='python',
mode='pattern', ...)`) plus Grep and `scripts/coderef`.

Left:

- **Fleet health check.** `scripts/fleet mcp-check` and
  `scripts/lib/fleet_mcp_state.py` check `--servers precis,claude-context`;
  every window now reports `claude-context` as dead or unknown. Drop it to
  `precis` alone; update `tests/test_fleet_watch.py` and
  `tests/test_fleet_mcp_state.py`.
- **Infra deletion.** `scripts/code-index`, `scripts/code-search/`,
  `docker/code-search/compose.yaml`. Stop the Milvus containers on the Mac.
- **Stale prose.** `scripts/coderef` docstring, `docs/how-to-setup-like-this.md`.
- **Fuzzy search gap.** Plain `search(kind='python', q='<phrase>')` returned
  nothing for a natural-language query on 2026-10-08; only `mode='pattern'`
  hits. Find out whether the python index lacks embeddings in the session MCP
  container, and whether `search(kind='md')` covers "where is the code that
  does X" well enough.

test: sessions boot without the Milvus stack; `scripts/fleet mcp-check`
reports only precis; a natural-language python search returns hits.
