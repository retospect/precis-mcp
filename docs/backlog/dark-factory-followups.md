# Dark-factory workflow follow-ups

Ship/deploy loop is live; these workflow additions remain.

- /testfeature <prompt>: an agent loop exercising the MCP surface
  (scripts/exercise-mcp seed) that finds bugs, fixes, /go; turn/cost-capped.
- /checklogs: read the recent LLM-error surface (prod agentlog + alert +
  failed kind='job' + error ref_events; local logs), cluster the top-N
  recurring failures, fix root cause, /go.
- Widen scripts/ship auto-fix to anything the gate can resolve without
  judgment (import sort, trivial mypy stubs).
- Deferred: holdout scenarios (anti-overfit eval outside the repo);
  digital-twin fidelity; auto-deploy as a daemon (vs /go-chained).
