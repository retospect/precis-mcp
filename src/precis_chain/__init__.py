"""precis_chain — pure-numpy polymer-chain geometry kernel
(docs/backlog/precis-chain-kernel.md).

The arithmetic every chain-shaped thing needs before any chemistry: centre-line
curves, rotation-minimizing frames, discrete curvature and a bend-radius rule,
helical register, the swept capsule tube, capsule clash with a broad phase,
loop reach, a rigid-body settle, and PDB/mmCIF atom records. DNA origami, RNA
folds, walker tracks and protein Calpha traces are all the same geometry
problem at this altitude; the domain vocabulary lives in the bindings
(``precis_se.chain`` first, then ``se-protein-chain-import``), never here.

The contract
------------

**Unit-agnostic.** No length, angle or energy in this package knows its unit.
Every function is homogeneous in the caller's length unit, angles are radians
throughout, and energies are in ``kT``. The single exception is
:mod:`precis_chain.pdb`, where the file format itself fixes angstroms — hence
the ``coords_A`` parameter name. Mixing units inside one call is the one way to
get silently wrong answers here, so pick a unit per design and stay in it.

**Pure functions over passed-in arrays.** No store access, no IO, no globals,
no mutation of an argument. Callers own persistence, units parsing and
vocabulary. Same house rule as :mod:`precis.structsolve` and
:mod:`precis_surface` — read for the shape, and note the deliberate
consequence: a caller must hand in every number, including the ones a domain
would call constants.

**No ``precis*`` or ``asa_*`` imports, ever.** numpy is the only dependency.
Enforced two ways: an AST walk over this package
(``tests/test_precis_chain_import_boundary.py``, the same approach as
``tests/test_hexfold_import_boundary.py``) and an import-linter contract in
``pyproject.toml``. Anything that needs the store, the CAD DSL or the ``se``
tree is a *binding* and lives outside. Where an idiom already exists inside
``precis`` (the FIRE-like descent of ``precis.structure.georelax.relax_graph``,
the array-in/array-out discipline of ``precis.structsolve.form_find``, the
``rot:rx,ry,rz`` Euler convention of ``precis.cad.vec``) it is *reimplemented*
here rather than imported, on purpose.

**Frames are ``(N, 3, 3)`` with columns ``(t, n, b)``.** ``frames[i][:, 0]`` is
the unit tangent, ``[:, 1]`` the normal, ``[:, 2]`` the binormal ``t x n``.
Each frame is a proper rotation taking local ``+x`` along the chain, local
``+y`` to the normal and local ``+z`` to the binormal. Rotation-*minimizing*,
not Frenet: all roll is authored by :func:`precis_chain.frames.apply_twist`,
never an artefact of the curve's shape. Positive twist is the right-hand rule
about the tangent.

**A loop of ``n`` units spans ``n + 1`` backbone bonds.** The loop contour is
``(n + 1) * c``, not ``n * c``, because the loop bridges two already-placed
backbone exits and so pays for a bond at each end. The design consequence: a
zero-unit crossover is a real connection with one bond of reach, feasible
exactly when the two exits are within ``c`` of each other. See
:mod:`precis_chain.loop`.

Numeric choices the spec left open (each argued at its definition)
------------------------------------------------------------------

- **Curvature at the endpoints** copies the nearest interior triple's value, so
  every returned array is ``(N,)`` and index-aligned with the path's points
  (:mod:`precis_chain.curvature`).
- **Capsule splitting is uniform in arc length**, with the piece count the max
  of the length and total-turning requirements — not a locally greedy walk, so
  the capsule count does not depend on how the path happened to be sampled
  (:func:`precis_chain.envelope.capsules_along`).
- **A capsule's pose origin is its ``a`` end**, not its midpoint, matching the
  CAD ``cyl`` primitive's base-at-local-origin convention
  (:func:`precis_chain.envelope.capsule_pose`).
- **Crossover azimuth window** defaults to a quarter of one unit's twist, so no
  unit ever sits exactly on the window boundary
  (:data:`precis_chain.register.CROSSOVER_WINDOW_FRACTION`).
- **Register tolerance** defaults to 1e-3 rad, between the 1e-4 rad that a
  21-unit two-turn repeat genuinely misses by and the ~1 deg at which real
  strain starts passing (:data:`precis_chain.register.REGISTER_TOL`).
- **Loop springs are one-sided** and the relax **ignores roll torque**; a body
  is two beads and has no roll degree of freedom
  (:mod:`precis_chain.relax`).
- **Clash and bend comparisons are strict** (``<``), so a design sitting exactly
  on its stated minimum gap or bend radius passes.

Modules
-------

- :mod:`precis_chain.path` — the ``Path`` triple (points, tangents, arc
  length), ``polyline``, ``hermite``, ``catmull_rom``, ``sample_at``,
  ``resample_arc_length``.
- :mod:`precis_chain.frames` — ``rmf_double_reflection`` (Wang et al. 2008
  double reflection), ``apply_twist``, ``twist_between``,
  ``accumulated_twist``.
- :mod:`precis_chain.curvature` — ``discrete_curvature`` (circumscribed circle
  of consecutive triples), ``bend_radius``, ``min_bend_radius_violations``.
- :mod:`precis_chain.motif` — the ``Motif`` repeat unit and its length/unit/turn
  arithmetic.
- :mod:`precis_chain.register` — ``phase_after``, ``commensurate``,
  ``crossover_positions`` over a caller-supplied ``Lattice``.
- :mod:`precis_chain.envelope` — ``Capsule``, ``capsules_along``,
  ``capsule_pose``.
- :mod:`precis_chain.clash` — ``capsule_distance``, ``clashes`` with an exact
  uniform-grid broad phase, and the vectorised segment-segment primitives
  under them.
- :mod:`precis_chain.fibre` — ``unit_frames``, ``backbone_exit``.
- :mod:`precis_chain.loop` — ``contour``, ``loop_feasible``, ``min_units``,
  ``loop_slack_energy``, ``loop_curve``.
- :mod:`precis_chain.relax` — ``relax_bundle`` over rigid segment bodies,
  hinges, loop springs, pins and excluded volume.
- :mod:`precis_chain.pdb` — ``write_pdb``, ``write_mmcif_atom_site``,
  ``read_trace``.
"""

