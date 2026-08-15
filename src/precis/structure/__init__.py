"""The ``structure`` kind — a legible atomistic cell + bond-graph IR.

A periodic cell filled with atoms and an explicit bond graph that the LLM reads
as *structure* (graph + numeric feedback), never pixels — the materials sibling
of ``cad`` (0041) / ``pcb`` (0042). This package is the **pure, numpy-only IR
core** (§1/§20): cell + scene + ops + probes + validator gate. The relaxer/DFT
(ASE/MLIP/GPAW) and the file I/O are backends added on top — ASE is a core
dependency, MLIP/GPAW rungs remain extras-gated; the store + handler (the DB
layer) wrap this core.

Compute-adjacent seams, each with its own module docstring:

- `relax` — the rented fidelity ladder (``clean``/``emt`` ours, always-on;
  higher rungs extras-gated, GPU-dispatched via the compute job lane).
- `preflight` — the tier-0 element-agnostic sanity gate in front of any MLIP
  spend (``PRECIS_STRUCTURE_PREFLIGHT``, default OFF); catches *authoring*
  faults, never physical verdicts.
- `cache` — content-addressed relax memoisation; forces are stored
  label-paired, never canonical-rank-indexed (see ``serialize_forces``).
- `importers` — pure per-source adapters for external DFT DBs;
  the one write path is ``store.structure_import``, keyed on
  ``(dataset, config_id)``; an external run never serves a compute cache hit
  and an external design refuses ``edit`` (derive a variant instead).
- `invariants` — representation-invariant fingerprint (composition ·
  per-layer · adsorbate site · coordination) powering the round-trip eval;
  ``handlers/structure.py::guard_energy_comparable`` refuses a
  cross-method-fingerprint ΔE.
"""

from __future__ import annotations

from .cell import Cell, ImageOffset
from .measures import anchor_identity_verified
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
    "anchor_identity_verified",
    "apply_ops",
    "evaluate_measure",
    "relax",
    "validate",
]
