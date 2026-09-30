# serving programme

**Status:** ends when the fleet serves ~24 sessions and big models from a
measured ceiling (multi-process precis serve behind a balancer, per-node
model choice on spark) with reproducible eval runs and a curation gate on
top. Today one `precis serve` is GIL-bound at ~28 calls/s regardless of
concurrency. Unblock the spark share first, then decide topology, then
build the spine. The shared session MCP server has its own thread
(session-mcp-shared-server); this one owns the fleet side.
**Last reviewed:** 2026-09-30 (pillar review same day added a seam with
local-compute, embedder-capacity-ownership as a wait, and five orphan
gripes)
**Worktree:** `serving-programme`

## Do next

1. **backlog/serving-programme-followups.md item 1** (spark /mnt/cluster NFS
   hang, re-confirmed 2026-09-30) — gates eval-run-spine items 4 and 9;
   anything built on that share hangs at statvfs for 120 s.
2. **backlog/serving-programme-followups.md item 2** (spark host prep;
   PG 16.15 + pgvector 0.5.1 verified 2026-09-30, prod's major still to
   match) — gates the frozen eval world as a restore.
3. **backlog/mcp-concurrency-load-test.md §Still owed** — py-spy at N=32
   names what holds the GIL; decides whether the multi-process arm is
   topology or workaround. One hour, and everything below reads its answer.
4. **backlog/vllm-per-node-serving.md Slice 0** — Nemotron NVFP4 vs gpt-oss
   control on spark; go/no-go for the whole oversubscription design (a
   Mamba-hybrid ceiling at c=8 kills it). Needs 2.
5. **backlog/serving-programme-followups.md item 5** (pin
   scripts/mcp_loadtest/ pure functions) — outside mypy scope and untested;
   verdict thresholds drift silently, and 3 and the multi-process arm both
   re-run this harness.
6. **backlog/eval-run-spine.md** — items 1-3 and 6 (run versioning, sequence
   numbers, verdict column) have no host dependency and unblock
   curation-gate; items 4 and 9 wait on 1-2.

## Horizon

Written from the 2026-09-29 resume pointer; the shared-server owners
correct it.

1. **backlog/serving-programme-followups.md item 6** (2-3 precis serve
   processes behind a balancer) — waits on the py-spy answer (Do next 3); a
   serve tier sized for ~24 sessions instead of the 28 calls/s ceiling.
2. **session-mcp-shared-server end state** (that thread's
   backlog/session-mcp-http-server.md) — waits on 1's topology answer; the
   end of the per-session container and the install-watchdog-exit-visibility
   class of defect that motivated it.
3. **backlog/vllm-per-node-serving.md beyond Slice 0** — waits on the Slice
   0 plateau (Do next 4); big-model serving on spark.
4. **backlog/eval-run-spine.md items 4, 8, 9** (blob store, contamination
   detection, frozen eval world) — waits on Do next 1-2 and 6; reproducible,
   comparable eval runs.
5. **backlog/local-serving-eval.md**, **backlog/llamacpp-fleet-ops.md** —
   wait on 3; per-node model choice against a measured ceiling.
6. **backlog/curation-gate.md** (+ its prerequisite
   backlog/review-container-readonly-role.md) — waits on the verdict column;
   the reviewer loop the roadmap and taxonomy threads park on.

## Parked

- **backlog/embedder-capacity-ownership.md** (local-compute thread) — a
  wait, not this thread's work: local-compute owns the item, but this
  thread's py-spy/topology answers (Do next 3) feed its capacity picture.
- **gr450103** — job_ssh_node worker leaks memory (~117 GB RSS over 2
  days on castor); infra, adjacent to this thread's fleet-serving scope
  but not owned by it. Unparks if it recurs on a serve host rather than a
  worker host.
- **gr451423**, **gr322060** — quest relax-sim infra-failing repeatedly
  (struct_relax executor); cluster GPAW/executor reliability, not a
  serving-programme item. Left here for lack of a better home.
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

`local-compute` owns what the served capacity *does* (summarise, insert,
mesh, link, categorise); this thread owns the MCP ceiling, vLLM Slice 0
go/no-go and the eval-run-spine that measures what got served.
