---
status: idea
title: scripts/deploy failure reporting — one host's prereq failure reads as a whole-cluster failure, and the embedder venv fails to converge intermittently
pillar: platform
---

# Deploy failure reporting

Bundled in the 2026-10-11 gripe triage. Both items make a red deploy hard to
read: the first overstates what failed, the second hides why.

## 1. A single prereq failure prints a whole-cluster failure banner (gr256065)

`scripts/deploy` prints "redeploy failed" and the mixed-versions banner when
one host fails a prerequisite step, even if every other host installed and
converged. The banner implies the cluster runs mixed versions when only that
host never started its install.

Fix direction: print the list of failed hosts with the failing play and task.
Distinguish a prereq failure (the host never reached the precis install, so it
still runs the old sha entirely) from a precis-install failure (the host may
be half-updated, which is the real mixed-versions case). Print the mixed
banner only for the second. Files: `scripts/deploy` (recap parsing and banner),
`deploy/redeploy-precis.yml`. Who: agent-code, S. Test with the fake-ansible
harness in `tests/test_deploy_render_worktree.py`.

## 2. Embedder venv "DID NOT CONVERGE" on the first pass (gr172390)

The `precis_embedder` role intermittently fails its convergence assert on
pass 1 of a deploy and passes on retry. The cause is unknown; a retry hides
it. Do not weaken or remove the assert.

Fix direction: the next time pass 1 fails, re-run the deploy with
`-e precis_embedder_trace=true` and keep the trace, then fix the root cause
it shows. Until a trace exists there is nothing to code. Files:
`deploy/roles/precis_embedder`.

Who: ops to capture the trace on the next occurrence, then agent-code.

## Acceptance

- A deploy where one host fails a prereq names that host and play, and does
  not print the mixed-versions banner.
- A first-pass embedder convergence failure is either gone or has a recorded
  trace and a root-cause fix, with the assert intact.
