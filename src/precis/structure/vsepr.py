"""Advisory geometry rules — the warn tier of the DRC.

Hybridization inference, VSEPR angle strain, π-bond twist, small-ring strain,
and hybridization conflicts. Never part of the hard-reject gate
(:func:`validate.validate`) — a finding here means "strained/inconsistent,
look at it", not "impossible": a pre-relax geometry has bad angles by design,
and an intentionally strained ring (epoxide, cyclopropane) is legal
chemistry. Pure reads over the Scene, same findings shape as validate.py
(:class:`validate.ValidationIssue`, ``severity="warn"``).
"""

from __future__ import annotations

import itertools

from . import elements, probe
from .scene import Scene
from .validate import ValidationIssue

#: Angle deviation from the VSEPR ideal beyond which ``angle_strain`` fires (deg).
ANGLE_TOL = 15.0

#: Dihedral deviation from planarity (0°/180°) beyond which ``pi_twist`` fires (deg).
TWIST_TOL = 20.0

#: Largest ring size the ``small_ring`` rule flags.
SMALL_RING_MAX = 4

#: Elements VSEPR/hybridization reasoning applies to. H and the halogens are
#: always terminal (one bond, no "shape" of their own); metals aren't VSEPR —
#: their coordination geometry is a different (crystal-field) story.
_HYBRID_ELEMENTS = {"B", "C", "N", "O", "Si", "P", "S"}

#: Known-reasonable coordination-number ranges for a metal acting as a
#: cluster/complex centre (gr285775) — small, commented, obviously
#: extendable. Sourced from common inorganic-cluster/MOF-node chemistry:
#: Zr6O4(OH)4 (the UiO-66 SBU) nodes run CN 6-8; Zn/Cu paddle-wheel and
#: tetrahedral MOF nodes commonly run 4-6; octahedral/tetrahedral Fe
#: complexes commonly run 4-6. Deliberately does NOT include the slab
#: metals this codebase also carries (Ni/Pd/Pt/Au, ``elements._MAX_VALENCE``)
#: — their correct bulk/surface coordination runs much higher (~9-12), which
#: would false-flag on every legitimate slab; :func:`_metal_coordination`
#: additionally only counts LIGAND (non-metal) declared bonds, so a bare
#: slab atom with zero declared bonds is skipped outright rather than
#: misread as CN=0.
_METAL_CN_RANGE: dict[str, tuple[int, int]] = {
    "Zr": (6, 8),
    "Zn": (4, 6),
    "Cu": (4, 6),
    "Fe": (4, 6),
}

#: sp3 ideal angle is lone-pair-adjusted per central atom (Bent's rule, cheap
#: nominal values) rather than the flat tetrahedral 109.47° — a bare-lone-pair
#: N/O/S/P compresses its bond angle below the no-lone-pair value.
_SP3_LONE_PAIR_OVERRIDE: dict[str, float] = {
    "N": 107.0,
    "O": 104.5,
    "S": 99.0,
    "P": 96.0,
}


def _infer_from_bonds_only(scene: Scene, label: str) -> str | None:
    """Hybridization from DECLARED bonds only — conservative, mirrors
    ``validate``'s rule 5 reasoning: an ``inferred`` (auto-detected) bond
    carries a guessed order-1, so counting it would false-flag geometry the
    detector merely noticed, not intent the LLM declared."""
    incident = [
        b for b in scene.bonds if b.provenance == "declared" and label in (b.i, b.j)
    ]
    if not incident:
        return None
    order2 = sum(1 for b in incident if b.order == 2)
    if any(b.order >= 3 for b in incident) or order2 >= 2:
        return "sp"
    if any(b.order == 2 or b.order == 1.5 or b.kind == "aromatic" for b in incident):
        return "sp2"
    return "sp3"


def _bonds_only_hybridization(scene: Scene, label: str) -> str | None:
    """:func:`_infer_from_bonds_only`, element-gated — the same inference
    :func:`infer_hybridization` falls back to, but ignoring any declared
    ``atom.hybridization`` override (what ``hybridization_conflict`` compares
    the declaration against)."""
    atom = scene.atoms[label]
    if atom.element not in _HYBRID_ELEMENTS:
        return None
    return _infer_from_bonds_only(scene, label)


