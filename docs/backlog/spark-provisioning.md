---
status: parked
pillar: local-compute
---

# Spark provisioning

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

Shipped 2026-10-21 (`fix/spark-provisioning`): the `vllm` and `sglang` roles
now converge the nvidia docker runtime themselves
(`deploy/roles/vllm/tasks/main.yml`, `deploy/roles/sglang/tasks/main.yml`) —
install-check `nvidia-ctk`, run `nvidia-ctk runtime configure
--runtime=docker` when the runtime is missing, restart docker via a
notified handler, then re-assert. Pinned in `tests/test_deploy_vllm.py` and
`tests/test_deploy_sglang.py`. Marker's OCR path is not a deploy role today
and stays out of scope.

## Docker Hub egress on spark blocked (gr189697) — needed only for the TTS pause

_Grouped 2026-09-26; was `spark-dockerhub-egress`._

TLS handshake to Docker Hub/ECR (AWS-hosted) stalls from spark; ghcr.io
works. Unblock path (not executed): pre-seed python:3.12-slim-bookworm from
melchior (`docker save | ssh | docker load`), then 45-tts.yml. Only the
1.5 s inter-article pause needs this; blocked, polish.

## Scheduled OS and driver updates for the three Sparks

_Added 2026-10-02; Reto's ruling 4 in review item local-compute-4._

castor (retired as compute; now serves gpt-oss-120b via SGLang), pollux and spark get OS package update+upgrade and NVIDIA driver
updates on a schedule, applied **inside the round deploy window**, one box
at a time so the big-model, embedding and science roles never all go down
together. The current state, read 2026-09-29: spark runs driver
580.159.03 / CUDA 13.0, and the twins are unread. Deliverables:
- An ansible play (`deploy/playbooks/`) that drains the host's lanes, runs
  `apt update && apt full-upgrade`, applies the pinned driver version,
  reboots if the kernel or driver changed, and waits for its heartbeat
  and served models to come back before moving to the next host.
- A pinned driver/CUDA version in the inventory, so an upgrade never pulls
  a driver the serving stack (Slice 0's vLLM/SGLang image) was not tested
  on.
- The cadence (monthly in the first round window of the month, proposed)
  recorded in `deploy/README.md`.
Deploy-role change: goes to the orchestrator as a branch, not a qland.

## Host items

_Added 2026-10-18; was `serving-programme-followups`. Order matters: 1 gates `eval-run-spine.md` items 4 and 9, 2 gates item 4 (GPU-in-container, egress) and the frozen world._

1. **Unhang spark's `/mnt/cluster` NFS mount.** `statvfs` times out at 120 s; `mount` lists the share, `stat` and `df` hang (re-confirmed 2026-09-30). Fix or unmount before building on it; suspect and plan in `threads/local-compute.md` item 4a and `cluster-fileserver-move.md`.
2. **Host prep.** Stop the Xorg+gnome-shell session (idle load ~1.4), install `uv`, remove the `/opt/precis` orphan. Read 2026-09-29: no precis units, 2.8 TB free, driver 580.159.03 / CUDA 13.0. Spark runs Postgres 16.15 with pgvector 0.5.1 (2026-09-30); confirm prod's major version matches before planning the frozen world as a restore (`eval-run-spine.md` decisions log). Docker Hub egress blocked, ghcr.io works (section 1).
3. **`py-spy` profile of `precis serve` at N=32.** Names the work holding the GIL (`mcp-concurrency-load-test.md` §Still owed); decides whether the 2-3 process topology (3 processes ~3.2x, measured 2026-10-15) is permanent. About 1 hour; runs wherever `precis serve` runs, not spark-specific.

Traps: the bench role must not enter inventory (no service group, no capability list); the `vllm-per-node-serving.md` "Known footguns" apply to any vLLM work here.

## Open questions / decisions log

- Parked 2026-10-26 (`fix/spark-provisioning`, re-dispatch): the only shipped-shaped
  slice (nvidia docker runtime convergence) already landed 2026-10-21
  (`b12b88eb5`, already on `main`). Everything left in this file needs either
  a live-inventory ruling (next item) or cluster-admin host access neither of
  which this worktree has — `status: ready` was stale and caused this
  re-dispatch. Reopen once the host-list ruling below lands or a session has
  cluster-admin access for the Host items.
- Resolved 2026-10-24: section 1's TTS base image sentence was stale (`deploy/roles/tts/tasks/main.yml` already derives the base from the Dockerfile `FROM`; `deploy/roles/tts/defaults/main.yml` confirms there is no `tts_base_image` var). Deleted; only the host-side pre-seed note remains.
- Proposed (needs ruling): section 2's host list is stale. castor is retired as a compute node (serves gpt-oss-120b via SGLang), pollux has one GB10, spark runs the compute and sandbox lanes. Proposal: restate the host list from the live inventory overlay before anyone builds the play. (Not actioned here — the live inventory overlay is gitignored and not present in this worktree; needs cluster-admin access.)
