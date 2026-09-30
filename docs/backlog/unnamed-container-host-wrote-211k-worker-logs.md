---
status: draft
pillar: platform
---

# An unnamed container host wrote 211K worker_logs rows for ten days, invisible to every host check

Observed 2026-09-29 while auditing the fleet. `worker_logs` has **five**
distinct hosts, not four:

| host | rows | first | last |
|---|---|---|---|
| melchior | 1.6M | 2026-08-30 | 2026-09-29 (live) |
| castor | 763K | 2026-08-30 | 2026-09-29 (live) |
| balthazar | 534K | 2026-08-30 | 2026-09-29 (live) |
| pollux | 377K | 2026-08-30 | 2026-09-29 (live) |
| **`725f57f94fb1`** | **211K** | 2026-08-30 | **2026-09-09, then silent** |

`725f57f94fb1` is a 12-hex Docker container ID — the ephemeral-identity
case `heartbeat._resolve_host_ephemeral` describes: a worker booted in a
container with no `--hostname`/`PRECIS_HOST_NAME` advertises the
container's throwaway ID as its fleet identity.

**Why nothing said anything.** `_detect_host_dark` excludes it twice over
(gr306275, gr331348): by the `meta.ephemeral` stamp, and by the identity
*shape* — `AND hh.host !~ '^[0-9a-f]{12}$'`. Both exclusions are correct
and deliberate; a torn-down container must not page critical. The
consequence is that this worker ran for ten days, wrote a fifth of
melchior's log volume, and then stopped, with no surface reporting either
event.

## Answered 2026-09-30, from the surviving rows

Read before the prune (see the deadline below). All three open questions are
now answered except the identity of the operator.

**It was claiming and executing prod jobs — not merely logging.** 23.8K of its
rows are `runner` pass rows, and the claim lines are explicit: batches like
`summarize:rake-lemma claimed=32 ok=32 failed=0` alongside
`axis:open-question claimed=16 ok=0 failed=16` and
`nursery claimed=53 ok=0 failed=0`. So an unattributable container did not just
take prod work for nine days, it took work it then failed wholesale — 53
nursery jobs claimed and none completed. This is the part that makes the guard
question below concrete rather than hypothetical: the cost of letting an
ephemeral identity claim is not noise, it is claimed-and-dropped work with
nobody to ask about it. (Per-job attribution could not be cross-checked: there
is no `jobs` table — a job is a ref — so job-level ownership lives in ref meta
and was not examined.)

**It ran a full worker, not a test harness or CI step.** 24 distinct passes,
including `scheduler`, `sweeper`, `nursery`, `corpus_reconcile`,
`health_digest` and `heartbeat` — the system-worker set, the same shape a fleet
node runs. 28.9K INFO, 1.3K WARNING, 423 ERROR.

**It stopped cleanly, it did not break.** Final rows, 2026-09-09 07:31 UTC:
`signal 15 received; draining (batch ends, streams abort)` → `stop signal
received; exiting loop`. SIGTERM, graceful drain. Something or someone shut it
down deliberately. Uptime 2026-08-31 17:28 → 2026-09-09 07:31, 9.4 days.

**Provenance, such as it is.** Two WARNINGs in its last hour say the `claude`
binary was not found on an LLM failover attempt — a fleet host has it, so this
was very likely not one. Its first rows are `db_log_handler` warnings about its
own logging setup rather than any startup banner, so nothing records who
launched it. `payload` carries only pass bookkeeping (`claimed`, `ok`,
`failed`, `handler`, `error_class`, `error_msg`, `traceback`) — no image tag,
no compose project, no operator. Earlier in its last hour it logged two bursts
of pgbouncer connection failures and recovered, which places it outside the
cluster's own network path but is not an identification.

**Still unknown:** whose container it was. Nothing in `worker_logs` names the
launcher, and that is the gap the ask below is about — the answer was never
recorded anywhere, which is the point.

**The evidence is being deleted, and there is a date on it.** Re-measured
2026-09-30, one day after the table above: the row count is down from 211K to
**30.7K**, and the earliest surviving row has moved from 2026-08-30 to
2026-08-31. `sweeper._gc_worker_logs` prunes `worker_logs` at
`PRECIS_WORKER_LOG_RETENTION_DAYS` (default 30), so this host's last row —
2026-09-09 — is deleted around **2026-10-09**, after which there is no record
in `worker_logs` that it ever ran. It has no `host_heartbeat` row either
(confirmed: the table holds four live named hosts and nothing else), so the
prune is the only copy. Any forensic question below — was it claiming jobs, did
it stop or break — has to be asked before that date or not at all. This is the
same mechanism that used to blind the `host-dark` detector — it bounded
itself to hosts with a `worker_logs` row inside the same 30-day window the
sweeper prunes, so it expired with its own evidence (fixed 2026-09-30 by an
explicit `meta.retired` marker): the retention horizon removes the evidence a
question depends on. If the answers are wanted, dump the
surviving rows somewhere durable first; that is cheap and can happen before any
design decision.

**Ask.** Not "alert on container hosts" — the exclusion is right. Two
things, and after the findings above the second is the more important:

1. A container-identity worker that writes to prod `worker_logs` should be
   *attributable*: one journalled event when a non-fleet identity starts
   writing, carrying whatever provenance exists (image, compose project,
   boot id, the launching command) so "whose container was it" is answerable
   without forensics and without racing the pruner.
2. **A guard on whether an ephemeral identity may claim jobs at all.** This
   one now has evidence: the container claimed 53 nursery jobs and failed all
   53, plus 16 axis jobs likewise. Work claimed by an identity that cannot be
   contacted, cannot be alerted on, and vanishes from the logs 30 days later
   is worse than work left unclaimed. A refusal is cheap; the current
   behaviour is not.

**Owner anchors:** `src/precis/workers/nursery.py::_detect_host_dark`
(the `!~ '^[0-9a-f]{12}$'` belt and the `meta.ephemeral` stamp),
`src/precis/workers/heartbeat.py::_resolve_host_ephemeral`,
`_CONTAINER_ID_RE`. Prior art: gr306275, gr331348.

test: a worker booting with a container identity is journalled once with
its provenance, and (if decided) refused a job claim.
