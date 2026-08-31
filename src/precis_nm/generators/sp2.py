"""The first two sp² carbon generators (docs/backlog/nm-kind.md
"Generators — parametric block factories", slice 4a build order (i)):
single-wall carbon nanotube (:func:`build_cnt`) and the C60 fullerene
(:func:`build_fullerene`). Both are closed-form/deterministic — no
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

#: Bond-search cutoffs (Å) — comfortably above the realized bond lengths,
#: comfortably below the next-nearest-neighbor distance for each family.
_CNT_BOND_CUTOFF = 1.6
_FULLERENE_BOND_CUTOFF = 1.55

#: Declared bond order for every generated sp² C-C bond (the ``ring``
#: template's ``aromatic=true`` convention, ``structure/ops.py`` — a
#: delocalized/aromatic bond, order 1.5, not the localized-order guess a
#: single Kekulé structure would need).
_SP2_BOND_ORDER = 1.5


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

    bonds = [(i, j, _SP2_BOND_ORDER) for i, j, _d in raw_bonds]

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
    bonds = [(i, j, _SP2_BOND_ORDER) for i, j, _d in raw_bonds]

    shell_radius = float(np.max(np.linalg.norm(coords, axis=1)))
    envelope = f"sphere:r{_fmt_len(shell_radius + VDW_MARGIN_A)}"
    provenance = (
        "C60 buckminsterfullerene — truncated icosahedron (icosahedron "
        f"vertices from φ=(1+√5)/2, truncation parameter t={t:.6g} solved "
        f"so 5:6 bonds land at {FULLERENE_L56:g} Å and 6:6 bonds at "
        f"{FULLERENE_L66:g} Å); {n_atoms} atoms, {len(bonds)} bonds, "
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
