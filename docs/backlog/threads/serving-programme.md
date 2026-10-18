---
roster: active
pillar: operations
effort: medium
---
# serving programme

## Resume

- **Next:** Collect the N=32 profile before choosing process topology (Do next 1).
- **Blocked by:** Nothing; active under Pillar 5 Operations from 2026-10-17.
- **Unblocks:** Measured MCP capacity and the evaluation spine.
- **Acceptance:** Use [mcp-concurrency-load-test](../mcp-concurrency-load-test.md) and the verification steps in [Do next](#do-next); check current deployment and worktree state before acting.
- **Worktree:** `serving-programme`
- **Builds:** Not estimated here; use the owning item's current slice estimate.
- **Detail:** [Ranked work](#do-next) · [Horizon](#horizon) · [Coordination map](INDEX.md).

## Thread context

Current declared activity: the `roster:` front matter above; dated allocation decisions below are historical.

**Status:** ends when the fleet serves ~24 sessions from a measured MCP
ceiling (multi-process precis serve behind a balancer) with reproducible
eval runs and a curation gate on top. Today one `precis serve` is GIL-bound
at ~28 calls/s regardless of concurrency. Decide topology from the py-spy
answer first, then build the spine. Local model serving (spark, vLLM, the
summariser) moved to `local-compute.md` on 2026-10-01; the shared session MCP
server has its own thread (session-mcp-shared-server); this one owns the
serve tier and the eval spine.
**Last reviewed:** 2026-10-01 (local model serving moved to local-compute;
pillar review 2026-09-30 added the seam, embedder-capacity-ownership as a
wait, and five orphan gripes)
**Worktree:** `serving-programme`
**Allocation decision:** active — Reto 2026-10-17 (Pillar 5 Operations); dormant 2026-10-01 to 2026-10-17.

## Do next

1. **backlog/mcp-concurrency-load-test.md §Still owed** — py-spy at N=32
   names what holds the GIL; decides whether the multi-process arm is
   topology or workaround. One hour, and everything below reads its answer.
2. **backlog/eval-run-spine.md** — items 1-3 and 6 (run versioning, sequence
   numbers, verdict column) have no host dependency and unblock
   curation-gate and `backlog/model-qualification.md` (which writes its
   promote/reject into the verdict column).

## Horizon

Written from the 2026-09-29 resume pointer; the shared-server owners
correct it.

1. **session-mcp-shared-server end state** (that thread's
   backlog/session-mcp-http-server.md) — waits on Do next 1's topology answer; the
   end of the per-session container and the install-watchdog-exit-visibility
   class of defect that motivated it.
2. **backlog/eval-run-spine.md items 4, 8, 9** (blob store, contamination
   detection, frozen eval world) — waits on Do next 3 and on local-compute's
   spark prerequisites (the `/mnt/cluster` NFS hang and spark host prep,
   `spark-provisioning.md` §Host items); reproducible, comparable
   eval runs. `backlog/model-qualification.md` (unthreaded) is its first
   consumer.
3. **backlog/curation-gate.md** (+ its prerequisite
   backlog/review-container-readonly-role.md) — waits on the verdict column;
   the reviewer loop the roadmap and taxonomy threads park on.
4. **2-3 `precis serve` processes behind a balancer** (no item yet; the
   measurement is backlog/mcp-concurrency-load-test.md's multi-process arm,
   N=8-12 sessions per process) — waits on Do next 1's topology answer;
   spec it as its own item when that answer lands.

## Parked

- **backlog/embedder-capacity-ownership.md** (local-compute thread) — a
  wait, not this thread's work: local-compute owns the item, but this
  thread's py-spy/topology answers (Do next 1) feed its capacity picture.

## No action needed

- **backlog/session-mcp-http-server.md** — in flight under the
  session-mcp-shared-server thread; not ranked here.
- mcp-concurrency-load-test — multi-process arm measured 2026-10-15 on the
  dev DB (3 processes ~3.2x throughput); the `--url` prod run is Reto's.

## Seam

This thread keeps the MCP serve ceiling (py-spy, the multi-process
balancer), the load-test harness and the eval-run-spine. `local-compute`
owns local model serving and what it does (the summariser, the single-spark
model, vllm-per-node-serving Slice 0, local-serving-eval, the spark NFS and
host-prep prerequisites); the eval-run-spine's items 4 and 9 wait on those
two prerequisites.