def infer_hybridization(scene: Scene, label: str) -> str | None:
    """Best-effort hybridization for ``label``, or ``None`` when it doesn't
    apply (element outside :data:`_HYBRID_ELEMENTS`) or can't be told
    (no declared bonds, no declaration).

    A declared ``atom.hybridization`` (Scene ``Atom`` field, "declared intent
    only") always wins when set; otherwise this infers from declared bond
    orders (:func:`_infer_from_bonds_only`).
    """
    atom = scene.atoms[label]
    if atom.element not in _HYBRID_ELEMENTS:
        return None
    if atom.hybridization is not None:
        return atom.hybridization
    return _infer_from_bonds_only(scene, label)


def ideal_angle(element: str, hybridization: str) -> float | None:
    """The VSEPR ideal bond angle (deg) for ``hybridization``, lone-pair
    adjusted for sp3 N/O/S/P (:data:`_SP3_LONE_PAIR_OVERRIDE`). ``None`` for
    an unrecognized hybridization string."""
    if hybridization == "sp":
        return 180.0
    if hybridization == "sp2":
        return 120.0
    if hybridization == "sp3":
        return _SP3_LONE_PAIR_OVERRIDE.get(element, 109.47)
    return None


def _declared_adjacency(scene: Scene) -> dict[str, set[str]]:
    """Graph adjacency over DECLARED bonds only — the neighbour source for
    ``angle_strain``/``pi_twist``. It must match the hybridization-inference
    source (:func:`_infer_from_bonds_only`): an ``inferred`` neighbour is the
    detector's guess about geometry, and measuring a declared-intent ideal
    against a guessed neighbour false-flags exactly the case the inference
    guard exists for (one declared single bond defaults the centre to sp3;
    an auto-detected second neighbour at 180° then reads as 70° of "strain"
    on chemistry that was never declared)."""
    adj: dict[str, set[str]] = {label: set() for label in scene.atoms}
    for b in scene.bonds:
        if b.provenance != "declared":
            continue
        if b.i in adj and b.j in adj:
            adj[b.i].add(b.j)
            adj[b.j].add(b.i)
    return adj


def _covalent_neighbors(
    scene: Scene, adj: dict[str, set[str]], label: str
) -> list[str]:
    """``label``'s graph neighbours (over ALL bonds), sorted, dropping any
    whose element isn't valence-bounded — a metal neighbour is adsorption
    geometry, not VSEPR (mirrors ``probe.covalent_coordination``'s filter)."""
    return sorted(
        n
        for n in adj.get(label, ())
        if n in scene.atoms and elements.max_valence(scene.atoms[n].element) is not None
    )


def _angle_strain(
    scene: Scene, adj: dict[str, set[str]], exempt_rings: list[list[str]]
) -> list[ValidationIssue]:
    ring_sets = [frozenset(r) for r in exempt_rings]
    findings: list[ValidationIssue] = []
    for label, atom in scene.atoms.items():
        hyb = infer_hybridization(scene, label)
        if hyb is None:
            continue
        ideal = ideal_angle(atom.element, hyb)
        if ideal is None:
            continue
        neighbors = _covalent_neighbors(scene, adj, label)
        if len(neighbors) < 2:
            continue
        for a, c in itertools.combinations(neighbors, 2):
            # small rings carry their own, expected, angle strain — that's
            # small_ring's business, not a per-pair finding here.
            if any({label, a, c} <= rs for rs in ring_sets):
                continue
            ang = probe.angle(scene, a, label, c)
            dev = abs(ang - ideal)
            if dev <= ANGLE_TOL:
                continue
            findings.append(
                ValidationIssue(
                    rule="angle_strain",
                    atoms=[a, label, c],
                    measured=round(ang, 1),
                    expected=ideal,
                    suggested_fix=(
                        f"{a}-{label}-{c} measures {ang:.1f}°, {dev:.1f}° off "
                        f"the {ideal:.1f}° {hyb} ideal for {atom.element} — "
                        "displace an atom or run relax fidelity='clean'; if "
                        "the double/single bond intent is wrong, fix the "
                        "bond orders instead."
                    ),
                    severity="warn",
                )
            )
    findings.sort(key=lambda f: sorted(f.atoms))
    return findings


