"""The validator gate — cheap rules run before any compute (ADR 0043 §5c/§6.4).

A microsecond pre-commit check that catches the LLM's physically-impossible
proposals before a relax spends time on them: sub-covalent atomic overlap and
over-coordination. Each finding names the rule, the offending value, and a
``suggested_fix`` in the op vocabulary (considerata §22-B). This is the DRC-lite
read run as a *gate*; the relax-guardrail (MLIP pre-relax) is a later increment.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import elements, probe
from .scene import Scene

#: Hard-sphere floor: atoms closer than this fraction of the covalent-radii sum
#: are treated as overlapping (unphysical).
OVERLAP_FRACTION = 0.6


@dataclass
class Finding:
    """One validator finding."""

    rule: str
    atoms: list[str]
    measured: float
    expected: float
    suggested_fix: str


def validate(scene: Scene) -> list[Finding]:
    """Return all gate findings (empty = clean). Pure read over the Scene."""
    findings: list[Finding] = []
    labels = list(scene.atoms)

    # 1. atomic overlap (sub-covalent distance)
    for ai in range(len(labels)):
        a = scene.atoms[labels[ai]]
        for bj in range(ai + 1, len(labels)):
            b = scene.atoms[labels[bj]]
            dist, _ = scene.cell.mic(a.frac, b.frac)
            floor = (
                elements.covalent_radius(a.element)
                + elements.covalent_radius(b.element)
            ) * OVERLAP_FRACTION
            if dist < floor:
                findings.append(
                    Finding(
                        rule="atom_overlap",
                        atoms=[a.label, b.label],
                        measured=round(dist, 3),
                        expected=round(floor, 3),
                        suggested_fix=(
                            f"{a.label}/{b.label} are {dist:.2f} Å apart, below the "
                            f"{floor:.2f} Å hard-sphere floor — displace one, or check "
                            f"the fractional coordinates (a 0.05 vs 0.5 typo?)."
                        ),
                    )
                )

    # 2. over-coordination (covalent valence exceeded)
    for label, atom in scene.atoms.items():
        mv = elements.max_valence(atom.element)
        if mv is None:
            continue  # metals are not valence-bounded
        cn = probe.coordination(scene, label)
        if cn > mv:
            findings.append(
                Finding(
                    rule="over_valence",
                    atoms=[label],
                    measured=cn,
                    expected=mv,
                    suggested_fix=(
                        f"{label} ({atom.element}) has {cn} neighbours but "
                        f"max valence is {mv} — remove a bond or a neighbour."
                    ),
                )
            )
    return findings
