"""The sp² carbon generators (docs/backlog/nm-kind.md "Generators —
parametric block factories"): slice 4a build order (i)'s single-wall
carbon nanotube (:func:`build_cnt`) and C60 fullerene
(:func:`build_fullerene`), plus build order (ii)'s cone/nanohorn
(:func:`build_cone`). All three are closed-form/deterministic — no
optimizer, no relax — because the math fixes every atom once the family's
integer/atom-count parameter is chosen.

**CNT — the standard chiral-rolling construction** (Dresselhaus/Saito
convention). Graphene lattice constant ``a = 2.461`` Å (``a_cc = a/√3 =
1.421`` Å, the C–C bond length), lattice vectors ``a1 = a·(√3/2, 1/2)``,
``a2 = a·(√3/2, -1/2)``, two-atom basis ``B = A + (a_cc, 0)``. The chiral
vector ``C_h = n·a1 + m·a2`` becomes the tube's circumference —
``radius = |C_h| / 2π = a·√(n² + nm + m²) / 2π`` (the formula
nm-kind.md's "Generators" section cites) — and is always perpendicular to
the translation vector ``T = t1·a1 + t2·a2`` (``d = gcd(n, m)``, ``dR = 3d``
when ``3d`` divides ``n - m`` else ``d``, ``t1 = (2m+n)/dR``,
``t2 = -(2n+m)/dR``) — the tube's shortest axial repeat. Every graphene
lattice site in the ``(C_h, T)`` rectangle (found by projecting candidate
lattice points onto the ``Ĉ_h``/``T̂`` unit directions and keeping those
inside ``[0, |C_h|) × [0, |T|)``) rolls onto the cylinder by mapping its
``C_h``-projection to an angle (``angle = u / radius``) and its
``T``-projection straight to ``z`` — a seamless wrap because ``C_h`` is
itself a lattice vector, so the pattern at ``angle=0`` and ``angle→2π``
matches exactly. The unit cell is then translated along ``z`` by whole
multiples of ``|T|`` until the requested ``length_A`` is covered (never
periodic — this is a **finite open tube**, both ends genuinely open
valence). Bonds: any pair within 1.6 Å (comfortably above the ~1.42 Å
sp² bond and below the ~2.4 Å next-nearest-neighbor distance). Every atom
with fewer than 3 bonds is a rim atom — it gets a port
(``roles=["covalent", "sp2-rim"]``) pointing along the tube axis, away
from the tube body (the attachment direction a stopper/end-cap fusion
would dock against).

**Bond order (gripe 279306 fix) — Pauling order 4/3.** A delocalized sp²
sheet has no single Kekulé structure to pick from (unlike C60's isolated
6:6/5:6 bond-length split below), so every bond gets the Pauling
bond-order estimate for a fully delocalized aromatic network: three
equivalent bonds per interior atom, each carrying a third of a π bond,
``order = 4/3`` — an interior (3-coordinate) atom's declared valence sums
to exactly ``3 × 4/3 = 4``, carbon's max valence, with zero headroom
(hence the float32-storage epsilon added to
``structure/validate.py``'s ``valence_budget_exceeded`` rule — a bond
order round-tripped through the ``real`` (float32) storage column comes
back as ``1.3333334...``, not the float64 ``1.3333333333333333``, and
three of those sum a hair over 4). ``kind="aromatic"`` (the genuinely
delocalized case ``Bond.kind``'s docstring names, ``structure/scene.py``).

**Fullerene (C60) — truncated icosahedron via a free truncation
parameter.** Round 1 supports exactly 60 atoms: Euler's formula forces
*every* closed trivalent sp² cage to have exactly 12 pentagons
(``V - E + F = 2`` with ``E = 3V/2`` and every face a 5- or 6-ring gives
``12 = 6·F₆ + 5·F₅ - ... `` collapsing to "exactly 12 pentagons,
independent of hexagon count") — C60 (Goldberg (1,1), the smallest
isolated-pentagon cage) is the only member this generator's fixed
construction realizes; any other atom count needs the general Goldberg
``(h, k)`` construction (a later round, nm-kind.md's family roster). The
12 icosahedron vertices (``(0, ±1, ±φ)`` and cyclic permutations, golden
ratio ``φ = (1+√5)/2``) truncate at a parameter ``t`` along each of the 30
icosahedron edges (``0 < t < 0.5``): the untouched middle of each
icosahedron edge becomes one **6:6 bond** (length ``s·(1 - 2t)·2``, shared
between two hexagons — no two pentagons are ever adjacent in C60, the
isolated-pentagon rule), and the chord between two truncation points
sharing an icosahedron vertex becomes one **5:6 bond** (length ``s·2t``,
since the two edges at an icosahedron vertex are ``60°`` apart and both
have raw length 2 — an equilateral triangle). Solving
``s·2t = 1.458`` (the experimental 5:6 bond) and ``s·(2 - 4t) = 1.401``
(the experimental 6:6 bond) for the two unknowns gives a closed form:
``s = L66/2 + L56``, ``t = L56 / (2s)`` — no numeric root-finding. The
Archimedean (equal-edge) case is exactly ``t = 1/3``; the small deviation
from it is what makes the two bond lengths distinct, matching real C60.
Bonds by nearest-neighbor cutoff (1.55 Å, between the two target lengths
and the next-nearest-neighbor distance). No ports round 1 (a fullerene
attaches to the rest of an assembly via a later *fusion* op that opens a
pentagon/hexagon face, nm-kind.md's nanobud entry — not a per-atom port).

**Bond order (gripe 279306 fix) — an honest Kekulé assignment.** Unlike
the delocalized CNT sheet, C60's bond lengths already encode a single,
unambiguous Kekulé structure: every 6:6 bond (the shorter, more
double-bond-character bond between two hexagons) is a genuine C=C double
bond (``order=2.0``), and every 5:6 bond (the pentagon-hexagon boundary)
is a genuine C-C single bond (``order=1.0``) — classified straight from
the realized bond length (``< 1.43`` Å = 6:6, the module's own
``FULLERENE_L66``/``FULLERENE_L56`` split), never re-derived from the
truncation bookkeeping. Every atom is 3-coordinate with exactly one 6:6
neighbor (the isolated-pentagon rule: no two pentagons share an edge, so
each pentagon's 5 bonds are all 5:6 and each atom's remaining bond is the
one 6:6 double bond) — checked loudly (theorems failing loudly, this
module's own standard for internal invariants) rather than trusted,
because a construction bug here would otherwise silently mis-sum an
atom's valence (two double bonds would sum to 6, not 4).
``kind="pairwise"`` (``Bond.kind``'s default — a definite, localized
bond, not the delocalized ``"aromatic"`` case the CNT generator uses).

**Cone/nanohorn — the standard wrapped-sheet disclination construction**
(:func:`build_cone`, slice 4a build order (ii); provenance: nm-kind.md's
"Generators" section, disclination-balance framing carried from the
nanobuds draft dr173020/pc64732). A carbon nanocone's apex carries
``P`` pentagons (``1 <= P <= 5``) instead of graphene's native hexagons;
Euler counting forces the opening half-angle ``α`` via
``sin(α) = 1 − P/6`` (``P = 0`` is the degenerate flat-sheet limit,
``α = 90°``; ``P = 6`` degenerates to a zero-radius capped-tube point,
``α = 0°`` — both rejected, see :func:`_validate_cone_params`).
**Construction**: cut a ``P × 60°`` wedge out of the flat graphene sheet
through its apex point and glue the two straight cut edges together — the
textbook procedure (Ge & Sattler 1994; Krishnan et al. 1997). The apex
must be a **hexagon center**, not an atom: graphene's honeycomb lattice
(space group p6mm) has an *exact* 6-fold rotation (with an A/B sublattice
swap) only about hexagon centers, never about atom sites (3-fold) or bond
midpoints (2-fold) — this module derives the nearest hexagon center to
the ``(a1, a2)`` origin as ``apex = (2/3)·a1 − (1/3)·a2`` (verified
numerically against an explicit 6-ring trace in the generator's test
suite), so that removing/gluing a ``P·60°`` wedge about it is an *exact*
lattice symmetry — no seam distortion, unlike gluing about an arbitrary
point. Every lattice site (both sublattices) within slant distance
``[ρ_min, ρ_min + length_A]`` of the apex (``ρ_min`` below) and within
the kept angular sector ``ω ∈ [0, φ)`` (``φ = 2π·(1 − P/6)``, the *kept*
sector — the *removed* wedge is ``φ`` to ``2π``) is retained; its
flat-sheet polar coordinates ``(ρ, ω)`` (``ρ`` = slant distance, exactly
preserved — **a cone is a developable surface**, so unrolling it is a
pure isometry with zero *strain*, unlike the fullerene's genuinely curved
shell) map onto the cone by ``cone_angle = ω · k`` (``k = 2π/φ``, the
kept sector's ``φ`` span becomes one full ``2π`` revolution around the
cone axis) and
``(x, y, z) = (ρ·sinα·cos(cone_angle), ρ·sinα·sin(cone_angle), ρ·cosα)``.
Bonds are then found by the same Cartesian nearest-neighbor cutoff
(:func:`_bonds_by_cutoff`) used by the other two families — no special
seam-stitching *bookkeeping* is needed, because the ``ω=0``/``ω=φ`` cut
edges are crystallographically identical (both are literal images of the
same lattice under the exact 60°-multiple rotation about the
hexagon-center apex established above), so their glued Cartesian
positions land at genuine bonding distance automatically, exactly as
CNT's own circumferential wrap already relies on periodic Cartesian
``cos``/``sin`` rather than any explicit seam bookkeeping.
**Why ``ρ_min`` (:func:`_cone_rho_min`) truncates the small end, not just
the single apex point.** Zero *strain* (isometry) only guarantees that
*arc length* along the surface is preserved exactly, for any angular
extent — it says nothing about the straight-line **chord** distance
between two bonded atoms, which is what a realized 3D bond length
actually is. Near the apex, real bonds (up to a full ``60°`` of flat
angular separation, for the very first hexagon ring at ``ρ = a_cc``) span
a *non-small* angle once compressed by ``k`` on the cone, and the
resulting chord badly undershoots ``a_cc`` (worked numerically: for
``P=4``, two genuinely bonded first-ring atoms map to a ``0.95`` Å
"bond" — a geometry artifact, not a real distortion any relaxation would
find, because it comes from the projection, not the chemistry). So the
generator truncates the small end at ``ρ_min = k·a_cc / (2·tol)``
(:data:`_CONE_CHORD_TOL`), past which every retained bond's chord-vs-arc
error stays under the module's tolerance — an honest, principled reason
to open-truncate near the apex, not merely "skip the one apex point".
**Deliberately an OPEN cone (truncated apex, both rims open)**: no atom
sits exactly at the apex (it's a hexagon center, not a lattice site), and
this generator additionally truncates the near-apex region entirely
(``ρ_min`` above) rather than attempting a closed pentagon cap — an
honest open frustum, not a fudged (or geometrically distorted) apex
closure (this round's explicit choice, per the task instruction: closing
the tip exactly is a later round). Both the small end (``ρ = ρ_min``) and
large end (``ρ = ρ_min + length_A``) boundaries are genuinely open
valence, each getting rim ports exactly like CNT's rim atoms. Bond order:
Pauling 4/3, ``kind="aromatic"`` — same delocalized-sheet reasoning as
CNT (the cone is the same graphene sheet, merely disclinated, not
re-hybridized).
"""

