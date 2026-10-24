"""Smooth place-and-route engine
(``docs/backlog/pcb-place-route-datamodel.md``): topology by search,
layers by belief, geometry by batched relaxation, coarse-to-fine.

Two drivers share one state. :mod:`precis.pcb.pr.pipeline` is the
**slice-1 prototype** (measurement only: two layers, fixed pads, a
straight ratsnest — 4a seed, 4b beliefs, 4c relaxation). :mod:`precis.
pcb.pr.v1` is the **slice-2 engine**: free parts (poses are variables
beside the wires, pads ride them, courtyards and the outline hold them,
rotation priced on the 90°/45° tiers), blocks (authored IR groups move
rigidly at a coarse level, then prolong), wire refinement (4 → 2 → 1 mm
spans with the beliefs carried by arclength), a 4d repair move for the
sampler's same-layer crossings (:mod:`precis.pcb.pr.uncross`), §Finish
(:mod:`precis.pcb.pr.finish`: rotation/grid sugar, wire clean-up, exact
kernel DRC, a resumable route state) and the bench against the vector
router (``tests/fixtures/pcb/pr_bench.py``, ``scripts/pcb-pr-bench``).

:mod:`precis.pcb.pr.state` carries the §State datamodel: the movable-
part pose SoA (:class:`~precis.pcb.pr.state.Parts`), per-net attributes
(:class:`~precis.pcb.pr.state.NetAttrs`: v/f bounds, role, copper
appetite α), the per-net current trees (:mod:`precis.pcb.pr.trees`:
``B·f = i`` and the IPC-2221 width per edge, which the wires take as
their width floor) and, for the free-part solve, the local-frame
geometry that rides a pose (:class:`~precis.pcb.pr.state.DynGeom`).

Not wired into ``pcb_route`` or any job: ``vroute`` stays the
production router until this beats it on the bench (Reto 2026-10-23),
and ``pcb`` is code-first — this engine is what the workspace calls as
a cached place/route service, not a new construction op. It lives in a
subpackage on purpose: ``tests/test_pcb_dead_exports.py`` scans
``src/precis/pcb/*.py`` non-recursively.

Torch is imported lazily inside functions (``src/precis/structure/
relax.py`` precedent): the host venv may be torch-free and the dev
container bakes it. The numpy-only half (:mod:`precis.pcb.pr.state`,
:mod:`precis.pcb.pr.trees`, :mod:`precis.pcb.pr.seed`,
:mod:`precis.pcb.pr.beliefs`, :mod:`precis.pcb.pr.uncross`) imports and
tests fine without it. No GPU host is reachable from a worktree; the
bench numbers are CPU (the kernel route — KeOps/Triton on the Spark —
is the spec's target, not yet built).
"""
