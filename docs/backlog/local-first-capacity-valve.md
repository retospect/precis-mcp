# local-first-capacity-valve

## Residuals (from OPEN-ITEMS)

Item 3 (saturation→cloud spillover) is the open half; local serving shipped
(`1c0d63b6`). `src/precis/utils/llm/router.py::_dispatch_openai_compat`
ignores `req.local_url`, always using `PRECIS_LLM_BASE_URL` — so a saturated
local slot pauses instead of spilling. Fix: honor `req.local_url` when set,
then flip the small local rung back to `openai_compat` so a saturated slot
retries the same rung against the cloud base URL. Gotchas: a local llama-swap
wants the dummy bearer, not the OpenRouter key; never send the
`openrouter_routing` extra_body to a local endpoint. Blocker: cloud rung
(glm-4.7-flash) ≠ the local 9B — pick an open-weight model served both sides
if transparent overflow matters. Only melchior serves the 9B and
`llm_call_log` has no host column, so verification needs an on-host check.
For the wider local-first revisit: the GLM no-think chat-template key for
llama.cpp is unconfirmed (`_dispatch_local` NOTE); melchior's idle local 80B
was deliberately left running.

test: an `openai_compat` dispatch with `req.local_url` set hits that endpoint
(dummy bearer, no openrouter extra_body), not `PRECIS_LLM_BASE_URL`.