from __future__ import annotations

import itertools
import math
from typing import Any

import numpy as np

from precis_nm.generators._types import GeneratedBlock, GeneratedPort, GeneratorError

#: Graphene lattice constant / C-C bond length (Å) — nm-kind.md "Generators".
GRAPHENE_A = 2.461
GRAPHENE_A_CC = GRAPHENE_A / math.sqrt(3)

#: Round-1 sanity cap on requested tube length (Å) — generator geometry
#: size, not a physical limit; a caller that genuinely needs a longer tube
#: builds several and joins them (a later fusion op).
CNT_LENGTH_CAP_A = 500.0

#: vdW envelope margin (Å) added around the realized atom shell — the
#: Body→courtyard rule transferred from pcb-component-model.md ("envelope
#: is derived, not stored twice": vdW body + a margin).
VDW_MARGIN_A = 1.7

#: Experimental C60 bond lengths (Å) — 6:6 (between two hexagons, more
#: double-bond character, shorter) and 5:6 (hexagon-pentagon boundary,
#: longer). See the module docstring's truncation-parameter derivation.
FULLERENE_L66 = 1.401
FULLERENE_L56 = 1.458

#: Bond-length split (Å) between a Kekulé double (6:6) and single (5:6)
#: bond — strictly between the two experimental lengths above, matching
#: the bond-length-cluster test's own threshold.
_FULLERENE_KEKULE_SPLIT_A = 1.43

