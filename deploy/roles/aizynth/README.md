# aizynth role — build the precis-aizynth retrosynthesis image

Configures a **Linux compute node** to run precis-mcp `retrosynth` jobs
(ADR 0056): asserts the container runtime, copies `docker/aizynth` to the
node, builds `precis-aizynth:latest` in place, downloads the AiZynth public
models onto the NAS (one-time), and drops the worker's `PRECIS_CHEM_*` /
`PRECIS_NODE` env as a systemd drop-in. The `route` kind + `retrosynth`
job_type are bundled in precis-mcp — nothing is pip-installed on the node.

Gated by topology: runs where `inventory_hostname in
precis_capabilities.chem_route`. The macOS gateway needs only the KIND
SURFACE (`PRECIS_CHEM_ENABLED` + `PRECIS_CHEM_ROUTE_NODE`) — wire that via
`roles/precis_web`, not here.

    ansible-playbook playbooks/43-aizynth.yml
    # skip the heavy first-time build + model download once present:
    ansible-playbook playbooks/43-aizynth.yml \
      -e aizynth_build_image=false -e aizynth_download_models=false

## aarch64: the LinChemIn venv compiles from source

`pip install linchemin` pulls in **hdbscan, which publishes no aarch64
wheel** — on an arm64 chem_route node (topology
`precis_capabilities.chem_route`) pip builds it from source and needs a C/C++
toolchain that `python:3.11-slim-bookworm` does not ship. The Dockerfile
therefore builds the linchemin venv in a `linchemin-builder` stage with
`build-essential`, and the final image `COPY --from=`s only the finished
venv — the toolchain never reaches the runtime image. On x86_64 the wheel
exists and the toolchain is simply unused; the stage is kept unconditional so
both arches build identically.

## Why the build runs with `DOCKER_BUILDKIT=0`

Deliberate, not legacy drift: the GPU node's Docker Hub link is flaky, and
the legacy builder resolves the pre-pulled base image from the local store
**without a registry metadata HEAD** — BuildKit would re-check the registry
and can fail the build on a lookup. Multi-stage `COPY --from=` works fine on
the legacy builder, so nothing is lost. Do not flip to BuildKit without a
fix for the cached-base resolution.
