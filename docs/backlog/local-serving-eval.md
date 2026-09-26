# Local serving eval

Grouped 2026-09-26 from 3 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## Independent local research (Reto want)

_Grouped 2026-09-26; was `local-research-agent`._

The smartest local model that fits the big Mac should do the bulk operational
research (ML-potential work on our catalyst; the other research processes)
with occasional opus consultations. Today it does ~nothing autonomously —
local models need stronger "do things, use tools" system prompts. The nightly
and morning meditations should also compose on the biggest local model that
fits. Needs design.

## Evaluate turbo-fieldflare (Swift/Metal MoE weight-streaming) for Mac serving

_Grouped 2026-09-26; was `turbo-fieldflare-eval`._

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

## Support EU LLM routing providers

_Grouped 2026-09-26; was `eu-llm-providers`._

Candidates: ShareAI, Eden AI, Orq.ai, Requesty (Frankfurt, GDPR-complete),
EUrouter, Cortecs AI; single-API EU-sovereign inference: Tensorix, IONOS AI
Model Hub, evroc. Review and pick the best ~3 to support as router
transports/base-URLs.