#: Bond-search cutoffs (Å) — comfortably above the realized bond lengths,
#: comfortably below the next-nearest-neighbor distance for each family.
_CNT_BOND_CUTOFF = 1.6
_FULLERENE_BOND_CUTOFF = 1.55

#: CNT bond order — Pauling estimate for a fully delocalized sp² sheet
#: (module docstring's "Bond order" section): 3 equivalent bonds/atom sum
#: to exactly carbon's max valence of 4.
_CNT_BOND_ORDER = 4.0 / 3.0

#: Fullerene Kekulé bond orders (module docstring's "Bond order" section)
#: — a genuine double bond on the shorter 6:6 bonds, single on 5:6.
_FULLERENE_ORDER_66 = 2.0
_FULLERENE_ORDER_56 = 1.0

#: Round-1 sanity cap on requested cone slant length (Å) — same rationale
#: as ``CNT_LENGTH_CAP_A``.
CONE_LENGTH_CAP_A = 500.0

#: Valid apex pentagon range for the cone/nanohorn generator (module
#: docstring's "Cone/nanohorn" section): P=0 is the degenerate flat-sheet
#: limit, P=6 the degenerate zero-radius capped-tube point — neither is a
#: cone this generator builds.
CONE_MIN_PENTAGONS = 1
CONE_MAX_PENTAGONS = 5

#: Chord-distortion tolerance for the near-apex truncation floor
#: (:func:`_cone_rho_min`, module docstring's "Cone/nanohorn" section): a
#: real bond of flat angular half-separation ``x = k·Δω/2`` maps to a cone
#: chord ``a_cc·sin(x)/x`` (vs. the true ``a_cc``); ``sin(x)/x ≈ 1 - x²/6``
#: for small ``x``, so ``x <= _CONE_CHORD_TOL`` keeps that ratio within
#: ``~1.5%`` at this module's chosen tolerance — comfortably inside the
#: bond-length test range ``[1.38, 1.47]`` Å around the true ``1.421`` Å.
_CONE_CHORD_TOL = 0.3


def _fmt_len(x: float) -> str:
    """Render a positive length for a cad-DSL token (``<key><number>``,
    plain decimal — the DSL's tokenizer has no exponent syntax)."""
    return f"{round(float(x), 4):g}"


def _bonds_by_cutoff(coords: np.ndarray, cutoff: float) -> list[tuple[int, int, float]]:
    """Every atom pair within ``cutoff`` Å, via a spatial hash grid (cell
    size = ``cutoff``) rather than an all-pairs scan — the CNT generator
    can realize thousands of atoms at the round-1 length cap, and only
    atoms sharing or neighboring a grid cell can ever be within cutoff of
    each other."""
    cells: dict[tuple[int, int, int], list[int]] = {}
    keys = [
        (
            math.floor(coords[i, 0] / cutoff),
            math.floor(coords[i, 1] / cutoff),
            math.floor(coords[i, 2] / cutoff),
        )
        for i in range(len(coords))
    ]
    for i, key in enumerate(keys):
        cells.setdefault(key, []).append(i)
    offsets = list(itertools.product((-1, 0, 1), repeat=3))
    bonds: list[tuple[int, int, float]] = []
    for i, key in enumerate(keys):
        for dx, dy, dz in offsets:
            for j in cells.get((key[0] + dx, key[1] + dy, key[2] + dz), ()):
                if j <= i:
                    continue
                d = float(np.linalg.norm(coords[i] - coords[j]))
                if d <= cutoff:
                    bonds.append((i, j, d))
    return bonds


# ── cnt ──────────────────────────────────────────────────────────────────


