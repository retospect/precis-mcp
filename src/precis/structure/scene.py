"""The in-memory working object — a cell filled with atoms + a bond graph.

ADR 0043 §12 evaluation model: a design is *kilobytes*, so it is hydrated once
into this small object and **all probes run against it in memory** — PG is the
system-of-record, never the per-probe compute path. The Scene is the
``(ref, version)`` working object; the store layer (increment 2) loads/saves it
from the ``struct_*`` tables.

Atoms carry *intent + current position* (the declared layer); per-atom derived
values (force/charge) are run-scoped and live elsewhere (§12). Bonds are the
editable graph (order + provenance + periodic image), not a DFT input (§8.1).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import numpy as np

from .cell import Cell, ImageOffset

# fixed-axis bitmask
FIX_X, FIX_Y, FIX_Z = 1, 2, 4
FIX_ALL = FIX_X | FIX_Y | FIX_Z

_LABEL_RE = re.compile(r"^a([A-Z][a-z]?)(\d+)$")


@dataclass
class Atom:
    """One atom: a design-scoped label + element + fractional position + intent."""

    label: str  # 'aPd123' — unique within the design
    element: str
    frac: np.ndarray  # (3,) fractional
    fixed: int = 0  # bitmask: FIX_X|FIX_Y|FIX_Z
    magmom: float | None = None
    oxidation: int | None = None
    hybridization: str | None = None  # declared intent only


@dataclass
class Bond:
    """A graph edge. Pairwise by default; ``image`` crosses a wall (§4.1)."""

    i: str
    j: str
    order: float = 1.0
    kind: str = "pairwise"  # pairwise | aromatic | eta-n | 3c2e
    provenance: str = "declared"  # declared | inferred | dft
    image: ImageOffset = (0, 0, 0)


@dataclass
class Scene:
    """A cell + atoms (by label) + bonds. The hydrated, in-memory design."""

    cell: Cell
    atoms: dict[str, Atom] = field(default_factory=dict)
    bonds: list[Bond] = field(default_factory=list)
    #: Per-element never-recycled high-water mark, seeded from the store on load
    #: so a label survives a vacancy (ADR 0043 §12 no-recycle). The store
    #: persists it on ``refs.meta`` (the dedicated counter table is a forward
    #: optimisation).
    label_hi: dict[str, int] = field(default_factory=dict)

    # -- composition / labels ------------------------------------------------

    def composition(self) -> dict[str, int]:
        """Element → count over the live atoms."""
        out: dict[str, int] = {}
        for atom in self.atoms.values():
            out[atom.element] = out.get(atom.element, 0) + 1
        return out

    def next_label(self, element: str) -> str:
        """Mint the next never-recycled label for ``element`` (``aPd124``).

        The high-water mark is the max of the live atoms *and* the persisted
        ``label_hi`` seed, so a label is never reissued after a vacancy.
        """
        hi = self.label_hi.get(element, 0)
        for label in self.atoms:
            m = _LABEL_RE.match(label)
            if m and m.group(1) == element:
                hi = max(hi, int(m.group(2)))
        return f"a{element}{hi + 1}"

    # -- geometry ------------------------------------------------------------

    def neighbors(
        self, label: str, radius: float
    ) -> list[tuple[str, ImageOffset, float]]:
        """Atoms within ``radius`` Å of ``label`` (MIC), nearest first.

        O(N) over the design's atoms — the small-in-memory regime (§12); a
        cell-list is the optimisation when cells get big.
        """
        a = self.atoms[label]
        out: list[tuple[str, ImageOffset, float]] = []
        for other in self.atoms.values():
            dist, img = self.cell.mic(a.frac, other.frac)
            if other.label == label and dist < 1e-9:
                continue  # skip self (true self-image bonds are declared, not auto)
            if dist <= radius:
                out.append((other.label, img, dist))
        out.sort(key=lambda t: t[2])
        return out

    def bonds_of(self, label: str) -> list[Bond]:
        """Every bond (pairwise or N-ary endpoint) touching ``label``."""
        return [b for b in self.bonds if label in (b.i, b.j)]
