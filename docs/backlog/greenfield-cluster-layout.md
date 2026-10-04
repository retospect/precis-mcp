---
status: draft
title: Greenfield cluster layout — where each precis role would run if we set the cluster up again
pillar: local-compute
---

# Greenfield cluster layout

Reto, 2026-10-03: "if we greenfield it, where is db, where is webserv, where
is massive model, where is medium. small model, ml-potential on meluxina,
what all am i missing." This item is the target layout and the gap list. It
is not a migration runbook. Each move it implies gets its own item once
Reto rules on the layout. Owner to rank it: `threads/local-compute.md`.

## Why

Today one Mac does too much. melchior runs the fleet's coding sessions, the
gate containers in the colima VM, the shared MCP server, precis-web, the
agent lane, a worker and an embedder (`deploy/README.md` §Runtime topology).
Its 2026-10-02 kernel panic was colima file exhaustion from that mix. The DB
node is a macOS box that also serves NFS, and the NFS share hangs on the
Linux clients (`cluster-fileserver-move.md`). The rest is placement, not
code: the MCP server is GIL-bound at about 28 calls/s per process, and a
session-level SET through pgbouncer poisoned every agent_rw write.

## Target layout

Hardware we have: melchior (M2 Ultra, 192 GB unified), three DGX Sparks
(castor, pollux, spark; GB10, 128 GB unified each), caspar (macOS, the DB
today), the NAS, and the MeluXina allocation (HPC, Slurm, once access lands).

| Role | Where | Why there |
|---|---|---|
| **Postgres primary + pgbouncer** | a dedicated Linux box, NVMe, ECC; no workers, no NFS (keeps caspar's daemon-free rule) | the DB is the only stateful core; Linux takes the macOS launchd/TCC workarounds out of the DB's path |
| **Postgres streaming replica** | a second box (caspar is enough) | hot standby plus point-in-time recovery; read-only agent queries (`agent_ro`) go here, so a read-only guard can never reach the agent_rw pool |
| **Files: PDFs, artifacts, archive** | the NAS. Object storage (S3-style) for blobs; NFS only for what needs a filesystem | a hung NFS mount stalls every reader; an object GET fails and retries |
| **Web + MCP HTTP + Caddy + webhooks + Discord/Slack bridges** | a small Linux server (or VM), stateless, N MCP processes behind Caddy | the GIL ceiling is per process, so scale out by process count; keep it off the dev machine |
| **Big model** (local `llm.chain.big`) | melchior, as a dedicated model server: DeepSeek V4-Flash class (284B total, 13B active, about 142 GB at Q4) | only box with the memory for it alone; precis operations are batch, so slow prompt reading matters less, and prefix caching covers the repeated skill text |
| **Medium model** (tuned operations) | one Spark, vLLM, a ~35B MoE (e.g. Qwen3.5-35B-A3B) with per-operation LoRA adapters | vLLM batching gives high aggregate throughput for many concurrent ops |
| **Small model + embeddings + PDF extraction** | one Spark: embedder (bge-m3), Marker extraction, small model | the fleet-wide embedder on spark is already ruled (local-compute 10-02); extraction needs a GPU too |
| **Frontier / judgment + coding sessions** | cloud Claude (Opus and Sonnet), via the router | claim fidelity, design review and coding sessions stay on the strongest model |
| **ML potentials (MACE, xTB) + DFT (GPAW, MPI)** | MeluXina, via a Slurm batch runner (stage inputs, sbatch, poll, fetch) | batch, parallel, GPU and multi-node; not the slullama tunnel, which is for serving |
| **Local science fallback** | one Spark (pollux today): controls, urgent single relaxes, first-completion tests | MeluXina queue waits; a local control run keeps a verdict honest |
| **Fine-tuning** | Sparks overnight (QLoRA ~35B); rent a GPU for anything larger | training competes with serving, so schedule it off-peak |
| **Workers** | each next to its resource: GPU lanes on Sparks, agent lane on the serving box, none on the DB | a worker away from its resource is a network hop per job |
| **Agent lane** (`job_claude_inproc`, reviewers, dream) | the serving box, or a small dedicated runner | needs OAuth and the MCP config; not on the dev machine |
| **Dev: fleet sessions, worktrees, local gate** | a dev workstation that runs no prod service | a test run or VM blow-up can no longer take prod down; CI stays on GitHub |
| **Monitoring + alerting** | the serving box or the NAS, plus an external dead-man check | the monitor must not die with what it watches |
| **Backups** | NAS, plus encrypted offsite; the restore drill stays | the NAS alone is one site |

## What the question left out

- **Embedder and PDF extraction.** GPU services every ingest depends on.
- **The DB replica and the read-only path.**
- **Object storage vs NFS.** The NFS hang is today's worst availability bug.
- **Process count for the MCP server.**
- **Dev/prod separation.** Today they share one Mac.
- **The agent lane.** It has its own constraints: OAuth, the MCP config.
- **Monitoring that survives its host, and offsite backup.**
- **Network.**
  - intra-cluster DB and file traffic on the LAN, Tailscale for remote (the Spark NFS hang is suspected asymmetric routing at Tailscale's 1280 MTU);
  - one secrets vault (exists);
  - UPS and power.
- **Integrations**, which need a home: reMarkable, anki-sync, papers-sync, TTS, nanopub/OTS publishing, alphafold, aizynth, autocatpath, alchemi.
- **The MeluXina batch runner and data staging.** No item exists yet. DFT, MACE and xTB all need it.
- **Model specs in the graph.** The `measures` consumer, per `measures-substrate.md`.

## Open questions for Reto

1. A new Linux DB box, or keep Postgres on caspar and add a replica?
2. A dev workstation separate from melchior, if melchior becomes the model server?
3. Object storage on the NAS (MinIO or similar), or only fix NFS (`cluster-fileserver-move.md`)?
4. How MeluXina shows in the local-vs-cloud share (`docs/roadmap.md`, compute reserve).

## Not in scope

Migration steps, cutover order and downtime plans: one item per move after
the layout is ruled. Model choice per operation: the eval-first plan sent to
local-compute (2026-10-03).
