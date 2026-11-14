---
status: in-progress
pillar: local-compute
title: Load-based scheduling — every launch is admitted against measured host capacity (gr647996)
---

# Load-based scheduling (gr647996)

Source: gripe gr647996 (rulings in its comments 5 and 7, Reto 2026-11-12).
Plan labels S1…S8 below are this spec's slices of gr647996. The launch-site
inventory (Devin, read-only, base `fab09555d`) is §Findings and §Launch-site
table; it names host roles, never node names.

## Bottom line

- Footprint admission is not built. The claim path reserves counted tokens
  only (`gpu`, `podman`, `tts`, `embedder`, `llm:<model>`, `llm-api:<provider>`);
  no launch path reserves memory or CPU. `utils/job_footprint.py` measures and
  stamps `meta.footprint` at terminal status; nothing reads it at claim time.
- One entry point, `precis.workers.admission` (`admit`, `release`, `spawn`),
  built by extending the shared reserve machinery. Every launcher calls it:
  the claim path, passes and cadences, detached children, and the bash gates
  through a CLI. Admission is on reservations sized by a learned predictor,
  against capacity the heartbeat measures.
- Four unrelated admission mechanisms run today and are retired into it: the
  DB claim path, `scripts/lib/gate-slot.sh`, per-executor env concurrency
  knobs, and extra worker units per host.
- A static ratchet (S7a, landed) freezes today's unadmitted launch sites; the
  baseline only shrinks. A live per-host census (S7b) catches the rest.
- Gates admit through their own DB connection; with the DB unreachable they
  run unadmitted under one host-local flock (gr647996 comment 5). Landing and
  deploying a fix never blocks on admission.

Orders: S1 → {S2, S5, S7a} → S3 → {S4, S6} → S7b → S8, with S2b after S2 once
rows exist and the decision layer after S3 (all gr647996).

## Ruled design

Current-tense requirements. Reto's rulings of gr647996 comments 5 and 7.

### Entry point

- `precis.workers.admission` exposes
  `admit(conn, host, footprint, holder) -> Admission | None` (all-or-nothing,
  TTL hold), `release(conn, admission)` and
  `spawn(admission, argv, ...) -> handle`.
- `admit` extends `precis.store._resource_slots_ops::reserve_resource_slots`
  and `insert_slot_hold`: the all-or-nothing reserve, refund, TTL hold and
  expired-hold reclaim already live there. The first callers are the three
  that reserve that way today: `precis.workers.executors._common::claim_executor_jobs`
  (`_finish_claim`, reserve-at-claim), `precis.utils.llm.local_serving::_reserve_one`
  and the outbound D2 hold in `precis.utils.llm.openai_tools`. `cluster-scheduling.md`
  Scope already rules "the slot hold and the rate bucket, no third".
- `spawn` is the only detached launcher. It replaces
  `precis.workers.job_types.run_slice::_launch` and
  `precis_pathway.runner::_spawn_detached`, wraps
  `precis.workers.child_compute::run_in_child`, refuses without a live
  `Admission`, and applies the per-task hard cap (cgroup scope, `--memory`,
  RLIMIT_DATA) sized from the footprint.
- Non-claim launch sites (passes, cadences, `precis ingest --watch` batches)
  call `admit()` as a context manager. Bash gates call the CLI
  (`python -m precis.workers.admission hold --footprint … -- <cmd>`).

### Gates and DB outages (comment 5)

- Dev and slice-host gates admit through their own DB connection and call
  `admit()` like every other launch site. One ledger, `resource_slots`. They
  do not borrow the claiming worker's connection, so interactive runs on a dev
  machine admit too.
- If the DB is unreachable, the gate runs with no admission. It takes one
  host-local flock so one gate runs at a time on that host, and logs that it
  ran unadmitted. There is no host-local ledger backend and nothing to
  reconcile afterwards.
- Why: with the DB down, workers cannot claim and dispatch and ticks stop, so
  hosts drain to their in-flight jobs and the contention admission prevents
  mostly disappears. What must keep working during an outage is landing and
  deploying the fix, so that path never blocks on admission.
- Safety net: S4's hard memory caps still apply; S7's live census and its
  `unadmitted-load` detector report gates that ran unadmitted (all gr647996).
- Accepted edge case: in a partial outage (a host cannot reach the DB but
  still runs DB-admitted jobs) an unadmitted gate can overcommit that host.
  The one-at-a-time flock and the memory caps limit the damage.

### Footprint request and capacity

`meta.footprint_req`, stamped at mint (gr595539) from the predictor, with a
static code default table for cold start:

| Field | Unit | Source / note |
|---|---|---|
| `cpu_milli` | millicores | p95 `cpu_s / wall_s`; also sets the job's thread count, never the host's core count |
| `mem_mb` | MiB | p95 peak RSS; container jobs: their `--memory` |
| `gpu` | cards | today's `gpu:1` |
| `vram_mb` | MiB | per model and size; learnable from one run |
| `llm` | `{"llm:<model>": n, "llm-api:<provider>": n}` | today's tokens and holds |
| `account` | `{"account:<lane>": n}` | CLI subscriptions (claude-sub, codex, kimi, devin); replaces `plans/lanes.py` `Lane.cap`, `PRECIS_DEVIN_CONCURRENCY`, `PRECIS_INPROC_CONCURRENCY` |
| `rate` | bucket names | admitted by `rate_limit.acquire`, not a slot |
| `cost_usd` | estimate | checked against `PRECIS_DAILY_COST_CEILING` and the per-tick cap |
| `wall_s` | seconds | predicted; for reservation and backfill |
| `scratch_mb` | MiB | optional; Docker VM disk is a real limit (`scripts/lib/docker-disk.sh`) |
| `off_host` | bool | detached remote work (devin_api, GitHub) costs the host only its launcher |

Capacity: the heartbeat writes hard `resource_slots` rows `mem_mb`,
`cpu_milli`, `vram_mb` per host, from probed totals (`/proc/meminfo`, sysctl,
`nvidia-smi`; on gate hosts the Docker VM via `docker info`, as
`gate_slot_default` does; on macOS container capacity is the colima VM's
memory, not the host's) minus reserves (`host_memory_os_reserve`, the
`host_llm_lanes` fraction, standing daemons: embedder, LLM servers,
precis-web, MCP). These are the numbers
`roles/hardening/templates/memory-budget.json.j2` sums at deploy time today.

### Admission loop (one node agent per host)

- Sample host and per-job usage every 5 s; track a per-job peak and a host
  peak. Per job means the whole process tree; container jobs are read through
  the cgroup or `docker stats`.
- Admit on reservations, not on measured load. A running job counts as
  max(reserved peak, observed). Unadmitted load (host used minus the sum of
  jobs: gates, sessions, docker) also counts.
- Caps: memory and VRAM at about 85–90% of capacity; CPU may oversubscribe to
  100–150%.
- Claim when a job exits, plus a 60 s fallback timer. Claim several per pass
  while they fit. No local task cache.
- The claim and the reservation write are one DB transaction (the
  `resource_slots` ledger).
- Reservations carry a lease tied to the worker heartbeat; a dead holder's
  capacity returns. S3 (gr647996) adds lease renewal before long jobs admit.
- Anti-starvation: when the highest-priority waiting job fits nowhere, the
  host where it would fit soonest stops admitting smaller jobs until it fits.
- At exit the agent writes the footprint and outcome once. A memory-cap kill
  writes `killed-for-memory` (gr595537) and requeues with a bigger footprint
  (gr595538).

### Metrics: one row per run

- Fields: job_type, host, machine-type key, features (below), outcome, peak
  RSS, peak VRAM, CPU-seconds, wall time, host load during the run.
- The machine-type key is derived, not a maintained list: arch + GPU model +
  core-count bucket.
- Grouping: memory pools across machines. VRAM is per GPU model. CPU and wall
  time are per machine type, falling back to pooled × a per-host speed factor
  (a first-heartbeat benchmark, or 1.0 then learned). A new machine needs no
  empty-machine runs.
- A run killed by its cap is a censored row (peak ≥ cap): drop it from the
  fit, or target cap × 1.5.

### Predictor

- Outputs, per (job, option): p95 of log peak RSS, log VRAM, log CPU-seconds
  and log wall time, plus cost and P(success). An option is a host type and,
  for LLM or builder jobs, a model or lane.
- Model: gradient-boosted trees with a quantile loss (scikit-learn
  `HistGradientBoostingRegressor`, a new core dependency). One global fit,
  recency-weighted with a half-life of about 2 weeks, refit nightly (hourly
  later).
- Cold start: a job_type with fewer than about 20 runs uses a conservative
  per-type default, which in effect runs it nearly alone.
