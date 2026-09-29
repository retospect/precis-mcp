---
status: draft
title: Eval-run spine — make agent runs joinable, replayable and non-perishable
prio: high
---

# Eval-run spine

Design session 2026-09-29 (Reto + agent, big-model-manage worktree). The
prompting context is the move to a locally-served open-weights model driving
many concurrent agent sessions over the precis MCP; this item is the
instrumentation half, and it ships **before** anything is served.

## Motivation / why

Three logs already record most of what an eval loop needs, and none of them
were designed to be joined into "did this run go well, and why":

* `agentlog` (migration 0034, `src/precis/agentlog.py`) — one ref per agentic
  pass: the assembled prompt, model, source, parent/job ref, a `touched` link
  to every chunk the run wrote. The run id already threads to subprocesses
  via `PRECIS_CURRENT_AGENTLOG`.
* `tool_calls` (migration 0133, `src/precis/tool_ledger.py`) — one row per
  `runtime.dispatch()` call, written at the
  `DispatchMixin.dispatch_with_status` chokepoint so it covers MCP, CLI and
  in-process ticks alike. Carries `agentlog_id`, so the join to the run
  already exists.
* `llm_call_log` (0061, extended by 0112 `placement` and 0122 token counts) —
  per-dispatch model/tier/transport/cost/latency plus content-addressed
  request and response blobs in `llm_blob`.

Six things are missing, and each one independently breaks an eval loop.

**No run versioning.** `agentlog` stores the assembled prompt but not what
produced it, so there is nothing to `GROUP BY` when the question is "did the
new skill wording help".

**No loop detection.** `tool_calls.input_keys` is argument *names* only. A run
that called the same tool with the same arguments eleven times is
indistinguishable from one that made eleven different calls.

**No payload, by deliberate design.** 0133 states "No payload content, ever"
and calls that the corpus-safety boundary: agent-supplied strings can carry
credentials, or injection text another agent later reads back as
instructions. Eval work wants arguments and results. The boundary is right
for the prod path and must survive.

**Retention deletes the evidence.** `agentlog` and `tool_calls` both GC at 30
days (`PRECIS_AGENTLOG_RETENTION_DAYS`, `PRECIS_TOOL_CALLS_RETENTION_DAYS`).
A regression suite or future training set built on them evaporates before
anyone notices it was valuable.

**No outcome.** A run that halted, a run that finished having achieved
nothing, and a run that finished well are the same row.

**The environment moves under the benchmark.** A tool call that enriches the
data graph means the next run of the same task faces an easier world — so a
re-run is not a replication. See the decisions log; the resolution is to
*detect* contamination rather than prevent it.

## In scope

1. **Run versioning** on `agentlog` meta: model id, prompt version, tool-schema
   version, skill-corpus content hash, precis sha, graph version.
2. **`interesting` flag** — a tag on the agentlog ref, not a column.
3. **`tool_calls`: sequence number within the run, and an args hash.** The
   hash is sha256 over `(verb, kind, canonical-JSON of the full input
   mapping)`. Values are still never written to this table — a hash is not a
   payload, so 0133's boundary holds unchanged and the prod path keeps its
   current behaviour byte-for-byte.
4. **Opt-in payload capture.** A per-run flag; when set, arguments and results
   are written as content-addressed blobs on the NFS share, id = sha256,
   referenced by id from the row. Redaction reuses the path
   `session-history-into-precis.md` specs rather than re-deriving it. A row
   whose blob has been swept reads as *expired*, never as an error.
5. **Retention pin** — an eval-flagged run and its `tool_calls` rows are
   exempt from the sweeper.
