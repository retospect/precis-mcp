---
status: idea
title: embedder venv fails its convergence assert intermittently on the first deploy pass
pillar: platform
---

# Deploy failure reporting

From the 2026-10-11 gripe triage (the failed-host / mixed-versions banner item
shipped in `scripts/deploy`).

## Embedder venv "DID NOT CONVERGE" on the first pass (gr172390)

The `precis_embedder` role intermittently fails its convergence assert on
pass 1 of a deploy and passes on retry. The cause is unknown; a retry hides
it. Do not weaken or remove the assert.

Fix direction: the next time pass 1 fails, re-run the deploy with
`-e precis_embedder_trace=true` and keep the trace, then fix the root cause
it shows. Until a trace exists there is nothing to code. Files:
`deploy/roles/precis_embedder`.

Who: ops to capture the trace on the next occurrence, then agent-code.

## Acceptance

- A first-pass embedder convergence failure is either gone or has a recorded
  trace and a root-cause fix, with the assert intact.