- Features are extracted automatically from params; nobody declares a size
  key. They include:
  - numeric params (log-scaled when wide);
  - the length of every string, list or dict;
  - low-cardinality strings as categories;
  - file bytes and page counts;
  - submitter;
  - model or lane;
  - machine type, for the time targets.
- Text features (brief, report or prompt), computed at mint:
  - `has_prompt` and the text kind;
  - token count (log);
  - Flesch–Kincaid grade, computed after stripping code, paths and handles;
  - average sentence length and type–token ratio;
  - code/path share;
  - task-size counts: distinct paths and `path::Symbol` anchors, handles,
    acceptance items or test files, numbered requirements;
  - for gripe fixes: gripe age, comment count and re-open count.
  - Later, only if LLM and builder wall-time error stays large: the brief
    embedding, or a nearest-neighbour residual.
- Feedback: the S7 census (gr647996) flags any key where p95/p50 > 4, meaning
  the features miss the size driver for that job_type.

### Decision layer

A later slice after S3 (gr647996): model choice and placement become one
step. Score each feasible (host, model) option by expected cost = cost +
(1 − P(success)) × next-rung cost, then pick the best one that fits capacity.
First read `precis.utils.llm.decide` and the `run_slice` lane ladder.

## Findings

State at `fab09555d` (no prod access: service_config rows and lane enablement
per host are inferred from code defaults and playbooks).

- Today's admission is `claim_executor_jobs` → `_finish_claim` →
  `reserve_resource_slots`: counted tokens only. `Store.job_footprint_summary`
  rolls up p50/p95; nothing reads it at claim time.
- Four mechanisms, none aware of the others:
  1. the claim path (DB `resource_slots`, per host);
  2. `scripts/lib/gate-slot.sh` (mkdir slot dirs in the git common dir, dev
     and slice host only);
  3. env-var concurrency knobs per executor and pass
     (`PRECIS_SANDBOX_CONCURRENCY`, `PRECIS_INPROC_CONCURRENCY`,
     `PRECIS_DEVIN_CONCURRENCY`, …);
  4. extra worker units per host (deploy "second scheduling lanes" 20d, 20e,
     20g, 20h, 35, 43), which multiply concurrency outside any count.
- Likely mechanism for gr601479 (unverified): several container paths start
  Docker work on the gate VM without a slot dir:
  - the `--quick`/`--checked` pre-qland lint, including a full
    `mypy src tests` on `--checked`;
  - `scripts/test --typecheck`, `--fast` and `--collect-only`;
  - `boundary_probe`'s per-probe `docker run precis-dev`;
  - the warm `precis-gate` and `precis-test-db` containers, which stay up
    between runs;
  - idle slice stacks, up to `PRECIS_SLICE_STACK_CAP=8`.

  Land launches also export `PRECIS_GATE_SLOTS=2`
  (`precis.workers.job_types.land_slice::land_gate_slots`) while interactive
  runs derive their own cap, so callers rank their queue against different
  slot counts.
- Largest unadmitted consumers: the OCR child behind `ingest/marker.py`
  (paper_ingest/reextract, `precis ingest --watch`, in-request
  `handlers/web.py`), kinetics/coverage/revalidate jobs minted with
  `requires={}`, the Claude and Codex CLI chokepoints (no host or account
  slot), and the gate-host Docker paths above.

## Launch-site table

S-labels in this section are the gr647996 slices (§Slices); `§` names point
at sections of this spec.

Host roles below are the inventory group names, never node names:

| Role | Meaning |
|---|---|
| GW | `gateway` group: agent/OAuth host, factory slice host, SMALL-LLM drain host |
| INF / SCH | `inference` / `scheduler` collapsed-worker hosts |
| CMP | `compute` GPU hosts (43 lane, heartbeat-only system worker) |
| LLM | `vllm_serving` / `sglang_serving` / llama.cpp hosts |
| SBX | `agent_sandbox_lane` |
| DATA | DB host |
| DEV | any worktree host running `scripts/test`/`scripts/ship` on a shared Docker VM; the slice host is one |
| MCP | the session `precis-mcp-http` container and the cluster `mcps` role |
| REMOTE | off-host: GitHub, Devin cloud, vendor APIs |

Work classes:

| Class | Meaning |
|---|---|
| 1 | Heavy science compute |
| 2 | LLM work (local inference or remote/CLI) |
| 3 | Simple compute; "3p" marks a sub-minute bounded probe (git/gh/ps…) |

Admission column:

| Value | Meaning |
|---|---|
| yes | Holds a counted reservation for the resource it actually burns |
| partial | Claimed through `claim_executor_jobs` (lease, executor/type caps, capability gate, maybe a token slot) but no mem/CPU footprint |
| no | Nothing |


