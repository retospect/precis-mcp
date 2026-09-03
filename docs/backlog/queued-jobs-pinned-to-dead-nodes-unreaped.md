---
status: draft
title: Generalize dead-node reaping to any queued pinned job (executor-agnostic)
prio: high
---

# Generalize dead-node reaping to any queued pinned job (executor-agnostic)

gr292747 shipped a quest-loop-specific reap arm
(`quest/loop.py::_reap_dead_node_pinned_loop`) for the wedge class it hit:
a job minted with `meta.params.target_node` pinned to a node that dies
before the job is ever claimed has `lease_until IS NULL` and stays
`STATUS:queued`, so it matches no existing recovery path — the sweeper's
`_enumerate_dead_node_orphans` requires `executor='ssh_node'` +
`STATUS:running`, and the quest reboot-orphan reaper requires a non-null
expired lease. But the class is wider than quest loops:

- `ssh_node` jobs are minted with `target_node` from `PRECIS_DFT_NODE`
  (`handlers/structure.py` `dispatch` mint, `workers/job_types/struct_relax.py`
  module constant) — the same "hardcoded/env node that turned out not to be
  permanent" shape; all 345 ssh_node jobs in the fortnight before spark's
  2026-08-29 retirement were spark-pinned. A queued ssh_node job pinned to a
  dead node today wedges silently, and its quest waits on it forever.
- Any future pinned executor repeats it.

Proposed: move the queued-never-leased-pinned-to-provably-dead-node
predicate (worker_logs silence + no fresh host_heartbeat, mirrored from
`_enumerate_dead_node_orphans`) into `workers/sweeper.py` as an
executor-agnostic arm. Decide the terminal status per executor: quest loops
want `cancelled` + same-pass re-mint (already handled by the loop arm — keep
or fold); ssh_node compute jobs probably want `failed` +
`failure_class='infra'` + `bubble_job_failure` so the quest harvest's
retry-vs-rule-out branch reads it as an infra death (mirror
`_transition_dead_node_orphan_to_failed`).

Mint-side hardening SHIPPED (2026-09-03): the `"spark"` literals are gone —
`handlers/structure.py` refuses the mint loudly when `PRECIS_DFT_NODE` is
unset, `struct_relax.py`'s helpers no-op / record an infra failure, and
deploy renders `PRECIS_DFT_NODE` from `precis_capabilities.dft` into the
web plist, worker templates, and 20b's collapsed-worker env union. What
remains here is only the executor-agnostic sweeper arm above.