def _pi_twist(scene: Scene, adj: dict[str, set[str]]) -> list[ValidationIssue]:
    findings: list[ValidationIssue] = []
    for bond in scene.bonds:
        if bond.provenance != "declared":
            continue
        # order 1.5 counts even without kind='aromatic' — a partial-double/
        # resonance bond declared as plain pairwise is still a π system, and
        # the sp2 inference above already treats it as one.
        if not (bond.order == 2 or bond.order == 1.5 or bond.kind == "aromatic"):
            continue
        i, j = bond.i, bond.j
        if i not in scene.atoms or j not in scene.atoms:
            continue
        hi = infer_hybridization(scene, i)
        hj = infer_hybridization(scene, j)
        if hi not in ("sp2", "sp") or hj not in ("sp2", "sp"):
            continue
        others_i = [n for n in _covalent_neighbors(scene, adj, i) if n != j]
        others_j = [n for n in _covalent_neighbors(scene, adj, j) if n != i]
        if not others_i or not others_j:
            continue
        a, d = others_i[0], others_j[0]
        dih = abs(probe.dihedral(scene, a, i, j, d))
        twist = min(dih, abs(dih - 180.0))
        if twist <= TWIST_TOL:
            continue
        findings.append(
            ValidationIssue(
                rule="pi_twist",
                atoms=[a, i, j, d],
                measured=round(twist, 1),
                expected=TWIST_TOL,
                suggested_fix=(
                    f"π system about {i}-{j} is twisted {twist:.1f}° out of "
                    "plane — rotate the substituents toward planarity or "
                    "reconsider the bond order."
                ),
                severity="warn",
            )
        )
    findings.sort(key=lambda f: sorted(f.atoms))
    return findings


def _small_ring(scene: Scene, rings: list[list[str]]) -> list[ValidationIssue]:
    findings: list[ValidationIssue] = []
    for ring in rings:
        # A close-packed metal lattice is full of 3-atom triangles over
        # auto-detected metal-metal bonds — lattice geometry, not strained
        # chemistry. Any metal member disqualifies the ring from this rule.
        if any(
            m in scene.atoms and elements.max_valence(scene.atoms[m].element) is None
            for m in ring
        ):
            continue
        ring_set = set(ring)
        has_sp = any(infer_hybridization(scene, m) == "sp" for m in ring)
        has_double = any(
            b.provenance == "declared"
            and b.order >= 2
            and b.i in ring_set
            and b.j in ring_set
            for b in scene.bonds
        )
        fix = (
            f"ring {'-'.join(ring)} has {len(ring)} members — 3-/4-membered "
            "rings carry serious angle strain (an intentional strained ring, "
            "e.g. epoxide/cyclopropane, is legal chemistry — this is "
            "advisory only)."
        )
        if has_sp or has_double:
            fix += (
                " sp hybridization or a double bond inside a ring this small "
                "is severely strained — double-check the bond orders/element."
            )
        findings.append(
            ValidationIssue(
                rule="small_ring",
                atoms=ring,
                measured=float(len(ring)),
                expected=5.0,
                suggested_fix=fix,
                severity="warn",
            )
        )
    findings.sort(key=lambda f: sorted(f.atoms))
    return findings


def _hybridization_conflict(scene: Scene) -> list[ValidationIssue]:
    findings: list[ValidationIssue] = []
    for label, atom in scene.atoms.items():
        if atom.hybridization is None:
            continue
        inferred = _bonds_only_hybridization(scene, label)
        if inferred is None or inferred == atom.hybridization:
            continue
        expected = ideal_angle(atom.element, atom.hybridization)
        measured = ideal_angle(atom.element, inferred)
        if expected is None or measured is None:
            # v1 models sp/sp2/sp3 only — a declared expanded-octet
            # hybridization (sp3d / sp3d2 on P/S) has no ideal angle here and
            # silently passes rather than false-flagging it as a conflict.
            continue
        findings.append(
            ValidationIssue(
                rule="hybridization_conflict",
                atoms=[label],
                measured=measured,
                expected=expected,
                suggested_fix=(
                    f"{label} ({atom.element}) is declared {atom.hybridization} "
                    f"(ideal angle {expected:.2f}°) but its declared bonds "
                    f"imply {inferred} (ideal angle {measured:.2f}°) — update "
                    "the hybridization declaration or fix the bond orders."
                ),
                severity="warn",
            )
        )
    findings.sort(key=lambda f: sorted(f.atoms))
    return findings


