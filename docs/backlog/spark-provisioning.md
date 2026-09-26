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