def _validate_cnt_params(raw: dict[str, Any]) -> tuple[int, int, float]:
    n_raw, m_raw, length_raw = raw.get("n"), raw.get("m"), raw.get("length_A")
    if n_raw is None or m_raw is None or length_raw is None:
        raise GeneratorError(
            "cnt needs 'n' (int >= 1), 'm' (int, 0 <= m <= n), and "
            "'length_A' (float > 0)"
        )
    try:
        n, m = int(n_raw), int(m_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"cnt 'n'/'m' must be integers, got n={n_raw!r} m={m_raw!r}"
        ) from exc
    if n != n_raw or m != m_raw:
        raise GeneratorError(
            f"cnt 'n'/'m' must be exact integers (the chiral indices), got n={n_raw!r} m={m_raw!r}"
        )
    if n < 1:
        raise GeneratorError(
            "cnt 'n' must be a positive integer — n=0 collapses the chiral "
            f"vector C_h = n·a1 + m·a2 to zero length (no circumference, no "
            f"tube); got n={n}"
        )
    if not (0 <= m <= n):
        raise GeneratorError(
            "cnt 'm' must satisfy 0 <= m <= n (the chiral vector "
            "C_h = n·a1 + m·a2 needs a1's coefficient n to dominate — the "
            f"(m, n) pair names the same tube by symmetry); got n={n}, m={m}"
        )
    try:
        length_A = float(length_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"cnt 'length_A' must be a number, got {length_raw!r}"
        ) from exc
    if not (0 < length_A <= CNT_LENGTH_CAP_A):
        raise GeneratorError(
            f"cnt 'length_A' must be > 0 and <= {CNT_LENGTH_CAP_A:g} Å "
            f"(round-1 generator-geometry sanity cap — join multiple tubes "
            f"for anything longer); got {length_A!r}"
        )
    return n, m, length_A


def build_cnt(raw: dict[str, Any]) -> GeneratedBlock:
    """A finite, open single-wall carbon nanotube of chirality ``(n, m)``
    and at-least-``length_A`` Å length (module docstring's construction)."""
    n, m, length_A = _validate_cnt_params(raw)

    a1 = GRAPHENE_A * np.array([math.sqrt(3) / 2, 0.5])
    a2 = GRAPHENE_A * np.array([math.sqrt(3) / 2, -0.5])
    d_ab = np.array([GRAPHENE_A_CC, 0.0])

    ch = n * a1 + m * a2
    circumference = float(np.linalg.norm(ch))
    radius = circumference / (2 * math.pi)

    d = math.gcd(n, m)
    dr = 3 * d if (n - m) % (3 * d) == 0 else d
    # Number-theory guarantee (Dresselhaus/Saito): dr always divides both
    # 2m+n and 2n+m exactly for a valid (n, m) pair — but that guarantee is
    # a property of the *current* validated domain (0 <= m <= n, n >= 1),
    # not of this formula in isolation; divmod + a remainder check keeps a
    # future loosened domain from silently truncating instead of failing
    # loud (this module's own "theorems failing loudly" standard).
    t1, t1_rem = divmod(2 * m + n, dr)
    t2_neg, t2_rem = divmod(2 * n + m, dr)
    if t1_rem != 0 or t2_rem != 0:
        raise GeneratorError(
            f"cnt (n={n}, m={m}): translation-vector coefficients "
            f"(2m+n)/dR={2 * m + n}/{dr} and (2n+m)/dR={2 * n + m}/{dr} "
            "did not divide evenly — the chiral-rolling construction's "
            "number-theory guarantee (dR always divides both exactly) "
            "assumes 0 <= m <= n; this (n, m) pair falls outside that "
            "assumption despite passing param validation (generator bug — "
            "file a gripe)"
        )
    t2 = -t2_neg
    t_vec = t1 * a1 + t2 * a2
    period = float(np.linalg.norm(t_vec))

    ch_hat = ch / circumference
    t_hat = t_vec / period

    # Search only the lattice points that can plausibly land inside the
    # (C_h, T) rectangle — its four corners in (p1, p2) integer-lattice
    # coordinates, padded by 2 for the second (B-sublattice) basis atom.
    corners_p1 = (0, n, t1, n + t1)
    corners_p2 = (0, m, t2, m + t2)
    p1_lo, p1_hi = min(corners_p1) - 2, max(corners_p1) + 2
    p2_lo, p2_hi = min(corners_p2) - 2, max(corners_p2) + 2

    eps = 1e-6
    cell_sites: list[tuple[float, float]] = []
    for p1 in range(p1_lo, p1_hi + 1):
        for p2 in range(p2_lo, p2_hi + 1):
            base = p1 * a1 + p2 * a2
            for site in (base, base + d_ab):
                u = float(np.dot(site, ch_hat))
                v = float(np.dot(site, t_hat))
                if -eps <= u < circumference - eps and -eps <= v < period - eps:
                    cell_sites.append((round(u, 8), round(v, 8)))
    cell_sites = list(dict.fromkeys(cell_sites))  # de-dup exact-boundary hits

    n_repeats = max(1, math.ceil(length_A / period))
    coords_list: list[list[float]] = []
    for k in range(n_repeats):
        for u, v in cell_sites:
            angle = u / radius
            coords_list.append(
                [radius * math.cos(angle), radius * math.sin(angle), v + k * period]
            )
    coords = np.array(coords_list, dtype=np.float64)
    actual_length = n_repeats * period
    n_atoms = len(coords)

    raw_bonds = _bonds_by_cutoff(coords, _CNT_BOND_CUTOFF)
    bond_count = [0] * n_atoms
    for i, j, _d in raw_bonds:
        bond_count[i] += 1
        bond_count[j] += 1

    ports: list[GeneratedPort] = []
    n_bottom = n_top = 0
    half = actual_length / 2.0
    for i in range(n_atoms):
        if bond_count[i] >= 3:
            continue
        z = coords[i, 2]
        if z < half:
            n_bottom += 1
            name = f"rim_b{n_bottom}"
            direction = [0.0, 0.0, -1.0]
        else:
            n_top += 1
            name = f"rim_t{n_top}"
            direction = [0.0, 0.0, 1.0]
        ports.append(
            GeneratedPort(
                name=name,
                atom_index=i,
                direction=direction,
                roles=["covalent", "sp2-rim"],
                expected_element="C",
            )
        )

    bonds = [(i, j, _CNT_BOND_ORDER, "aromatic") for i, j, _d in raw_bonds]

    envelope = f"cyl:r{_fmt_len(radius + VDW_MARGIN_A)}h{_fmt_len(actual_length)}"
    provenance = (
        f"SWCNT (n={n}, m={m}) — chiral rolling construction, graphene "
        f"a={GRAPHENE_A:g} Å (a_cc={GRAPHENE_A_CC:.4g} Å); "
        f"radius = a·√(n²+nm+m²)/2π = {radius:.4g} Å; "
        f"axial period |T| = {period:.4g} Å × {n_repeats} repeat(s) = "
        f"{actual_length:.4g} Å; {n_atoms} atoms, {len(bonds)} bonds, "
        f"{len(ports)} open-valence rim port(s)."
    )
    return GeneratedBlock(
        envelope=envelope,
        ports=ports,
        topology={
            "chiral_index": [n, m],
            "radius_A": radius,
            "pentagons": 0,
        },
        provenance=provenance,
        elements=["C"] * n_atoms,
        coords=coords,
        bonds=bonds,
    )


