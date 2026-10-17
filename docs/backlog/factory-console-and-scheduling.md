---
status: idea
pillar: platform
---

# Factory console + capability-reserved decentralized scheduling

> **Status: design-of-record, substantially shipped.** The scheduling
> framing (old §15) is superseded by
> [`cluster-scheduling.md`](cluster-scheduling.md) — the unified master
> for current state, ordering, and laws. This file keeps only the
> console/registry/capability-model scope that is still open.

Shipped (migrations 0072, 0073; collapsed-worker cutover 2026-08-04): see
the `precis.workers` package docstring and `deploy/README.md`; design in
`git log`. Residuals of the cutover: `cluster-scheduling.md` §L.

## The one-sentence principle (kept as the design frame)

Everything the factory does is a consumer that runs where its capability
lives, reserves that capability locally, and releases it when done —
hosts publish what they can do, work declares what it needs, and
claiming a unit of work is the same transaction as reserving the
resource it consumes. No central scheduler, no cross-node call for a
local capability.

Capability classes (the decided test): *physical need OR heavy dep →
gate; otherwise wire it everywhere.* Incidental gates (a vaulted key, a
light dep, a cache dir) are not real scarcity — universalize them.
Trust is a write axis orthogonal to capability: read/fetch goes
everywhere, write/mutate is the boundary, enforced per-todo via
`meta.envelope = {egress, write, return}` across three tiers
(tool-level / DB-role / no-net container).

## Open scope

### 8c — spend/compute envelope facets (unbuilt)

The envelope grows two cost facets alongside the affect/exfil ones:

```
meta.envelope = { egress, write, return,
                  spend:   none | metered | full,   # for-pay APIs / real $
                  compute: local | premium }        # GPU / supercompute
```

- **spend → the LLM router + the budget breaker.** A metered call needs
  `spend ≥ metered` AND breaker budget — per-todo authorization × fleet
  availability. The transport tiers (OAuth Max-plan ≈ free / metered API
  key / local OSS) *are* the spend tiers.
- **compute → the claim path.** Reserving a `gpu`/premium slot requires
  `compute ≥ premium`; an idle GPU does not mean a backlog-groom todo
  may fire a fold on it.
- **Rollout constraint (decided):** ship permissive-dark (no behavior
  change), assign tiers to the legit spenders, then flip to
  deny-by-default — authorize expensive consumption up, never restrict
  it down.
- Lands on the same container-executor knobs as cluster-scheduling §H:
  spend picks the injected secret, compute gates the slot reservation.

### 9b — quests tab write side

Editable prio slider + enable/disable (STATUS active↔dormant), both
reusing the existing quest handler; render the `serves` DAG. The
read-only panel (`_quests` in `routes/factory.py`) is live.

### Console v2

Evidence gr162694: console v2 enhancement requests (slices 3-4).

### 11b — model-eval surface

The web "compare" button on the model picker; wire the heavy-axis
scorers (`code` = run the fix's tests, `summarize-extract` = rubric
judge, `reasoning-convergence` = live telemetry); curated
`scripts/llm_eval/gold_set/` from real historical gripes/papers +
endpoint-scoped `record_eval(quant=)`. The harness + deterministic axes
(`long-context-recall`, `tool-structured`) and CLI
(`precis llm eval`) are live.

### Skills follow-up

Capability-goes-to-work produces smaller, capability-scoped todos; the
authoring skills (`precis-decomposition-help`, `precis-todo-tree-help`)
should teach agents to author work that way — a small,
single-capability unit routes; a monolithic one parks.

## Decisions remaining

- **Cost of retiring the Max-plan flat-rate** — vaulting the token is
  free, but moving agentic load onto metered API keys is a spend
  decision (mitigated by pushing reasoning to local qwen).
- **Vault read-scope** for the high-value OAuth secret (which
  roles/hosts may read it — it is *spend*, higher-value than the
  EPO/S2 keys).

## Explicitly NOT in scope / decided constraints

- Do NOT reintroduce a central scheduler or cross-node reservation —
  reservation target is always `(me, resource)` (claim-gating routes
  work).
- llama-swap stays (per-node VRAM swapper); only litellm-the-proxy
  retired.
- embedder / web / asa-bot stay separate processes (distinct failure
  domains) — console rows with start/stop, not `prio`.
- `target_node` survives as node gate + advisory cache-affinity hint,
  not as the routing mechanism.

Related: factory-post-auth.md (write-route auth gap on the same file).
