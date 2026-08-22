# Retire claude-context (Milvus + embed shim + node stack)

With kind `python` (507975cd's predecessor) and kind `md` (507975cd)
both live, the precis session MCP covers repo code (lexical +
structural) and repo prose (hybrid semantic, DB-free) — the
claude-context stack's remaining value is near zero. Decommission:
drop the `claude-context` entry from `.mcp.json`, the Milvus/shim boot
from the SessionStart hook, `scripts/code-index`/`code-search` + the
docker compose stack; update CLAUDE.md's orientation pointers to
`search(kind='python')` / `search(kind='md')` + Grep + coderef. Gate:
run a week with `PRECIS_MD_ROOTS=repo:.` enabled in the session MCP
env first; parity-check a few fuzzy queries against `search_code`
before deleting. Owner anchor: `precis.md_index` package docstring.
test: sessions boot without the Milvus stack; no hook errors; the two
kinds answer the queries `search_code` used to.