6. **Run outcome — two columns, not one**: a mechanical terminal state and a
   separate nullable verdict (see the decisions log), plus the re-prefill
   token count (see `vllm-per-node-serving.md` — without it, "this run was
   slow" and "this run was evicted four times" are the same row).
7. **Failure-cause tag vocabulary**: `cause:missing-tool` /
   `cause:bad-hint` / `cause:reasoning-gap`. The failure corpus is
   eval-flagged runs carrying one of these, not a new store.
8. **Contamination detection**: novel-work fraction per run, computed from the
   args hash — what share of a run's tool calls did work versus retrieved
   something a prior run deposited.
9. **A frozen eval world**: a restored copy of prod on a non-serving host,
   re-derived from a dump plus migrations and versioned, reset from its
   template between batches. Runs write to it freely — that is the point —
   and the reset, not read-only-ness, is what makes the next batch pristine.
   See the decisions log for why it is a restore rather than an as-of query,
   and why the reset is per batch rather than per run.

## Explicitly NOT in scope

* Any dashboard or UI. The schema is meant to be queried by an LLM.
* The judge itself — that is `llm-judge-reliability.md`, which already has a
  frozen instrument and a gold set.
* Fine-tuning or trace distillation. This item makes the corpus collectable;
  what is done with it is later and separate.
* `conv` refs for human Claude Code sessions — that stays
  `session-history-into-precis.md`'s deliverable (see decisions log).
* Relaxing `tool_calls`' no-payload rule on the prod path.
* Subset extraction from prod. The frozen world starts as a full restored
  copy; carving a task-scoped subgraph out of it is its own deliverable and
  is only justified once clone-per-run is (decisions log).

## Acceptance criteria

* One SQL statement joins run → `tool_calls` → `llm_call_log` for a single
  run and returns the ordered tool sequence with latencies and the LLM cost.
* A duplicate-args query over one run surfaces a deliberately-induced loop.
* An eval-flagged run and its tool rows survive a sweeper pass run with the
  retention window set to zero.
* Novel-work fraction is computable for a run without reading any payload.
* With capture off, `tool_calls` rows are identical to what ships today —
  asserted by a test, since this is the corpus-safety boundary.
* A captured run's blobs round-trip, and a run whose blob was deleted renders
  as expired rather than raising.
* The frozen world resets from its template and a re-run of the same task
  against the reset world issues the same novel-work fraction it did the
  first time — the check that the reset actually restored the world, and the
  same query that detects contamination when it does not.
* The eval world's version is recorded on every run made against it, so a
  scored result can be traced to the world it was scored in.

## Target + blast radius

`src/precis/agentlog.py` · `src/precis/tool_ledger.py` ·
`src/precis/runtime/dispatch.py::DispatchMixin.dispatch_with_status` · new
migration · the sweeper's GC passes. No handler, verb or route changes, so
the agent-facing MCP surface is untouched.

## Open questions / decisions log

**Decided 2026-09-29 (Reto):**

* **Store**: `agentlog` plus a content-addressed blob on the file server, with
  an id — not `conv` refs. The split is by origin: `conv` for human sessions,
  `agentlog` for machine runs. This also answers
  `session-history-into-precis.md`'s own open question about
  cross-subsumption with `agentlog`; fold the answer back there when either
  ships.
* **Hashes always, values only behind the eval flag.**
* **Contamination is detected, not prevented — in the live arm.** A task
  whose novel-work fraction decays toward zero across runs has been consumed
  by its own history and gets retired from the suite. This is what makes a
  moving graph survivable without time travel. In the frozen arm a restore
  makes contamination impossible, so the detector is the live arm's
  instrument and the frozen arm's tripwire, not a universal requirement.
* **Two benchmarks, two environments.** Model/prompt capability ("is this
  model better") needs a frozen graph — the graph is a version alongside
  model and prompt. System capability ("can the whole thing answer this
  today") wants the live graph and *should* get easier over time; the
  improvement just can't be attributed to the model.

* **The frozen world is a restored database, not an as-of query.** "Pretend
  it is 2026-08-20" is only partly available: corpus body chunks are
  append-only so `created_at <= T` works for them, but drafts edit in place
  by design, `refs.meta` is mutated, soft-delete uses `retired_at`, and
  `ord < 0` card variants are DELETE+INSERT. An as-of view would be right for
  some kinds and quietly wrong for others — wrong in a way that looks like a
  result. A restored copy is exact. **Freeze content, track schema**: the
  base is re-derived from a dump plus migrations whenever a migration touches
  something the eval reads, and each derivation is versioned so a run records
  which world it saw. A binary preserved forever stops running against the
  code under test.
* **Reset between batches first; clone per run only if contamination proves
  real within a batch.** Prod is 74 GB (453k refs, 4.05M chunks, 3.73M
  embeddings, measured 2026-09-29), so `CREATE DATABASE … TEMPLATE` is a
  ~74 GB file copy — tolerable once per batch, not once per run, and 24
  concurrent clones would want ~1.8 TB. So: one frozen eval database,
  restored from the template between batches; within a batch, the
  novel-work fraction (item 8) says whether runs are contaminating each
  other. The cheap mechanism is the gate on whether the expensive one is
  needed.
* **Placement: `spark`, the retired third box** (revised 2026-09-29, was
  pollux). Not a serving box: castor's 128 GB is unified memory shared
  between model weights and KV cache, so a Postgres competing for it costs
  decode bandwidth on a box that is bandwidth-bound. pollux was the first
  answer but is earmarked as the second replica; spark is earmarked for
  nothing, runs zero precis units since its 2026-08-29 retirement, and is the
  same hardware. It hosts the eval database and slice 0's benchmarking
  together. This is a bench role, not cluster duty — no service group, no
  capability list, inventory guard untouched. Network RTT is not the
  constraint (`llm-tier-ladder-cloud-cutover.md` Finding 3 measured 1.5 ms
  and eliminated it).

* **Outcome is two columns, decided 2026-09-29 (Reto).** `outcome` is
  mechanical and knowable without judgment: `completed`, `completed_empty`,
  `halted_step_cap`, `halted_wall_clock`, `error`, `abandoned`. `verdict` is
  a separate nullable column, filled later by the judge or by hand.
  The reason for the split: "finished well" fuses *did it terminate cleanly*
  with *was the work any good*, and only the first is derivable from the run
  itself. Fusing them writes judge error into a terminal state and makes the
  cheap question — how many runs died on the step cap last night — unanswerable
  without trusting the judge. `completed_empty` is the case the motivation
  section names: finished, no error, produced nothing.
* **Blob GC: 30 days unreferenced, eval-flagged pinned. Decided 2026-09-29
  (Reto).** Matches `agentlog`'s existing window rather than inventing a
  second number, and the retention pin (item 5) already exempts eval-flagged
  runs, so the policy is one rule plus the exemption that is being built
  anyway. Sweep on *unreferenced*, not on age alone — blobs are
  content-addressed, so the shared system+tools+skills prefix collapses to a
  single copy across every run that used it and the stored volume is far
  below the naive sum. Env var, because retention is expected to move.

**Open:**

* **Spark has 2.8 TB free (measured 2026-09-29), which loosens the reset
  calculus.** The 74 GB restore plus 24 template clones is ~1.8 TB and fits,
  so clone-per-run is affordable there in a way it would not have been on a
  serving box. Reset-per-batch stays the default because it is simpler and
  the novel-work fraction says whether more is needed — but the decision is
  now a preference, not a disk constraint.
* **Check the Postgres major version before planning the restore.** Spark
  already runs Postgres 16. A dump from a newer major will not restore into
  it, and the frozen world is defined as a restore. Confirm what prod is on
  and either match it on the bench or plan a dump/restore path that crosses
  the version — and confirm `pgvector` is installed there at all, since
  nothing in the eval works without it.

* **Subset extraction — needed only if clone-per-run is.** Reducing 74 GB to
  a few GB makes template cloning seconds and 24 clones tens of GB. The hard
  part is that a random sample breaks the graph: citations, claim hubs and
  draft anchors point at refs that are no longer there. It wants a closure
  over the eval tasks' seed refs, plus the operational rows (skills, `llm`
  cards, `service_config`, `resource_slots`) taken wholesale since they are
  tiny. Do not build this until the batch-reset arm says it is necessary.
* **Resume or restart a run that fails halfway.** No longer blocked — the
  vocabulary below gives it the state to resume *from*. `halted_step_cap` and
  `halted_wall_clock` are the resumable ones; `error` and `completed_empty`
  are not obviously either.
* **Wall-clock cap per run.** A step cap exists
  (`utils/claude_agent.py::call_claude_agent`, `max_turns=20`), plus
  `utils/load_gate.py` and `workers/auto_check.py`'s `timeout_at`. There is no
  per-run wall-clock kill switch.
* Whether the failure-cause tag is agent-assigned, operator-assigned, or
  judge-assigned. Judge-assigned inherits the judge's error bars.
