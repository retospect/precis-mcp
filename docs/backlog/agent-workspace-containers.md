---
status: draft
prio: high
title: Agent workspace containers — persistent workspaces, disposable containers, git in/out
model: opus
---

# Agent workspace containers — coder agents in containers for sims & co

Reto, 2026-09-06: "reliably run coder agents in containers for simulations
and whatnot. I am not sure that is set up right, and multiple agents may
want to over time share the same container, and we want git and stuff."

## Current state (the "is it set up right?" answer)

The substrate exists and its trust model is good, but almost none of it is
live, and nothing in it persists between runs:

- **`sandbox_run`** (job_type + `claude_docker` executor): throwaway
  container per job, `/work` volume as the whole IN/OUT bus, container has
  NO DB creds (read-only token'd MCP callback at most), melchior
  hard-excluded, cgroup-capped. Ships **dark** — registered only under
  `PRECIS_SANDBOX_ENABLED`; no host has it on.
- **`agent_container`** (containerized plan_tick): shipped, but the
  capability probe + unhealthy-latch that make the flip safe are NOT
  deployed (`agent-container-capability-probe.md`) — flipping
  `precis_agent_container_enabled` before deploying them is the known trap.
- **sim-harness slice 3** (per-sim pinned images, `mode:run`, harvest) is
  blocked on the `sandbox_run` `mode:run` + harvest slices.
- Nothing survives a run: no persistent workspace, no git identity or
  remote wiring inside the container, each job re-stages from scratch.

So: not wrong, but dark + stateless. "Reliably" needs the enable path
walked in order, and "share over time + git" needs a persistence layer the
current design deliberately lacks.

## Design stance: persistent WORKSPACES, disposable CONTAINERS

Sharing a long-lived *container* across agents is the wrong durable unit:
a crashed/OOM'd container takes the shared state with it, upgrades mean
draining tenants, and accumulated scribbles from one prompt-injected run
persist into the next agent's context. Invert it:

- **Workspace = the pet.** One named volume (or `agent_sandbox`-owned host
  dir) per project/sim: `precis-ws-<slug>`, holding the git clone, uv
  cache, sim outputs. Survives every container.
- **Container = cattle.** One per job, mounts the workspace, dies after.
  "Multiple agents share the same container over time" becomes "multiple
  jobs mount the same workspace over time" — same continuity, none of the
  shared-runtime fragility. Crash recovery = re-mint the job.
- **Serial by default, leases for overlap.** One live job per workspace,
  executor-enforced via the same lease shape the todo tree just got
  (`claimed-by:` + `expires_at`, shipped 49b49c0c): claim
  `workspace:<slug>` before mount, release on terminal job status, expiry
  frees a dead claimer's workspace. Concurrent access, when actually
  needed, = **git worktrees inside the volume** (one per job — the same
  discipline this repo uses for its own parallel sessions), not shared
  mutable checkouts.

## Git in/out (the trust boundary stays where it is)

The container never holds push creds (same reasoning as no-DB-creds):

- **In:** the trusted executor (deploy-side) clones/fetches into the
  workspace before launch — creds live only outside.
- **Inside:** the agent commits freely as a noreply bot identity on a work
  branch (`agent/<job-id>` or `precis-verify/<date>` per the sim-harness
  rule). Committing needs no network; git identity is baked into the image
  (`user.name=precis-agent`, `user.email=noreply`).
- **Out:** harvest (already the contract: "text → git, trusted-side push")
  pushes the work branch from the trusted side and records provenance.
  Never the default branch — review is the merge.

## Slices (in enable-order; each independently shippable)

1. **Light one host (castor).** 2026-09-07: the whole mechanical chain
   is DEPLOYED AND VERIFIED on castor — inventory group flipped
   (balthazar out: macOS/no-podman, never ran; stale service rows
   balthazar+spark prio→0), `PRECIS_SANDBOX_HOSTS`+`PRECIS_NODE` env,
   `playbooks/34-code-task-image.yml` (Dockerfile-hash staleness key,
   mirror.gcr.io fallback, bounded build; castor's container egress
   blackholes deb.nodesource.com — UniFi-DPI family — so the image is
   controller-built and hand-carried per the play's header recipe),
   `playbooks/35-precis-worker-sandbox.yml` (the `--only
   job_claude_docker` lane; castor's system worker is heartbeat-only),
   the `code-task-run` wrapper + non-root `sandbox` user in the image,
   executor /work chmod for the mapped uid, and `job_claude_docker`
   added to the worker `--only` argparse choices (3rd instance of that
   drift). Smoke jobs 329238/329257: dispatch→allowlist→claim→podman
   launch→wrapper→claude→reap all work. **Blocked on gr329258**: the
   vault CLAUDE_CODE_OAUTH_TOKEN is 401-rejected, so `claude -p` burns
   the run on auth and out/ stays empty. Exit once Reto re-mints the
   token (`claude setup-token` → vault): re-run the smoke (root
   td329234), see the harvest folder + kill-mid-run cleanliness.
   Residual: empty-result assertion for sandbox_run (out/ empty ∧
   short runtime ⇒ flag, not bare success) — noted in gr329258.
2. **Workspace volumes + git staging.** `params.workspace: <slug>` on
   `sandbox_run`; executor ensures the volume, executor-side
   clone/fetch before launch, harvest pushes the work branch. Exit: two
   sequential jobs on one slug see each other's commits.
3. **Workspace lease.** Executor takes/refreshes a lease on
   `workspace:<slug>` (expiry-stamped, released on terminal status); a
   second job on a leased workspace queues rather than launching. Exit:
   two concurrent mints on one slug serialize; a SIGKILL'd first job frees
   the workspace within the TTL.
4. **`mode:run` + pinned sim images on workspaces.** Unblocks sim-harness
   slice 3 (its acceptance criterion 3 becomes runnable as written); sim
   outputs land in the workspace and harvest into `PRECIS_ROOT/sim/<slug>/`.
5. **Concurrent jobs via in-volume git worktrees** — only when a real
   consumer needs parallel agents on one project; until then the lease's
   serialization IS the concurrency answer.

## Decisions (Reto, 2026-09-06)

- **S1 host:** castor (pollux keeps the untested fold lane; melchior/GPU
  never).
- **S2 workspace storage:** host dirs under `/var/lib/precis-sandbox/ws/`,
  owned by `agent_sandbox` (debuggable + NFS-shareable; not podman volumes).
- **S2 coder image:** minimal — base + python/uv + git. Per-job deps via uv
  into the workspace; per-sim pinned images unchanged.
- **S4 auto-mint:** sim quest watches MAY auto-mint `mode:run` jobs from the
  start — no soak gate. Blast radius is bounded by the quest anti-spin cap +
  the budget breaker, not by holding runs human-minted.

## Non-goals

- A long-lived shared daemon container with exec-attach (rejected above).
- Slurm/aws-batch backends — the `ComputeBackend` seam already planned in
  `sandbox_run` covers it later.
- GPU sims in sandbox containers (compute-lane jobs own GPU work).
