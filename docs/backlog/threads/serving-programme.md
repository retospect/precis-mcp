# serving programme

**Status:** ends when the fleet serves ~24 sessions and big models from a
measured ceiling (multi-process precis serve behind a balancer, per-node
model choice on spark) with reproducible eval runs and a curation gate on
top. Today one `precis serve` is GIL-bound at ~28 calls/s regardless of
concurrency. Unblock the spark share first, then decide topology, then
build the spine. The shared session MCP server has its own thread
(session-mcp-shared-server); this one owns the fleet side.
**Last reviewed:** 2026-09-30
**Worktree:** `serving-programme`

## Do next

1. **backlog/serving-programme-followups.md item 1** (spark /mnt/cluster NFS
   hang) — gates eval-run-spine items 4 and 9; anything built on that share
   hangs at statvfs for 120 s.
2. **backlog/serving-programme-followups.md item 2** (spark host prep +
   verify Postgres major/pgvector) — gates the frozen eval world as a
   restore; an unverified PG 16 assumption would sink the restore plan late.
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
   end of the per-session container and the gr341515 class.
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

- (none beyond Horizon's stated waits)

## No action needed

- **backlog/session-mcp-http-server.md** — in flight under the
  session-mcp-shared-server thread; not ranked here.
- mcp-concurrency-load-test MEASURED section — the finding stands; no
  re-measurement until the multi-process arm exists.
