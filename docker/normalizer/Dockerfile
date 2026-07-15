# precis-normalizer — a standalone LinChemIn container (ADR 0056 slice 3).
#
# The *service*-engine normalizer: turns any planner's native output (any
# LinChemIn input_format — askcosv2, ibm_retro, …) into the precis-canonical
# route.json that precis_chem.normalize.parse_syngraph reads. A container
# engine (AiZynth) bundles its own normalizer in its image; a service engine
# (ASKCOS) has no such image, so precis runs THIS generic one on the paths the
# service returned.
#
# Image = code only; no models, no engine. Lightweight (linchemin + rdkit, no
# torch/aizynth). Built on the compute node like the aizynth wrapper.
#
# Build:  docker build -t precis-normalizer:latest docker/normalizer
# Run  :  see precis_chem.jobs._default_normalizer (the dispatch composes it).
FROM python:3.11-slim-bookworm

# rdkit needs a couple of shared libs on the slim base; tini reaps the child.
ARG LINCHEMIN_VERSION=3.2.0
RUN apt-get update \
 && apt-get install -y --no-install-recommends libxrender1 libxext6 tini \
 && rm -rf /var/lib/apt/lists/* \
 && pip install --no-cache-dir "linchemin==${LINCHEMIN_VERSION}" packaging

# linchemin's dynaconf refuses to import `facade` without $HOME/linchemin/
# settings.yaml (AttributeError: CONSTRUCTORS). Generate it at build; the
# container runs as root, so HOME=/root matches.
RUN HOME=/root linchemin_configure

COPY to_route.py /usr/local/lib/to_route.py

WORKDIR /work/out
ENTRYPOINT ["tini", "--"]
CMD ["python", "/usr/local/lib/to_route.py", "--help"]
