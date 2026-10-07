---
status: draft
pillar: local-compute
---

# slullama HPC model — placement-chain rung + cluster access

- **Status**: leg 1 (static card) SHIPPED (dark); leg 2 (chain rung) OPEN —
  blocked on external cluster access.
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
been performed by writing this draft.