# ── fullerene ────────────────────────────────────────────────────────────

#: Cyclic permutations of the 3 axes — the icosahedron vertex set is every
#: cyclic (not full) permutation of (0, ±1, ±φ), see the module docstring.
_CYCLIC_AXES = ((0, 1, 2), (2, 0, 1), (1, 2, 0))


def _icosahedron_vertices() -> np.ndarray:
    phi = (1.0 + math.sqrt(5.0)) / 2.0
    verts: list[tuple[float, float, float]] = []
    for axes in _CYCLIC_AXES:
        for s1, s2 in itertools.product((1.0, -1.0), (1.0, -1.0)):
            v = [0.0, 0.0, 0.0]
            vals = (0.0, s1, s2 * phi)
            for k, ax in enumerate(axes):
                v[ax] = vals[k]
            verts.append((v[0], v[1], v[2]))
    return np.array(verts, dtype=np.float64)


def _validate_fullerene_atoms(raw: dict[str, Any]) -> int:
    atoms_raw = raw.get("atoms")
    if atoms_raw is None:
        raise GeneratorError("fullerene needs 'atoms' (int; round 1 supports only 60)")
    try:
        atoms = int(atoms_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"fullerene 'atoms' must be an integer, got {atoms_raw!r}"
        ) from exc
    if atoms != atoms_raw:
        raise GeneratorError(
            f"fullerene 'atoms' must be an exact integer, got {atoms_raw!r}"
        )
    if atoms != 60:
        raise GeneratorError(
            "fullerene round 1 supports exactly 60 atoms (C60, the Goldberg "
            "(1,1) isolated-pentagon cage) — Euler's formula forces exactly "
            "12 pentagons on any closed trivalent sp² cage regardless of "
            f"size, but the *hexagon* count (and hence total atoms) varies "
            f"by Goldberg (h, k) index; this generator's fixed truncated-"
            f"icosahedron construction only realizes (h, k) = (1, 1) = C60. "
            f"General Goldberg construction is a later round; got atoms={atoms}"
        )
    return atoms


