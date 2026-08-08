# Docker Hub egress on spark blocked (gr189697) — needed only for the TTS pause

TLS handshake to Docker Hub/ECR (AWS-hosted) stalls from spark; ghcr.io
works. Also `tts_base_image` (python:3.11) ≠ the Dockerfile FROM (3.12), so
the pull-if-missing guard checks the wrong image. Unblock path (not
executed): pre-seed python:3.12-slim-bookworm from melchior
(`docker save | ssh | docker load`), then 45-tts.yml. Only the 1.5 s
inter-article pause needs this; blocked, polish.
