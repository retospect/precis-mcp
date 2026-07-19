# precis-remarkable — upload a compiled PDF to the reMarkable cloud
# (send-to-tablet). A tiny sidecar image whose ONLY job is to run the
# ``ddvk/rmapi`` Go binary — the maintained fork that speaks the moving-target
# reMarkable sync protocol and uploads a PDF non-interactively (``rmapi put``).
# Containerising it keeps the foreign binary + its cloud network egress + the
# device credential in a throwaway box instead of on the worker host: the
# worker compiles the RM2 PDF with its own MacTeX (already provisioned for
# draft_export) and drives this image with a one-shot ``docker run`` per send —
# mirroring docker/tts + docker/aizynth.
#
# CODE + BINARY only — no models, no precis wheel. The contract is files on a
# bind mount plus one by-key secret (never on argv):
#   in : /work/in/doc.pdf         the compiled PDF to upload
#        /work/in/params.json     {"folder","name","timeout_s"}
#   env: REMARKABLE_RMAPI_CONFIG  the rmapi config body (devicetoken: …), passed
#                                 --env by KEY (value inherited, never in argv)
#   out: /work/out/result.json    {"ok","returncode","output","name","folder"}
#
# Build:  docker build -t precis-remarkable:<sha> \
#           --build-arg RMAPI_VERSION=v0.0.34 docker/remarkable
# Run  :  see precis.export.remarkable.send_via_container (the driver composes
#         the argv + stages in/out).
FROM debian:bookworm-slim

ARG RMAPI_VERSION=v0.0.34

# ca-certificates: rmapi's TLS to the reMarkable cloud; tini: reap the child;
# curl: fetch the release binary at build time; python3(-stdlib): the
# dep-light entrypoint (no pip, no precis).
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates tini curl python3 \
 && rm -rf /var/lib/apt/lists/*

# Fetch the ddvk/rmapi release binary for the build arch. The legacy builder
# (docker/aizynth builds with DOCKER_BUILDKIT=0) sets no TARGETARCH, so detect
# via uname: aarch64 = the Apple-silicon colima default on melchior; x86_64 for
# a Linux node. --retry so a stalled GitHub CDN connection fails fast + retries.
RUN set -eux; \
    arch="$(uname -m)"; \
    case "$arch" in \
      aarch64|arm64) asset="rmapi-linux-arm64.tar.gz" ;; \
      x86_64|amd64)  asset="rmapi-linuxx86-64.tar.gz" ;; \
      *) echo "unsupported arch: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL --connect-timeout 30 --retry 5 --retry-delay 5 --retry-connrefused \
      "https://github.com/ddvk/rmapi/releases/download/${RMAPI_VERSION}/${asset}" \
      -o /tmp/rmapi.tgz; \
    tar -xzf /tmp/rmapi.tgz -C /usr/local/bin rmapi; \
    rm /tmp/rmapi.tgz; \
    chmod +x /usr/local/bin/rmapi; \
    /usr/local/bin/rmapi version || true

COPY precis-remarkable-run /usr/local/bin/precis-remarkable-run
RUN chmod +x /usr/local/bin/precis-remarkable-run

WORKDIR /work
ENTRYPOINT ["tini", "--"]
CMD ["precis-remarkable-run"]