def build_fullerene(raw: dict[str, Any]) -> GeneratedBlock:
    """The C60 buckminsterfullerene cage (module docstring's truncated-
    icosahedron-with-a-free-parameter construction)."""
    n_atoms = _validate_fullerene_atoms(raw)

    icosa = _icosahedron_vertices()
    icosa_edges: list[tuple[int, int]] = []
    for i in range(12):
        for j in range(i + 1, 12):
            if abs(float(np.linalg.norm(icosa[i] - icosa[j])) - 2.0) < 1e-6:
                icosa_edges.append((i, j))
    if len(icosa_edges) != 30:
        # Guards shipped-output correctness (icosahedron: 12 vertices,
        # degree 5 each, 30 edges) — a bare `assert` vanishes under
        # `python -O`; this module's own "theorems failing loudly"
        # standard applies to internal invariants too, not just param input.
        raise GeneratorError(
            f"fullerene: icosahedron vertex construction produced "
            f"{len(icosa_edges)} edges, expected 30 (generator bug — file "
            "a gripe)"
        )

    # s, t solve s*2t = L56 (pentagon edge) and s*(2-4t) = L66 (hex-hex
    # edge) simultaneously — closed form, module docstring's derivation.
    s = FULLERENE_L66 / 2.0 + FULLERENE_L56
    t = FULLERENE_L56 / (2.0 * s)

    # One new vertex per (icosahedron vertex i, neighbor j) ordered pair —
    # the truncation point on edge i-j nearest i, belonging to pentagon i.
    new_vertex: dict[tuple[int, int], np.ndarray] = {}
    for i, j in icosa_edges:
        new_vertex[(i, j)] = s * (icosa[i] + t * (icosa[j] - icosa[i]))
        new_vertex[(j, i)] = s * (icosa[j] + t * (icosa[i] - icosa[j]))
    keys = list(new_vertex.keys())
    coords = np.array([new_vertex[k] for k in keys], dtype=np.float64)
    if len(coords) != n_atoms:
        raise GeneratorError(
            f"fullerene: truncation produced {len(coords)} vertices, "
            f"expected {n_atoms} (generator bug — file a gripe)"
        )

    raw_bonds = _bonds_by_cutoff(coords, _FULLERENE_BOND_CUTOFF)
    # Kekulé assignment (module docstring's "Bond order" section): the
    # shorter 6:6 bonds are genuine double bonds, the longer 5:6 bonds
    # genuine single bonds — classified straight from the realized length,
    # never re-derived from the truncation-parameter bookkeeping.
    double_count = [0] * n_atoms
    bonds: list[tuple[int, int, float, str]] = []
    for i, j, d in raw_bonds:
        if d < _FULLERENE_KEKULE_SPLIT_A:
            order = _FULLERENE_ORDER_66
            double_count[i] += 1
            double_count[j] += 1
        else:
            order = _FULLERENE_ORDER_56
        bonds.append((i, j, order, "pairwise"))
    if double_count != [1] * n_atoms:
        offenders = [i for i, c in enumerate(double_count) if c != 1]
        raise GeneratorError(
            f"fullerene: Kekulé assignment gave atom(s) {offenders} a double-"
            "bond count other than exactly 1 (every C60 atom has exactly one "
            "6:6 double-bond neighbor under the isolated-pentagon rule — "
            "generator bug — file a gripe)"
        )

    shell_radius = float(np.max(np.linalg.norm(coords, axis=1)))
    envelope = f"sphere:r{_fmt_len(shell_radius + VDW_MARGIN_A)}"
    provenance = (
        "C60 buckminsterfullerene — truncated icosahedron (icosahedron "
        f"vertices from φ=(1+√5)/2, truncation parameter t={t:.6g} solved "
        f"so 5:6 bonds land at {FULLERENE_L56:g} Å and 6:6 bonds at "
        f"{FULLERENE_L66:g} Å); {n_atoms} atoms, {len(bonds)} bonds "
        "(Kekulé assignment: 30 double bonds on the 6:6 edges, 60 single "
        "on the 5:6 edges, each atom exactly one double bond), "
        "12 pentagons, 20 hexagons (Euler's formula: every closed "
        "trivalent sp² cage has exactly 12 pentagons)."
    )
    return GeneratedBlock(
        envelope=envelope,
        ports=[],
        topology={"pentagons": 12, "hexagons": 20},
        provenance=provenance,
        elements=["C"] * n_atoms,
        coords=coords,
        bonds=bonds,
    )


# ── cone / nanohorn ────────────────────────────────────────────────────────


def _validate_cone_params(raw: dict[str, Any]) -> tuple[int, float]:
    p_raw, length_raw = raw.get("pentagons"), raw.get("length_A")
    if p_raw is None or length_raw is None:
        raise GeneratorError(
            "cone needs 'pentagons' (int, 1-5 apex pentagons) and "
            "'length_A' (float > 0, max slant distance from the apex)"
        )
    try:
        p = int(p_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"cone 'pentagons' must be an integer, got {p_raw!r}"
        ) from exc
    if p != p_raw:
        raise GeneratorError(
            f"cone 'pentagons' must be an exact integer, got {p_raw!r}"
        )
    if p == 0:
        raise GeneratorError(
            "cone 'pentagons' must be 1-5 — P=0 removes no wedge at all "
            "(sin(α)=1-0/6=1, half-angle=90°): that's a flat graphene "
            "sheet, not a cone; there is no disclination to build here"
        )
    if p == 6:
        raise GeneratorError(
            "cone 'pentagons' must be 1-5 — P=6 removes the entire 360° "
            "(sin(α)=1-6/6=0, half-angle=0°): the cone degenerates to a "
            "zero-radius point, i.e. a capped-tube apex, not an open "
            "cone/nanohorn wall; build a CNT and cap it via a later "
            "fusion op instead"
        )
    if not (CONE_MIN_PENTAGONS <= p <= CONE_MAX_PENTAGONS):
        raise GeneratorError(
            f"cone 'pentagons' must satisfy {CONE_MIN_PENTAGONS} <= P <= "
            f"{CONE_MAX_PENTAGONS} — Euler counting bounds a single sp² "
            "disclination apex to the closed unit interval of pentagon "
            "counts between the flat-sheet (P=0) and capped-tube (P=6) "
            f"degenerate limits; got P={p}"
        )
    try:
        length_A = float(length_raw)
    except (TypeError, ValueError) as exc:
        raise GeneratorError(
            f"cone 'length_A' must be a number, got {length_raw!r}"
        ) from exc
    if not (0 < length_A <= CONE_LENGTH_CAP_A):
        raise GeneratorError(
            f"cone 'length_A' must be > 0 and <= {CONE_LENGTH_CAP_A:g} Å "
            f"(round-1 generator-geometry sanity cap); got {length_A!r}"
        )
    return p, length_A


