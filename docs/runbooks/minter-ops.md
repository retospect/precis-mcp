# Minter ops — dispatch internals, rejection log lines, CLI

When you need to run the minter (`dispatch`) worker pass by hand, read its
rejection log lines, or understand the executor/job_type capability check
behind `precis-minter-help`.

## What the minter does, step-by-step

1. **Candidate scan.** SQL: every open todo with `meta.executor`,
   no existing live `kind='job'` child, status in `open|doing`,
   not under a paused / recurring ancestor.
2. **Per-candidate claim.** `SELECT … FROM refs WHERE ref_id = …
   FOR UPDATE OF r SKIP LOCKED` so two minter workers (different
   hosts) serialise on the row.
3. **Validate.** Executor must be known (`is_known_executor`);
   job_type must exist; `job_type.compatible_executors` must
   include the chosen executor; `job_type.requires ⊆
   executor.provides`. Bad combinations are logged and skipped —
   the todo stays open, no zombie queued job lands.
4. **Auto-inject auto_check.** If `meta.auto_check` is absent,
   write `{'type': 'child_job_succeeded'}` into the parent's meta.
5. **Mint the child job.** `parent_id` = the todo; `meta` carries
   `job_type`, `executor`, `params`, `dispatched_from_todo`;
   `STATUS:queued` open tag.
6. **Append `ref_events`** on the parent: `source='minter',
   event='job-minted', payload={'job_id': N, ...}`.

Once the job is queued, the `job_claude_inproc` worker (also in
the default rotation) picks it up by `STATUS:queued`, runs the
executor, and flips status to succeeded / failed.

## What gets rejected at mint time?

The minter logs the rejection and moves on; the todo stays
open. The operator notices via worker logs, which carry structured
entries with the rejection reason.

| Cause | Log line |
|---|---|
| Unknown `meta.executor` value | `dispatch: parent #N has unknown meta.executor=...` |
| Missing `meta.job_type` | `dispatch: parent #N has missing meta.job_type` |
| Unknown `meta.job_type` | `dispatch: parent #N has unknown meta.job_type=...` |
| Executor / job_type mismatch | `dispatch: parent #N job_type=X incompatible with executor=Y` |
| Required capability missing | `dispatch: parent #N executor=X missing caps for Y: {...}` |

(Log lines still carry the `dispatch:` prefix — `workers/dispatch.py`
is the still-named module; `registry.py`'s `log_name="dispatch"`
keeps `worker_logs` attribution matched to it.)

## The executor / job_type registry

## Host capabilities (what an executor advertises)

Executors live in `src/precis/workers/executors/__init__.py`
(`EXECUTOR_PROVIDES`); job_types live in
`src/precis/workers/job_types/__init__.py` (the `_REGISTRY` +
lazy loaders). Each executor advertises the capabilities it
provides (e.g. `clones_dir`); a job_type's requirements must be a
subset at submit.

Executors today: `claude_inproc` (offline `claude -p`, provides
`{claude_bin, git, clones_dir, claude_config_mount, mcp_config}`),
`ssh_node` (remote GPU-node compute, provides `{has_gpaw}`),
`claude_docker` (sandboxed detached container run, provides
`{podman, claude_oauth}` — only satisfiable on
`PRECIS_SANDBOX_ENABLED=1` hosts), `job_inproc` (in-process bounded
compute with slot reservation, empty PROVIDES — gated by
`resource_slots`, not this capability check), and `coordinator`
(yield/resume phase machines — provides `{claude_bin}`, needed by
`quest_tick`'s inline LLM slice; most coordinator job_types declare
`REQUIRES=frozenset()` since the real work happens in spawned
children — the job_type's `dispatch` does the work in slices and
parks at `STATUS:waiting_*` between them).
Job_types pair with a compatible executor at submit; see the table
in `precis-job-help`.

## Running the minter

```sh
precis worker --only minter                # drain alone (debug / backfill)
precis worker                              # default cycle includes minter
precis worker --profile system             # explicit profile (default)
```

The pass is SQL-only and cheap — multi-host safe via `FOR UPDATE
OF r SKIP LOCKED` per candidate parent.