### 1a. Executor lanes (the claim path)

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `src/precis/workers/executors/_common.py::claim_executor_jobs` (+ inner `_finish_claim`, reserve-at-claim) | nothing itself; claims rows and reserves `meta.requires` all-or-nothing | every worker | n/a | **this is the partial admission point** | `_MAX_RUNNING_DEFAULTS` (`derived_drain:1`), `_EXECUTOR_CAP_DEFAULTS` (`ssh_node:8`), `_EXECUTOR_RESERVE_DEFAULTS` (`run_slice:1, land_slice:1`), `_mem_pressured_hosts` soft veto, `app_state` `claim.max_running.*` / `claim.executor_cap.*` / `claim.reserve.*` / `claim.share.<lane>` | **extend into the single entry point** (§Ruled design); retire the caps (§Home-made caps to retire) |
| `executors/job_inproc.py::run_job_inproc_pass` → `_run_one` → `spec.dispatch` (in-process, worker thread) | the job_type's dispatch | GW (collapsed + 20d drain lanes + 20e), INF, SCH | 2/3 per type | partial (`embedder`/`llm:` tokens where declared) | `limit=1` per tick; lane count `precis_worker_drain_lanes` (20d) | route: claim while the footprint fits instead of `limit=1` + extra lane units |
| `executors/claude_inproc.py::run_claude_inproc_pass` → `ThreadPoolExecutor(pool_size)` → `_run_one` → plan_tick/fix_gripe/doctor_tick/plugin dispatch → `claude -p` child | Claude CLI subprocesses | GW (20e + collapsed) | 2 | partial (no llm/account slot; subscription window cap + daily cost ceiling only) | `_inproc_concurrency` (`PRECIS_INPROC_CONCURRENCY`, default 1, 20e sets it), `_inproc_pass_budget_s` 120 s | route: pool width = what admission grants (`account:<lane>` + mem); delete env width |
| `executors/coordinator.py::run_coordinator_pass` → `_run_one` (quest_tick inline claude CLI, survey_extract, deferred_llm_call, good_search) | in-process dispatch + claude CLI | every `system` worker | 2 | partial | one claim per dispatch; `PRECIS_COORDINATOR_PASS_BUDGET_S` 120 s | route (LLM account slot for quest_tick's CLI call) |
| `executors/ssh_node.py::run_ssh_node_pass` → `_run_one` → `spec.submit` (detached) or `_dispatch_bounded` | detached children / blocking dispatch thread | GW (20g slice lane + collapsed), CMP + INF (43 lane) | 1/3 per type | partial (`gpu:1` only where minted/declared) | `limit=1`; executor cap 8 + reserves; `gpu` slot capacity | route; the fill loop replaces `limit=1` |
| `executors/ssh_node.py::_dispatch_bounded` (daemon thread) / `workers/child_compute.py::run_in_child` (spawn, own pgid) | legacy blocking compute (struct_*, hexfold_relax, coverage, aggregate, explore, retrosynth, fold) | CMP, INF | 1 | partial | wall bound from spec | route via the spawn helper; the child gets the hard mem cap |
| `executors/claude_docker.py::_launch_build` / `_launch_run` / `_launch_script` → `podman run -d --name sandbox-<id>` | agent/build/script container | SBX | 2 (build) / 3 (run, script) | partial (`podman` token reserved) | `_concurrency` (`PRECIS_SANDBOX_CONCURRENCY`=2), `_load_ok` (`PRECIS_LOAD_CEILING`), `_cgroup_caps` (`PRECIS_SANDBOX_MEMORY` 8g / `_CPUS` 2 / pids 512) | route; keep cgroup caps but derive them from the footprint; delete `_concurrency`/`_load_ok` |
| `executors/claude_docker.py::_launch_stage` → thread → `_sandbox_script.py` (uv lock / wheel download subprocess) | dependency staging | SBX | 3 | partial (stage job claimed; no footprint) | none | route (it is a job; give it a footprint) |
| `executors/_sandbox_read_mcp.py` Popen `precis serve --read-only` | per-run read MCP child | SBX | 3 | no (rides the parent job) | none | leave; fold into the parent job's footprint |
| `executors/devin_api.py::_launch` → REST create_session | cloud Devin session | REMOTE (claimed on any lit worker) | 2 | partial (claim only) | `_concurrency` (`PRECIS_DEVIN_CONCURRENCY`=2) | route as an `account:devin` slot in the same reserve call; no host mem/CPU; delete the env cap |
| `executors/agent_container.py::containerize_claude_argv` (via `utils/claude_agent.py::call_claude_agent` when `PRECIS_AGENT_CONTAINER`) | `docker`/`podman run … claude` | GW (where lit) | 2 | no | `container_limit_flags` (cpuset/shares only) | route (container mem counts against the host) |

### 1b. `ssh_node` job types (detached children via the shared launcher)

`run_slice.py::_launch` (Popen `/bin/sh -c`, `start_new_session=True`) is the one detached launcher behind run_slice, land_slice, dep_check, lane_health and boundary_probe. release_cut, release_deploy, ci_rerun, ci_red_digest and release_confirm reach it through `release_cut.py::_launch` / `launch_script`.

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `job_types/run_slice.py::_launch` (called from run_slice submit) | agent CLI (claude/codex/kimi/devin) in a slice worktree; the agent then runs `scripts/test`/`ship` | slice host (GW) | 2 (+3 via its gates) | partial (ssh_node claim + `run_slice:1` reserve) | `plans/lanes.py` `Lane.cap` = `llm_account.concurrency`; `live_lane_counts`; `LANE_WAIT_PARK_CAP`; `factory.free_lane_wait_min` | **make `_launch` the admitted spawn helper**; turn the lane cap into an `account:<lane>` resource |
| `job_types/run_slice.py::_tests_green_on_main` → `_run` (`uv run pytest -n0 …` on a host worktree) | bare host pytest (moot fallback) | slice host | 3 | **no** (no gate slot, not in a container) | `_MOOT_TEST_TIMEOUT_S` | route (gate footprint) |
| `job_types/run_slice.py::_devin_auth_status`, `_devin_model_prices`, `_git` | `devin auth status`, `devin models list`, git | slice host | 3p | n/a | timeouts | leave (probe) |
| `job_types/land_slice.py::_launch_ship` / `_launch_verify` (via `_launch`) | `scripts/ship --quick/--checked`, `scripts/test --selected …` | slice host | 3 (heavy gate) | partial (claim) + gate-slot inside ship/test | `land_gate_slots` (`PRECIS_LAND_GATE_SLOTS`, default 2), `test_nproc` (`PRECIS_LAND_TEST_NPROC`, default 3) | route; drop the slot-subset override |
| `job_types/land_slice.py` (`uv run pytest --collect-only`), (impacted-tests), (preflight helper), (`git push`) | host-side collection / selection / git | slice host | 3 / 3p | no | timeouts | leave (bounded); collect-only is borderline, see unsure list |
| `workers/slice_stacks.py::_docker` | `docker compose down/ps/inspect/network rm` | slice host | 3p | n/a | `PRECIS_SLICE_STACK_CAP`=8, idle 3600 s | leave (teardown); count idle stacks' RSS as reserved, see §Done-check |
| `job_types/boundary_probe.py` (`docker run precis-dev … /probe.py`) via child launched at | probe containers, `--network none` | slice host | 3 | **no gate slot**; claim only | 600 s per probe | route (gr601479 suspect) |
| `job_types/dep_check.py::submit` → `_launch` → `python -m …dep_check` → (git fetch/worktree, `uv lock`, `uv sync --dry-run` ×2 Pythons) | uv resolution | slice host | 3 | partial | none | route (light footprint) |
| `job_types/lane_health.py` → child → (`claude -p ping`), (`codex login status`) | adapter probes | slice host | 2 (tiny) | partial | `_PROBE_TIMEOUT_S` | leave (probe); account slot optional |
| `job_types/release_cut.py::_launch` (`scripts/release --cut-only`), `release_deploy.py::submit` (`--deploy-only`), `ci_rerun.py::submit` (`scripts/ci-rerun`), `ci_red_digest.py::submit` (`scripts/ci-failed`), `release_confirm.py::_launch_close` | release/CI scripts (git, gh, ansible from the slice host) | slice host | 3 | partial | factory refusal / pace | leave (light, serialised by release logic); register a footprint anyway |
| `job_types/pr_action.py::submit` → gh | gh API | slice host | 3p | partial | — | leave |
| `job_types/struct_relax.py::_default_runner` (`ssh <node> docker run` gpaw/ML), `kill_container`, `_gpu_reset` (`nvidia-smi`), stale sweep | GPU DFT/MLIP container | CMP | 1 | partial (`gpu:1` via registry `requires`) | `PRECIS_DFT_CONTAINER_CMD`, MPI/OMP env, `container_limit_flags` | route; add mem/VRAM footprint and `docker --memory` from it |
| `job_types/struct_neb.py`, `struct_md.py`, `struct_search.py` (legacy dispatch on the ssh_node thread) | MLIP compute (unsure whether in-process or via struct_relax's runner) | CMP | 1 | partial (`gpu:1`) | spec wall bound | route |
| `precis_pathway/runner.py::_spawn_detached` (seed/kinetics/modes; `taskset` via `_pinned_cmd`) | `python -u -m precis_pathway.runner` child, own session | CMP, INF | 1 | partial: seed/modes `gpu:1` (`quest/compute.py`, `handler.py`); **kinetics `requires={}`** (`kinetics_job.py`) | `_apply_memory_cap` RLIMIT_DATA (`PRECIS_AUTOCATPATH_MEM_LIMIT_GB`=32), seed cpuset (`PRECIS_AUTOCATPATH_SEED_CPUSET`) | route; keep the RLIMIT as the hard limit, sized from the footprint |
| `precis_pathway/runner.py::_run_request_subprocess`, `run_kinetics_subprocess`, `run_kinetics_sweep_subprocess`, `run_kinetics_tpd_subprocess` | blocking children (explore, coverage, aggregate, revalidate, kinetics fallback) | CMP, INF | 1 | partial (aggregate `gpu:1`; coverage/revalidate `requires={}`) | same RLIMIT | route |
| `precis_chem/jobs.py::_default_runner` (`ssh <node> podman run` aizynth / normalizer) | retrosynth container | CMP (route node) | 1/3 | partial (no gpu/podman token on the `retrosynth` row) | none | route |
| `precis_bio/jobs.py::_default_runner` (`ssh <node> docker run` AlphaFold3) | fold container | CMP | 1 | partial (`gpu:1`) | `alphafold_mem_limit` (deploy) | route |
| `precis_se/hexfold_relax_job.py` → `child_compute.run_in_child` | MACE relax in a spawned child | CMP | 1 | partial (gpu only if minted with it) | wall bound | route |
| `precis_se/pocket_realize_job.py` | ssh_node dispatch (unsure: xtb/MACE in-thread) | CMP | 1 | partial | — | route; **unsure** of the compute shape |

### 1c. `job_inproc` / `claude_inproc` / `coordinator` job types (in-process dispatch)

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `job_types/embed_batch.py::_dispatch` | bge-m3 batch (in-proc or remote embedder) | GW/INF/SCH | 3 | partial (`embedder:1`) | `params.limit` | route; footprint replaces the capacity-1 token |
| `job_types/derived_drain.py::_dispatch` (thread pool width `_derived_concurrency` from `llm:` capacity) | SMALL-LLM summarize/classify drain | GW | 2 | yes on the LLM dimension (`llm:` per call) | `_MAX_RUNNING_DEFAULTS` 1; `PRECIS_SMALL_DRAIN_CONCURRENCY` override (`materialize.py`) | leave the LLM path; delete the env width + per-type cap |
| `job_types/paper_ingest.py`, `paper_reextract.py`, `pres_reextract.py` → `ingest/pipeline.py` → `ingest/marker.py` (spawn `multiprocessing` child, marker/surya OCR) | multi-GB OCR child (GPU where present) | GW/INF/SCH | 3 (heavy) | partial | none | route (one of the biggest unadmitted RSS consumers) |
| `job_types/pcb_place.py` (topoplace placer/router, in-proc) | CPU-heavy placement | GW/INF/SCH | 3 (heavy CPU) | partial | — | route |
| `pcb_argue.py`, `pcb_review.py`, `datasheet_pull.py`, `gripe_validate.py`, `graph_garden.py`, `cite_resolve.py`, `youtube_bulk.py`, `paper_pull.py`, `pdf_attach.py`, `secret_status.py` | LLM / fetch / light compute | GW/INF/SCH | 2/3 | partial | — | route (small default footprints) |
| `plan_dispatch.py`, `factory_rescue_tick.py`, `ci_verdict_poll.py` (gh), `ci_watch.py` (gh), `release_poll.py`, `pr_poll.py` | factory bookkeeping + gh | GW | 3p | partial | — | leave (light); default footprint |
| `precis_se/simp_job.py`, `precis_se/manufacture_job.py` | se compute in-proc | GW/INF/SCH | 3 | partial | — | route |
| claude_inproc types: `plan_tick`, `fix_gripe` (git clone + claude), `diagnose_gripe` (git clone), `doctor_tick`, `meditation`, `briefing`, `card_forge`, `conflict_sweep`, `draft_refresh`, `datasheet_facts`, `diagram_propose`, `cad_propose`, `cad_discuss`, `structure_propose`, `reground_claim`, `reading_brief`, `remarkable_*_send`, `news_poll`, `money_refresh`, `taproot_backfill`, `elsevier_abstract_backfill`, `good_search` triage, `precis_se/atomic/propose` | `claude -p` (MeasuredPopen) + git | GW | 2 | partial | `PRECIS_INPROC_CONCURRENCY` | route (account slot) |
| `job_types/draft_export.py` → `export/compile.py` / `utils/compile_guard.py` (latexmk) | LaTeX compile | GW | 3 | partial | — | route |
| `job_types/pourbaix_bulk.py` (pymatgen + mp_api, claude_inproc lane) | CPU compute + network | GW | 1/3 | partial | — | route; it sits on the wrong lane for its class |
| coordinator types: `quest_tick` (inline claude CLI), `survey_extract`, `deferred_llm_call`, `good_search` | LLM | all `system` workers | 2 | partial | pass budget | route |

### 1d. In-process passes and scheduler cadences (no claim at all)

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `workers/runner.py::run_loop` (serial rotation over `cli/worker.py::_build_handlers`) | every registered pass, one at a time per worker process | GW/INF/SCH (+ every lane unit) | 2/3 | no (process = slot) | rotation order, `--skip`, lane units | becomes the node agent's pull loop (S8, gr647996) |
| LLM passes: `summarize`, `llm_summarize` (`ThreadPoolExecutor`), `classify` (`ThreadPoolExecutor`), `classify_topics`, `card_describe`, `hub_refine`, `hub_tagline`, `conflict_search`, `chase_trigger`, `inbound_ground`, `inbound_chase`, `llm_reconcile`, `paper_glossary`, `context_sentence`, `axis`, `diagnose_scan`, `backlog_groom`, `idea_feed`, `briefing`, `dream_consolidate`, `gripe_triage`, `transcript_audit` | router calls → `local_serving.acquire` (`llm:`), outbound D2 hold (`llm-api:`), or `claude_p`/`codex_exec` subprocess | GW/INF/SCH | 2 | yes for `llm:`/`llm-api:`; **no** for the CLI transports | `PRECIS_SUMMARIZE_CONCURRENCY`/`_MAX_CONCURRENCY`, `PRECIS_CLASSIFY_CONCURRENCY`/`_MAX_CONCURRENCY`, `service_config.concurrency` (mig. 0091) | leave the slot-held paths; delete the pool-width knobs (width = slots granted); CLI transports take an `account:` slot |
| Cadences in `workers/scheduler.py::CADENCES` that spend (`structural`, `deep_review` → `workers/review.py`; `dream_agent`), run on the scheduler thread (`start_scheduler_thread`) or the rotation | `call_claude_agent` → claude subprocess (maybe containerised) | GW (+INF for reviewers) | 2 | no | `utils/load_gate.py::skip_if_high_load` (`PRECIS_LOAD_CEILING`), dedup windows | route (account slot + mem); delete the load gate |
| Other cadences (`cron_tick`, `watch_poll`, `materialize`, `lane_health` mint, `gripe_dispatch`, `secret_check`, …) | SQL / mints | all | 3p | no | interval | leave |
| CPU/IO passes: `chunk_keywords` (KeyBERT on bge-m3), `tag_embeddings`, `embed` (manual `--only`), `clusterize` (daily SOM), `paper_rank`, `fetch`/`fetch_oa` (`ThreadPoolExecutor`), `gp_fetch`, `bib_parse`, `paper_reconcile`, `corpus_reconcile`, `openalex_enrich`, `orcid_enrich`, `paper_meta_enrich` | in-process compute / HTTP | GW/INF/SCH | 3 | no (HTTP is rate-bucketed) | `PRECIS_OA_FETCH_WORKERS`, `PRECIS_OA_FETCH_PASS_WALL_S`, `_throttle.due` | route the heavy ones (`chunk_keywords`, `clusterize`, `embed`); leave fetch on the rate bucket |
| `openalex_snapshot` pass (20f lane) | 675 GB S3 stream into Postgres | DATA | 3 | no | `_throttle.due` ~30 d | route as a long low-prio unit with a footprint; retire the lane unit |
| `briefing_audio`, `cast_audio` → `tts/encode.py` (ffmpeg) + TTS model | TTS render | TTS host (`tts` capability) | 3 (GPU/CPU) | partial (registry `requires={'tts'}` is a capability gate for a pass, not a reservation; unsure) | `PRECIS_TTS_SLOTS` | route |
| `heartbeat` pass + threads: `heartbeat.py` (`ps -Ao pcpu,comm`), systemctl, `PRECIS_TEMP_CMD` (shell); `capability_probe.py` nvidia-smi, memory_pressure; `agent_container.py` probe | probes | every host | 3p | n/a | timeouts | leave; heartbeat becomes the live-check source (§Done-check) |
| `workers/conditions.py` (`ssh` restart-once) | remote service restart | GW → target | 3p | no | once | leave (remediation) |
| `gripe_triage.py` git, board script; `gripe_autoclose.py` git; `utils/workspace.py` git | git | GW | 3p | no | timeouts | leave |
| `utils/stall_capture.py` (detached tcpdump) | packet capture | fetch host | 3p | no | `MAX_PER_DAY`, 30 s | leave |

### 1e. LLM CLI and transport chokepoints

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `utils/_claude_subprocess.py::run_claude` / `run_claude_gated` (MeasuredPopen) / `run_claude_async` (`create_subprocess_exec`) | `claude` CLI | any claude-capable host | 2 | no host/llm slot; subscription window cap (pool ledger) + `PRECIS_DAILY_COST_CEILING` + `--max-budget-usd` | per-tick cost cap | **route**: the CLI chokepoint takes an `account:claude-sub` hold (D2 shape) |
| `utils/claude_p.py::call_claude_p`, `utils/claude_agent.py::call_claude_agent` | the above | — | 2 | as above | — | covered by the chokepoint |
| `utils/codex_exec.py::_run_codex` (MeasuredPopen), `_probe_login_status` | `codex exec` | slice host / GW | 2 | no | — | route like claude |
| `utils/llm/local_serving.py::acquire` → `_reserve_one` | (reserves `llm:<model>` + TTL hold) | all | 2 | **yes** | slot capacity from `served_by` | **keep**; first caller of `admit()` |
| `utils/llm/openai_tools.py` D2 hold (`llm-api:<provider>`) + `utils/rate_limit.py::acquire` | (hold + rate bucket) | all | 2 | **yes** (concurrency + rate) | `PRECIS_OUTBOUND_FALLBACK_CONCURRENCY` | keep; second caller of `admit()` |
| `utils/llm/kimi.py` (HTTP wire via router) | kimi subscription API | all | 2 | yes (outbound policy) + window cap | bulk guard | keep |

### 1f. Standing daemons, deploy units, timers and cron

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `deploy/playbooks/20b-precis-worker-collapsed.yml` (`precis worker --profile all`) | collapsed worker | GW, INF, SCH | 2/3 | n/a (it is the claimant) | per-unit `MemoryMax` (`roles/hardening` `precis_lane_memory`) | becomes the node agent |
| `20c-precis-heartbeat-serving.yml` (`--only heartbeat`) | heartbeat-only worker | serving, compute, gateway | 3p | n/a | lane `heartbeat` 3G/4G | leave |
| `20d-precis-worker-drain.yml` (N× `--only job_inproc`) | extra job_inproc claimants | GW | 2 | n/a | `precis_worker_drain_lanes` (2), pinned by `test_claim_fairness.py` | **delete** once the node agent fills by footprint |
| `20e-precis-worker-agentlane.yml` (`--only job_claude_inproc --only job_inproc`, `PRECIS_INPROC_CONCURRENCY`) | planner lane | GW | 2 | n/a | env width | **delete** (S8, gr647996) |
| `20f-precis-worker-oasnap.yml` (`--only openalex_snapshot`) | snapshot lane | DATA | 3 | n/a | singleton | delete when it becomes a job |
| `20g-precis-worker-kwdrain.yml` (`--only chunk_keywords --dedicated`, `--shard-count/--shard-index`) | keyword drain lanes (opt-in) | `chunk_keywords_drain` hosts | 3 | n/a | hosts × lanes | delete (shards → footprint-filled claims) |
| `20g-precis-worker-slices.yml` (`--only job_ssh_node --batch-size 1` + `--only plan_tick`) | slice poll lane | GW | 2/3 | n/a | `--batch-size 1` | delete (node agent) |
| `20h-precis-worker-hubrefine.yml` (`--only hub_refine`) | hub_refine lane | GW | 2 | n/a | singleton | delete (node agent) |
| `35-precis-worker-sandbox.yml` (`--only job_claude_docker`) | sandbox claimant | SBX | 2/3 | n/a | lane `sandbox` 1536M/2G + 8G container reserve | delete (node agent); keep per-container cgroup |
| `43-precis-worker-compute.yml` (`--only job_ssh_node`) | GPU/compute claimant | CMP, INF | 1 | n/a | lane `compute` 40G/48G + 40G container reserve | delete (node agent) |
| `37-precis-worker-agent.yml` / `20-precis-worker.yml` | legacy split units (rollback only) | — | — | n/a | — | delete with the lane retirement |
| `19a` precis-embedder (`precis serve-embeddings`, `Nice`, `CPUAffinity`) | embedder daemon | inference hosts | 3 | n/a | `PRECIS_EMBEDDER_SLOTS` (capability_probe) | leave; declare as a host reserve, and derive slot capacity from footprint |
| `04` llama-swap, `49` vLLM (docker run), `49a` SGLang (docker run) | LLM servers | LLM | 2 | n/a | `host_llm_lanes` fraction, `host_lane_exclusive_groups` | leave (persistent); their memory share is a host reserve, and `llm:` slots stay the admission |
| `28-precis-watch.yml` (`precis ingest --watch --subprocess-concurrency N`; `ExecStartPre docker rm -f surya-vllm-*`) → `cli/watch.py::_run_in_process_group` (Popen, own session) + `ThreadPoolExecutor(len(shards))` | ingest batch children (marker/surya ~19 GB) | watch host | 3 (heavy, GPU) | **no** | `precis_watch_subprocess_concurrency` | route each batch child through `admit()` |
| `27-extract-watch.yml` (`acatome-extract watch` launchd + kickstart cron) | external PDF extractor | macOS extract host | 3 | no | periodic kickstart for memory | **unsure** (external binary); wrap or declare as a reserve |
| `31` asa-bot / `48` asa-slack (`asa_bot/claude_invoke.py` chain executor `max_workers=4`; `precis_client.py` spawns a precis MCP stdio child) | claude per chat turn | GW | 2 | no | 4 chain threads | leave (interactive); optional account slot |
| `14-mcps.yml` code-sandbox (plist/service) | code-sandbox MCP | mcps hosts | 3 | no | — | **unsure** what it executes; inspect before deciding |
| `deploy/mcp-http/precis-mcp-http-ensure.sh` → `mcp_supervisor.py` (server child), prepare | session MCP server | DEV, GW | 3 | n/a | `PRECIS_MCP_TOOL_CONCURRENCY` | leave (request path) |
| `38` precis-web → `precis_web/export_jobs.py` build threads (latexmk/docx) | draft exports | web host | 3 | no | `_BUILD_SLOTS` | leave (request path, bounded); **unsure** |
| `roles/backups` cron (`pg_backup.sh`, `pg_backup_dir.sh`, `restic-dbdump.sh`, `b2_sync.sh`, `usb_backup.sh`, `restore_test.sh`, `drill_pull.sh`) | DB dumps / sync / drill | DATA | 3 | no | `scripts/lib/backup-window.sh` window | leave; declare as a timed host reserve |
| `roles/hardening`: earlyoom, `memsnap.timer`, `os-update-weekly.timer`, cron (`tasks/main.yml`) | OOM guard / snapshots / updates | Linux hosts | 3p | n/a | earlyoom thresholds | leave (safety) |
| `roles/monitoring` cron (`linux.yml:36/59`), `api_monitors` cron (`pip_audit.sh`), `config_pull` cron, `logrotate` cron, `nfs-keepalive.timer`, `precis-embedder-watchdog.timer`, `com.precis.colima.plist`, `com.precis.colima-docker-fs.plist` | maintenance | various | 3p | n/a | — | leave |

### 1g. Dev/slice-host scripts (bash)

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `scripts/test` main path (`gate_slot_acquire` → `compose up precis-test-db` → `run_pytest` `compose run precis-dev`) | pytest container + RAM test DB | DEV | 3 heavy | gate-slot | `PRECIS_GATE_SLOTS` / `gate_slot_default`, `PRECIS_GATE_WAIT_MAX_S`, 24g `mem_limit` | route (§Ruled design CLI) |
| `scripts/test --typecheck` (–882, `compose run … mypy`) | mypy container | DEV | 3 | **no** (skips the slot by design) | retry once on 137 | route (gr601479 suspect) |
| `scripts/test --fast` / `--collect-only` (skip) | pytest container, no DB | DEV | 3 | **no** | — | route (small footprint) |
| `scripts/ship::setup_gate_infra` (`compose up -d --wait precis-test-db`, `precis-gate`) | warm gate + test DB, resident between ships | DEV | 3 | **no** (up before and after the slot) | 24g `mem_limit` (`docker/dev/compose.yaml`) | count residency as a reservation held by the worktree (§Done-check) |
| `scripts/ship::run_gate` pre-qland lint ("no gate slot": ruff + mypy-changed; full `mypy src tests` + lint-imports + hygiene on `--checked`/`--release`) | lint/mypy in the gate | DEV | 3 | **no** | — | route (gr601479 suspect) |
| `scripts/ship::run_gate --slot` (`--checked` selection), full / impacted gate, hybrid `--remote` fallback | pytest gate | DEV | 3 heavy | gate-slot | `PRECIS_GATE_N` (xdist) | route |
| `scripts/ship::_remote_gate_admit` (`PRECIS_REMOTE_GATE_SLOTS`=2) + `gh run …` | GitHub check.yml runs | REMOTE | 3 | own cap (GitHub runner budget) | 2 ci/* runs | leave (not host load; out of scope) |
| `scripts/mutate-diff` (`gate_slot_acquire` → `compose run`) | mutation runs | DEV | 3 heavy | gate-slot | — | route |
| `scripts/nightly` → `scripts/test` (+ isolated re-run of suspects) | full suite | DEV | 3 heavy | gate-slot | — | route (inherits); **unsure** how it is scheduled |
| `scripts/dev`, `scripts/db` (`compose run precis-dev`) | ad-hoc dev container | DEV | 3 | no | — | leave (interactive), but count it in the live check |
| `scripts/precis-index`, `precis-embed`, `precis-summarize`, `precis-add`, `precis-watch` (`docker compose run precis-cli …`) | local index/embed/summarize/ingest | DEV | 3 (embed/summarize heavy) | no | — | route the heavy three; leave `precis-add` |
| `scripts/mcp-loadtest` (`compose up` + `compose run`) | load test | DEV | 3 heavy | no | `PRECIS_MCP_TOOL_CONCURRENCY` | route |
| `scripts/build-image`, `build-all`, `build-tblite-wheel`, `wheel-smoke`, `geom-wheel-smoke`, `guide-web`, `guide-capture`, `precis-shell` | docker build/run | DEV | 3 heavy (builds) | no | — | route the builds; leave `precis-shell` (interactive) |
| `scripts/review-paper/run.sh`, `context-audit/run.sh`, `exercise-mcp/run.sh` | docker + `claude` harnesses | DEV | 2/3 | no | — | leave (manual); **unsure** of their frequency |
| `scripts/skill-eval` → `src/precis/skill_eval/runners.py` (`claude -p`, budgeted) | Claude CLI | DEV | 2 | no | budget | leave (advisory); optional account slot |
| `scripts/fixer-tick` (launchd) → `src/precis/fixer/tick.py` (git, claude build, `scripts/ship`, `scripts/deploy`) | laptop fixer loop | DEV (laptop) | 2/3 | its gate inherits gate-slot | lockfile = 1 tick (serialisation, not capacity) | leave the lock; gate covered by ship |
| `scripts/fleet-wave`, `scripts/fleet`, `scripts/agent-start` → `claude-tree`/`codex-tree`/`kimi-tree`/`devin-tree`, `scripts/codex-fleet-session`, `scripts/fleet-remote` (ssh tmux) | interactive builder CLIs in tmux | DEV / remote fleet host | 2 (+3 via their gates) | no | human-sized waves; `--kimi-max` | leave the CLI (light); their gates are admitted via scripts/test|ship |
| `scripts/hooks/session-end-reap.sh`, `scripts/reap-test-dbs`, `reap-idle-gates`, `reap-worktrees` (`compose down -v`) | teardown | DEV | 3p | n/a | idle thresholds | leave |
| `scripts/deploy`, `scripts/host-ops/*` (play, os-update `docker run nvidia-smi`, nfs-recover, vllm-stop), `scripts/release`, `scripts/round` | ops | DEV → cluster | 3 | no | deploy lock | leave (operator) |
| `.github/workflows/*.yml` (all `ubuntu-latest`/matrix, GitHub-hosted) | CI | REMOTE | 3 | n/a | — | leave (no self-hosted runners) |

### 1h. Request-path and library launch sites (MCP server / CLI)

| Launch site | Starts | Host(s) | Class | Admission? | Own cap | Recommendation |
|---|---|---|---|---|---|---|
| `handlers/_python_runtrace.py` (`python -P runner --entry …`) | traced user code | MCP | 3 | no | `max_events`, timeout | **unsure**: could be heavy; route if it runs user tests |
| `handlers/_python_write.py` (ruff), `_python_impact.py`, `_python_provenance.py`, `job.py`, `_memory_shape.py`, `skill.py`, `graph/garden/_anchors.py` (git) | probes | MCP | 3p | n/a | timeouts | leave |
| `handlers/web.py` → `ingest/marker.py::extract_pdf_text` (spawn child) | OCR inside a request | MCP | 3 heavy | **no** | — | route: mint a job instead of in-request OCR |
| `handlers/structure.py` / `precis_se/atomic/relax_block.py` / `precis_se/properties/surface.py` → `structure/relax.py` → `structure/_xtb_isolated.py` (spawn xtb child) | xtb polish in a request | MCP | 1 (light) | no | — | **unsure** of the in-request vs job split; route if more than seconds |
| `render/sandbox.py` (`python -I` figure code with rlimits) | untrusted render | MCP / workers | 3 | no | rlimits (CPU s, AS, NPROC, fsize) | leave (bounded request path); keep rlimits as hard caps |
| `export/compile.py`, `utils/compile_guard.py` (latexmk), `utils/tex_llm_fix.py` (chktex), `ingest/pdf_metadata.py` (exiftool), `export/remarkable.py` (rmapi), `tts/encode.py` (ffmpeg) | tool binaries | MCP / workers | 3 | no | timeouts | leave on the request path; route when called from a job (parent footprint) |
| `llm_eval/scorers.py` (`python -I` candidate code), `mesh_eval/scorers.py` | eval harness | DEV | 3 | no | timeout | leave (manual) |
| `store/schema_dump.py` (pg_dump), `sim/verify.py`, `sim/ingest.py`, `bench/runner.py`, `skill_eval/__main__.py`, `cli/memory_recall_set.py`, `install_watchdog.py`, `precis_web/routes/env.py` (plutil) | dev tooling / probes | DEV / MCP | 3p | n/a | — | leave |
| `precis_pathway/replay.py` | external replay harness (bundle re-run) | wherever run | 1 | no | — | leave (manual acceptance tool); **unsure** if any worker calls it |
| `src/precis/remote/{ssh,slurm,credentials,_runtime_check}.py` (Popen ssh, ssh-agent) | remote batch library | — | 1 | n/a | — | leave: **unwired** (no importers outside the package); route when wired |
| `dispatch.py` background threads (`checklist_sync`, `oracle_sync`) | boot-time re-ingest | MCP / CLI | 3 | no | advisory lock | leave |

## Home-made caps to retire

Python, claim path:
- `src/precis/workers/executors/_common.py::_MAX_RUNNING_DEFAULTS` +
  `job_type_running_caps` (`claim.max_running.*`)
- `_common.py::_EXECUTOR_CAP_DEFAULTS` / `_EXECUTOR_RESERVE_DEFAULTS` +
  `executor_lane_limits` (`claim.executor_cap.*`, `claim.reserve.*`)
- `_common.py::_mem_pressured_hosts`: soft veto, superseded by `mem_mb`
  admission

Python, executors and passes:
- `executors/claude_docker.py::_concurrency` (`PRECIS_SANDBOX_CONCURRENCY`) and
  `::_load_ok` (a duplicate of `load_gate`)
- `executors/claude_inproc.py::_inproc_concurrency`
  (`PRECIS_INPROC_CONCURRENCY`)
- `executors/devin_api.py::_concurrency` (`PRECIS_DEVIN_CONCURRENCY`) →
  `account:devin`
- `src/precis/utils/load_gate.py::skip_if_high_load` (callers
  `workers/dream_agent.py`, `workers/review.py`)
- `workers/materialize.py::_SMALL_DRAIN_CONCURRENCY_ENV`
  (`PRECIS_SMALL_DRAIN_CONCURRENCY` override; `derived_drain._derived_concurrency`
  stays)
- `workers/llm_summarize.py` `PRECIS_SUMMARIZE_CONCURRENCY` /
  `_MAX_CONCURRENCY`; `workers/classify.py` `PRECIS_CLASSIFY_MAX_CONCURRENCY` +
  `cli/_worker_passes.py::_classify_pass` `PRECIS_CLASSIFY_CONCURRENCY` +
  `service_config.concurrency` (mig. 0091, keep the column, stop reading it)
- `workers/capability_probe.py::_probe_podman` / `_probe_tts` /
  `_probe_embedder` fixed `PRECIS_*_SLOTS` counts → measured capacity
- `job_types/land_slice.py::land_gate_slots` (`PRECIS_LAND_GATE_SLOTS`) and
  `DEFAULT_TEST_NPROC` / `test_nproc` (xdist width becomes a footprint size
  parameter)
- `plans/lanes.py` `Lane.cap` (from `llm_account.concurrency`) +
  `live_lane_counts`: moves into the `account:<lane>` resource, not deleted
  outright (vendor concurrency is real)
- `cli/worker.py` `--batch-size` / `ssh_node` `limit=1` / `job_inproc`
  `limit=1`: claim width becomes "while it fits"

Bash:
- `scripts/lib/gate-slot.sh::gate_slot_acquire` / `gate_slot_default` /
  `gate_slot_release` (`PRECIS_GATE_SLOTS`, `PRECIS_GATE_WAIT_MAX_S`) → the
  admission CLI. Keep `gate_oom_note`, the ticket fairness (port it) and
  `lock-holder.sh` reclaim.

Deploy:
- lane units `20d` (`precis_worker_drain_lanes`), `20e`, `20g-slices`,
  `20g-kwdrain` (shards), `20h`, `35`, `43`, `20f`, plus `20`/`37` rollbacks
- `roles/hardening/defaults/main.yml` `precis_lane_memory` per-lane
  `memory_high/max` + `container_reserve`: replaced by per-job caps; keep
  `host_memory_os_reserve`, `host_llm_lanes`, `host_lane_exclusive_groups` as
  capacity inputs
- `28-precis-watch.yml` `precis_watch_subprocess_concurrency`

Keep as hard safety limits, but size them from the footprint:
- `precis_pathway/runner.py::_apply_memory_cap` (`PRECIS_AUTOCATPATH_MEM_LIMIT_GB`)
- `claude_docker._cgroup_caps`
- `docker/dev/compose.yaml` `mem_limit: 24g`
- `render/sandbox.py` rlimits
- `utils/container_limits.py` cpuset/shares (`PRECIS_JOB_CPUSET`, the system
  core fence)

Leave (not host load):
- `scripts/ship::_remote_gate_admit` (`PRECIS_REMOTE_GATE_SLOTS`, GitHub
  runners)
- `PRECIS_MCP_TOOL_CONCURRENCY`
- `precis_web/export_jobs.py::_BUILD_SLOTS` (request path; revisit)
- `workers/slice_stacks.py` `CAP_DEFAULT` (eviction hygiene, but count idle
  stacks in the live check)

## Done-check

### Static: Python, `tests/test_launch_admission_ratchet.py` (S7a, gr647996; landed 6838e03d2)

AST walk over `src/` and `scripts/` python for launch callees (`subprocess.*`,
`MeasuredPopen`, `asyncio.create_subprocess_*`, `os.system/popen/fork/spawn*/exec*`,
`pty.spawn`, `multiprocessing` `.Process`, `ProcessPoolExecutor`, and a
docker/podman/ssh argv literal in a scope that makes none of those calls).

- Not a site: a literal probe binary (`PROBES`; sub-minute, bounded, no
  compute footprint) or a docker/podman probe verb; a call in a function that
  calls `admission.admit`/`admission.spawn`; anything inside the admission
  module.
- Baseline `tests/data/launch_admission_baseline.txt`, function-level keys
  (`path::qualname count`), shrink-only: fails when a key appears or a count
  rises, and when a key vanishes or a count falls without the baseline
  shrinking. Regenerate with `--regen` (refuses growth without
  `--allow-growth`).
- Not yet in the test: the second assertion, that `Popen(…,
  start_new_session=True)` appears only inside `admission.spawn` (detached
  launches are the leak class). It needs S4 to exist.

### Static: bash, `tests/test_bash_launch_admission.py` (S7a, landed 6838e03d2)

- Scans bash/sh files under `scripts/` for `docker|podman run|create|start|exec|build`,
  `docker compose … up|run|exec|create|start|build` (and a local `compose`
  wrapper), `ssh <host>`, and backgrounded jobs (`&`, `nohup`, `setsid`).
  Teardown and inspection verbs never match.
- A hit needs `admit_acquire` in command position earlier in the same function
  (or top-level flow), or `# admission: exempt — <reason>` on or above the line.
- Baseline `tests/data/bash_launch_admission_baseline.txt`
  (`path::function count`), shrink-only, same rules.
- Not yet covered (deploy ratchets, open): the count of
  `precis worker … --only` units in `deploy/playbooks/` may only shrink, and
  occurrences of `PRECIS_[A-Z_]*(CONCURRENCY|SLOTS|MAX_RUNNING|LANES)` across
  `src`/`scripts`/`deploy` may only shrink. Decide where they land (S7b or the
  slice that deletes each knob).

### Live check (S7b, gr647996): per host, every heartbeat

Extend `precis.workers.heartbeat` near the existing `ps -Ao pcpu=,comm=` call.

- Census: `ps -Ao pid,pgid,ppid,rss,pcpu,etimes,comm`; `docker ps --format`
  with labels and `docker stats --no-stream`; on gate hosts the Docker VM's
  containers.
- Match each process group or container above a threshold (> 512 MiB RSS, or
  > 50 % CPU for > 60 s) to an admitted holder: `resource_slot_holds`
  (`holder`, `holder_process`, `holder_boot_id`); running jobs on this
  `meta.lease_host` (`meta.compute_handle.pgid`, `meta.container`, launcher
  pid); or the host's declared standing daemons (systemd units, launchd
  labels).
- Write `host_heartbeat.meta`: `unadmitted = [{pgid|container, comm, rss_mb, cpu}]`,
  `admitted_mem_mb`, `used_mem_mb`.
- Nursery detector `unadmitted-load` fires on two consecutive beats and also
  reports gates that ran unadmitted under the outage fallback.
  `footprint-drift` (measured > admitted + margin) feeds the predictor.
  A per-job-type p95/p50 > 4 flag marks features that miss the size driver.
- On a dev host, `scripts/admit status` prints ledger holders against
  `docker ps` by compose project and flags any running container with no live
  holder: the "docker gate had no slot dir" signature.

## Slices

Each slice lands independently. Order: S1 → {S2, S5, S7a} → S3 → {S4, S6} →
S7b → S8; S2b after S2; the decision layer after S3. Handle for all labels:
gr647996.

### S1 (gr647996): `admit()` primitive and capacity rows

- Status: in review fixes; not on `main` (`src/precis/workers/admission.py`
  absent at `31ab345be`).
- Files: `src/precis/workers/admission.py` (new); `store/_resource_slots_ops.py`
  (multi-unit `mem_mb`/`cpu_milli`/`vram_mb` hard rows; the hold carries the
  footprint); `workers/capability_probe.py` and `workers/heartbeat.py`
  (publish measured capacity minus reserves); `utils/llm/local_serving.py::_reserve_one`
  and the `openai_tools.py` D2 hold re-pointed at `admit()` (behaviour
  identical). A migration only if the hold needs a `footprint jsonb` column.
- Acceptance tests: `tests/workers/test_admission.py` (new; admit/release,
  all-or-nothing, lease return of a dead holder), `tests/test_resource_slots.py`,
  `tests/test_local_serving.py`, `tests/utils/test_outbound_policy.py`,
  `tests/test_capability_probe.py`, `tests/test_heartbeat_pass.py`.
- Deps: none.

### S2 (gr647996): outcome rows, features and a p95 first predictor

- Status: not started.
- Files: `utils/job_footprint.py` (outcome row per run with the metrics
  fields; feature extraction from params and, at mint, from text; a
  `footprint_for(job_type, params)` returning a job_type p95 plus margin, with
  the static cold-start table); `handlers/job.py` put path and
  `workers/_mint.py` stamp `meta.footprint_req` (gr595539);
  `executors/_common.py::effective_requires` merges it. Censored (cap-killed)
  rows are excluded from the fit.
- Acceptance tests: `tests/workers/test_job_footprint.py`,
  `tests/workers/test_materialize.py`, `tests/test_reserve_at_claim.py`; new
  tests for the feature extractor (numeric/length/categorical/text features,
  no declared size key) and the cold-start path (< 20 runs → conservative
  default).
- Deps: S1 (vocabulary only).

### S2b (gr647996): boosted-tree fit

- Status: not started; waits for a few weeks of S2 rows.
- Files: the predictor fit and nightly refit job; `HistGradientBoostingRegressor`
  quantile fit per target (new core dependency in `pyproject.toml`);
  recency weighting (half-life about 2 weeks); machine-type key and per-host
  speed factor.
- Acceptance tests (new): the fit reproduces a synthetic size-driven
  footprint at p95 on held-out rows; a new machine type falls back to the
  pooled × speed-factor estimate; censored rows do not lower the estimate.
- Deps: S2.

### S3 (gr647996): claim path admits by footprint; claim while it fits

- Status: not started. (gr595542 has since landed the first, narrow
  pull-side admission on the same ledger — GPU `vram_mb` per
  `model_footprint` row and `ssh_node` RSS as `mem_mb`, plus the
  model-affinity sort key; see `cluster-scheduling.md` Pillar 3b. S3's
  claim-path rework onto `admission.admit()` supersedes that wiring —
  hold rows with renewal replace the hold-less reserve — and broadens
  RSS admission beyond the science executor.)
- Files: `executors/_common.py` (`_finish_claim` → `admit`; fit-then-deficit
  per gr595541; lease renewal before long jobs admit; delete
  `_MAX_RUNNING_DEFAULTS`, `_EXECUTOR_CAP_DEFAULTS`, `_EXECUTOR_RESERVE_DEFAULTS`,
  `_mem_pressured_hosts`); `job_inproc.py`, `ssh_node.py`, `claude_inproc.py`,
  `claude_docker.py`, `devin_api.py` (drop `limit=1`, `_concurrency`,
  `_load_ok`, `_inproc_concurrency`); the CLI chokepoint
  `utils/_claude_subprocess.py` and `utils/codex_exec.py` takes
  `account:<lane>`. The node agent implements the admission loop
  (§Ruled design): 5 s sampling, 60 s fallback claim timer, anti-starvation.
- Acceptance tests: `tests/workers/test_claim_fairness.py` (drop the
  playbook-relation pin), `tests/test_claim_ordering.py`,
  `tests/test_reserve_at_claim.py`, `tests/test_placement_gate.py`,
  `tests/test_ssh_node_executor.py`, `tests/test_inproc_concurrency.py`,
  `tests/test_job_inproc_process_gate.py`, `tests/test_suspended_job_types.py`.
- Deps: S1, S2.

### S4 (gr647996): one detached `spawn()` with hard caps

- Status: not started. `spawn()` was deferred from S1 and lands here.
- Files: `admission.spawn`; re-point `job_types/run_slice.py::_launch`
  (land_slice, dep_check, lane_health, boundary_probe, release_* follow),
  `precis_pathway/runner.py::_spawn_detached`, `_run_request_subprocess` and
  the kinetics runners, `workers/child_compute.py::run_in_child`,
  `struct_relax._default_runner`, `precis_chem` and `precis_bio`
  `_default_runner` (add `--memory` from the footprint). Kinetics, coverage
  and revalidate stop minting `requires={}` and get a CPU and memory
  footprint. A cap kill writes `killed-for-memory` and requeues bigger
  (gr595537, gr595538).
- Acceptance tests: `tests/test_land_slice.py`, `tests/test_run_slice.py`,
  `tests/test_lane_health.py`, `tests/test_boundary_probe.py`,
  `tests/test_quest_compute.py`, `tests/test_child_compute.py`,
  `tests/test_pathway_cpuset.py`, `tests/test_ssh_node_executor.py`; the
  second assertion of the Python ratchet (`start_new_session=True` only inside
  `admission.spawn`); a new test that `spawn` refuses without a live
  `Admission`.
- Deps: S1 (S3 for the token handoff).

### S5 (gr647996): dev and slice-host gates on the admission CLI

- Status: not started. Harness diff: lands with `--remote`.
- Files: `scripts/lib/gate-slot.sh` → `scripts/lib/admit.sh` (thin wrapper
  over `python -m precis.workers.admission`); `scripts/test` (cover
  `--typecheck`, `--fast`, `--collect-only`); `scripts/ship` (pre-qland lint
  and `setup_gate_infra` residency take a hold); `scripts/mutate-diff`;
  `job_types/boundary_probe.py`; `job_types/run_slice.py::_tests_green_on_main`;
  `land_slice.py` stops exporting `PRECIS_GATE_SLOTS`.
- Behaviour (gr647996 comment 5): the CLI admits through its own DB
  connection; on an unreachable DB it takes one host-local flock, logs
  "ran unadmitted" and runs. Keep `gate_oom_note`, the ticket fairness and
  `lock-holder.sh` reclaim.
- Acceptance tests: `tests/test_gate_slot_fairness.py`,
  `tests/test_gate_slot_wait.py`, `tests/test_lock_holder_reclaim.py`,
  `tests/test_gate_mem_limit.py`, `tests/test_scripts_test_serial.py` and the
  other `tests/test_scripts_test_*.py`, `tests/test_land_slice.py`;
  `tests/test_bash_launch_admission.py` baseline shrinks as scripts call
  `admit_acquire`; new tests for the DB-down fallback (flock held, one gate at
  a time, unadmitted log line) and that a failed precis write does not stop a land (follow-up 1).
- Deps: S1 (interface only); independent of S2–S4.
- Open questions for whoever specs S5 are in §Open questions.

### S6 (gr647996): passes, cadences and the watch daemon

- Status: not started.
- Files: `workers/review.py`, `workers/dream_agent.py` (drop
  `skip_if_high_load`; hold `account:` and memory); delete `utils/load_gate.py`;
  `cli/watch.py::_run_in_process_group` per-batch `admit`; `ingest/marker.py`
  callers; `workers/chunk_keywords.py`, `workers/clusterize.py`,
  `workers/llm_summarize.py`, `workers/classify.py` (pool width from granted
  slots).
- Acceptance tests: `tests/test_load_gate.py` (deleted with the module),
  `tests/test_deep_review.py`, `tests/test_dream_agent.py`,
  `tests/test_llm_summarize.py`, `tests/test_cli_classify.py`,
  `tests/test_watch.py`, `tests/workers/test_derived_drain.py`.
- Deps: S1.

### S7a (gr647996): static ratchets

- Status: landed as 6838e03d2: `tests/test_launch_admission_ratchet.py` with
  `tests/data/launch_admission_baseline.txt`, and
  `tests/test_bash_launch_admission.py` with
  `tests/data/bash_launch_admission_baseline.txt`. Both fail on growth today
  and on a baseline that did not shrink. Remaining under S7a: the
  `start_new_session` assertion (needs S4) and the two deploy ratchets
  (§Done-check).

### S7b (gr647996): live census and detectors

- Status: not started.
- Files: `workers/heartbeat.py` census, nursery `unadmitted-load` and
  `footprint-drift` detectors, `scripts/admit status`.
- Acceptance tests: `tests/test_heartbeat_pass.py`,
  `tests/workers/test_health_digest.py`, plus tests for the census-to-holder
  match, the two-consecutive-beats rule and the p95/p50 > 4 flag (new).
- Deps: S1, S5.

### S8 (gr647996): retire the lane units (node agent)

- Status: not started.
- Files: `deploy/playbooks/` 20d, 20e, 20f, 20g-*, 20h, 35, 43, 20, 37; 20b
  renders the one node-agent unit per host; `roles/hardening/defaults/main.yml`
  drops per-lane caps and keeps reserves; `retire-worker-lanes.yml` extended.
- Acceptance tests: `tests/test_deploy_memory_budget.py`,
  `tests/test_heartbeat_worker_units.py`, playbook tests; the deploy ratchets
  report fewer `--only` units.
- Deps: S3, S4, S6 live and quiet on `unadmitted-load` for a soak window.

### Decision layer (gr647996, later slice)

- Status: not started; after S3. Joint model-and-placement choice per
  §Ruled design. Acceptance tests: new, against the expected-cost rule on a
  fixed table of (host, model) options.

## Explicitly NOT in scope

- Cross-machine bin-packing before measurements show packing is the
  bottleneck (`cluster-scheduling.md` Pillar 3b).
- The LLM endpoint slot count and class lanes (`llm-lane-scheduler.md`); the
  `llm:`/`llm-api:` holds stay as they are and become `admit()` callers.
- Rate buckets and paid-API concurrency (`utils/outbound_policy.py`,
  `utils/rate_limit.py`): admitted by the bucket, not a slot.
- GitHub-hosted runner budgets: `scripts/ship::_remote_gate_admit`
  (`PRECIS_REMOTE_GATE_SLOTS`), CI workflows.
- A host-local ledger backend for gates (ruled out, comment 5).
- Request-path caps: `PRECIS_MCP_TOOL_CONCURRENCY`,
  `precis_web/export_jobs.py::_BUILD_SLOTS`, `render/sandbox.py` rlimits
  (kept as hard caps).
- Interactive builder CLIs in tmux, laptop launchd loops, and external
  binaries (the extract-watch host): left alone or declared as reserves.

## Acceptance criteria

- No launch site outside the shrink-only baselines: both S7a tests pass with
  the baselines at the §Launch-site table's "leave" rows only (probes,
  teardown, request path, dev-manual).
