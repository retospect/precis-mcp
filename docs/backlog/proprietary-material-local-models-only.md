---
status: idea
pillar: local-compute
---

# Proprietary material is processed by local models only

Tag privileged or proprietary content (`proprietary` / `local-only`) on
refs/chunks so it is never sent to a cloud LLM API; a corpus can then mix
cloud-processable and local-only material.

- Routing: `utils/claude_agent.py` (agentic dispatch), `utils/claude_p.py`
  (one-shot judge), the planner writer and the reviewers exclude tagged
  content from cloud calls and route to a local backend (ollama, llama.cpp).
- Needs a local-model adapter peer to the `claude -p` path, plus a guard that
  refuses to assemble a cloud prompt containing any tagged ref.
- Pairs with the per-surface persona redesign (writer, chat, reviewer each
  get a role and backend).

test: a cloud-bound prompt containing a tagged ref raises; the same request
routes to the local backend.