def _cone_rho_min(k: float) -> float:
    """The near-apex truncation floor (module docstring's "Cone/nanohorn"
    section): a cone is a developable surface — unrolling it is a pure
    isometry that preserves *arc length* along any path exactly, for any
    (not just infinitesimal) angular extent, since ``sinα·k = 1`` by
    construction (``α`` the half-angle, ``k`` the angular compression
    factor ``2π/φ``). But a chemical bond's realized geometry is a
    straight-line *chord*, not an arc, and chords are NOT preserved once
    the subtended angle stops being small: two atoms at flat slant ``ρ``
    separated by flat angle ``Δω`` sit at true flat chord
    ``2ρ·sin(Δω/2)``, but map to cone chord ``2ρ·sinα·sin(k·Δω/2))``
    (both come straight from the two constructions' own polar geometry) —
    equal only in the small-angle limit. Near the apex, real bonds
    (``|Δω|`` up to a full ``60°`` for the very first hexagon ring, whose
    ``ρ`` is only ``a_cc`` itself) have a non-small subtended angle, and
    the cone chord badly undershoots ``a_cc`` — exactly the honest
    geometric reason this generator won't attempt a closed apex, and why
    it truncates rather than merely omitting the one literal apex point.
    Worst-case ``Δω`` for a genuine bond at radius ``ρ`` scales as
    ``a_cc/ρ`` (a bond vector oriented purely tangentially); requiring the
    half-angle argument ``x = k·Δω/2 = k·a_cc/(2ρ)`` stay under
    ``_CONE_CHORD_TOL`` gives ``ρ_min = k·a_cc / (2·_CONE_CHORD_TOL)``."""
    return (k * GRAPHENE_A_CC) / (2.0 * _CONE_CHORD_TOL)


def build_cone(raw: dict[str, Any]) -> GeneratedBlock:
    """An open (truncated-apex, both rims open) carbon nanocone/nanohorn
    wall carrying ``P`` apex pentagons, spanning ``length_A`` Å of slant
    distance beyond the auto-truncated small end (module docstring's
    "Cone/nanohorn" section, including why the small end is truncated
    past ``_cone_rho_min`` rather than at the true apex)."""
    p, length_A = _validate_cone_params(raw)

    a1 = GRAPHENE_A * np.array([math.sqrt(3) / 2, 0.5])
    a2 = GRAPHENE_A * np.array([math.sqrt(3) / 2, -0.5])
    d_ab = np.array([GRAPHENE_A_CC, 0.0])
    apex = (2.0 / 3.0) * a1 - (1.0 / 3.0) * a2

    phi = 2.0 * math.pi * (1.0 - p / 6.0)
    half_angle = math.asin(1.0 - p / 6.0)
    k = (2.0 * math.pi) / phi  # angular compression factor, >= 1

    rho_min = _cone_rho_min(k)
    rho_max = rho_min + length_A

    # Every lattice site (both sublattices) within slant [rho_min, rho_max]
    # of the apex and within the kept angular sector [0, phi) — see module
    # docstring for why this is an exact (not approximate) seam.
    n_search = math.ceil(rho_max / GRAPHENE_A) + 3
    eps = 1e-6
    rho_omega: list[tuple[float, float]] = []
    for p1 in range(-n_search, n_search + 1):
        for p2 in range(-n_search, n_search + 1):
            base = p1 * a1 + p2 * a2
            for site in (base, base + d_ab):
                rel = site - apex
                rho = float(np.linalg.norm(rel))
                if rho < rho_min - eps or rho > rho_max + eps:
                    continue
                omega = float(math.atan2(rel[1], rel[0])) % (2.0 * math.pi)
                # A site whose TRUE angle is exactly 0 (systematic along the
                # omega=0 cut ray, every ring, by the apex's 6-fold
                # symmetry) can have rel[1] land on tiny float noise like
                # -2.2e-16 — atan2 returns a small negative angle, and `%
                # (2*pi)` wraps that to 2*pi (bit-identical), not ~0. Fold
                # it back before the window test, or the omega=0 ray is
                # silently dropped (reviewer finding: spuriously
                # degree-2 mid-cone atoms, not real rim atoms).
                if omega >= 2.0 * math.pi - eps:
                    omega -= 2.0 * math.pi
                if not (-eps <= omega < phi - eps):
                    continue
                rho_omega.append((rho, omega))

    if not rho_omega:
        raise GeneratorError(
            f"cone(pentagons={p}, length_A={length_A!r}): no lattice atoms "
            f"found in slant range [{rho_min:.3g}, {rho_max:.3g}] Å — "
            "increase length_A"
        )

    coords_list: list[list[float]] = []
    for rho, omega in rho_omega:
        cone_angle = omega * k
        r_cyl = rho * math.sin(half_angle)
        z = rho * math.cos(half_angle)
        coords_list.append(
            [r_cyl * math.cos(cone_angle), r_cyl * math.sin(cone_angle), z]
        )
    coords = np.array(coords_list, dtype=np.float64)
    n_atoms = len(coords)

    # Same Cartesian nearest-neighbor bonding pass as cnt/fullerene — the
    # cut-edge seam needs no special handling (module docstring's "no
    # special seam-stitching code is needed" claim) once the near-apex
    # region (where the isometric unroll's chord-vs-arc distortion is
    # large) has been truncated away by ``rho_min``.
    raw_bonds = _bonds_by_cutoff(coords, _CNT_BOND_CUTOFF)
    bonds = [(i, j, _CNT_BOND_ORDER, "aromatic") for i, j, _d in raw_bonds]

    bond_count = [0] * n_atoms
    for i, j, _order, _kind in bonds:
        bond_count[i] += 1
        bond_count[j] += 1

    z_vals = coords[:, 2]
    z_mid = (float(np.min(z_vals)) + float(np.max(z_vals))) / 2.0
    ports: list[GeneratedPort] = []
    n_small = n_large = 0
    for i in range(n_atoms):
        if bond_count[i] >= 3:
            continue
        if z_vals[i] < z_mid:
            n_small += 1
            name = f"rim_small{n_small}"
            direction = [0.0, 0.0, -1.0]  # toward the (unrealized) apex
        else:
            n_large += 1
            name = f"rim_large{n_large}"
            direction = [0.0, 0.0, 1.0]  # away from the apex
        ports.append(
            GeneratedPort(
                name=name,
                atom_index=i,
                direction=direction,
                roles=["covalent", "sp2-rim"],
                expected_element="C",
            )
        )

    cone_height = rho_max * math.cos(half_angle)
    cone_radius = rho_max * math.sin(half_angle)
    envelope = f"cone:r{_fmt_len(cone_radius + VDW_MARGIN_A)}h{_fmt_len(cone_height)}"
    half_angle_deg = math.degrees(half_angle)
    provenance = (
        f"Carbon nanocone (P={p} apex pentagons) — wrapped-graphene-sheet "
        "disclination construction (Ge & Sattler 1994; Krishnan et al. "
        f"1997; nm-kind.md/dr173020/pc64732): half-angle sin(α)=1-P/6 → "
        f"α={half_angle_deg:.4g}°; kept sector φ=2π(1-P/6)="
        f"{math.degrees(phi):.4g}° of flat graphene, apex at a hexagon "
        "center (exact 60°-multiple lattice symmetry — no seam "
        f"distortion); slant range [{rho_min:.3g}, {rho_max:.3g}] Å "
        f"(small end auto-truncated past the near-apex chord-distortion "
        f"floor, see module docstring); {n_atoms} atoms, {len(bonds)} "
        f"bonds, {len(ports)} open-valence rim port(s) (OPEN cone — "
        "truncated apex, both rims open; no attempt to close the tip "
        "pentagon-by-pentagon this round)."
    )
    return GeneratedBlock(
        envelope=envelope,
        ports=ports,
        topology={"pentagons": p, "cone_half_angle_deg": half_angle_deg},
        provenance=provenance,
        elements=["C"] * n_atoms,
        coords=coords,
        bonds=bonds,
    )