def _metal_coordination(scene: Scene) -> list[ValidationIssue]:
    """Advisory metal coordination-number check (gr285775): metals carry no
    valence bound (``validate.py`` rule 2 explicitly skips them — see
    :data:`elements._MAX_VALENCE`), so a wildly over-coordinated metal (a Zr
    bonded to 20 oxygens) passes the hard-reject gate silently. Never
    gating — real coordination chemistry varies plenty even within
    :data:`_METAL_CN_RANGE`'s table.

    Counts DECLARED bonds only (this module's usual discipline), and only to
    LIGAND (non-metal) neighbours — a metal-metal lattice bond, or a bare
    metal atom with no declared bonds at all (every ``slab``-built design),
    isn't "coordination chemistry" in the cluster/complex sense this table
    models, so it's excluded from the count rather than misread as CN=0 and
    warned on every ordinary slab.
    """
    findings: list[ValidationIssue] = []
    for label, atom in scene.atoms.items():
        cn_range = _METAL_CN_RANGE.get(atom.element)
        if cn_range is None:
            continue
        ligand_bonds = [
            b
            for b in scene.bonds_of(label)
            if b.provenance == "declared"
            and (other := scene.atoms.get(b.j if b.i == label else b.i)) is not None
            and elements.max_valence(other.element) is not None
        ]
        if not ligand_bonds:
            continue  # not acting as a coordination centre at all — skip
        cn = len(ligand_bonds)
        lo, hi = cn_range
        if lo <= cn <= hi:
            continue
        findings.append(
            ValidationIssue(
                rule="metal_coordination",
                atoms=[label],
                measured=float(cn),
                expected=float(hi if cn > hi else lo),
                suggested_fix=(
                    f"{label} ({atom.element}) has {cn} declared ligand bonds — "
                    f"outside the {lo}-{hi} coordination range typical for "
                    f"{atom.element} clusters/complexes (advisory only: unusual "
                    "coordination is possible, but double-check the bonds)."
                ),
                severity="warn",
            )
        )
    findings.sort(key=lambda f: sorted(f.atoms))
    return findings


def _unmodeled_charge_state(scene: Scene) -> list[ValidationIssue]:
    """Advisory note (gr285775): a declared charge whose effective valence
    budget (``validate.py`` rules 2/5, :func:`elements.effective_valence`)
    has no explicit :data:`elements._CHARGED_VALENCE` entry, so the budget
    check silently fell back to the neutral :func:`elements.max_valence` —
    flag it rather than trusting a guess at exotic charge-state chemistry.
    Metals are excluded: their coordination isn't valence-bounded at all
    (rules 2/5 skip them outright), so this note doesn't apply to them.
    """
    findings: list[ValidationIssue] = []
    for label, atom in scene.atoms.items():
        if atom.charge == 0 or elements.max_valence(atom.element) is None:
            continue
        _, known = elements.effective_valence(atom.element, atom.charge)
        if known:
            continue
        findings.append(
            ValidationIssue(
                rule="unmodeled_charge_state",
                atoms=[label],
                measured=float(atom.charge),
                expected=0.0,
                suggested_fix=(
                    f"{label} ({atom.element}) declares charge {atom.charge:+d}, "
                    "which has no entry in the charged-valence table "
                    f"(elements._CHARGED_VALENCE) — the valence budget check "
                    f"fell back to neutral {atom.element}'s max valence; "
                    "double-check this charge state by hand."
                ),
                severity="warn",
            )
        )
    findings.sort(key=lambda f: sorted(f.atoms))
    return findings


def advisories(scene: Scene) -> list[ValidationIssue]:
    """All warn-tier geometry findings (empty = clean). Pure read over the
    Scene — never call this from a hard-reject gate; see the module
    docstring."""
    adj = _declared_adjacency(scene)
    # A ring up to 5 atoms exempts its member angles from angle_strain (its
    # own, expected, strain) — independent of SMALL_RING_MAX, which only
    # *flags* 3-/4-rings; a 5-ring gets the angle exemption without a
    # small_ring finding of its own.
    angle_exempt_rings = probe.rings(scene, max_size=5)
    small_rings = probe.rings(scene, max_size=SMALL_RING_MAX)
    findings: list[ValidationIssue] = []
    findings += _angle_strain(scene, adj, angle_exempt_rings)
    findings += _pi_twist(scene, adj)
    findings += _small_ring(scene, small_rings)
    findings += _hybridization_conflict(scene)
    findings += _metal_coordination(scene)
    findings += _unmodeled_charge_state(scene)
    return findings
