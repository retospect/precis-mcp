# Ops: llm.chain.medium points MEDIUM at a completion wire

Prod `app_settings` `llm.chain.medium` = glm-4.7 over `openai_compat` — a
completion wire that cannot call a tool. With the `resolve_chain` tools
filter shipped, agentic MEDIUM calls fall back to `_default_chain` →
claude_agent/opus: correct but pricier than intended. To keep OSS on agentic
MEDIUM, flip the rung to `transport: openai_tools` (BIG already proves the
combination); editor at `/status?tab=services`. Leaving as-is is also
acceptable — only agentic calls escalate. Operator call (Reto). Gotcha:
`llm.chain.*` is edited live in `/factory` — re-read the row before writing.
