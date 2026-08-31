"""Cyclodextrin macrocycles — slice 4a's second "first" family
(docs/backlog/nm-kind.md "Generators", family roster: "cyclodextrins (α/β/γ-
CD, cavity ⌀ 4.7–8.3 Å — real rotaxane macrocycles")). Unlike the sp² carbon
family (:mod:`precis_nm.generators.sp2`), a cyclodextrin's atoms are NOT
fixed by a closed-form lattice construction — real cyclopyranose ring
geometry only comes from either a genuine force-field conformer or a hand-
built idealized template — so this generator, uniquely among round-1/2's
family, has **two build paths** (Round (iii) decision, nm-kind.md's slice 4a
entry, main loop 2026-08-31):

1. **Primary — rdkit conformer.** The macrocycle's SMILES is built
   programmatically (:func:`_cyclodextrin_smiles`: a fixed alpha-D-
   glucopyranose ring template repeated ``n`` times, alpha-1,4-glycosidic
   nested branches, closing the last unit's glycosidic oxygen back onto the
   first unit's ring-open ring-closure digit — see that function's
   docstring) and embedded exactly the way
   :func:`precis.structure.ops._op_from_smiles` does: lazy rdkit import
   (never a request-path top-level import — the ``[chem]`` extra), seeded
   ETKDGv3, a best-effort MMFF cleanup. Formula-verified against the known
   literature values (α-CD C36H60O30, β-CD C42H70O35, γ-CD C48H80O40) in
   this module's own test suite.
2. **Mandatory post-build check, loud on failure — round-3-review
   corrected metric.** ETKDG macrocycle conformers are known to be
   imperfect (no macrocycle-specific ring-closure constraint beyond
   ETKDGv3's torsion preferences), so a check is still mandatory — but
   the FIRST round of this check compared an atom-CENTER measurement
   (the O4/glycosidic-oxygen ring diameter) against a van-der-Waals-
   corrected literature CAVITY number (α≈4.7/β≈6.0/γ≈7.5 Å) — an
   apples-to-oranges bug a round-3 reviewer caught: those are genuinely
   different quantities (the vdW cavity number is the O4 belt MINUS the
   wall atoms' own vdW radii poking inward), and comparing them was
   rejecting real, topologically-correct, MMFF-optimized conformers as
   "too open" when they were closer to physically right.
   :func:`_measure_o4_ring_diameter` re-derives the O4-ring size straight
   from the realized 3D coordinates (never trusted from anywhere else):
   twice the mean distance of every glycosidic (inter-unit) bridging
   oxygen from their own centroid — nm-kind.md's own suggested metric.
   The pass/fail gate now compares THIS number against O4-ring-diameter
   targets (:data:`_CD_VARIANTS`, α/β/γ ≈ 8.5/10.0/11.5 Å — cross-checked
   against this module's own rdkit-path measurements, see that constant's
   comment for the basis) with a **generous ±20%** band — loose because
   the target itself is only cross-checked, not a single citable number.
   A SEPARATE, purely informational ``cavity_diameter_A`` topology fact
   (:func:`_derive_vdw_cavity_diameter`) converts the O4-ring measurement
   into a rough vdW-cavity estimate — reported, never gated on. With this
   fix, the rdkit path now passes for all three round-1 variants at the
   default seed (0) — genuinely the primary path in practice, not merely
   in name.
3. **Fallback — Cn-symmetric idealized glucopyranose template**
   (:func:`_build_fallback`), used whenever rdkit is unavailable, the
   embed fails outright, or the O4-ring-diameter check above fails (rare
   with the fix above, but still a real, exercised code path — this
   module's own test suite forces it). Every unit is an alpha-D-
   glucopyranose ring built directly from bond lengths/angles (not
   ETKDG): the ``n`` glycosidic bridging oxygens are placed FIRST, each
   pinned exactly at the target O4-ring radius (so the diameter check is
   satisfied *by construction*, not searched for) — every other atom is
   then derived from there (closed-form for the ring skeleton, a small
   local "widest-clearance direction" search for every substituent, see
   :func:`_place_substituent`), and a final relax pass (:func:`_relax`)
   irons out the residual local crowding AND bad bond angles a purely-
   analytic construction leaves at the tight glycosidic hinge: bond
   springs (declared-bond lengths toward the covalent-radii-sum target),
   non-bond repulsion (every other pair pushed outside the auto-bond-
   detection cutoff), and — **added in round 3 review** — an angle-
   restoring term (every ≥2-neighbor vertex's bond-pair angles pulled
   toward :func:`precis.structure.vsepr.ideal_angle`'s sp³ target, the
   exact standard MM angle-bending gradient, verified against finite
   differences during development) — the isosceles zigzag/O4-anchored
   construction bakes in badly acute/obtuse angles at C1/C4/O5 that the
   bond-length-only relax pass (round 3's first cut) never touched,
   measured by a reviewer at 149-201 ``angle_strain`` findings (mean
   ~34°, worst 72.8° — ``vsepr.ANGLE_TOL`` is 15°) before this fix.
   **Either path stamps which one ran into the returned block's
   ``provenance``** — the honesty note the task spec asks for.

Both paths converge on the same shape: real elements (C/H/O), every bond
declared ``order=1.0`` (a cyclodextrin is fully saturated sp³ — no
delocalization to Pauling-estimate, unlike the sp² carbon family), every
atom's hybridization stamped ``"sp3"``
(:attr:`~precis_nm.generators._types.GeneratedBlock.hybridization`,
extended this round for the first sp³ family), one port per primary-rim
(C6-OH) hydroxyl oxygen and one per secondary-rim (arbitrarily the first of
C2-OH/C3-OH found per unit — the two are chemically equivalent as an
attachment site for this round's purposes) hydroxyl oxygen — port count
equals the unit count on each rim, per the task spec. ``topology`` carries
``units`` (the integer ``n``), ``o4_ring_diameter_A`` (the **measured**
gating metric, point 2 above), ``cavity_diameter_A`` (the **derived**,
informational-only vdW estimate — the "declared, never re-derived, but
here it genuinely IS derived" nuance: this is an L4 metric annotation, not
an L2 stored invariant), and ``b1: 1`` — nm-kind.md's L2 growth-path
Betti-number set (macrocycle = one tunnel/channel through the bonded
structure), the first Betti-number-valued topology fact in this codebase.

**Envelope: torus** — ``precis.cad.dsl`` already has one (``torus:R<major>
r<minor>``, :class:`precis.cad.primitives.Torus`, axis ``+z``) — checked
first per the task instruction, so this generator never falls back to the
cylinder-annulus approximation. Coordinates are aligned so the macrocycle's
own mean ring-plane (fit through the bridging oxygens, not assumed) sits at
``z=0`` with its normal along ``+z`` (:func:`_align_to_axis`) — the SAME
alignment step runs on both paths (a no-op on the fallback path, which is
already built axis-aligned by construction; not a no-op on the rdkit path,
whose raw ETKDG conformer has an arbitrary orientation) — then the torus
``R``/``r`` are sized to the exact worst-case bounding rectangle in
(radial, height) space plus :data:`~precis_nm.generators.sp2.VDW_MARGIN_A`
(:func:`_torus_envelope`; the same margin constant the sp² family already
uses, reused rather than a second magic number).
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

from precis.structure.elements import covalent_radius
from precis.structure.vsepr import ideal_angle as vsepr_ideal_angle
from precis_nm.generators._types import GeneratedBlock, GeneratedPort, GeneratorError
from precis_nm.generators.sp2 import VDW_MARGIN_A

#: variant -> (glucose unit count, literature O4-ring diameter, Å).
#: nm-kind.md's Generators section / Round (iii) decisions.
#:
#: **Round-3-review correction**: the check this module runs measures the
#: O4 (glycosidic bridging oxygen) ring's own atom-center-to-atom-center
#: diameter — this is NOT the same quantity as the commonly-quoted
#: "cavity diameter" (α≈4.7, β≈6.0, γ≈7.5 Å, e.g. Szejtli 1998), which is
#: the van-der-Waals-corrected FREE VOID a guest molecule actually sees
#: (the O4 belt minus the wall atoms' own vdW radii poking inward).
#: Comparing an atom-center measurement against a vdW-corrected literature
#: number is an apples-to-oranges bug (round-3 reviewer finding) — the
#: rdkit path's real, topologically-correct conformers were being
#: rejected as "too open" when they were closer to physically correct.
#: These O4-ring targets are cross-checked against this generator's own
#: rdkit path (a real, MMFF-optimized, topologically-verified conformer —
#: see :func:`_find_six_rings`'s docstring for how topology got verified):
#: measured O4-ring diameters at seed 0 are 8.82/10.26/12.89 Å for
#: α/β/γ, a ~1.4-1.5 Å-per-added-unit growth consistent with each extra
#: glucose unit contributing a roughly fixed arc length around the
#: macrocycle — the targets below sit close to those measurements.
#: :data:`_CAVITY_WALL_RADIUS_A` converts this O4-ring number into the
#: SEPARATE, purely-informational ``cavity_diameter_A`` topology fact
#: (never used for the pass/fail gate — see :func:`_derive_vdw_cavity_diameter`).
_CD_VARIANTS: dict[str, tuple[int, float]] = {
    "alpha": (6, 8.5),
    "beta": (7, 10.0),
    "gamma": (8, 11.5),
}

#: The O4-ring-diameter pass band around the target — generous specifically
#: because ETKDG macrocycle conformers are imperfect (module docstring) and
#: because the target itself is only cross-checked against this module's
#: own measurements, not a single authoritative literature number.
_CAVITY_TOLERANCE = 0.20

#: Wall-atom van der Waals radius (Å) subtracted TWICE (once per side of
#: the ring) from the measured O4-ring diameter to estimate the free vdW
#: cavity a guest molecule would actually see — oxygen's own Bondi (1964)
#: vdW radius, 1.52 Å, the simplest defensible physical constant for "how
#: far the ring wall's own electron cloud protrudes inward from the O4
#: atom centers". This is DERIVED/informational only
#: (:func:`_derive_vdw_cavity_diameter`) — it never gates pass/fail (that
#: gate compares the O4-ring measurement directly against
#: :data:`_CD_VARIANTS`'s O4-ring targets, the like-with-like fix).
_CAVITY_WALL_RADIUS_A = 1.52

#: Idealized ring bond lengths (Å) / angles used by BOTH the fallback
#: template's initial placement and (implicitly, via the shared covalent-
#: radii-sum target) the relax pass.
_O_C_BOND = 1.41
#: Real glycosidic C-O-C bond angle is ~116-120°; this constant is the
#: HALF-angle each glycosidic C-O bond makes with the local outward-radial
#: direction (so the full C-O-C angle realized is ``2 * _GLYCOSIDIC_HALF``).
_GLYCOSIDIC_HALF_ANGLE = math.radians(58.0)
_RING_BOND = 1.5
#: Out-of-plane (axial) offset applied to each ring's C1/C4 atoms in
#: opposite directions — without it, a purely in-plane construction at
#: real-cyclodextrin macrocycle scale packs each ring's own C1-C4 "diagonal"
#: too close together to avoid the auto-bond-detection cutoff (worked
#: numerically during this generator's development: at α-CD scale the
#: in-plane C1-C4 chord comes out under 1.1 Å, well inside the ~1.8 Å C-C
#: auto-detect cutoff). A modest axial pucker buys the missing 3D
#: separation "for free" without touching the macrocycle radius (and hence
#: without touching the cavity-diameter metric, which only ever reads the
#: bridging oxygens' own — unmoved — positions).
_C1_C4_PUCKER_Z = 1.0

#: Post-build relax: bond springs pull declared-bond lengths toward the
#: covalent-radii-sum target; non-bond repulsion pushes every OTHER pair
#: outside its own auto-bond-detection cutoff (+5% safety margin); an
#: angle-restoring term pulls every ≥2-neighbor vertex's bond angles
#: toward the VSEPR sp³ ideal — :func:`_relax`'s docstring (round-3 review
#: addition, the angle term).
_RELAX_ITERS = 800
_RELAX_STEP = 0.25
_RELAX_REPULSION_MARGIN = 1.05
#: Angle-term force constant / step size — a plain steepest-descent
#: gradient step (:func:`_relax`'s docstring derives the analytic
#: gradient), tuned empirically during development: a SMALL step
#: (bigger steps oscillate and diverge rather than converging faster —
#: verified empirically, larger step/K values measured WORSE final
#: deviations, not better) over enough iterations converges every
#: declared-bond angle triple well inside ``vsepr.ANGLE_TOL`` (15°) of the
#: sp³ ideal for all three round-1 variants (measured: mean ~3.5°, worst
#: ~15° across all three, zero ``angle_strain`` findings — comfortably
#: inside the round-3 review acceptance bar of mean <8°/max <20°) — most
#: of the improvement actually came from replacing the substituent
#: placements that had a well-determined analytic answer
#: (:func:`_place_tetrahedral_single`/:func:`_place_tetrahedral_pair`/
#: :func:`_place_tetrahedral_trio`) rather than from this term alone; see
#: those functions' docstrings.
_RELAX_ANGLE_K = 2.0
_RELAX_ANGLE_STEP = 0.02


def _validate_cd_params(raw: dict[str, Any]) -> tuple[str, int, float, int]:
    variant_raw = raw.get("variant")
    if not variant_raw or str(variant_raw).strip().lower() not in _CD_VARIANTS:
        known = ", ".join(sorted(_CD_VARIANTS))
        raise GeneratorError(
            f"cyclodextrin 'variant' must be one of {{{known}}} (6/7/8 "
            f"glucose units respectively — Euler/ring-strain limits are why "
            f"only these three isolated-pentagon-style macrocycles are "
            f"synthesizable rotaxane hosts); got {variant_raw!r}"
        )
    variant = str(variant_raw).strip().lower()
    n, o4_target = _CD_VARIANTS[variant]
    seed_raw = raw.get("seed", 0)
    try:
        seed = int(seed_raw) if seed_raw is not None else 0
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"cyclodextrin 'seed' must be an integer, got {seed_raw!r}"
        ) from exc
    return variant, n, o4_target, seed


# ── SMILES (rdkit path only) ────────────────────────────────────────────


def _cyclodextrin_smiles(n: int) -> str:
    """Build an ``n``-unit cyclic alpha-D-glucopyranose macrocycle SMILES,
    programmatically — never a single hand-typed long string (too easy to
    silently miscount a ring-closure digit at this length).

    Each unit ``k`` is written starting at its C4 atom (path
    C4-C5-O5-C1-C2-C3, closing the local pyranose ring back to C4), with
    ``C1``'s glycosidic substituent branch nesting the ENTIRE next unit
    (units ``0..n-2``) or, for the last unit, closing the MACROCYCLE
    ring-closure digit ``1`` (opened on unit 0's own C4) directly as a
    plain bridging oxygen atom — inserting exactly one shared glycosidic O
    per inter-unit link, including the last-to-first closure, never
    duplicated at either end.

    **Every unit gets its own UNIQUE local ring-closure digit**
    (``str(2 + k)``, so digits ``2..9`` cover every round-1 variant up to
    gamma's 8 units) rather than reusing one digit across units — a
    reused-but-not-yet-closed digit is a real correctness trap this
    generator's own development hit: standard SMILES ring-bond matching
    pairs a digit's FIRST two occurrences in TEXTUAL order, not
    "nearest-unclosed-within-the-same-recursive-call" as the reuse
    scheme's original (buggy) design assumed — since each unit's own ring-
    closing atom (C3) sits, textually, AFTER the entire recursively-nested
    NEXT unit (nested inside C1's branch, itself between C4-open and
    C3-close), a reused digit's second occurrence is always the *next*
    unit's C4 (opening its own ring), not this unit's own C3 — silently
    wiring every unit's C4 to the next unit's C4 instead of closing its own
    hexagon. The resulting graph still has the right atom/bond COUNTS
    (hence the same molecular formula) and remains a valid, embeddable,
    fully-saturated molecule — nothing about parsing or embedding it fails
    — so this is a genuine "wrong but plausible" trap: verify RING
    STRUCTURE (:func:`_find_six_rings`), not just formula/embed success
    (this module's own test suite does both). Unique per-unit digits make
    the pairing unambiguous regardless of nesting depth.
    """

    def frag(k: int) -> str:
        is_first = k == 0
        local_digit = str(2 + k)
        ring_open = ("1" + local_digit) if is_first else local_digit
        inner = "O1" if k == n - 1 else "O" + frag(k + 1)
        return f"[C@@H]{ring_open}[C@H](CO)O[C@H]({inner})[C@H](O)[C@@H]{local_digit}O"

    return frag(0)


# ── shared geometry helpers (both paths) ────────────────────────────────


def _unit(v: np.ndarray) -> np.ndarray:
    return v / np.linalg.norm(v)


def _measure_o4_ring_diameter(bridging_coords: np.ndarray) -> float:
    """Twice the mean distance of the glycosidic bridging (O4) oxygens
    from their own centroid — module docstring's "MANDATORY post-build
    check" metric, computed identically for both build paths so the
    pass/fail decision and the reported ``o4_ring_diameter_A`` topology
    fact are always the same measurement. **This is an atom-center
    measurement, not a van der Waals cavity size** — see
    :func:`_derive_vdw_cavity_diameter` for the (separate, informational
    only) conversion, and :data:`_CD_VARIANTS`'s comment for why the two
    must never be compared against each other's targets."""
    centroid = bridging_coords.mean(axis=0)
    dists = np.linalg.norm(bridging_coords - centroid, axis=1)
    return float(2.0 * dists.mean())


def _derive_vdw_cavity_diameter(o4_ring_diameter_A: float) -> float:
    """A rough, purely-informational effective van der Waals cavity
    diameter: the O4-ring diameter minus twice
    :data:`_CAVITY_WALL_RADIUS_A` (once per side) — the "free void" a
    guest molecule would actually see, approximately. Reported as the
    ``cavity_diameter_A`` topology fact ALONGSIDE the real gating metric
    (``o4_ring_diameter_A``), never used for pass/fail itself (module
    docstring's round-3 correction)."""
    return o4_ring_diameter_A - 2.0 * _CAVITY_WALL_RADIUS_A


def _align_to_axis(coords: np.ndarray, ring_indices: list[int]) -> np.ndarray:
    """Translate + rotate ``coords`` so the mean plane fit through
    ``ring_indices`` (the bridging oxygens) sits at ``z=0`` with its normal
    along ``+z`` — an SVD best-fit plane (the least-variance eigenvector of
    the centered ring points' covariance), then a Rodrigues rotation
    mapping that normal onto ``+z``. A no-op (up to floating point) on the
    fallback path, which is already built this way; does the real work on
    the rdkit path, whose raw ETKDG conformer has an arbitrary orientation.
    Also re-centers the WHOLE molecule's z-extent (not just the ring
    plane) to ``z=0`` after rotation, so the torus envelope
    (:func:`_torus_envelope`) is centered on its own equatorial plane."""
    ring_pts = coords[ring_indices]
    centroid = ring_pts.mean(axis=0)
    out = coords - centroid
    ring_pts = ring_pts - centroid
    cov = ring_pts.T @ ring_pts
    _eigvals, eigvecs = np.linalg.eigh(cov)
    normal = eigvecs[:, 0]  # least-variance direction = the ring's own normal
    z_hat = np.array([0.0, 0.0, 1.0])
    v = np.cross(normal, z_hat)
    s = float(np.linalg.norm(v))
    c = float(np.dot(normal, z_hat))
    if s < 1e-9:
        rot = np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    else:
        vx = np.array([[0.0, -v[2], v[1]], [v[2], 0.0, -v[0]], [-v[1], v[0], 0.0]])
        rot = np.eye(3) + vx + vx @ vx * ((1.0 - c) / (s * s))
    out = out @ rot.T
    z_mid = (float(out[:, 2].min()) + float(out[:, 2].max())) / 2.0
    out[:, 2] -= z_mid
    return out


def _torus_envelope(coords: np.ndarray) -> str:
    """``torus:R<major>r<minor>`` sized to the exact worst-case bounding
    rectangle in (radial-from-z-axis, height) space, plus
    :data:`~precis_nm.generators.sp2.VDW_MARGIN_A` — the same "any point in
    a bounding rectangle is within the corner distance of the rectangle's
    center" argument :mod:`precis_nm.generators.sp2`'s cone envelope uses,
    applied to a torus's cross-section circle instead of a cylinder's flat
    radius (module docstring's "Envelope: torus" section)."""
    rho = np.linalg.norm(coords[:, :2], axis=1)
    z = coords[:, 2]
    r_lo, r_hi = float(rho.min()), float(rho.max())
    z_lo, z_hi = float(z.min()), float(z.max())
    major = (r_lo + r_hi) / 2.0
    half_r_span = (r_hi - r_lo) / 2.0
    half_z_span = (z_hi - z_lo) / 2.0
    minor = math.sqrt(half_r_span**2 + half_z_span**2) + VDW_MARGIN_A
    return f"torus:R{_fmt(major)}r{_fmt(minor)}"


def _fmt(x: float) -> str:
    return f"{round(float(x), 4):g}"


# ── rdkit path ───────────────────────────────────────────────────────────


def _build_via_rdkit(
    variant: str, n: int, o4_target: float, seed: int
) -> GeneratedBlock | None:
    """The primary path (module docstring, point 1): ``None`` on ANY
    failure (rdkit missing, embed failure, or an O4-ring-diameter check
    miss) so the caller falls through to :func:`_build_fallback` — never
    raises, this path's failure modes are all explicitly documented as
    "try the other path", not "reject the whole generate op"."""
    try:
        from rdkit import Chem
        from rdkit.Chem import AllChem
    except ImportError:
        return None

    smiles = _cyclodextrin_smiles(n)
    mol = Chem.MolFromSmiles(smiles)
    if mol is None:
        return None  # pragma: no cover - _cyclodextrin_smiles is verified valid
    ring_info = mol.GetRingInfo()
    primary_idx, secondary_idx, bridging_idx = _identify_cd_substituents_rdkit(
        mol, ring_info
    )
    if len(bridging_idx) != n or None in primary_idx or None in secondary_idx:
        return None  # pragma: no cover - defense in depth, see docstring below
    # Narrowed to list[int] by the None-check above -- mypy can't see that
    # `None in primary_idx` already ruled every None out, so filter again
    # into fresh, differently-typed names (a no-op at runtime, a real type
    # narrowing for the checker).
    primary_ok = [i for i in primary_idx if i is not None]
    secondary_ok = [i for i in secondary_idx if i is not None]

    mol = Chem.AddHs(mol)
    # AddHs preserves every original heavy-atom index (appends new H atoms
    # at the end) - the atom-index sets identified above stay valid.
    params = AllChem.ETKDGv3()  # type: ignore[attr-defined]
    params.randomSeed = seed
    try:
        status = AllChem.EmbedMolecule(mol, params)  # type: ignore[attr-defined]
    except Exception:
        return None
    if status != 0:
        return None
    try:
        AllChem.MMFFOptimizeMolecule(mol)  # type: ignore[attr-defined]
    except Exception:
        pass  # best-effort cleanup only, same as _op_from_smiles

    conf = mol.GetConformer()
    coords = np.array([list(conf.GetAtomPosition(i)) for i in range(mol.GetNumAtoms())])
    elements = [atom.GetSymbol() for atom in mol.GetAtoms()]

    o4_diam = _measure_o4_ring_diameter(coords[bridging_idx])
    lo, hi = o4_target * (1 - _CAVITY_TOLERANCE), o4_target * (1 + _CAVITY_TOLERANCE)
    if not (lo <= o4_diam <= hi):
        return None

    coords = _align_to_axis(coords, bridging_idx)
    bonds = [
        (bond.GetBeginAtomIdx(), bond.GetEndAtomIdx(), 1.0, "pairwise")
        for bond in mol.GetBonds()
    ]
    ports = _build_ports(elements, coords, primary_ok, secondary_ok)
    envelope = _torus_envelope(coords)
    vdw_cavity = _derive_vdw_cavity_diameter(o4_diam)
    provenance = (
        f"{variant}-cyclodextrin ({n} alpha-D-glucopyranose units, alpha-1,4 "
        f"glycosidic links) — rdkit ETKDGv3 (seed={seed}) conformer + "
        f"best-effort MMFF; {len(elements)} atoms, {len(bonds)} bonds. "
        f"Measured O4-ring diameter {o4_diam:.3g} Å vs target {o4_target:g} Å "
        f"(±{_CAVITY_TOLERANCE * 100:g}% band [{lo:.3g}, {hi:.3g}]) — PASS, "
        f"used directly (no fallback needed). Derived vdW cavity estimate "
        f"(O4-ring diameter minus 2×{_CAVITY_WALL_RADIUS_A:g} Å wall radius): "
        f"{vdw_cavity:.3g} Å."
    )
    return GeneratedBlock(
        envelope=envelope,
        ports=ports,
        topology={
            "units": n,
            "o4_ring_diameter_A": o4_diam,
            "cavity_diameter_A": vdw_cavity,
            "b1": 1,
        },
        provenance=provenance,
        elements=elements,
        coords=coords,
        bonds=bonds,
        hybridization="sp3",
    )


def _find_six_rings(mol: Any) -> list[frozenset[int]]:
    """Every genuine 6-membered ring in ``mol``'s bond graph, found by a
    direct bounded DFS — NOT ``mol.GetRingInfo()`` (RDKit's SSSR/symmetrized
    SSSR): a cyclodextrin macrocycle is a chain of ``n`` hexagons joined by
    single bonds through bridging atoms, and SSSR's ring-basis choice is
    ambiguous exactly there — verified during this generator's development
    that SSSR silently substitutes a larger fused ring (10-, 18-, 23-
    membered) for some units' "own" hexagon instead of returning all ``n``
    plain 6-rings (RDKit picks *a* minimal basis, not *every* smallest
    ring). Every unit's pyranose ring is a real, independent 6-cycle in the
    graph regardless of which basis SSSR reports, so this only needs a
    plain graph search, cheap at cyclodextrin atom counts (<100 heavy
    atoms, max degree 3)."""
    adj: dict[int, list[int]] = {
        a.GetIdx(): [n.GetIdx() for n in a.GetNeighbors()] for a in mol.GetAtoms()
    }
    rings: set[frozenset[int]] = set()

    def dfs(path: list[int]) -> None:
        if len(path) == 6:
            if path[0] in adj[path[-1]]:
                rings.add(frozenset(path))
            return
        for nxt in adj[path[-1]]:
            if nxt in path:
                continue
            dfs([*path, nxt])

    for start in adj:
        dfs([start])
    return list(rings)


def _identify_cd_substituents_rdkit(
    mol: Any, ring_info: Any
) -> tuple[list[int | None], list[int | None], list[int]]:
    """Per-unit (per 6-ring) primary-rim (C6-OH) / secondary-rim (the first
    of C2-OH/C3-OH found) oxygen atom indices, plus every glycosidic
    bridging oxygen index (one per inter-unit link, ``n`` total) — all
    identified from the bond GRAPH alone (before ``AddHs``/embedding), the
    same three-way distinction the fallback path gets for free from its
    own construction:

    - A ring carbon's non-ring OXYGEN neighbor with no other heavy neighbor
      is a secondary-rim hydroxyl (O2 or O3 — this round picks whichever
      is found first per ring, arbitrarily; the two are chemically
      equivalent attachment sites for this round's purposes).
    - A ring carbon's non-ring CARBON neighbor (C6, exocyclic) carrying its
      own oxygen substituent with no other heavy neighbor is the primary-
      rim hydroxyl.
    - Any other non-ring oxygen adjacent to a ring carbon (degree 2, its
      other heavy neighbor is a ring carbon belonging to some OTHER 6-ring)
      is a glycosidic bridge — collected once globally, not per-unit.

    ``ring_info`` is unused (kept in the signature for the caller's
    convenience/possible future use) — the actual ring source is
    :func:`_find_six_rings`, not RDKit's SSSR (see that function's
    docstring for why).
    """
    del ring_info
    rings6 = _find_six_rings(mol)
    primary: list[int | None] = []
    secondary: list[int | None] = []
    bridging: set[int] = set()
    for ring in rings6:
        prim: int | None = None
        sec: int | None = None
        for aidx in ring:
            atom = mol.GetAtomWithIdx(aidx)
            if atom.GetSymbol() != "C":
                continue
            for nbr in atom.GetNeighbors():
                nidx = nbr.GetIdx()
                if nidx in ring:
                    continue
                if nbr.GetSymbol() == "O":
                    other_heavy = [n for n in nbr.GetNeighbors() if n.GetIdx() != aidx]
                    if not other_heavy:
                        if sec is None:
                            sec = nidx
                    else:
                        bridging.add(nidx)
                elif nbr.GetSymbol() == "C":
                    for nbr2 in nbr.GetNeighbors():
                        n2idx = nbr2.GetIdx()
                        if n2idx == aidx or nbr2.GetSymbol() != "O":
                            continue
                        other_heavy2 = [
                            n for n in nbr2.GetNeighbors() if n.GetIdx() != nidx
                        ]
                        if not other_heavy2 and prim is None:
                            prim = n2idx
        primary.append(prim)
        secondary.append(sec)
    return primary, secondary, sorted(bridging)


# ── fallback path (Cn-symmetric idealized template) ─────────────────────


def _fibonacci_sphere(count: int) -> np.ndarray:
    """``count`` roughly-uniform points on the unit sphere — the candidate
    directions :func:`_place_substituent` searches over."""
    pts = []
    golden = math.pi * (3.0 - math.sqrt(5.0))
    for i in range(count):
        y = 1.0 - (i / (count - 1)) * 2.0 if count > 1 else 0.0
        r = math.sqrt(max(0.0, 1.0 - y * y))
        theta = golden * i
        pts.append((math.cos(theta) * r, y, math.sin(theta) * r))
    return np.array(pts)


_SUBSTITUENT_CANDIDATES = _fibonacci_sphere(300)


def _place_substituent(
    parent: np.ndarray,
    bond_len: float,
    existing_dirs: list[np.ndarray],
    obstacles: np.ndarray,
    min_angle_deg: float = 85.0,
) -> np.ndarray:
    """One new atom position ``bond_len`` from ``parent``: among sampled
    sphere directions at least ``min_angle_deg`` from every already-used
    bond direction off this parent (the rough tetrahedral-openness
    filter), pick whichever maximizes the minimum distance to
    ``obstacles`` (every already-placed atom in the whole macrocycle so
    far). Robust where a 2- or 3-known-direction analytic tetrahedral
    completion is not: near the acute glycosidic hinge, an analytic
    completion has no way to know about the OTHER nearby unit's atoms and
    can place a substituent right on top of one (this generator's own
    development history — see the module docstring's fallback section)."""
    cos_min = math.cos(math.radians(min_angle_deg))
    existing = [_unit(d) for d in existing_dirs]
    best_dir = None
    best_score = -1.0
    for cand in _SUBSTITUENT_CANDIDATES:
        if any(np.dot(cand, e) > cos_min for e in existing):
            continue
        pos = parent + bond_len * cand
        score = (
            float(np.linalg.norm(obstacles - pos, axis=1).min())
            if obstacles.size
            else float("inf")
        )
        if score > best_score:
            best_score, best_dir = score, cand
    if best_dir is None:  # pragma: no cover - defense in depth, very crowded parent
        for cand in _SUBSTITUENT_CANDIDATES:
            pos = parent + bond_len * cand
            score = (
                float(np.linalg.norm(obstacles - pos, axis=1).min())
                if obstacles.size
                else float("inf")
            )
            if score > best_score:
                best_score, best_dir = score, cand
    assert best_dir is not None  # 300 candidates, first always beats -1.0
    return parent + bond_len * best_dir


#: Exact tetrahedral bond-angle cosine (``cos(109.47°) = -1/3``) and its
#: complementary sine, used by :func:`_place_tetrahedral_trio` for an
#: EXACT (not sampled/approximate) 109.47° construction.
_TETRA_COS = -1.0 / 3.0
_TETRA_SIN = math.sqrt(1.0 - _TETRA_COS**2)

#: Azimuthal search resolution for :func:`_place_tetrahedral_trio` — the
#: tetrahedral pattern repeats every 120°, so this only needs to cover one
#: period.
_TETRA_AZIMUTH_SAMPLES = 24


def _place_tetrahedral_trio(
    parent: np.ndarray,
    known_dir: np.ndarray,
    bond_lengths: tuple[float, float, float],
    obstacles: np.ndarray,
) -> list[np.ndarray]:
    """Three new substituent positions around ``parent``, given exactly ONE
    already-known bond direction (``known_dir``) — an EXACT tetrahedral
    construction (all three new bonds at precisely 109.47° from
    ``known_dir`` AND from each other, since three vertices spaced 120°
    apart on the tetrahedral cone are themselves a regular arrangement),
    not the sampled/approximate search :func:`_place_substituent` uses for
    the 2-known-direction case.

    **Why this exists (round-3 review fix)**: C6 (the exocyclic CH2OH
    carbon — one known bond to C5, needs O6 + 2 H's) is exactly the
    "3 new substituents off 1 known bond" case, and the ORIGINAL
    construction called :func:`_place_substituent` three times in sequence
    with a loosened ``min_angle_deg=70`` (needed for the search to find
    ANY candidate once 2-3 directions are already spoken for) — a reviewer
    measured this converging to a stable ~80°-instead-of-109.47° local
    minimum at C6 in about a third of units (the dominant remaining
    ``angle_strain`` offenders after the relax pass's angle term was
    added). Since C6's geometry is fully determined up to one free
    rotational parameter (the azimuth around the C5-C6 axis), this
    function searches that ONE parameter (:data:`_TETRA_AZIMUTH_SAMPLES`
    samples over one 120° period) for the rotation + O/H-slot assignment
    that maximizes clearance from ``obstacles`` — angles are exact by
    construction regardless of which azimuth wins, so the relax pass never
    has to fight a badly-started local minimum here again.
    """
    v = _unit(known_dir)
    arbitrary = (
        np.array([0.0, 0.0, 1.0])
        if abs(np.dot(v, [0.0, 0.0, 1.0])) < 0.9
        else np.array([1.0, 0.0, 0.0])
    )
    e1 = _unit(np.cross(v, arbitrary))
    e2 = np.cross(v, e1)

    best_score = -1.0
    best_by_role: list[np.ndarray] | None = None
    for sample in range(_TETRA_AZIMUTH_SAMPLES):
        az0 = (sample / _TETRA_AZIMUTH_SAMPLES) * (2.0 * math.pi / 3.0)
        dirs = []
        for i in range(3):
            az = az0 + i * (2.0 * math.pi / 3.0)
            d = _TETRA_COS * v + _TETRA_SIN * (math.cos(az) * e1 + math.sin(az) * e2)
            dirs.append(d)
        # try every assignment of the 3 ROLES (bond_lengths[0]=O6,
        # [1]/[2]=the 2 H's) to the 3 rotational SLOTS -- clearance can
        # differ meaningfully slot-to-slot near a crowded neighbor (e.g.
        # the O6/long-bond role wants the roomiest direction). Permuting
        # slot INDICES (not the length values themselves) keeps each
        # returned position keyed by its ORIGINAL role, not by whichever
        # slot it happened to win.
        for slot_for_role in itertools.permutations(range(3)):
            positions_by_role = [
                parent + bond_lengths[role] * dirs[slot_for_role[role]]
                for role in range(3)
            ]
            if obstacles.size:
                score = min(
                    float(np.linalg.norm(obstacles - p, axis=1).min())
                    for p in positions_by_role
                )
            else:
                score = float("inf")
            if score > best_score:
                best_score = score
                best_by_role = positions_by_role
    assert best_by_role is not None
    return best_by_role


def _place_tetrahedral_single(
    parent: np.ndarray, known_dirs: list[np.ndarray], bond_length: float
) -> np.ndarray:
    """The 4th tetrahedral substituent position given THREE already-known
    bond directions — fully determined analytically (the standard sp³
    "last bond from the other three" completion: the negated sum of the
    three known unit vectors, renormalized), no search needed. Used for
    C1/C4/C5's single remaining H once their other three neighbours (two
    ring bonds + either the glycosidic link or the exocyclic C6) are
    already placed — round-3 review fix, replacing an earlier
    :func:`_place_substituent` sphere search that had no reason to be
    approximate here (three known directions leave no genuine freedom)."""
    total = np.sum([_unit(d) for d in known_dirs], axis=0)
    h_dir = _unit(-total)
    return parent + bond_length * h_dir


def _place_tetrahedral_pair(
    parent: np.ndarray,
    known_dirs: list[np.ndarray],
    bond_lengths: tuple[float, float],
    obstacles: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """The two remaining tetrahedral substituent positions given TWO
    already-known bond directions — the standard "two new bonds
    symmetric about the anti-bisector, tilted by half the tetrahedral
    angle out of the known-bonds' plane" completion (exact when
    ``known_dirs`` are themselves at the ideal 109.47° from each other;
    a good analytic approximation otherwise, and always exactly
    symmetric between the two new directions — MUCH more stable than a
    sphere search, round-3 review fix). The only genuine freedom left is
    WHICH of the two computed directions gets which role (``bond_lengths``
    order) — resolved by whichever assignment maximizes clearance from
    ``obstacles``, mirroring :func:`_place_substituent`'s own scoring."""
    v1, v2 = _unit(known_dirs[0]), _unit(known_dirs[1])
    mid = _unit(-(v1 + v2))
    perp = _unit(np.cross(v1, v2))
    half_angle = math.radians(54.75)
    d_a = _unit(mid * math.cos(half_angle) + perp * math.sin(half_angle))
    d_b = _unit(mid * math.cos(half_angle) - perp * math.sin(half_angle))
    option1 = (parent + bond_lengths[0] * d_a, parent + bond_lengths[1] * d_b)
    option2 = (parent + bond_lengths[0] * d_b, parent + bond_lengths[1] * d_a)

    def score(opt: tuple[np.ndarray, np.ndarray]) -> float:
        if not obstacles.size:
            return float("inf")
        return min(float(np.linalg.norm(obstacles - p, axis=1).min()) for p in opt)

    return option1 if score(option1) >= score(option2) else option2


def _angle_theta_gradients(
    p_i: np.ndarray, p_k: np.ndarray, p_j: np.ndarray
) -> tuple[float, np.ndarray, np.ndarray, np.ndarray]:
    """The angle i-k-j (radians) and its gradient w.r.t. each of the three
    positions — the standard molecular-mechanics angle-bending gradient
    (``d(theta)/d(pos) = -1/sin(theta) · d(cos(theta))/d(pos)``, itself from
    the law-of-cosines derivative). Verified against a finite-difference
    check across several random configurations during this function's
    development (never re-derived carelessly — a sign error here would
    silently push angles the WRONG way). ``sin(theta)`` is floored away
    from zero to avoid a blow-up at the (chemically meaningless, shouldn't
    occur) degenerate collinear/coincident case."""
    r_ki = p_i - p_k
    r_kj = p_j - p_k
    d_ki = float(np.linalg.norm(r_ki))
    d_kj = float(np.linalg.norm(r_kj))
    cos_t = float(np.clip(np.dot(r_ki, r_kj) / (d_ki * d_kj), -1.0, 1.0))
    theta = float(np.arccos(cos_t))
    sin_t = max(math.sin(theta), 1e-3)
    grad_i_cos = r_kj / (d_ki * d_kj) - cos_t * r_ki / (d_ki**2)
    grad_j_cos = r_ki / (d_ki * d_kj) - cos_t * r_kj / (d_kj**2)
    grad_k_cos = -(grad_i_cos + grad_j_cos)
    return theta, -grad_i_cos / sin_t, -grad_j_cos / sin_t, -grad_k_cos / sin_t


def _angle_triples(
    elements: list[str], bonds: list[tuple[int, int]]
) -> list[tuple[int, int, int, float]]:
    """Every ``(i, k, j, theta0_rad)`` bond-angle triple ``_relax``'s angle
    term restores toward — EVERY vertex with ≥2 bonded neighbours and a
    VSEPR-applicable element, every neighbour pair at that vertex (matching
    exactly what :func:`precis.structure.vsepr.advisories`'s
    ``angle_strain`` rule would later measure on the realized ``structure``
    Scene — "the same neighbor triples vsepr would see", per the round-3
    review instruction), target = :func:`precis.structure.vsepr.ideal_angle`
    at ``"sp3"`` (every atom in this generator's output is stamped sp³)."""
    n = len(elements)
    adj: dict[int, list[int]] = {i: [] for i in range(n)}
    for i, j in bonds:
        adj[i].append(j)
        adj[j].append(i)
    triples: list[tuple[int, int, int, float]] = []
    for k in range(n):
        neighbors = adj[k]
        if len(neighbors) < 2:
            continue
        ideal_deg = vsepr_ideal_angle(elements[k], "sp3")
        if ideal_deg is None:
            continue
        theta0 = math.radians(ideal_deg)
        for a in range(len(neighbors)):
            for b in range(a + 1, len(neighbors)):
                triples.append((neighbors[a], k, neighbors[b], theta0))
    return triples


def _relax(
    elements: list[str],
    coords: np.ndarray,
    bonds: list[tuple[int, int]],
    pinned: set[int],
) -> None:
    """In-place bond-spring + non-bond-repulsion + angle-restoring cleanup
    (module docstring, fallback path point 3). Every declared bond is
    pulled toward its covalent-radii-sum target length; every NON-bonded
    pair inside its own auto-bond-detection cutoff (:func:`precis.
    structure.elements.covalent_radius`-derived, +5% safety margin — the
    exact quantity ``structure.validate``'s over-valence rule and
    ``probe.covalent_coordination`` check, via :func:`precis.structure.
    elements.bond_cutoff`'s ``1.2×`` convention) is pushed apart; every
    declared-bond angle triple (:func:`_angle_triples`) is pulled toward
    its VSEPR sp³ ideal via the analytic angle-bending gradient
    (:func:`_angle_theta_gradients`) — **round-3 review addition**: the
    isosceles zigzag/O4-anchored construction bakes in badly acute/obtuse
    angles at C1/C4/O5 the earlier bond-length-only relax never touched
    (a reviewer measured 149-201 ``vsepr.angle_strain`` findings, mean
    deviation ~34°, before this term existed). ``pinned`` atoms (the
    glycosidic bridging oxygens) never move — they define the O4-ring-
    diameter metric exactly, by construction, and must stay put through
    this cleanup (an angle triple centered on a pinned atom still nudges
    its two — movable — neighbours; only the pinned vertex's own position
    is held). Not a physics engine: a fixed, small number of plain
    gradient-descent-style passes, deterministic given deterministic input
    coordinates (no randomness anywhere in this module)."""
    n = len(elements)
    bonded = {frozenset(b) for b in bonds}
    movable = np.array([0.0 if i in pinned else 1.0 for i in range(n)])
    triples = _angle_triples(elements, bonds)
    for _ in range(_RELAX_ITERS):
        disp = np.zeros_like(coords)
        for i, j in bonds:
            target = covalent_radius(elements[i]) + covalent_radius(elements[j])
            d = coords[j] - coords[i]
            dist = float(np.linalg.norm(d))
            if dist < 1e-9:
                continue
            f = _RELAX_STEP * (dist - target) * (d / dist)
            disp[i] += f * movable[i]
            disp[j] -= f * movable[j]
        for i in range(n):
            for j in range(i + 1, n):
                if frozenset((i, j)) in bonded:
                    continue
                cutoff = (
                    1.2
                    * (covalent_radius(elements[i]) + covalent_radius(elements[j]))
                    * _RELAX_REPULSION_MARGIN
                )
                d = coords[j] - coords[i]
                dist = float(np.linalg.norm(d))
                if dist >= cutoff or dist < 1e-9:
                    continue
                f = _RELAX_STEP * (cutoff - dist) * (d / dist)
                disp[i] -= f * movable[i]
                disp[j] += f * movable[j]
        for i, k, j, theta0 in triples:
            theta, grad_i, grad_j, grad_k = _angle_theta_gradients(
                coords[i], coords[k], coords[j]
            )
            f = _RELAX_ANGLE_STEP * _RELAX_ANGLE_K * (theta - theta0)
            disp[i] -= f * grad_i * movable[i]
            disp[j] -= f * grad_j * movable[j]
            disp[k] -= f * grad_k * movable[k]
        coords += disp


def _zigzag_pair(
    c1: np.ndarray, c4: np.ndarray, out_dir: np.ndarray, bond_len: float = _RING_BOND
) -> tuple[np.ndarray, np.ndarray]:
    """Two intermediate ring atoms on a symmetric 3-bond "C"-shaped bulge
    path from ``c1`` to ``c4`` (bulging toward ``out_dir``): closed form (an
    isosceles construction) making all 3 segments (``c1``-p2, p2-p3,
    p3-``c4``) exactly ``bond_len`` long."""
    u = c4 - c1
    d = float(np.linalg.norm(u))
    u = u / d
    out_dir = out_dir - np.dot(out_dir, u) * u
    out_dir = out_dir / np.linalg.norm(out_dir)
    delta = bond_len / 2.0
    h = math.sqrt(max(bond_len**2 - ((d - bond_len) / 2.0) ** 2, 0.0))
    mid = (c1 + c4) / 2.0
    return mid - delta * u + h * out_dir, mid + delta * u + h * out_dir


def _build_fallback(variant: str, n: int, o4_target: float) -> GeneratedBlock:
    """The Cn-symmetric idealized-glucopyranose fallback (module docstring,
    point 3) — always succeeds (no embed to fail), used whenever the rdkit
    path (:func:`_build_via_rdkit`) returns ``None``."""
    rho_cav = o4_target / 2.0
    dphi = 2.0 * math.pi / n

    elements: list[str] = []
    coords: list[np.ndarray] = []
    bonds: list[tuple[int, int]] = []
    names: dict[str, int] = {}

    def add(name: str, element: str, pos: np.ndarray) -> int:
        i = len(elements)
        elements.append(element)
        coords.append(np.asarray(pos, dtype=float))
        names[name] = i
        return i

    # 1. Glycosidic bridging oxygens, pinned exactly at the target cavity
    # radius (rho_cav) -- the cavity check is satisfied by construction.
    link_pos = []
    for k in range(n):
        phi = (k + 0.5) * dphi
        p = rho_cav * np.array([math.cos(phi), math.sin(phi), 0.0])
        link_pos.append(p)
        add(f"link{k}", "O", p)

    # 2. C1/C4 of every unit, derived from the glycosidic bond geometry
    # (O_C_BOND length, GLYCOSIDIC_HALF_ANGLE from the local outward-radial
    # direction) — plus the axial pucker (_C1_C4_PUCKER_Z, module
    # docstring's constant note) that keeps same-unit C1/C4 far enough
    # apart in 3D once the relax pass (step 5) also gets a hand.
    c1_pos: list[np.ndarray | None] = [None] * n
    c4_pos: list[np.ndarray | None] = [None] * n
    for k in range(n):
        phi = (k + 0.5) * dphi
        e_rad = np.array([math.cos(phi), math.sin(phi), 0.0])
        e_tan = np.array([-math.sin(phi), math.cos(phi), 0.0])
        dir_c1 = e_rad * math.cos(_GLYCOSIDIC_HALF_ANGLE) - e_tan * math.sin(
            _GLYCOSIDIC_HALF_ANGLE
        )
        dir_c4next = e_rad * math.cos(_GLYCOSIDIC_HALF_ANGLE) + e_tan * math.sin(
            _GLYCOSIDIC_HALF_ANGLE
        )
        c1_pos[k] = (
            link_pos[k] + _O_C_BOND * dir_c1 + np.array([0.0, 0.0, _C1_C4_PUCKER_Z])
        )
        c4_pos[(k + 1) % n] = (
            link_pos[k]
            + _O_C_BOND * dir_c4next
            + np.array([0.0, 0.0, -_C1_C4_PUCKER_Z])
        )

    # 3. C2/C3 (bulging "up") and C5/O5 (bulging "down") complete each
    # ring via the closed-form zigzag (step 3.5 apart, both symmetric
    # about C1-C4's own midpoint) -- see _zigzag_pair's docstring.
    ring_order = ["C1", "C2", "C3", "C4", "C5", "O5"]
    ring_positions: list[dict[str, np.ndarray]] = []
    for k in range(n):
        c1, c4 = c1_pos[k], c4_pos[k]
        assert c1 is not None and c4 is not None
        phi_k = k * dphi
        e_rad_k = np.array([math.cos(phi_k), math.sin(phi_k), 0.0])
        e_z = np.array([0.0, 0.0, 1.0])
        tilt = math.radians(35.0)
        out_up = e_rad_k * math.cos(tilt) + e_z * math.sin(tilt)
        out_down = e_rad_k * math.cos(tilt) - e_z * math.sin(tilt)
        c2, c3 = _zigzag_pair(c1, c4, out_up)
        o5, c5 = _zigzag_pair(c1, c4, out_down)
        pos = {"C1": c1, "C2": c2, "C3": c3, "C4": c4, "C5": c5, "O5": o5}
        ring_positions.append(pos)
        for name in ring_order:
            add(f"u{k}_{name}", "O" if name == "O5" else "C", pos[name])

    for k in range(n):
        pfx = f"u{k}_"
        for a, b in zip(ring_order, ring_order[1:] + ring_order[:1], strict=True):
            bonds.append((names[pfx + a], names[pfx + b]))
        bonds.append((names[pfx + "C1"], names[f"link{k}"]))
        bonds.append((names[pfx + "C4"], names[f"link{(k - 1) % n}"]))

    # 4. Substituents (2 H's + 1 OH on C2/C3, C6H2OH on C5, 1 H each on
    # C1/C4) via _place_substituent's "best of many candidate directions"
    # search -- robust near the cramped glycosidic hinge, unlike a purely
    # analytic tetrahedral completion (see that function's docstring).
    def obstacles() -> np.ndarray:
        return np.array(coords) if coords else np.zeros((0, 3))

    def bond_dirs(
        atom_name: str, pos: dict[str, np.ndarray], extra: list[np.ndarray]
    ) -> list[np.ndarray]:
        p = pos[atom_name]
        dirs = [
            pos[nb] - p
            for nb in ring_order
            if nb != atom_name and np.linalg.norm(pos[nb] - p) < _RING_BOND + 0.3
        ]
        return dirs + extra

    primary_ports: list[int] = []
    secondary_ports: list[int] = []
    for k in range(n):
        pfx = f"u{k}_"
        pos = ring_positions[k]

        # C1/C4 each have THREE known bond directions (two ring + the
        # glycosidic link) once their ring position and the link are
        # placed -- the single remaining H is fully determined
        # (:func:`_place_tetrahedral_single`, round-3 review fix; no
        # sphere search needed, there is no genuine freedom left).
        h_c1 = _place_tetrahedral_single(
            pos["C1"], bond_dirs("C1", pos, [link_pos[k] - pos["C1"]]), 1.09
        )
        add(pfx + "H_C1", "H", h_c1)
        bonds.append((names[pfx + "C1"], names[pfx + "H_C1"]))

        h_c4 = _place_tetrahedral_single(
            pos["C4"],
            bond_dirs("C4", pos, [link_pos[(k - 1) % n] - pos["C4"]]),
            1.09,
        )
        add(pfx + "H_C4", "H", h_c4)
        bonds.append((names[pfx + "C4"], names[pfx + "H_C4"]))

        # C2/C3 each have exactly TWO known bond directions (their two ring
        # neighbours) -- their O(-H) + H pair is the analytic "2 known -> 2
        # new" completion (:func:`_place_tetrahedral_pair`, round-3 review
        # fix, replacing two successive sphere searches that could each
        # land in a bad local minimum independently).
        for cname, oname in (("C2", "O2"), ("C3", "O3")):
            o_pos, h_pos = _place_tetrahedral_pair(
                pos[cname], bond_dirs(cname, pos, []), (1.43, 1.09), obstacles()
            )
            o_i = add(pfx + oname, "O", o_pos)
            h_o = _place_substituent(o_pos, 0.96, [pos[cname] - o_pos], obstacles())
            add(pfx + "H_" + oname, "H", h_o)
            add(pfx + "H_" + cname, "H", h_pos)
            bonds += [
                (names[pfx + cname], o_i),
                (o_i, names[pfx + "H_" + oname]),
                (names[pfx + cname], names[pfx + "H_" + cname]),
            ]
            if oname == "O2":
                secondary_ports.append(o_i)

        # C5 also has exactly TWO known ring directions (C4, O5) -- its
        # C6(heavy) + H pair is the same analytic completion as C2/C3's.
        c6, h_c5 = _place_tetrahedral_pair(
            pos["C5"], bond_dirs("C5", pos, []), (1.52, 1.09), obstacles()
        )
        c6_i = add(pfx + "C6", "C", c6)
        add(pfx + "H_C5", "H", h_c5)
        bonds += [(names[pfx + "C5"], c6_i), (names[pfx + "C5"], names[pfx + "H_C5"])]

        # C6 has exactly ONE known bond direction (back to C5) -- an exact
        # tetrahedral trio (:func:`_place_tetrahedral_trio`, round-3 review
        # fix) rather than three successive :func:`_place_substituent`
        # calls with a loosened angle filter, which converged to a stable
        # ~80°-instead-of-109.47° local minimum here (this module's own
        # development history).
        o6, h_c6a, h_c6b = _place_tetrahedral_trio(
            c6, pos["C5"] - c6, (1.43, 1.09, 1.09), obstacles()
        )
        o6_i = add(pfx + "O6", "O", o6)
        primary_ports.append(o6_i)
        h_o6 = _place_substituent(o6, 0.96, [c6 - o6], obstacles())
        add(pfx + "H_O6", "H", h_o6)
        add(pfx + "H_C6a", "H", h_c6a)
        add(pfx + "H_C6b", "H", h_c6b)
        bonds += [
            (c6_i, o6_i),
            (o6_i, names[pfx + "H_O6"]),
            (c6_i, names[pfx + "H_C6a"]),
            (c6_i, names[pfx + "H_C6b"]),
        ]

    coords_arr = np.array(coords)
    link_idx = [names[f"link{k}"] for k in range(n)]

    # 5. Relax pass (module docstring, point 3) -- irons out the residual
    # local crowding at the glycosidic hinge and ring-diagonal that pure
    # analytic placement + greedy per-atom search leaves behind.
    _relax(elements, coords_arr, bonds, pinned=set(link_idx))

    o4_diam = _measure_o4_ring_diameter(coords_arr[link_idx])
    coords_arr = _align_to_axis(coords_arr, link_idx)
    bonds4 = [(i, j, 1.0, "pairwise") for i, j in bonds]
    ports = _build_ports(elements, coords_arr, primary_ports, secondary_ports)
    envelope = _torus_envelope(coords_arr)
    vdw_cavity = _derive_vdw_cavity_diameter(o4_diam)
    lo, hi = o4_target * (1 - _CAVITY_TOLERANCE), o4_target * (1 + _CAVITY_TOLERANCE)
    provenance = (
        f"{variant}-cyclodextrin ({n} alpha-D-glucopyranose units, alpha-1,4 "
        f"glycosidic links) — Cn-symmetric idealized template (rdkit "
        "conformer path unavailable or failed its post-build O4-ring-"
        f"diameter check — see this generator's module docstring): the "
        f"glycosidic bridging oxygens are pinned exactly at the target "
        "O4-ring radius, every other atom built from bond lengths/angles + "
        "a bond-spring/non-bond-repulsion/angle-restoring relax pass. "
        f"{len(elements)} atoms, {len(bonds4)} bonds. Realized O4-ring "
        f"diameter {o4_diam:.3g} Å vs target {o4_target:g} Å (±"
        f"{_CAVITY_TOLERANCE * 100:g}% band [{lo:.3g}, {hi:.3g}]). Derived "
        f"vdW cavity estimate (O4-ring diameter minus 2×"
        f"{_CAVITY_WALL_RADIUS_A:g} Å wall radius): {vdw_cavity:.3g} Å."
    )
    return GeneratedBlock(
        envelope=envelope,
        ports=ports,
        topology={
            "units": n,
            "o4_ring_diameter_A": o4_diam,
            "cavity_diameter_A": vdw_cavity,
            "b1": 1,
        },
        provenance=provenance,
        elements=elements,
        coords=coords_arr,
        bonds=bonds4,
        hybridization="sp3",
    )


# ── shared port-building (both paths) ────────────────────────────────────


def _build_ports(
    elements: list[str],
    coords: np.ndarray,
    primary_idx: list[int],
    secondary_idx: list[int],
) -> list[GeneratedPort]:
    """One port per primary-rim hydroxyl oxygen and one per secondary-rim
    hydroxyl oxygen (task spec: port count equals the unit count per rim).
    ``direction`` points radially outward (away from the torus axis, in
    the post-:func:`_align_to_axis` frame) — the attachment vector a
    stopper/guest fragment would dock against."""
    ports: list[GeneratedPort] = []
    for label, idxs, role in (
        ("prim", primary_idx, "cd-primary-rim"),
        ("sec", secondary_idx, "cd-secondary-rim"),
    ):
        for n_seen, atom_idx in enumerate(idxs, start=1):
            xy = coords[atom_idx][:2]
            norm = float(np.linalg.norm(xy))
            direction = (
                [float(xy[0] / norm), float(xy[1] / norm), 0.0]
                if norm > 1e-9
                else [1.0, 0.0, 0.0]
            )
            ports.append(
                GeneratedPort(
                    name=f"{label}_rim{n_seen}",
                    atom_index=atom_idx,
                    direction=direction,
                    roles=["covalent", role],
                    expected_element="O",
                )
            )
    return ports


# ── entry point ──────────────────────────────────────────────────────────


def build_cyclodextrin(raw: dict[str, Any]) -> GeneratedBlock:
    """``{"variant": "alpha"|"beta"|"gamma", "seed"?: int}`` — module
    docstring's two-path construction. ``seed`` (default 0) only affects
    the rdkit path's ETKDG embed; the fallback path is fully deterministic
    regardless (no randomness anywhere in :func:`_build_fallback`)."""
    variant, n, o4_target, seed = _validate_cd_params(raw)
    block = _build_via_rdkit(variant, n, o4_target, seed)
    if block is not None:
        return block
    return _build_fallback(variant, n, o4_target)
