"""The validator gate — cheap rules run before any compute (ADR 0043 §5c/§6.4).

A microsecond pre-commit check that catches the LLM's physically-impossible
proposals before a relax spends time on them: sub-covalent atomic overlap,
over-coordination, and an implausibly-long declared bond. Each finding names
the rule, the offending value, and a ``suggested_fix`` in the op vocabulary
(considerata §22-B). This is the DRC-lite read run as a *gate*; it is also the
hard-reject step ahead of a cloud relax dispatch (gripe 51393) — a design with
any finding here never reaches the GPU node.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import elements, probe
from .scene import Scene

#: Hard-sphere floor: atoms closer than this fraction of the covalent-radii sum
#: are treated as overlapping (unphysical).
OVERLAP_FRACTION = 0.6

#: Declared-bond length ceiling: a bond longer than this multiple of the
#: element-pair covalent-radii sum is implausible as an order-1 bond. Looser
#: than :func:`elements.bond_cutoff`'s 1.2× auto-detection slack (which only
#: has to catch a *stretched* bond, not reject one outright) — 1.3× leaves
#: room for a legitimately elongated bond mid-pathway (a dissociating
#: transition state) while still catching the LLM's actual mistakes: wrong
#: atom, wrong periodic image, a fractional-coordinate typo.
BOND_LENGTH_FACTOR = 1.3


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

    # 3. implausibly-long declared bond (MIC, same distance path as #1).
    # ``inferred``/auto-detected bonds can't fail this — they're only ever
    # emitted within the (looser) detection cutoff — so this only fires on a
    # bond the LLM actually declared via ``add_bond``.
    for bond in scene.bonds:
        if bond.provenance != "declared":
            continue
        if bond.i not in scene.atoms or bond.j not in scene.atoms:
            continue
        a, c = scene.atoms[bond.i], scene.atoms[bond.j]
        dist, _ = scene.cell.mic(a.frac, c.frac)
        expected = elements.covalent_radius(a.element) + elements.covalent_radius(
            c.element
        )
        ceiling = expected * BOND_LENGTH_FACTOR
        if dist > ceiling:
            findings.append(
                Finding(
                    rule="bond_too_long",
                    atoms=[a.label, c.label],
                    measured=round(dist, 3),
                    expected=round(ceiling, 3),
                    suggested_fix=(
                        f"bond {a.label}-{c.label}: length {dist:.2f} Å is "
                        f"{dist / expected:.1f}× the expected {a.element}-{c.element} "
                        f"covalent range (~{expected:.2f} Å) — impossible as declared; "
                        "move the atoms closer, fix the periodic image, or remove the bond."
                    ),
                )
            )

    # 4. declared bond order the atom's element can't support (cheap check —
    # order vs. nominal max valence only; a real hybridization/aromaticity
    # model is a later increment).
    # deferred: this doesn't sum orders across an atom's *other* bonds (a
    # real over-valence-by-order budget), just flags one bond whose own
    # order already exceeds the element's ceiling — the single-bond case is
    # cheap and unambiguous, the multi-bond budget needs hybridization
    # modeling to do honestly.
    for bond in scene.bonds:
        if bond.provenance != "declared":
            continue
        # One finding per bond, even when *both* endpoints' elements can't
        # support the declared order — report the first offender and stop.
        for label in (bond.i, bond.j):
            bond_atom = scene.atoms.get(label)
            if bond_atom is None:
                continue
            mv = elements.max_valence(bond_atom.element)
            if mv is not None and bond.order > mv:
                findings.append(
                    Finding(
                        rule="bond_order_exceeds_valence",
                        atoms=[bond.i, bond.j],
                        measured=bond.order,
                        expected=mv,
                        suggested_fix=(
                            f"bond {bond.i}-{bond.j} declares order {bond.order:g}, but "
                            f"{label} ({bond_atom.element}) has max valence {mv} — "
                            "lower the bond order or check the element."
                        ),
                    )
                )
                break
    return findings
