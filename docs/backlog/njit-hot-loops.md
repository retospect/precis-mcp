---
status: draft
title: njit the remaining scalar hot loops (chain relax, pcb coupling pairs) and close the prod parity gap
pillar: 3d-design
prio: low
---

# njit the remaining scalar hot loops

numba is a core dep (`pyproject.toml`, next to shapely). Kernels shipped so
far, each with an old-vs-new parity test holding the pre-kernel code
verbatim:

| kernel | module | parity test | measured |
|---|---|---|---|
| pcb maze A* | `pcb/maze.py::_astar_kernel` | `tests/test_pcb_maze.py` | pcb-easyeda thread's |
| structure pair-MIC (bonds, coordination, validate overlap, min-dist, clean relax, `relax_graph`) | `structure/_pair_kernel.py` | `tests/test_structure_pair_kernel.py` | ~300x bonds, ~700x `relax_graph` (200/100 atoms) |
| hexfold stick relax | `hexfold/_stick_kernel.py` | `tests/test_hexfold_stick_kernel_parity.py` | ~15x, bit-identical |

Also shipped, no numba: `utils/segmentation.py::segment_dp` row-vectorised
(~32x, identical cuts — the "k-1 largest gaps" closed form is NOT safe:
float ties pick different cuts), and the annealer `risk()` per-name peak
(`tests/test_pcb_risk_parity.py`).

**Rules the kernels follow** (keep them for new ones): import numba only
inside the `_*_kernel.py` module, lazily from callers, so `cli.main` and the
slim embedder venv never load llvmlite; `cache=True`; `error_model="numpy"`
wherever the old numpy code produced NaN/inf instead of raising; NaN must
propagate through any max/convergence fold (`if m > best` swallows it).

**Prod dogfood 2026-10-02** (deploy 7242d4c9, session MCP, wall incl.
round-trip): `st459564` (1,494 C) `view=validate` first call 3.3 s
(compile/cache-load + 1,454 warnings rendered, 0 errors); `view=fragments`
0.47 s → 1 fragment C1494. Pre-kernel bonds on that size ≈ 50 s.

## Open

1. **Prod-data parity gap.** Bit-exact old-vs-new on real prod structures
   was not run — the read-only compare script (load `st457880`/`st459564`,
   run the reference copies from `tests/test_structure_pair_kernel.py`) was
   a denied direct prod read. Evidence today is synthetic-only. Reto's call
   whether to run it — td461156 (`waiting-for:reto`).
2. **pcb annealer coupling pairs** — after the `risk()` fix,
   `cost.py::coupling_pair_term` + `ir.py` `_mid`/`pin_point`/`segment_points`
   from `optimize._rescan_after_move` are ~50% of an anneal (cProfile, n=200,
   400 iters). Cache midpoints or vectorise the pair terms; owner is the pcb
   thread (`threads/pcb-platform.md`).
3. **`precis_chain/relax.py::relax_bundle`** + `clash.candidate_pairs` —
   medium-high; needs the `Capsule`/dict spatial hash restructured into
   arrays first.
4. **Not numba, simpler fixes:** `cad/fieldops.py` EDT/labels →
   `scipy.ndimage`; `viz3d/sheetsmooth.py` → CSR + `np.add.at`.
5. **Serve warm-up.** First structure call per process pays compile or
   cache-load (~1–3 s). If it shows as user latency, warm the kernels at
   serve start.

Checked and rejected (not numba-shaped): SOM training (BLAS), DRC
(shapely STRtree), annealer main loop (objects), surface remesh
(dict/set topology), hexfold build/defects, search fusion.
