# req.model pins bypass the backend-coherence check

dream's PRECIS_DREAM_AGENT_MODEL env-pin and asa's hard-pinned
`--model claude-opus-4-8` (`src/asa_bot/claude_invoke.py`) set `req.model`
directly, which dispatch honors over `resolve_model(tier, backend=)` — so the
ADR 0066 Part-3 coherence check (inside resolve_model) never runs for them,
and an OSS slug can still land on a claude transport under a half-applied
flip. Reviewer finding #2 from the Phase-1 flip-safety landing. Owner
`src/precis/utils/llm/router.py`.
