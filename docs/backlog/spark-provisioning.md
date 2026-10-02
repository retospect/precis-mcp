---
status: ready
pillar: local-compute
---

# Spark provisioning

Grouped 2026-09-26 from 2 items that are sub-parts of one deliverable (each keeps its own section below; the originals are in the history). Split a section back out only when it becomes independently shippable.

## spark: nvidia docker runtime not configured by ansible

_Grouped 2026-09-26; was `spark-nvidia-runtime-ansible`._

The live fix (`nvidia-ctk runtime configure --runtime=docker` + docker
restart) is in no role, so a from-scratch spark redeploy silently re-breaks
Marker's OCR path fleet-wide (`docker: unknown or invalid runtime`). Add the
task to the GPU-node provisioning role. Owner `deploy/roles/`. Mechanical.

## Docker Hub egress on spark blocked (gr189697) — needed only for the TTS pause

_Grouped 2026-09-26; was `spark-dockerhub-egress`._

TLS handshake to Docker Hub/ECR (AWS-hosted) stalls from spark; ghcr.io
works. Also `tts_base_image` (python:3.11) ≠ the Dockerfile FROM (3.12), so
the pull-if-missing guard checks the wrong image. Unblock path (not
executed): pre-seed python:3.12-slim-bookworm from melchior
(`docker save | ssh | docker load`), then 45-tts.yml. Only the 1.5 s
inter-article pause needs this; blocked, polish.

## Scheduled OS and driver updates for the three Sparks

_Added 2026-10-02; Reto's ruling 4 in review item local-compute-4._

castor, pollux and spark get OS package update+upgrade and NVIDIA driver
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
