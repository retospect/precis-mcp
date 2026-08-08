# Evaluate turbo-fieldflare (Swift/Metal MoE weight-streaming) for Mac serving

Reto want (youtube:189018): serves a 26B MoE in ~2 GB RAM at ~23 tok/s by
streaming experts off SSD just-in-time — a fit for the local-first revisit,
especially balthazar (the SMALL Mac, ~3 GB free). Evaluate before adopting:
(a) it's a Mac app/CLI, not obviously an OpenAI-compatible /v1 server — the
router's local-serving path needs /v1/chat/completions, so it needs a shim or
doesn't slot in as a placement:"local" rung; (b) Apple-Silicon + MoE-decode
specific (no help for spark/CUDA); (c) confirm a small chat model we care
about is servable, not just the demo Gemma-3 MoE. Note: the "tokenbert"
sibling idea is a different substrate — the keyword pass depends on the
bge-m3 embedder service (ADR 0020), not the llm slot path; separate effort.
