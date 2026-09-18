# precis-dft compute image — GPAW + ASE + the precis-dft container CLI.
#
# Built on spark (aarch64). Base is the locally-present CUDA image
# (Docker Hub is unreachable from the cluster; nvidia/cuda:*-base is
# already pulled on spark). CUDA-base = the GPU runtime libs are
# present; the nvidia-container-toolkit injects the driver at
# `docker run --gpus all`. GPAW here is CPU-functional out of the box
# (the design's fallback); GPU enablement (CuPy) is layered on top
# once validated — see docker/README or the precis-dispatch doc.
#
# PAW datasets are NOT baked in — they're mounted read-only at runtime
# (GPAW_SETUP_PATH), so the image stays small and the dataset version
# is swappable.
#
# Build context is the precis-mcp repo root — the image needs
# `docker/precis-dft/` (this file + its pyproject) and `src/precis_dft/`,
# nothing else. Ansible (deploy/roles/dft) rsyncs exactly those two subtrees
# to the node preserving that layout, so the same command works there.
#
# Build (on spark, from the repo root or the rsync'd context):
#   sudo docker build -t precis-dft:cpu -f docker/precis-dft/Dockerfile .
# Smoke test:
#   sudo docker run --rm -v $PWD/in:/work/in:ro -v $PWD/out:/work/out \
#        -v <paw>:/opt/gpaw-setups:ro -e GPAW_SETUP_PATH=/opt/gpaw-setups \
#        precis-dft:cpu precis-dft-run gpaw-relax --in /work/in --out /work/out
# Parallel smoke test (result.json records the rank count it ran with):
#   … precis-dft:cpu mpirun --allow-run-as-root -np 4 \
#        precis-dft-run gpaw-relax --in /work/in --out /work/out

FROM nvidia/cuda:12.6.1-base-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive

# Build + runtime deps for GPAW's C extension (libxc + BLAS + FFTW), plus
# OpenMPI: GPAW's own parallelism is MPI domain/k-point decomposition, not
# threads. Without libmpi at build time the extension compiles serial and a
# relax runs on ONE core no matter how many the container is given.
RUN apt-get update && apt-get install -y --no-install-recommends \
      python3 python3-dev python3-venv python3-pip \
      build-essential gfortran pkg-config \
      libxc-dev libopenblas-dev libfftw3-dev \
      libopenmpi-dev openmpi-bin \
      ca-certificates \
    && rm -rf /var/lib/apt/lists/*

ENV VENV=/opt/venv
RUN python3 -m venv "$VENV"
ENV PATH="$VENV/bin:$PATH"

# Scientific stack. GPAW compiles against the apt libxc/openblas above, and
# against OpenMPI via the siteconfig below — `mpicompiler` is what makes the
# built `_gpaw` extension MPI-capable, so `mpirun -np N precis-dft-run …`
# actually decomposes the calculation instead of running N identical serial
# copies of it. Built from source (--no-binary) for the same reason: a
# prebuilt wheel would be serial.
RUN pip install --no-cache-dir --upgrade pip wheel \
 && pip install --no-cache-dir numpy scipy ase \
 && printf 'mpicompiler = "mpicc"\nmpilinker = "mpicc"\n' > /tmp/gpaw-siteconfig.py \
 && GPAW_CONFIG=/tmp/gpaw-siteconfig.py pip install --no-cache-dir --no-binary gpaw gpaw

# Fail the BUILD, loudly, if MPI did not compile in. A serial `_gpaw` is not a
# broken image — it runs, it produces plausible numbers, it just ignores every
# rank past the first. That class of silent wrong-but-running is exactly what
# cost a 47h-per-relax misdiagnosis on the host side (gr346449); it does not
# get to hide in an image too.
RUN python -c "import _gpaw, gpaw.mpi as m; \
ok = getattr(m, 'have_mpi', hasattr(_gpaw, 'Communicator')); \
assert ok, 'GPAW built WITHOUT MPI — mpirun would run N serial copies'; \
print('GPAW MPI: on')"

# precis-dft — only the in-container path (cli + _container + structures)
# is exercised here, and none of it imports precis-mcp, so --no-deps keeps
# the monorepo's deps (precis-mcp itself, psycopg, pymatgen) out. The
# pyproject copied in is docker/precis-dft/pyproject.toml — a container-only
# build file, NOT the monorepo's, which would install all of precis-mcp.
COPY docker/precis-dft/pyproject.toml docker/precis-dft/README.md /src/precis-dft/
COPY src/precis_dft /src/precis-dft/src/precis_dft
RUN pip install --no-cache-dir --no-deps /src/precis-dft

# GPAW parallel knobs are set per-run by the host; default to serial. The
# host chooses the rank count and invokes `mpirun -np N` itself
# (PRECIS_DFT_MPI_RANKS in precis-mcp's struct_relax.build_run_argv), because
# only it knows the core budget the container was capped at.
ENV OMP_NUM_THREADS=1
ENV GPAW_SETUP_PATH=/opt/gpaw-setups

# ── GPU enablement (validated foundation, not yet wired into GPAW) ──
# Verified on spark (GB10, compute capability 121 / Blackwell sm_121,
# host CUDA 13 driver): inside `docker run --gpus all`,
#     pip install "cupy-cuda12x[ctk]"
# imports, sees the GPU, and JIT-compiles + runs kernels — i.e. CUDA
# 12.x nvrtc supports the GB10. The [ctk] extra is required because
# this -base image has no CUDA headers/nvrtc. GPAW's own GPU offload
# (gpaw.new GPU mode / parallel={'gpu': True}) is a separate, not-yet-
# verified step; until it's wired + benchmarked, CuPy is NOT baked in
# (it adds ~GB for no functional gain while GPAW still runs on CPU).
# Uncomment to make the image GPU-foundation-ready:
#   RUN pip install --no-cache-dir "cupy-cuda12x[ctk]"

ENTRYPOINT []
CMD ["precis-dft-run", "--help"]
