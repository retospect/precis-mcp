"""Slice-1 place-and-route prototype
(``docs/backlog/pcb-place-route-datamodel.md``, §Slices item 1).

**Measurement only.** Two copper layers, fixed pads, a straight ratsnest
with crossings: the 4a CDT topology seed, 4b layer beliefs and the 4c
batched relaxation live here so the spec's numbers (legal result, via
count against ``realize``, loop settling, swept-check blocks, evals/s)
can be measured before the method commits to anything.

:mod:`precis.pcb.pr.state` also carries the fuller §State datamodel the
later slices solve over — the movable-part pose SoA
(:class:`~precis.pcb.pr.state.Parts`, capacity-padded with a live mask
and free list) and per-net attributes
(:class:`~precis.pcb.pr.state.NetAttrs`: v/f bounds, role and copper
appetite α) — both built from the same IR. Slice 1 reads neither (its
pads are fixed and it has no appetite term), but one state object spans
the slices. This package is
not wired into ``pcb_route`` or any job, replaces nothing, and changes
no production routing behaviour. It deliberately lives in a subpackage:
``tests/test_pcb_dead_exports.py`` scans ``src/precis/pcb/*.py``
non-recursively, so prototype code must never be a bare module there.

Torch is imported lazily inside functions (``src/precis/structure/
relax.py`` precedent): the host venv is torch-free and the dev container
bakes it. The numpy-only half (:mod:`precis.pcb.pr.state`,
:mod:`precis.pcb.pr.seed`, :mod:`precis.pcb.pr.beliefs`) imports and
tests fine without it.
"""
