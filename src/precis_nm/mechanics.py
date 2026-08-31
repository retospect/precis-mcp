"""L4 mechanics ceilings — slice 4a build order (iii)
(docs/backlog/nm-kind.md "Generators — parametric block factories",
"Mechanics ceilings" section): closed-form, defect-free continuum estimates
over a design's bound ``structure`` scenes. **Every number here is
advisory** — it never gates a ``validate`` finding, never blocks a write,
and is always rendered with the honesty caveat this module's own
:data:`HONESTY_NOTE` states once per view (:meth:`precis_nm.handler.
NmHandler._render_mechanics`): these are *pristine-lattice ceilings*, not
predictions of real strength — real materials fail at defects (grain
boundaries, vacancies, kinks) far below a defect-free continuum estimate
(the Griffith-crack gap); the literature/measurement layer, not this
module, supplies real-world numbers.

Three closed-form estimates, matching nm-kind.md's "Mechanics ceilings"
section:

1. **Min-cut tensile ceiling** (:func:`min_cut`) — "many bonds tougher than
   few" is literally graph theory: the maximum tensile force two points of
   a bonded structure can sustain before mechanically separating is
   bounded above by the WEAKEST cut — the minimum number of bonds whose
   simultaneous rupture disconnects one point from the other — times the
   single-bond rupture force. **Capacity choice (documented per the task
   spec): unit capacity per bond, not order-weighted.** A bond-order
   weighting would need an extra physical assumption this module doesn't
   have good grounds for (does a declared order-2 bond really sustain 2×
   the tensile rupture force of an order-1 bond under axial pulling, the
   same way it sustains roughly double the *dissociation* energy? Bond
   order tracks π-bond count / electron delocalization, not necessarily
   axial mechanical strength linearly) — unit capacity is the simpler,
   more defensible ceiling: it counts how many *independent* bonds must
   simultaneously break, which is exactly what "many bonds tougher than
   few" means. Max-flow (hence min-cut, by the max-flow-min-cut theorem)
   is computed with a plain pure-Python Edmonds-Karp BFS-augmenting-path
   solver — no new dependency, and cyclodextrin/CNT-scale molecule graphs
   (a few hundred atoms) are trivially small for it.
2. **Euler buckling** (:func:`euler_buckling_ceiling_nN`) — a generated
   tube treated as a thin-walled hollow beam: ``P_cr = π²EI/L²``,
   ``E`` = :data:`E_MODULUS_PA` (1 TPa, the standard sp² in-plane modulus
   order of magnitude), ``I = π·r³·t`` (a thin cylindrical shell's second
   moment of area), ``t`` = :data:`TUBE_WALL_THICKNESS_A` (3.4 Å, the
   graphite interlayer spacing — the conventional "wall thickness" stand-
   in for a single-atom-thick sp² sheet, since a true zero-thickness shell
   has no bending stiffness). **Schema-limitation workaround, documented
   honestly**: a generated block's ``chiral_index``/``radius_A`` topology
   facts are NOT persisted anywhere (``precis_nm.generators``'s module
   docstring: "persisting it onto ``nm_topology`` is a later round" — that
   table has no slot for a scalar-valued invariant yet) — so this module
   re-derives ``(radius, length)`` from the block's stored **envelope**
   instead (:func:`tube_geometry_from_envelope`): a ``cyl:r<>h<>``
   envelope (what every sp² tube/cone-family generator emits) is treated
   as a tube candidate, radius corrected by subtracting the generators'
   own :data:`~precis_nm.generators.sp2.VDW_MARGIN_A` (the margin every
   sp² generator's envelope already adds around the realized shell) to
   recover the physical shell radius. A cone's ``cone:r<>h<>`` envelope
   is NOT treated as a tube (a cone's wall isn't a constant-radius
   cylinder — buckling of a tapered shell is a different, harder formula,
   out of scope this round).
3. **Harmonic strain energy** (:func:`harmonic_strain_energy_eV`) — a
   generic, single, order-of-magnitude force constant
   (:data:`K_THETA_EV_PER_RAD2`, documented as such — real force constants
   vary by hybridization/element and would need a DFT/force-field rung,
   the later "charge/optical panel" phase's territory) applied over every
   declared-bond angle triple in a bound scene, using the SAME
   hybridization-inference + VSEPR-ideal-angle machinery
   :mod:`precis.structure.vsepr` already uses for the (separate, gating)
   warn-tier ``angle_strain`` rule — reused here, not re-derived, so a
   pristine generator output (already VSEPR-ideal by construction) scores
   near-zero strain and a genuinely bent structure scores positive,
   consistently with what ``vsepr.py`` would also flag.
"""