# ── nanobud fusion op — SCOPE CHECK, NOT IMPLEMENTED THIS ROUND ────────────
#
# nm-kind.md's family roster asks for nanobud as a *fusion op* between two
# already-generated blocks (e.g. a fullerene fused onto a CNT/cone wall),
# framed as a [2+2]-cycloaddition-style 4-ring junction: two parallel
# fullerene 6:6 carbons bond to two adjacent tube-wall carbons, the four
# original ring bonds involved go from double/aromatic to single, no atoms
# removed. Scope-checked this round (slice 4a round 2) and deliberately
# SKIPPED — three real blockers, not just missing glue code:
#
# 1. **No rotation-capable cross-design merge exists yet.** Bonds are
#    Scene-local (:class:`precis.structure.scene.Bond` uses atom labels
#    within ONE scene) — fusing a fullerene design onto a CNT design needs
#    them merged into one scene first. The existing merge primitive,
#    ``import_fragment`` (:meth:`precis.handlers.structure.StructureHandler
#    ._import_fragment`), only offers a Cartesian **translation** offset —
#    no rotation. ``attach`` (``structure/ops.py::_op_attach``) DOES rotate
#    a fragment, but via a single dangling-bond direction (the "sum of unit
#    vectors to existing neighbours" construction) — it has nothing to grab
#    onto here, because both fusion partners are already fully-bonded,
#    closed-shell fragments with no open valence at the junction atoms. A
#    [2+2] junction needs a genuinely different, two-constraint alignment
#    (the 6:6 bond's edge vector mapped onto the wall bond's edge vector,
#    **and** each ring's local outward surface normal mapped to face the
#    other, i.e. a 2-vector-to-2-vector frame alignment) — new geometry
#    code, not a call to something that already exists.
# 2. **Site selection is a chemistry judgment call, not a mechanical one.**
#    Does ``fuse`` take explicit atom-label pairs from the caller (pushes
#    the geometric-compatibility judgment onto the LLM/human), or does it
#    search the two structures for a compatible bond pair (curvature-
#    matched, correctly facing) itself? A wrong choice silently mints a
#    strained or chemically implausible junction — this determines the op's
#    whole shape and belongs with whoever owns the fill-loop design (nm-
#    kind.md's slice 4b "propose" pattern already wrestles with exactly
#    this class of choice), not a local implementation guess.
# 3. **No place to record the result.** nm-kind.md's L2 growth path names
#    "disclination content" (pentagon/heptagon counts) as a future stored
#    invariant, but ``nm_topology`` (0001/0003 migrations) only has a shape
#    for threading/chirality pairs today — nm-kind.md says so explicitly
#    ("round 1 has nowhere in that table's shape for a scalar-valued
#    invariant like a chiral index"). A fusion's pentagon/hexagon budget
#    declaration needs a schema decision before any fuse op can honestly
#    record what it did — an architecture call, not a local one.
#
# An honest partial ([2+2] geometry with no schema-level bookkeeping, or a
# fuse op that silently assumes the caller always names the right atoms)
# was judged worse than skipping outright this round, per the task's own
# instruction. Next round: resolve (2) and (3) first (both are design
# decisions), then build the 2-vector frame-alignment geometry described in
# (1) — likely as a new ``fuse`` op living in :mod:`precis_nm.handler`
# (store-aware, same ``_prepare_*``/``_finish_*`` deferred-write split
# ``generate`` already uses) rather than in this pure-generator module,
# since it operates on two *already-bound* blocks, not a fresh one.
