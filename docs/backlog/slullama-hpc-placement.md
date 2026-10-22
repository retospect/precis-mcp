---
status: draft
pillar: local-compute
---

# External HPC — slullama LLM rung, batch runner, Apptainer image

- **Status**: batch runner (submit, recover, collect) and Apptainer image
  specced 2026-10-21, not started; the `precis.remote` core is on main with
  no caller. slullama leg 1 (static card) SHIPPED (dark); leg 2 (chain rung)
  OPEN, blocked on external cluster access.
- **Owner:** thread `local-compute` Do next 2b. This file is the one spec for
  external HPC; the former precis-dispatch item's "second backend" trigger
  is folded in below (§From precis-dispatch).
- **Refs**: memory `slullama_cluster_llm`; ADR 0066 (git-only ADR,
  capability tiers + placement chains); `precis.utils.llm` package
  docstring §4 ("Chain"); `precis.llm_catalog.seed_slullama_card`.

## Why

precis reaches an external Slurm HPC GPU (Meluxina, via the ICHEC interim
service — https://ichec.github.io/interim-service/interim-service.html) as
a client, over a thin SSH tunnel maintained by `slullama` (separate repo,
`~/work/projects/code/slullama`): a head-node daemon that `sbatch`-wakes a
GPU node, serves Ollama's OpenAI-compat `/v1`, reverse-proxies it to the
tunnel, and idle-tears-down (`idle_timeout≈30min`, `keep_alive=extend` on
the cluster side). Since ADR 0066 retired location-coupled tiers, this is
**not** a new tier — it's (1) a static `llm` card advertising the tunnel,
and (2) a placement-chain rung pointing an existing capability tier's local
rung at it, mirroring the DGX-pair integration
(`precis.utils.llm.local_serving` module docstring).

## Built (leg 1 — static card)

- `src/precis/llm_catalog.py::seed_slullama_card` mints the card —
  `served_by=[{host, endpoint, model, max_parallel, source:"static"}]`,
  every field falls back to a `PRECIS_SLULLAMA_*` env var
  (`docs/reference/config-variables.md` §4). CLI: `precis llm seed
  --slullama`.
- `source="static"` (`llm_catalog.SERVED_BY_SOURCES`) shields the entry
  from `workers/llm_serving.py::advertise_local_llm`'s per-heartbeat
  auto-discovery prune — the tunnel is never polled, so idle-teardown isn't
  defeated.
- `utils/llm/local_serving.py::acquire` reserves the slot (`max_parallel`
  is the nice-citizen concurrency cap) and repoints dispatch at the tunnel
  endpoint + server-side model tag.
- `deploy/roles/ssh_tunnels/` generalized for an external endpoint
  (`ssh_host`/`ssh_user`/`ssh_key`/`ssh_port` override; a human provisions
  `authorized_keys` out-of-band, Ansible only pre-seeds `known_hosts`).
- Tests: `tests/test_local_serving.py::test_seeded_slullama_card_reserves_and_caps`,
  `tests/test_llm_serving.py` (prune guard + rebuild guard both leave the
  static entry alone).
- **Footgun to respect:** `seed_slullama_card` replaces the *entire*
  `served_by` list for its `model_id` on each refresh (key-level merge). Safe
  only while `qwen-hpc`'s `model_id` never collides with a real
  llama-swap-discovered tag — keep it distinct. (A collision would survive one
  heartbeat via the source-guard but a re-`seed` would drop the auto entry.)

## Open (leg 2 — placement-chain rung)

Add a LOCAL rung to an existing tier's chain via the operator
`app_settings` override (`live_config.chain_override`, read by
`router.resolve_chain`):

```
llm.chain.big = [{"placement":"local","transport":"openai_tools","model":"qwen-hpc"},
                  {"placement":"cloud","transport":"openai_tools","model":"<fallback>"}]
```

1. **Tier: BIG vs FRONTIER — undecided.** BIG has precedent (the DGX-pair
   rung already lives there); FRONTIER would suit an opus-class OSS model
   too big for the Spark pair. Reto's call.
2. **Unserved-host behavior — verified against current code, corrects a
   prior wrong assumption.** `router._skip_unserved_local_rung` only prunes
   a `Transport.LOCAL` rung (the dead `:4000` litellm-proxy case) — it does
   **not** fire for `Transport.OPENAI_TOOLS` rungs. On a host that isn't
   `melchior`, `local_serving.acquire("qwen-hpc")` finds no slot (the
   `served_by` endpoint is loopback-only, not LAN-routable, so it's
   host-private — see `local_serving.py`'s cluster-scoped-serving
   section) → `FailoverProvider` still *attempts* rung 0 against whatever
   the hosted-OSS default endpoint is, fails (unrecognized model), logs a
   WARN, and falls to the cloud rung. End state matches "falls to cloud, no
   `sbatch` traffic" (the tunnel itself is never touched), but it's a
   failed-attempt-then-fallback on every non-`melchior` BIG dispatch, not a
   clean skip — worth knowing before wiring this live on a multi-host
   fleet; may want a loopback-aware extension of
   `_skip_unserved_local_rung` to cover `OPENAI_TOOLS` too.
3. **Cluster access.** Meluxina login node on a non-standard SSH port
   (host, port and username live only in the gitignored overlay
   `deploy/inventory/hosts.yml` — never in a tracked file, test or commit
   message; this repo is public); the Meluxina username is distinct from the
   ICHEC account; SSH pubkey registered through the provider's helpdesk
   (out-of-band — matches the `ssh_tunnels` external-endpoint provisioning
   path, which already expects a human-provisioned `authorized_keys`). GPU
   partition (`gpu`) + a `--qos` to pick. Modules only load on compute
   nodes, which is fine — slullama's head-node proxy is pure Python +
   Slurm CLIs, Ollama runs inside the job. Open risk, unconfirmed: EuroHPC
   login nodes may reap long-running daemons — needs empirical
   confirmation once access lands.

## Acceptance

- `llm.chain.big` (or `.frontier`) rung resolves to a live tunnel response
  from a real Meluxina allocation.
- A non-serving host's dispatch falls to the cloud rung without ever
  reaching the tunnel or triggering `sbatch`.
- `docs/reference/config-variables.md` §4 stays accurate once real values
  are set.

## MeluXina batch LLM plan and autocatpath pilot (draft, 2026-10-04)

Reto requests an LLM plan plus a small remote-controlled Slurm test from
autocatpath running an ML potential. Existing experiment/rerun holds remain;
the pilot uses an independent fixture, not a held scientific campaign.
This batch plan does not enable the earlier login-node proxy proposal:
LuxProvide's current usage policy disallows long-running login-node processes.

Owners: MeluXina for SSH/Slurm lifecycle; chemistry/catpath for the ML-potential
worker and wheel; local-compute for the later LLM qualification workload.
Actual local checkout is `/Users/reto/catpath` (clean main when checked);
implementation belongs in an isolated worktree, not that checkout's main.

### First pilot: ML potential through autocatpath

1. Precis resolves SSH credentials through its existing vault. First verify
   supported secret names, multiline private-key entry, passphrase handling and
   pinned host identity. No credentials in job manifests, logs or shared notes.
   The vault is readable by every DSN holder today; decision D1 (under Open
   for Reto) decides where the key lives instead.
2. Check authentication and run `myquota` once to discover project, storage and
   granted resources; inspect permitted partition/QoS. Do not infer entitlement
   from cluster visibility or username. No repeated quota polling.
3. Stage an immutable local autocatpath wheel, compatible dependencies and a
   tiny deterministic ML-potential fixture. Record source SHA, wheel checksum,
   model/weights identity and task manifest; do not substitute latest main.
4. Submit one bounded Slurm allocation via the catpath backend. Install/test on
   appropriate compute resources, use `srun` for execution, declare account,
   partition/QoS, CPUs/GPUs/memory and short walltime. Choose a fixture and
   tolerances before submission; no DFT or LLM job is required for this pilot.
5. Persist task/job IDs, disconnect, resume monitoring and retrieve energies,
   forces and execution metadata (or a bounded tiny relaxation if that is the
   existing supported worker contract). Verify shapes, units, finite values,
   successful exit and correspondence to the staged input.
6. Collect logs and result checksums before reviewed scratch cleanup. Record
   failed/timed-out/missing outcomes. Reconnection must not duplicate a job;
   uncertain submission acknowledgement must reconcile before any resubmit.

Gate: one real ML-potential result through catpath's SSH/Slurm path, captured
by Precis with provenance. A mocked scheduler test or SSH login alone is not
this result. Report actual job ID, resources, runtime and allocation usage.

### Later LLM pilot

Use the same staging/submission/collection contracts for batch inference,
starting with a small pinned evaluation pack from the existing graph workload.
Select a model only after checking allocation, memory, software compatibility
and licence; stage pinned weights and dependencies without fetching models on
every job. Compare completion coverage, task quality, walltime/node-hours and
queue/startup cost against the existing reference. No interactive serving rung,
always-on allocation, training campaign or production routing change yet.

### Rate controls and provider references

Separate submission pacing, pending/in-flight cap, array concurrency, status
poll backoff and node-hour/storage budget; Slurm account/QoS limits remain
authoritative. Batch homogeneous tasks with a measured array concurrency cap;
share controller limits across workers rather than letting each poll separately.
`myquota` is limited to once per 10 seconds and must not be repeatedly watched.

- Connecting: https://docs.lxp.lu/first-steps/connecting/
- Usage policy: https://docs.lxp.lu/access/PoliciesSummary/
- Jobs/arrays: https://docs.lxp.lu/first-steps/handling_jobs/
- Allocations: https://docs.lxp.lu/access/allocation_monitoring/#myquota

Open prerequisites: usable vault-backed SSH consumer, actual project/resource
entitlement, chosen compatible wheel/model and pilot fixture. Coordinator
assigns implementation/validation and live pilot sequencing; no remote run has
been performed by writing this draft. The consumer is specced in § Batch
runner below; the image in § Apptainer image.

## Batch runner: submit, recover, collect (spec, 2026-10-21)

Wires `precis.remote` into the worker as a detached job_type, so a precis job
can run on the external HPC and come back as the same records a local run
writes. No live remote run happens until slice B4, under the pilot gate above.

### What exists, and what it lacks

`precis.remote.slurm.SlurmRunner` (on main, tests `tests/test_remote_slurm.py`)
has six operations over an injected transport: `stage`, `submit`, `recover`,
`status`, `cancel`, `collect`. `precis.remote.credentials.vault_ssh_session`
gives that transport from vault refs with pinned host keys and an identity
that lives at most 60 s. Nothing calls either.

Properties to keep:

- **Durable intent before acceptance.** `submit` fsyncs a journal entry under
  a caller token before `sbatch`. The remote run directory holds a
  `submitted.once` guard and a `receipt`. Re-running `submit` with the same
  token never submits twice.
- **Uncertain acknowledgement reconciles; it does not resubmit.** `recover`
  finds the job id from the receipt, `squeue` and `sacct` by job name
  `mpx-<token>`. Two matches raise `submission_conflict`. No match leaves
  `submission_unknown`.
- **Scheduler identity check.** `status` refuses accounting whose user,
  account or job name differ from the intent.
- **Hash-checked staging and collection.** Every staged file and every
  collected output is checked against sha256 on both ends. For collection
  this catches transport damage only; the remote computes the hash (B3).

Gaps this spec closes (slices B1–B3):

1. No caller: no job_type, no handle in `meta.compute_handle`, no host pin.
2. The journal is a local directory. If the worker host loses it, every
   in-flight intent is lost to `_load`.
3. Preemption and node failure are terminal `failed`. Nothing decides whether
   to run the work again.
4. `stage` takes the whole bundle as in-memory bytes, capped at 2 GiB, over
   one `cat` with a 120 s timeout. A 3 GiB SIF does not fit that path.
5. No cleanup. Stage and run directories stay on the project filesystem, and
   inodes are the binding quota (Do next 2b in the thread).
6. `collect` returns bytes and outcomes. Nothing turns them into precis
   records.

### Slices

**B1. Runner changes in `precis.remote.slurm` (unit tests, fake transport).**
LANDED (hpc-b1, `6459335ef`+`73334fe8a`).

- `sbatch --no-requeue`. A preempted or node-failed job then shows as a
  terminal `PREEMPTED`/`NODE_FAIL` row in `sacct`, instead of rerunning in
  place under the same job id over a half-written run directory. Rerun
  decisions move to B2, where they are visible and capped.
- `status` maps `PREEMPTED`, `NODE_FAIL` and `BOOT_FAIL` to state
  `preempted`, separate from `failed`. The job script's own nonzero exit stays
  `failed`.
- **Image stage, separate from the run stage.** New `stage_image(path,
  sha256)` streams a local file over ssh stdin in fixed chunks, writes
  `images/<sha256>.sif.tmp`, checks `sha256sum`, then renames it. It is a
  no-op when `images/<sha256>.sif` exists and its hash matches. Images are
  kept between jobs (one inode each). Run stages stay small: manifests,
  scripts and inputs, under the existing 2 GiB cap.
- `cleanup(handle)`, allowed only after `collect` reached `complete`: removes
  `run-<token>`, then removes `stage-<id>` when no other journal entry that
  has not been collected names it. Images are removed only by an explicit
  `prune_images(keep=[sha…])`, never by `cleanup`.
- `Limits.max_wall_seconds` and `max_inflight` come from the caller (B2
  reads them from settings). The 600 s default stays as the pilot ceiling.
- **Many tasks per job.** `task_ids` already exist on the intent. The job
  script reads `tasks.json` from the stage, writes `out/<task_id>/…`, and
  `collect` takes per-task output names so a missing task is
  `missing_output` for that task only. One `sbatch` carries many short
  ML-potential relaxes (use (c) in the thread). Slurm arrays are out of
  scope until a measured queue cost says one job per batch is too slow.

**B2. Job type `hpc_batch` on the `ssh_node` executor (detached submit/poll).**

New `src/precis/workers/job_types/hpc_batch.py`, with `submit` and `poll` per
the `JobTypeSpec` contract. Params: `workload` (adapter name), `tasks`,
`resources` (nodes, cpus, gpus, memory, wall seconds), `image_sha256`.
Account, partition, QoS, remote root, SSH profile and vault refs come from
`app_settings` and the gitignored overlay, never from params or the repo.
Which of these may live in `app_settings` is decision D2 below (recommended:
SSH host, port, user and `host_keys` from the overlay only).

- **Host pin.** Every `hpc_batch` job carries `params.target_node` = the one
  controller host (from `PRECIS_HPC_CONTROLLER_NODE`, as `struct_relax` pins
  with `PRECIS_DFT_NODE`). One host to start: the journal lock is a local
  `flock`, so two hosts would each think they own the in-flight budget.
- **Token.** `sha256(job ref id, attempt)[:32]`. A worker restart or a lease
  reclaim reuses the same token, so the remote `submitted.once` guard stops a
  second `sbatch` even if the local journal is gone.
- **Journal durability.** `journal_root` is a persistent host path under the
  worker's data directory, not container scratch. After every runner call,
  the job writes the journal state into `meta.compute_handle`. If the journal
  file is missing, `poll` rewrites it from `meta.compute_handle` (same
  `profile_id` and `user`, or refuse) before calling `recover`.
- **The stored handle is a hint, not an authority.** `meta` is writable by
  agent_rw, so rehydration recomputes everything that names a remote path or
  job: token = `sha256(job ref id, attempt)[:32]`, `remote_dir` =
  `remote_root/run-<token>`, `stage_id` from the job's own params. If the
  stored handle disagrees with any recomputed value, `poll` refuses (fail
  `infra`, "compute_handle disagrees with recomputed <field>") and touches
  nothing remote. `cleanup`, `scancel` and every remote `cat` take their
  targets from `remote_root` plus the recomputed token, never from the handle.
- **Stage template.** `job.sh` is a versioned template in the repo, its
  sha256 pinned in the runner and recorded in provenance; per-job data goes
  only into `tasks.json`, never into the script text.
- **One SSH session per pass.** `poll` opens one `vault_ssh_session` per
  `ssh_node` pass and polls every handle through it. The runner's
  `status_interval` (30 s) and `submission_interval` (10 s) bound scheduler
  load whatever the pass rate is.
- **State map.**

  | runner state | precis job | action |
  |---|---|---|
  | `submission_unknown` | running | `recover` each pass; after `meta.deadline`, fail `infra` with "submission unknown: check squeue by job name `mpx-<token>` before any resubmit" |
  | `pending`, `running` | running | renew lease |
  | `succeeded` | running → done | `collect`, then B3, then `cleanup` |
  | `failed`, `timeout`, `cancelled` | failed | `collect` logs only, record the failure, `cleanup` |
  | `preempted` | queued again | new attempt number, so a new token; at most `hpc.max_preempt_retries` (default 2), then failed `infra` |
  | collection `pending` | running | retry `collect` next pass; same intent |
  | collection `failed` (hash or size) | failed | keep the remote run directory for inspection; no `cleanup` |

- **Cancel.** `precis jobs kill` on an `hpc_batch` job calls
  `runner.cancel`, and the job turns terminal only after `status` sees a
  terminal row. The request alone is not proof (the runner says so too).
  Cancel is host-bound: the kill is acted on at the controller's next `poll`,
  so while the controller is down it waits (see runbook R2 under B4).
- **Rerun budget.** The preempt retry cap counts attempts, not cost. Add
  `hpc.node_hours_budget`: a rerun is refused, failed `infra`, when the
  node-hours already recorded for the job (B3 measures) plus the requested
  allocation would exceed it.

**B3. Collect into precis records.**

Collected output is untrusted input. The remote side computes the sha256 the
local side compares against, so a compromised HPC account controls both the
file and its hash: the hash check catches transport damage, not tampering.
The checks below are the security boundary, not data hygiene.

- **Logs.** `slurm.out`/`slurm.err` tails (bounded, 64 KiB each) become
  `job_event` chunks on the job, as the `ssh_node` relax path writes them,
  with a provenance flag `origin=remote_unverified`. Agent-facing readers
  (job views, fisheye, search snippets) surface that flag as "remote,
  unverified" next to the text: log tails are a prompt-injection channel.
- **Results.** Each workload adapter maps `out/<task_id>/result.json` to the
  writer the local path already uses. For relaxes that is the
  `struct_relax` calculation record, so downstream readers cannot tell a
  remote relax from a local one except by provenance. `result.json` is parsed
  with a strict schema (unknown keys refuse) under a size cap; no pickle, npz,
  `eval` or any other format that executes or allocates from the file; no
  path component is ever taken from the file. Each result must name both its
  staged input hash and the task-manifest hash, and both must equal the local
  values. The shape, units and finite-value check runs after that. A result
  that fails any check is `invalid_output` for that task.
- **Nothing from the remote run directory is executed, imported or sourced
  locally**, including by adapters and tests.
- **Accounting rows.** The full `sacct` rows stay in the local journal; only
  the extracted measures below go into `meta` and `measure` rows.
- **Provenance** on every written record: intent token, scheduler job id,
  stage id, image sha256, task manifest hash, and the source sha of the
  workload wheel.
- **Accounting measures.** One `kind='measure'` row per job with `run_key` =
  token and subject = the job ref: elapsed seconds, queue wait seconds
  (Start minus submit), and node-hours by allocated TRES from `sacct`. Mint
  the measurands if absent. These are the inputs the rate controls above and
  the "is HPC worth it" read need.

**B4. Live pilot.** The ML-potential pilot above, run through `hpc_batch`
instead of by hand. Same gate: one real result captured by precis with
provenance, plus the actual job id, resources, runtime and allocation use.
Needs B1–B3, image I1 (MLIP) and Reto's go for the allocation spend.

Prerequisites (blocking). B4 is blocked on R1 (D1 and D2 ruled
2026-10-21: risk accepted for now); R2 must exist
before the first live run but blocks nothing else.

- **D1** (ruled: risk accepted for now): who can reveal the HPC key and passphrase.
- **D2** (ruled: risk accepted for now): where SSH host, port, user and `host_keys`
  come from.
- **R1. Key rotation and revocation runbook.** Rotation: mint the keypair
  offline → register the public key with the provider → `precis secret set`
  both refs (key, passphrase) → verify the old key is refused by a login
  attempt → delete the old key everywhere. Immediate revocation: within the
  first hour, delete the secret (or the host key file under D1 (b)) and
  contact the provider to remove the public key. The SSH profile records the
  key's creation date so an age alert can fire.
- **R2. Move-controller runbook.** One controller host is a single point of
  failure. The runbook: stop the controller worker, copy the journal to the
  new host, change the controller node setting, restart; same `profile_id`
  and `user`. Without the journal copy, the new controller rehydrates from
  `meta.compute_handle` under B2's recompute rule. While the controller is
  down, in-flight Slurm jobs keep running and finish on the cluster;
  nothing is collected, and `precis jobs kill` is recorded but acted on only
  at the controller's next poll, after a successful `recover`.

### Tests (B1–B3, no network)

- Fake transport replays scheduler output for: lost acknowledgement then
  receipt found; lost acknowledgement then `sacct` match; two matches
  (conflict); `PREEMPTED` with `--no-requeue`; collection timeout then
  success; hash mismatch.
- Journal deleted between passes: `poll` rehydrates from
  `meta.compute_handle` and the fake transport sees no second `sbatch`.
- Tampered `meta.compute_handle` (token, `remote_dir` or `stage_id` changed):
  `poll` refuses, and the fake transport sees no `cat`, `scancel` or `rm`.
- B3 hostile output, each `invalid_output` with nothing written: unknown key,
  oversize file, a pickle/npz payload, a path in a field, wrong input hash,
  wrong task-manifest hash, a hash pair that matches but a non-finite value.
- Log tails land with `origin=remote_unverified`, and the job view shows the
  "remote, unverified" label.
- `cleanup` keeps a stage another uncollected intent still names.
- `hpc_batch` with a fake runner drives a precis job through each row of the
  state map against the test DB.

### From precis-dispatch (folded 2026-10-21)

Phase 1 (the `ssh_node` executor, live) is that item's shipped half. Its
fired trigger: once this batch runner and the pollux GPAW relax both run in
prod, extract the shared `WorkloadSpec` / `Runner` / `Stager` seam from the
two real cases — not before (no stub runners or contract tests). Decided
2026-06-21: the abstraction lives in precis-mcp; images go through a local
registry on caspar backed by NFS (plain HTTP on the tailnet; revisit
auth/TLS before an image leaves it); jobs run as one shared
`precis-compute` cluster user. Risks still open: GPAW GPU on Blackwell +
CUDA 13 + aarch64 (CuPy likely needs a source build; the same image runs on
CPU as fallback); pin one PAW dataset version under the NFS potentials dir.
Handle durability is covered by `meta.compute_handle` re-adoption in
`workers/executors/ssh_node.py`.

## Apptainer image (SIF-1..5, spec 2026-10-21)

The project filesystem cannot hold a loose Python environment (about 26k
inodes free). One SIF per workload is one inode. The 2026-10-05/07 handoff
(td471801, paused) froze the plan below as five closure conditions; none is
built. Build order: I1 is the ML-potential image for B4; the GPAW image
follows the same items after its prerequisite.

**Prerequisite for the GPAW image: one GPAW relax completes in prod on
pollux.** None has (thread `chemistry`, Do next: both DFT-rung jobs ever
dispatched failed). The pollux image has no MPI; the MPI build is on branch
`chemistry-dft-mpi-env`, and Reto ruled 2026-10-10 to rebuild it only when
something needs the `gpaw` rung. HPC DFT (use (b)) is that need, but the
local completion comes first: a SIF of an image that has never finished a
relax moves the failure to a queue we pay for. The ML-potential image has no
such prerequisite.

**Build.** Convert the existing Docker image for each workload
(`docker/precis-dft` for GPAW; a MACE + torch-CUDA image from the pinned
catpath wheel for ML potentials) with `apptainer build x.sif
docker-archive://…`, on an amd64 builder (the Mac and the DGX nodes are
arm64). Install offline from the hash-pinned wheel set; no network at build
or run time. Size cap: SIF plus external controls at most 3 GiB, refuse above
it; no larger cap or repacking fallback.

Each item below is one of td471801's five conditions, with its test.

- **I1 (SIF-1). The launcher never unpacks the image.** It runs the SIF
  read-only (squashfs mount) and refuses before any root filesystem is
  materialised, including when a helper fails after the capability check.
  Test: launcher dry run against a host shim that reports "no FUSE / no
  setuid" exits nonzero with a durable refusal, and leaves no sandbox
  directory behind.
- **I2 (SIF-2). Environment allowlists, outer and inner.** Run with
  `--cleanenv --contain --no-home`, binds only the stage (read-only), the run
  directory and job-private local scratch. The host-launcher and in-image
  allowlists are separate. Any `APPTAINER_*` bind, config or loader variable,
  `LD_*` or shell variable not on the list refuses the run. Test: each
  forbidden variable set in the fake environment refuses.
- **I3 (SIF-3). A write table, enforced at write time.** One row per writer:
  path, byte cap, inode cap, enforcement, failure. Caches, temp, `HOME` and
  Apptainer's own cache/config/tmp all go to job-private node-local scratch,
  capped (proposed 64 MiB and 256 inodes, plus a 1 GiB and 1024-inode
  reserve). The project filesystem gets only the result files, an early
  report, the ledger and the logs (16 MiB results, 16 MiB scheduler logs).
  If local scratch is unset, not local, not writable, wrongly owned, a symlink
  or short of headroom, the job refuses; it never falls back to `HOME`, the
  project filesystem or the default tmp. Test: each guard on a fake
  scratch tree.
- **I4 (SIF-4). One supervised entry, real time budget.** One container
  entry runs the checks and the model. Metadata steps have short timeouts,
  separate from the whole-job wall time. The job's real exit code and an
  early report reach the run directory even when the checks fail. Pilot
  budget: at least 540 s left for the model inside a 600 s allocation after
  image, hash and mount checks; measured, not assumed. Test: a failing check
  writes the early report and the checker's own exit code, and clears any
  earlier success markers (the bootstrap hook already does this; keep it).
- **I5 (SIF-5). Identities without cycles.** Image, runtime profile, task
  manifest and index hashes form a chain with no self-reference: the image
  holds no manifest, SIF hash or job id; the external profile binds the SIF
  hash and the image's internal input index; the manifests are bound
  read-only at fixed paths. An installed-file census is inside the image or
  in a hash-bound file off the cluster. External controls at most 64 KiB
  each. The expected CPU reference manifest is separate from the generated
  artifact. Test: rebuild from the same inputs gives the same index hash;
  changing one wheel changes the profile hash and refuses the old manifest.

**Image acceptance.** The SIF exists with a measured size and sha256, fits the
3 GiB cap, and passes I1–I5 off the cluster. The first on-cluster check (FUSE
or squashfs mount, `--nv` driver passthrough, ABI) happens inside B4's one
allocation, with the early report collected even if it fails. No separate
preflight allocation.

### Open for Reto (defaults taken so the build is not blocked)

- First workload on HPC: ML potentials, not GPAW. Default taken: yes, because
  the GPAW image waits on the pollux completion above.
- `--no-requeue`, with reruns decided by precis and capped at 2. Default
  taken: yes.
- One controller host holds the journal and runs every `hpc_batch` job.
  Default taken: yes; the host is chosen at deploy time from the overlay. The
  host pin does not protect the key: it decides only where the job runs. The
  key and passphrase resolve through `precis.secrets::require_vault_secret` →
  `vault.reveal`, which migration 0111 grants to PUBLIC, so every DSN holder
  (every worker, the web app, the session MCP as agent_rw) can reveal them
  today. See D1.
- **D1 — RULED (Reto 2026-10-21): risk accepted for now; build as
  specced (vault refs). Revisit in the multi-user overhaul
  (`multi-user-overhaul.md`). Who can reveal the HPC SSH key.** (a) Promote the
  deferred per-name vault ACL (`precis.secrets` module docstring) so only a
  dedicated controller DB role can reveal the two HPC refs; needs a new role,
  a migration and a GRANT. (b) Keep the key and passphrase out of the DB: a
  host-local file on the controller (0600, root-owned), read through a new
  `require_host_secret` seam that `precis.remote.credentials::vault_ssh_session`
  takes instead of the vault refs. Recommendation: (b), because it does not
  depend on the ACL being built and no DSN holder or other host can read it.
- **D2 — RULED (Reto 2026-10-21): risk accepted for now; app_settings
  as specced. Revisit in the multi-user overhaul. Where SSH host, port,
  user and host-key pins come from.** They are planned for `app_settings`, which agent_rw can write
  (`precis.settings::set_setting`); a write there could point the controller
  at an attacker's server with a matching pinned key. Option: these four come
  only from the gitignored overlay, rendered at deploy time like
  `PRECIS_DFT_NODE`; `app_settings` keeps account, partition, QoS and limits
  only. Recommendation: overlay-only.
- The inode raise or cleanup request to the provider (thread Do next 2b) is
  still Reto's to send.