from __future__ import annotations

import math
from collections import deque

from precis.cad import dsl as cad_dsl
from precis.structure import probe
from precis.structure import vsepr as struct_vsepr
from precis.structure.scene import Scene
from precis_nm.generators.sp2 import VDW_MARGIN_A

#: Single-bond C-C rupture force under axial pulling, AFM single-molecule
#: force-spectroscopy range ~4-6 nN (nm-kind.md's "Mechanics ceilings"
#: section cites this range) — the midpoint is used as the per-bond
#: capacity in :func:`min_cut`'s tensile-ceiling conversion.
RUPTURE_FORCE_NN = 5.0

#: In-plane sp² Young's modulus order of magnitude (Pa) — the standard
#: ~1 TPa figure for graphene/CNT walls (nm-kind.md's "Mechanics ceilings").
E_MODULUS_PA = 1.0e12

#: Conventional "wall thickness" (Å) for a single-atom-thick sp² shell —
#: the graphite interlayer spacing, the standard stand-in this ceiling
#: formula uses (a literal zero-thickness shell has no bending stiffness
#: at all — ``I`` would vanish).
TUBE_WALL_THICKNESS_A = 3.4

#: A single, generic, order-of-magnitude harmonic angle force constant
#: (eV/rad²) — module docstring's point 3. NOT element/hybridization-
#: specific; a later rung (DFT/force-field) would replace this with real
#: per-element k_θ values.
K_THETA_EV_PER_RAD2 = 0.5

#: Rendered once per ``view='mechanics'`` response
#: (:meth:`precis_nm.handler.NmHandler._render_mechanics`) — the module
#: docstring's honesty caveat, never omitted.
HONESTY_NOTE = (
    "All figures below are defect-free continuum estimates over a pristine "
    "lattice — advisory only, never gate validate/put/edit. Real strength "
    "is defect-dominated (grain boundaries, vacancies, kinks push real "
    "failure well below these ceilings); consult the literature/measured "
    "layer for real-world numbers."
)


# ── 1. min-cut tensile ceiling ──────────────────────────────────────────


def min_cut(scene: Scene, source: str, sink: str) -> tuple[int, float]:
    """Max-flow/min-cut (unit capacity per bond, module docstring's point 1)
    between atoms ``source`` and ``sink`` over EVERY bond in ``scene``
    (declared + auto-detected alike — a real physical bond either way, for
    the purposes of "how many must break to separate these two points").
    Returns ``(min_cut_bond_count, tensile_ceiling_nN)`` —
    ``min_cut_bond_count * RUPTURE_FORCE_NN``. ``0`` (not an error) when
    ``source``/``sink`` sit in different connected components (or either
    is missing from ``scene``) — a genuinely disconnected structure has
    zero tensile ceiling between those two points, the honest answer, not
    a failure.
    """
    if source not in scene.atoms or sink not in scene.atoms or source == sink:
        return 0, 0.0
    # Undirected unit-capacity graph: each bond becomes two directed edges,
    # residual capacity tracked in a plain dict (Edmonds-Karp — a BFS
    # augmenting-path search per round, polynomial and simple; molecule-
    # scale graphs here are a few hundred atoms at most).
    capacity: dict[tuple[str, str], int] = {}
    for bond in scene.bonds:
        if bond.i not in scene.atoms or bond.j not in scene.atoms:
            continue
        capacity[(bond.i, bond.j)] = capacity.get((bond.i, bond.j), 0) + 1
        capacity[(bond.j, bond.i)] = capacity.get((bond.j, bond.i), 0) + 1

    def bfs_augment() -> list[str] | None:
        parent: dict[str, str] = {source: source}
        queue: deque[str] = deque([source])
        while queue:
            u = queue.popleft()
            if u == sink:
                path = [sink]
                while path[-1] != source:
                    path.append(parent[path[-1]])
                return list(reversed(path))
            for (a, b), cap in capacity.items():
                if a == u and cap > 0 and b not in parent:
                    parent[b] = u
                    queue.append(b)
        return None

    max_flow = 0
    while True:
        path = bfs_augment()
        if path is None:
            break
        bottleneck = min(capacity[(path[k], path[k + 1])] for k in range(len(path) - 1))
        for k in range(len(path) - 1):
            edge = (path[k], path[k + 1])
            rev = (path[k + 1], path[k])
            capacity[edge] -= bottleneck
            capacity[rev] = capacity.get(rev, 0) + bottleneck
        max_flow += bottleneck
    return max_flow, max_flow * RUPTURE_FORCE_NN