from __future__ import annotations

from precis_chain.clash import (
    candidate_pairs,
    capsule_distance,
    capsule_gaps,
    clashes,
    segment_closest_points,
    segment_distance,
)
from precis_chain.curvature import (
    bend_radius,
    discrete_curvature,
    min_bend_radius_violations,
)
from precis_chain.envelope import Capsule, capsule_pose, capsules_along, total_turning
from precis_chain.fibre import UnitFrames, backbone_exit, unit_frames
from precis_chain.frames import (
    accumulated_twist,
    apply_twist,
    rmf_double_reflection,
    twist_between,
)
from precis_chain.loop import (
    contour,
    loop_curve,
    loop_feasible,
    loop_slack_energy,
    min_units,
)
from precis_chain.motif import (
    Motif,
    length_for_units,
    turns,
    units_for_length,
    units_per_turn,
)
from precis_chain.path import (
    Path,
    catmull_rom,
    hermite,
    polyline,
    resample_arc_length,
    sample_at,
)
from precis_chain.pdb import read_trace, write_mmcif_atom_site, write_pdb
from precis_chain.register import (
    CROSSOVER_WINDOW_FRACTION,
    REGISTER_TOL,
    Lattice,
    commensurate,
    crossover_positions,
    phase_after,
)
from precis_chain.relax import (
    Attachment,
    BundleResult,
    Hinge,
    LoopSpring,
    Pin,
    hinge_stiffness,
    relax_bundle,
)

__version__ = "0.1.0"

__all__ = [
    "CROSSOVER_WINDOW_FRACTION",
    "REGISTER_TOL",
    "Attachment",
    "BundleResult",
    "Capsule",
    "Hinge",
    "Lattice",
    "LoopSpring",
    "Motif",
    "Path",
    "Pin",
    "UnitFrames",
    "accumulated_twist",
    "apply_twist",
    "backbone_exit",
    "bend_radius",
    "candidate_pairs",
    "capsule_distance",
    "capsule_gaps",
    "capsule_pose",
    "capsules_along",
    "catmull_rom",
    "clashes",
    "commensurate",
    "contour",
    "crossover_positions",
    "discrete_curvature",
    "hermite",
    "hinge_stiffness",
    "length_for_units",
    "loop_curve",
    "loop_feasible",
    "loop_slack_energy",
    "min_bend_radius_violations",
    "min_units",
    "phase_after",
    "polyline",
    "read_trace",
    "relax_bundle",
    "resample_arc_length",
    "rmf_double_reflection",
    "sample_at",
    "segment_closest_points",
    "segment_distance",
    "total_turning",
    "turns",
    "twist_between",
    "unit_frames",
    "units_for_length",
    "units_per_turn",
    "write_mmcif_atom_site",
    "write_pdb",
]
