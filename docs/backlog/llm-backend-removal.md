# Backend (PRECIS_LLM_BACKEND) — remove the parallel axis

The fleet-wide anthropic/openai binary switch must be hand-synced with each
tier's PRECIS_MODEL_* id — nothing enforces the pairing (already produced one
real bug). Reto: "it should all go to the router." Grep-confirmed:
`resolve_backend`/`Backend` are consumed only inside `select_transport` +
`dispatch()`'s base-url coercion; a resolved model id already determines its
transport, so infer transport from the model id and drop Backend +
PRECIS_LLM_BACKEND — which also yields per-tier backend for free. Needs a
spec: the claude-detection rule (a prefix check may suffice),
`live_config.backend_override`'s fate, and the callers passing `backend=`
explicitly. Not audited: PRECIS_EMBEDDER / PRECIS_EMBEDDER_BACKEND may be the
same two-axis smell. Owner `src/precis/utils/llm/router.py`. Needs design.