- A job minted after S2 carries `meta.footprint_req`; a claim reserves
  `mem_mb`, `cpu_milli` (and `vram_mb`, `gpu`, `account:` where declared) in
  the same transaction as the claim; a dead holder's capacity returns.
- On a gate host with the DB reachable, every gate path in `scripts/test`,
  `scripts/ship` and `scripts/mutate-diff` (including `--typecheck`, `--fast`,
  `--collect-only` and the pre-qland lint) holds a slot; with the DB
  unreachable one gate runs at a time, unadmitted, logged, and a land is never
  blocked.
- Every detached launch goes through `admission.spawn` with a hard memory
  cap; a cap kill is recorded as `killed-for-memory` and requeues bigger.
- No `PRECIS_*_CONCURRENCY`/`_SLOTS` knob, per-executor cap table, lane unit
  or `utils/load_gate.py` remains from §Home-made caps to retire (hard safety
  limits and the "leave" list excepted).
- `host_heartbeat.meta.unadmitted` is empty on a quiet host for the S8 soak
  window; a deliberately launched unadmitted container fires
  `unadmitted-load` within two beats.

## Target + blast radius

Workers (claim path, executors, passes, cadences, heartbeat, nursery),
`store/_resource_slots_ops.py`, `utils/job_footprint.py` and the mint doors,
`utils/_claude_subprocess.py`/`codex_exec.py`, `scripts/test`/`ship`/
`mutate-diff` and `scripts/lib/*` (harness diff: `--remote`), `deploy/`
playbooks and `roles/hardening`, `pyproject.toml` (scikit-learn). A wrong
capacity row or footprint stalls claims on a host.