# ── 2. Euler buckling ────────────────────────────────────────────────────


def euler_buckling_ceiling_nN(radius_A: float, length_A: float) -> float:
    """``P_cr = π²EI/L²`` for a thin-walled cylindrical tube of ``radius_A``
    Å and ``length_A`` Å (module docstring's point 2), reported in nN."""
    r_m = radius_A * 1e-10
    length_m = length_A * 1e-10
    t_m = TUBE_WALL_THICKNESS_A * 1e-10
    moment_of_inertia = math.pi * r_m**3 * t_m
    p_cr_n = (math.pi**2) * E_MODULUS_PA * moment_of_inertia / (length_m**2)
    return p_cr_n * 1e9  # N -> nN


def tube_geometry_from_envelope(envelope: str | None) -> tuple[float, float] | None:
    """``(radius_A, length_A)`` for a generated tube block, re-derived from
    its STORED envelope (module docstring's point 2, "schema-limitation
    workaround") — ``None`` when ``envelope`` isn't a ``cyl:r<>h<>``
    config (a cone's tapered wall isn't a constant-radius buckling
    candidate this round, and any other shape is simply not a tube), or
    when subtracting the generators' own VDW margin would leave a
    non-positive radius (a hand-authored ``cyl`` envelope smaller than the
    margin — not a generated tube, don't guess)."""
    if not envelope:
        return None
    try:
        spec = cad_dsl.parse(envelope)
    except cad_dsl.DslError:
        return None
    if spec.alias != "cyl":
        return None
    radius_A = spec.params["r"] - VDW_MARGIN_A
    length_A = spec.params["h"]
    if radius_A <= 0 or length_A <= 0:
        return None
    return radius_A, length_A


# ── 3. harmonic strain energy ────────────────────────────────────────────


def harmonic_strain_energy_eV(scene: Scene) -> tuple[float, int]:
    """Sum of ``½·k_θ·(θ−θ₀)²`` (module docstring's point 3) over every
    declared-bond angle triple in ``scene`` — ``θ₀`` from
    :func:`precis.structure.vsepr.ideal_angle` at each vertex's inferred
    hybridization (:func:`precis.structure.vsepr.infer_hybridization`),
    ``θ`` measured via :func:`precis.structure.probe.angle`. Vertices with
    no VSEPR-applicable element (metals, or an element outside
    ``vsepr``'s hybridization table) or fewer than 2 declared covalent
    neighbours contribute nothing. Returns ``(total_energy_eV,
    triple_count)`` so a caller can render "N angles, X eV" rather than a
    bare number that reads as zero-because-empty (the maze.py filled-
    fraction lesson, applied at this metric's own scale)."""
    adj: dict[str, set[str]] = {label: set() for label in scene.atoms}
    for bond in scene.bonds:
        if bond.provenance != "declared":
            continue
        if bond.i in adj and bond.j in adj:
            adj[bond.i].add(bond.j)
            adj[bond.j].add(bond.i)

    total_eV = 0.0
    n_triples = 0
    for label, atom in scene.atoms.items():
        hyb = struct_vsepr.infer_hybridization(scene, label)
        if hyb is None:
            continue
        ideal = struct_vsepr.ideal_angle(atom.element, hyb)
        if ideal is None:
            continue
        neighbors = sorted(adj.get(label, ()))
        if len(neighbors) < 2:
            continue
        for i in range(len(neighbors)):
            for j in range(i + 1, len(neighbors)):
                measured = probe.angle(scene, neighbors[i], label, neighbors[j])
                dev_rad = math.radians(measured - ideal)
                total_eV += 0.5 * K_THETA_EV_PER_RAD2 * dev_rad**2
                n_triples += 1
    return total_eV, n_triples
