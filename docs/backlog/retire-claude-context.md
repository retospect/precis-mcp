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

Repo half shipped 2026-10-09: `scripts/fleet mcp-check` / `watch` and
`scripts/lib/fleet_mcp_state.py` ask for `--servers precis` alone;
`scripts/code-index`, `scripts/code-search/` and `docker/code-search/` are
deleted; `scripts/coderef` and `docs/how-to-setup-like-this.md` describe the
python kind instead of the vector store.

Left (local ops and a product question, not repo code):

- **Stop the Milvus containers on the Mac.** The `precis-code-search` compose
  project (etcd + minio + milvus, named volumes) is no longer referenced by
  anything in the tree; `docker compose -p precis-code-search down -v` on the
  host, then confirm nothing on :19530 / :8182.
- **Fuzzy search gap.** Plain `search(kind='python', q='<phrase>')` returned
  nothing for a natural-language query on 2026-10-08; only `mode='pattern'`
  hits. Find out whether the python index lacks embeddings in the session MCP
  container, and whether `search(kind='md')` covers "where is the code that
  does X" well enough.

test: sessions boot without the Milvus stack; a natural-language python
search returns hits.
