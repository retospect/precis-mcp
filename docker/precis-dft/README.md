# precis-dft compute image

The container every `gpaw` rung of `struct_relax` runs on the compute
node. The host side is precis-mcp's
`precis.workers.job_types.struct_relax.build_run_argv`, which does
`ssh <node> docker run … precis-dft:<tag> [mpirun -np N] precis-dft-run
gpaw-relax …` (rank count from `PRECIS_DFT_MPI_RANKS`, default 0 = serial).

Image source is `src/precis_dft/` in this repo; `pyproject.toml` here is
the container-only build file (see its header for why it is not the
monorepo's).

## Build

Docker Hub is unreachable from the cluster, so the build uses the
locally-present CUDA base and a build context rsync'd to the node.
`deploy/roles/dft` does this for you:

```bash
ansible-playbook playbooks/42-dft.yml -e dft_build_image=true
```

By hand, the context is this repo's root and only two subtrees matter:

```bash
rsync -az --exclude __pycache__ --relative \
  ./docker/precis-dft ./src/precis_dft <node>:/tmp/precis-dft-build/
# on the node:
cd /tmp/precis-dft-build \
  && sudo docker build -t precis-dft:cpu -f docker/precis-dft/Dockerfile .
```

## Run

```bash
sudo docker run --rm \
  -v /path/in:/work/in:ro -v /path/out:/work/out \
  precis-dft:cpu precis-dft-run gpaw-relax --in /work/in --out /work/out
# → writes /path/out/result.json  {ok, scalars{E_tot,...}, relaxed_poscar, ...}
```

PAW datasets: **no mount needed** — GPAW 25.7 bundles 559 setups via the
`gpaw_data` package (in `setup_paths`). A `-v <paw>:/opt/gpaw-setups:ro`
+ `GPAW_SETUP_PATH` still overrides them when a pinned external set is
wanted (the NFS-staged path the dispatch design assumes).

## Validated on spark (2026-06-21, GB10 / ARM)

- **Builds**: GPAW 25.7.0 compiles from source against apt
  libxc/openblas on aarch64; ASE 3.28, precis-dft installed (the base is
  Python 3.10; the container subset runs on it).
- **Real DFT runs**: fcc-Al PBE/LCAO relax → `ok:true`,
  E_tot=-3.716 eV, SCF converged. The full path image → entrypoint →
  GPAW → `result.json` works.
- **GPU foundation works**: in `--gpus all`, `cupy-cuda12x[ctk]` sees
  the GB10 (compute capability 121 / Blackwell) and JIT-compiles +
  runs kernels. GPAW's own GPU offload is the next (separate) step;
  see the commented stanza in the Dockerfile.

## Not yet done

- **Rebuilding with MPI on the node.** The Dockerfile now builds GPAW
  against OpenMPI (and fails the build if it lands serial), but the image
  present on the node is still the pre-MPI one. Until
  `dft_build_image=true` has run, leave `PRECIS_DFT_MPI_RANKS` at 0 —
  `mpirun -np N` against a serial `_gpaw` runs N identical copies of the
  same calculation.
- GPAW GPU offload (gpaw.new GPU mode + CuPy) — foundation proven, not
  wired/benchmarked.
- Pushing the image to a registry (caspar `/opt/nfs/registry`) — Phase
  1 runs it locally on spark; see `precis-mcp/docs/design/precis-dispatch.md`.
