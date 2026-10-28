"""Cheap, lossless job input and phase timing for deferred relax preparation.

POSCAR loses graph intent and periodicity. Capture the whole Scene before
queueing so preflight sees exactly the validated request, even if the design
changes while the job waits. Canonical geometry hashing belongs in the worker.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from time import perf_counter
from typing import Any

import numpy as np

from .cell import Cell
from .scene import Atom, Bond, Measure, Scene

log = logging.getLogger(__name__)


@contextmanager
def phase(structure_id: str | int, name: str) -> Iterator[None]:
    """One INFO line per completed or failed phase, including its owner."""
    start = perf_counter()
    try:
        yield
    finally:
        log.info(
            "structure=%s relax phase=%s elapsed_ms=%.3f",
            structure_id,
            name,
            (perf_counter() - start) * 1000,
        )


def pack_scene(scene: Scene) -> dict[str, Any]:
    """JSON-native snapshot; no derived perception or canonical ordering."""
    return {
        "lattice": scene.cell.lattice.tolist(),
        "pbc": list(scene.cell.pbc),
        "atoms": [
            {**asdict(atom), "frac": atom.frac.tolist()}
            for atom in scene.atoms.values()
        ],
        "bonds": [asdict(bond) for bond in scene.bonds],
        "measures": [asdict(measure) for measure in scene.measures],
        "label_hi": dict(scene.label_hi),
    }


def unpack_scene(data: dict[str, Any]) -> Scene:
    """Restore all intent, including fixed axes, charge and provenance."""
    return Scene(
        cell=Cell(np.array(data["lattice"], dtype=float), tuple(data["pbc"])),
        atoms={
            atom["label"]: Atom(**{**atom, "frac": np.array(atom["frac"], dtype=float)})
            for atom in data["atoms"]
        },
        bonds=[
            Bond(**{**bond, "image": tuple(bond["image"])}) for bond in data["bonds"]
        ],
        measures=[Measure(**measure) for measure in data["measures"]],
        label_hi=dict(data["label_hi"]),
    )