## Open questions / decisions log

Decided (Reto, 2026-11-12): gate admission model and DB-outage fallback
(gr647996 comment 5); admission loop, metrics and predictor (comment 7).

Open, for whoever specs S5 (gr647996 comment 5 follow-ups):

1. Does `scripts/ship` or the land step write to precis (for example the
   `resolved: <sha>` comment)? If so, make that write best-effort and queue
   it so a DB outage cannot stop a land.
2. What does a worker do with a running job when it loses its DB connection?
   The answer decides how much of the partial-outage case is real.

Open, other:

- Where the two deploy ratchets (unit count, env-var count) land: S7a, S7b or
  the slice that deletes each knob.
- Whether the OCR child behind in-request `handlers/web.py` becomes a minted
  job (the table says route: mint a job).

## Unverified

- The gr601479 mechanism is inferred from code paths.
- Whether `briefing_audio`/`cast_audio` `requires={'tts'}` reserves or only
  gates.
- The compute shape of `struct_neb`, `struct_md`, `struct_search` and
  `pocket_realize` (in-thread MLIP vs container).
- How heavy and how frequent the in-request compute in `handlers/structure.py`,
  `precis_se` property evaluation and `handlers/_python_runtrace.py` is.
- What the `14-mcps` code-sandbox executes; the extract-watch unit is an
  external binary.
- How `scripts/nightly` is scheduled (no launchd/cron definition in the repo).
- `precis_pathway/replay.py` callers; `precis.remote` is unwired today.
- Live lane enablement per host (`service_config` rows,
  `precis_worker_kwdrain_enabled`, `agent_sandbox_lane` membership): code
  defaults only.

## Relationship to other specs

- `cluster-scheduling.md` Pillar 3b owns the footprint-admission frame and
  node-agent reading this spec implements; the claim key and mint-time matrix
  are `job-scheduling-fairness.md`; LLM endpoint slots are
  `llm-lane-scheduler.md`. Where this spec and those differ (size key,
  `max_running` caps, CPU headroom rule), this spec's rulings of gr647996 win.
