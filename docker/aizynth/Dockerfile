# precis-aizynth — thin wrapper running AiZynthFinder's `aizynthcli` (ADR 0056
# slice 1b). AiZynth ships no official image, so we build a small one (pip),
# `FROM python:3.11-slim` pinned by digest for reproducible provenance — the
# image digest is the retrosynth cache key's engine component (ADR 0056 §6).
#
# The image is CODE + ENV only. The policy/stock model files (tens to hundreds
# of MB) are NOT baked in — they mount read-only from the NAS at /models at run
# time (ADR 0056 §5), referenced by /models/config.yml. Built **on the compute
# node** (`podman build`), never shipped as a tarball (ADR 0056 §5).
#
# Build:  podman build -t precis-aizynth:<sha> docker/aizynth
# Run  :  see precis_chem.aizynth.build_aizynth_argv (the dispatch composes it).
FROM python:3.11-slim-bookworm

# AiZynth pulls rdkit + onnxruntime; the slim base needs a couple of shared
# libs for rdkit's drawing/IO. tini reaps the aizynthcli child cleanly.
ARG AIZYNTH_VERSION=4.3.2
RUN apt-get update \
 && apt-get install -y --no-install-recommends libxrender1 libxext6 tini \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "aizynthfinder==${AIZYNTH_VERSION}"

# Config lives with the mounted models (node-deploy concern), not in the image.
ENV PRECIS_AIZYNTH_CONFIG=/models/config.yml

COPY precis-aizynth-run /usr/local/bin/precis-aizynth-run
RUN chmod +x /usr/local/bin/precis-aizynth-run

# The shim cd's here and writes trees.json into the bind-mounted /work/out.
WORKDIR /work/out
ENTRYPOINT ["tini", "--"]
CMD ["precis-aizynth-run"]
