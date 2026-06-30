"""The run-cube cache key — content-addressed relax memoisation (ADR 0043 §23.16).

A relax at an energy rung (``ml``/``dft-*``) is expensive; the same geometry at
the same fidelity / model / params produces the same result. So a relax request
is **cache-first**: hash the *input* geometry into a content address, look the
``(structure_sha, fidelity, model, params, code_version)`` tuple up in the
run-cube (``struct_runs``), and on an exact hit return the stored run with **zero
compute** — including the relaxed geometry, so the write-back is real even on a
*different* design that happens to share the same input (the §8/§10 content-
addressing promise, scoped to the cache and needing no CoW-snapshot table).

Why this is correct to cache:

- The key is over the **input** geometry — cell + per-atom element / fractional
  position / fixed-axis mask / magmom / oxidation — *not* labels (naming is
  design-scoped, geometry is not) and *not* bonds (a bond is graph intent, never
  a DFT input, §8.1; two designs with identical atoms relax identically whatever
  their bond annotations).
- ``code_version`` rolls the whole cache forward when the relax algorithm
  changes such that results would differ — bump :data:`RELAX_CODE_VERSION`.
- The cube is **append-only and never invalidated** (ADR §23.16, decision A2):
  a new geometry hashes to a new key, so a stale hit is impossible by
  construction.

The rung-0 ``clean`` geometry repair is **not** cached: it is instant, pure, and
has no energy to memoise — caching it would only bloat the cube.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import numpy as np

from .scene import Scene

#: Bump when the relax algorithm changes such that a cached result would differ
#: from a fresh run (a new optimiser, a changed convergence contract, a backend
#: semantics change). Rolling this forward retires every prior cache entry
#: without touching a row — old keys simply stop matching.
RELAX_CODE_VERSION = "1"

#: Fractional / Cartesian rounding for the content hash. Two geometries that
#: agree to this tolerance share a cache entry; finer differences miss. 1e-6 is
#: far below any physically meaningful displacement yet absorbs float jitter.
_HASH_NDIGITS = 6


def _round(x: Any) -> Any:
    return round(float(x), _HASH_NDIGITS)


def _atom_signature(atom: Any) -> list[Any]:
    """The relax-relevant, label-free signature of one atom."""
    return [
        atom.element,
        [_round(v) for v in atom.frac],
        int(atom.fixed),
        None if atom.magmom is None else _round(atom.magmom),
        atom.oxidation,
    ]


def structure_sha(scene: Scene) -> str:
    """A content address over the geometry that determines a relax.

    Label- and bond-independent (see module docstring): canonicalised over the
    cell + a *sorted* list of per-atom signatures, so the same physical
    configuration hashes identically regardless of design, labelling, or atom
    insertion order.
    """
    atoms = sorted((_atom_signature(a) for a in scene.atoms.values()), key=repr)
    payload = {
        "lattice": [[_round(v) for v in row] for row in scene.cell.lattice],
        "pbc": [bool(p) for p in scene.cell.pbc],
        "atoms": atoms,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def run_cache_key(
    scene: Scene,
    *,
    fidelity: str,
    model: str | None,
    params: dict[str, Any] | None = None,
    code_version: str = RELAX_CODE_VERSION,
) -> str:
    """The cube cache key for relaxing ``scene`` at this rung / model / params."""
    payload = {
        "sha": structure_sha(scene),
        "fidelity": fidelity,
        "model": model,
        "params": params or {},
        "code_version": code_version,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def canonical_order(scene: Scene) -> list[str]:
    """Atom labels in the same label-free canonical order :func:`structure_sha`
    sorts on. Capture this on the **input** scene *before* relaxing: it is the
    one ordering two designs with an identical input geometry agree on, so the
    relaxed positions stored under it map back correctly across designs."""
    return sorted(
        scene.atoms,
        key=lambda la: repr(_atom_signature(scene.atoms[la])),
    )


def serialize_geometry(scene: Scene, order: list[str]) -> dict[str, Any]:
    """The relaxed final geometry, stored on a run so a cache hit can write it
    back onto *any* design that shares the input (no CoW-snapshot table needed
    for the cache; the variant/adopt-as-head story stays separately deferred).

    ``order`` is the input-scene :func:`canonical_order` (label-free, shared by
    every design with the same input), so the stored ``frac`` rows are indexed
    by canonical rank — not by the saving design's labels."""
    return {
        "frac": [[float(v) for v in scene.atoms[la].frac] for la in order],
        "lattice": [[float(v) for v in row] for row in scene.cell.lattice],
    }


def apply_geometry(scene: Scene, geometry: dict[str, Any]) -> None:
    """Write a cached relaxed geometry onto ``scene`` by canonical rank.

    The cache hit may come from a *different* design, so labels need not match:
    rank *i* of the stored geometry is the atom at rank *i* of *this* scene's
    :func:`canonical_order`. Because the hit means the two input geometries are
    identical (to the hash tolerance), those orders coincide. A count mismatch
    means the geometry does not correspond and is left unapplied (the scalar
    envelope is still returned)."""
    cached_frac = geometry.get("frac") or []
    order = canonical_order(scene)
    if len(cached_frac) != len(order):
        return
    # Cell is a frozen dataclass; atom relax leaves it unchanged, but a future
    # variable-cell run could store a different lattice — swap in a fresh Cell
    # (Scene itself is mutable) only when it actually differs.
    cell_lattice = geometry.get("lattice")
    if cell_lattice is not None:
        new_lattice = np.array(cell_lattice, dtype=float)
        if not np.allclose(new_lattice, scene.cell.lattice):
            from .cell import Cell

            scene.cell = Cell(new_lattice, scene.cell.pbc)
    for rank, label in enumerate(order):
        scene.atoms[label].frac = scene.cell.wrap(
            np.asarray(cached_frac[rank], dtype=float)
        )
