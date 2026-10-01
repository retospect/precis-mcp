# serving programme

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

## Do next

1. **backlog/mcp-concurrency-load-test.md §Still owed** — py-spy at N=32
   names what holds the GIL; decides whether the multi-process arm is
   topology or workaround. One hour, and everything below reads its answer.
2. **backlog/serving-programme-followups.md item 5** (pin
   scripts/mcp_loadtest/ pure functions) — outside mypy scope and untested;
   verdict thresholds drift silently, and 1 and the multi-process arm both
   re-run this harness.
3. **backlog/eval-run-spine.md** — items 1-3 and 6 (run versioning, sequence
   numbers, verdict column) have no host dependency and unblock
   curation-gate and `backlog/model-qualification.md` (which writes its
   promote/reject into the verdict column).

## Horizon

Written from the 2026-09-29 resume pointer; the shared-server owners
correct it.

1. **backlog/serving-programme-followups.md item 6** (2-3 precis serve
   processes behind a balancer) — waits on the py-spy answer (Do next 1); a
   serve tier sized for ~24 sessions instead of the 28 calls/s ceiling.
2. **session-mcp-shared-server end state** (that thread's
   backlog/session-mcp-http-server.md) — waits on 1's topology answer; the
   end of the per-session container and the install-watchdog-exit-visibility
   class of defect that motivated it.
3. **backlog/eval-run-spine.md items 4, 8, 9** (blob store, contamination
   detection, frozen eval world) — waits on Do next 3 and on local-compute's
   spark prerequisites (the `/mnt/cluster` NFS hang and spark host prep,
   `serving-programme-followups.md` items 1-2); reproducible, comparable
   eval runs. `backlog/model-qualification.md` (unthreaded) is its first
   consumer.
4. **backlog/curation-gate.md** (+ its prerequisite
   backlog/review-container-readonly-role.md) — waits on the verdict column;
   the reviewer loop the roadmap and taxonomy threads park on.

## Parked

- **backlog/embedder-capacity-ownership.md** (local-compute thread) — a
  wait, not this thread's work: local-compute owns the item, but this
  thread's py-spy/topology answers (Do next 1) feed its capacity picture.
- **gr450103** — job_ssh_node worker leaks memory (~117 GB RSS over 2
  days on castor); infra, adjacent to this thread's fleet-serving scope
  but not owned by it. Unparks if it recurs on a serve host rather than a
  worker host.
- **gr451423** — quest relax-sim infra-failing repeatedly
  (struct_relax executor); cluster GPAW/executor reliability, not a
  serving-programme item. Left here for lack of a better home. (gr322060, the
  qu164903 relax-sim tracker, moved to the chemistry thread.)
- **gr453339** — caspar has no `/opt/precis/venv` and `/mnt/cluster` is
  still NFS-wedged post-reboot; deploy precondition, not this thread's to
  fix. Unparks when a deploy targets caspar specifically.
- **gr260050** — `claude -p` under an interactive ssh user on melchior
  cannot dispatch (OAuth state); blocks CLI-driven repair passes on that
  host. Adjacent to serving infra, not owned here.

## No action needed

- **backlog/session-mcp-http-server.md** — in flight under the
  session-mcp-shared-server thread; not ranked here.
- mcp-concurrency-load-test MEASURED section — the finding stands; no
  re-measurement until the multi-process arm exists.

## Seam

This thread keeps the MCP serve ceiling (py-spy, the multi-process
balancer), the load-test harness and the eval-run-spine. `local-compute`
owns local model serving and what it does (the summariser, the single-spark
model, vllm-per-node-serving Slice 0, local-serving-eval, the spark NFS and
host-prep prerequisites); the eval-run-spine's items 4 and 9 wait on those
two prerequisites.
