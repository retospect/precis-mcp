"""The ``structure`` kind — a legible atomistic cell + bond-graph IR (ADR 0043).

A periodic cell filled with atoms and an explicit bond graph that the LLM reads
as *structure* (graph + numeric feedback), never pixels — the materials sibling
of ``cad`` (0041) / ``pcb`` (0042). This package is the **pure, numpy-only IR
core** (§1/§20): cell + scene + ops + probes + validator gate. The relaxer/DFT
(ASE/MLIP/GPAW) and the file I/O are extras-gated backends added on top; the
store + handler (the DB layer) wrap this core.
"""

from __future__ import annotations

from .cell import Cell, ImageOffset
from .measures import evaluate as evaluate_measure
from .ops import OpError, apply_ops
from .relax import RelaxResult, RelaxUnsupported, relax
from .scene import Atom, Bond, Measure, Scene
from .validate import Finding, validate

__all__ = [
    "Atom",
    "Bond",
    "Cell",
    "Finding",
    "ImageOffset",
    "Measure",
    "OpError",
    "RelaxResult",
    "RelaxUnsupported",
    "Scene",
    "apply_ops",
    "evaluate_measure",
    "relax",
    "validate",
]
