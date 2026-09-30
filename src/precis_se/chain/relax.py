"""The ``relax_chain`` op — a mechanical settle of a design's helix
segments, and the ``meta.loop_curve`` seam it writes.

**What this is not.** A mechanical settle, not thermodynamics and not
sampling, with **no topology detection**: a loop spring will pull a loop
straight through a helix and nothing here notices that it did
(:mod:`precis_chain.relax`'s own module docstring says the same, and the
skill says it to the agent). It finds a low-energy configuration near the
one it was handed — a design that starts threaded wrongly stays threaded
wrongly, and a settled geometry is a *proposal*, never a claim that the
design is now correct. ``chain_clash`` on the settled tree is still the
only non-overlap answer (the kernel's excluded volume is a penalty, so a
taut loop settles its two capsules ~1% interpenetrated).

**Why this op is handler-level** (``precis_se.atomic.apply.
HANDLER_LEVEL_OPS``): it spends compute, and it reads the store. The
hinge stiffness is the worm-like-chain constant
:func:`precis_chain.relax.hinge_stiffness`, whose persistence length comes
from a ``material`` row when the design has one
(:data:`precis_se.compose.LP_KEY`, resolved through
:func:`precis_se.library._resolve_star_value`) and from
:mod:`precis_se.chain.nucleic`'s coded default otherwise. The op's summary
line names which, per helix, with the row's conditions — a settle run
against a 45 nm measured Lp and one run against the coded 50 nm are
different answers and must not look alike.

**Bodies are the ``layout_chain`` children**, one two-bead rigid body per
segment child (the kernel's segmentation decision). A helix with no
segment children has nothing to settle and is refused by name: laying it
out silently would write children the caller never asked for, and skipping
it silently would settle a design missing a quarter of its geometry and
report success.

**The bundle is built in nanometres, not metres.** The kernel is
unit-agnostic but FIRE is not scale-free in its *defaults*: at metre-scale
coordinates every force in a nucleic-acid design is ~1e-9, so the default
convergence tolerance is met at step 0 and the default per-step cap
(5% of a body length) is ~3e-10 — a settle that reports converged without
moving. Nanometres are :mod:`precis_se.chain.nucleic`'s own unit and put
the forces at order 1. Everything crossing the boundary back into the tree
is metres again (:data:`_NM_PER_M`).

**The pose write-back contract** is :func:`precis_se.formfind.op_formfind`'s,
verbatim: only a child already stamped ``origin='proposed'`` moves (which
``layout_chain`` stamps every segment it cuts), ``move=`` is the author's
explicit authorisation, a name that cannot move is refused rather than
ignored, and every refusal path raises **before** the first write — the op
lands whole or not at all. An immovable child is not merely skipped: both
its beads are **hard-pinned**, so the user's placement constrains the
settle instead of being quietly contradicted by it.
"""

from __future__ import annotations

import dataclasses
import itertools
import math
from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.cad import dsl as cad_dsl
from precis.cad.vec import as_vec3, euler_rad_from_matrix, rotation
from precis.design import states as design_states
from precis.errors import BadInput
from precis.utils.units import format_quantity, parse_quantity
from precis_chain.envelope import Capsule, capsule_pose
from precis_chain.loop import contour, loop_curve
from precis_chain.relax import (
    Attachment,
    BundleResult,
    Hinge,
    LoopSpring,
    Pin,
    carry_rotation,
    hinge_stiffness,
    relax_bundle,
)
from precis_se import compose as se_compose
from precis_se import library as se_library
from precis_se.chain.layout import (
    HelixGeometry,
    helix_geometry,
    local_capsule,
    segment_capsule,
    segment_pose,
)
from precis_se.chain.occupancy import apply_occupancy
from precis_se.chain.vocab import (
    HELIX_ROLE,
    SEGMENT_ROLE,
    STRAND_ROLE,
    ChainError,
    DomainSpec,
    chain_role,
    group_domains,
)
from precis_se.ops import OpError, SeTree, compose_world_pose
from precis_se.state_arg import merged_occupancy

#: Nanometres per metre — the one scale factor in this module (module
#: docstring: the bundle is built in nanometres and written back in metres).
_NM_PER_M = 1.0e9

#: Keys the op accepts.
_ALLOWED_KEYS = frozenset({"op", "move", "iters", "state"})

#: A walker body whose envelope is flat (or a sphere) still needs an axis
#: the kernel can carry a rotation on: the shorter of its extents is
#: floored here (metres) rather than left degenerate.
_WALKER_MIN_EXTENT_M = 1e-9

#: Default FIRE steps. 500 is the kernel's own default and settles a
#: two-helix crossover to its contour in ~100; the 192-segment rectangle
#: acceptance criterion (< 30 s) is what caps it.
DEFAULT_ITERS = 500

#: Upper bound on ``iters=`` — a settle is human-Apply gated, not a place
#: to spend unbounded compute.
MAX_ITERS = 5000

#: Convergence threshold on the max per-bead force, in the bundle's
#: nanometre units. A loop spring's constant is 1.0 nm-force per nm, so
#: this is a residual loop extension of 1e-4 nm = 0.1 pm — three orders
#: below a backbone bond, and well inside what the segmentation itself
#: approximates. The kernel's own 1e-6 default is not reachable in a few
#: hundred steps with a stiff excluded-volume term present, and a settle
#: that reports ``converged=False`` forever teaches a caller to ignore the
#: flag.
TOL_NM = 1e-4

#: Samples per stored loop curve. 16 points is a shape a renderer and a
#: realizer can both use; the kernel's own default is 32, which doubles
#: the jsonb on a design with a few hundred loops for a curve nobody
#: measures that finely (the curve's arc length is approximate anyway —
#: :func:`precis_chain.loop.loop_curve`).
LOOP_CURVE_SAMPLES = 16


@dataclass
class _Body:
    """One segment child as the settle sees it.

    ``a_nm``/``b_nm`` are the body's two bead positions in world
    nanometres, taken from the child's **current** placement (so a second
    ``relax_chain`` continues from the first's result rather than resetting
    to the declaration). ``carry`` maps a vector from the *nominal* body
    frame — the one ``layout_chain``'s capsule pose defines, which is the
    frame the helix's backbone exits are computed in — into that current
    placement, so an exit offset stays attached where it belongs after any
    number of settles.
    """

    name: str
    helix: str
    start: int
    end: int
    a_nm: np.ndarray
    b_nm: np.ndarray
    radius_nm: float
    length_nm: float
    carry: np.ndarray
    movable: bool
    #: A state-owning non-chain block settled as one more rigid body
    #: (:func:`_walker_body`) — never hinged, never written back as a
    #: segment; its settled placement goes to the state's own pose slot.
    walker: bool = False


@dataclass(frozen=True)
class _Lp:
    """The persistence length one helix's hinges are built from, and where
    it came from — the string the summary line names."""

    value_m: float
    source: str
    from_store: bool


def _rot(euler: Any) -> np.ndarray:
    """The ``Rz @ Ry @ Rx`` rotation matrix for a stored ``rot`` triple —
    se's pose convention (:func:`precis.cad.vec.rotation`)."""
    rx, ry, rz = (float(v) for v in as_vec3(list(euler)))
    return np.asarray(rotation(rx, ry, rz).R, dtype=float)


#: The carry :func:`precis_chain.relax.relax_bundle` applies to an
#: :class:`~precis_chain.relax.Attachment`'s offset when its body's axis
#: turns. Imported rather than reimplemented so this module's post-settle
#: exit positions agree with the ones the kernel used *inside* the settle.
_carry_rotation = carry_rotation


def _triple(v: np.ndarray) -> tuple[float, float, float]:
    """A ``(3,)`` array as the kernel's own 3-tuple parameter type."""
    return (float(v[0]), float(v[1]), float(v[2]))


def _unit(v: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(v))
    return v / norm if norm > 1e-12 else np.array([0.0, 0.0, 1.0])


def _helices(tree: SeTree) -> dict[str, HelixGeometry]:
    """Every helix's geometry, or an ``OpError`` naming the one that cannot
    produce any — unlike the DRC pass (which reports a malformed record as
    a finding), an op refuses: it is about to write poses derived from that
    geometry."""
    out: dict[str, HelixGeometry] = {}
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        if chain_role(node) != HELIX_ROLE:
            continue
        try:
            out[name] = helix_geometry(node)
        except (ChainError, KeyError, ValueError) as exc:
            raise OpError(
                f"relax_chain: helix {name!r} has no usable geometry ({exc}) — "
                "fix the declaration before settling poses derived from it"
            ) from exc
    if not out:
        raise OpError(
            "relax_chain: this design declares no helices — declare_helix "
            "first (the settle's bodies are a helix's layout_chain segments)"
        )
    return out


def _segment_children(tree: SeTree, helix: str) -> list[Any]:
    """A helix's ``layout_chain`` children in ``ord`` order."""
    return sorted(
        (
            node
            for node in tree.blocks.values()
            if chain_role(node) == SEGMENT_ROLE
            and (node.chain or {}).get("helix") == helix
        ),
        key=lambda node: int((node.chain or {}).get("ord", 0)),
    )


def _movable_names(tree: SeTree, op: dict[str, Any], bodies: list[_Body]) -> set[str]:
    """Which segment children this call may move — ``formfind``'s contract
    (module docstring), with a helix name accepted as shorthand for all of
    its segments."""
    names = [b.name for b in bodies]
    by_helix: dict[str, list[str]] = {}
    for body in bodies:
        by_helix.setdefault(body.helix, []).append(body.name)
    raw = op.get("move")
    if raw is None:
        movable = {
            b.name
            for b in bodies
            if tree.blocks[b.name].origins.get("pose") == "proposed"
        }
        if not movable:
            raise OpError(
                "relax_chain: no movable segments — every layout_chain child's "
                "pose is user contract (origin unset = user). Authorize "
                "explicitly with move=[...helix or segment names...] or "
                "move='all', or stamp them revisable (set_pose "
                "origin='proposed'); re-running layout_chain also stamps them"
            )
        return movable
    if raw == "all":
        return set(names)
    if not isinstance(raw, list) or not all(isinstance(n, str) for n in raw):
        raise OpError(
            "relax_chain 'move' must be 'all' or a list of helix/segment block "
            f"names, got {raw!r}"
        )
    named: set[str] = set()
    for name in raw:
        if name in by_helix:
            named.update(by_helix[name])
            continue
        if name in names:
            named.add(name)
            continue
        roster = ", ".join(sorted(by_helix))
        raise OpError(
            f"relax_chain 'move' names no helix and no segment: {name!r}. "
            f"Helices: {roster} (or any of their {len(names)} "
            "<helix>.s<k> segment children by name)"
        )
    if not named:
        raise OpError("relax_chain 'move' is an empty list — nothing to settle")
    return named


def material_lp_m(
    store: Any, tree: SeTree, names: list[str]
) -> dict[str, tuple[float, str]]:
    """``{block: (persistence length in metres, where it came from)}`` for
    every named block the design has a ``material``
    :data:`precis_se.compose.LP_KEY` row for — and **only** those, so a
    caller supplies its own coded default and says so.

    The row's number comes from :func:`precis_se.library.value_row_number`
    and is converted from the property registry's own unit (``nm`` by
    default) to metres through :func:`precis.utils.units.parse_quantity`. A
    row whose unit is not a length cannot be a persistence length, so it is
    treated as **absent** rather than guessed at: a coded default is a
    known number and a mis-read row is not.

    An unsaved tree (no ``own_slug``, or no ref under it yet) reads
    nothing — a ``put`` that declares a design and settles it in one call
    has no design-level ``made-of`` link to resolve against yet.

    One reader for both consumers (``relax_chain``'s hinge stiffness and
    the handler-side ``chain_floppy`` re-emission) so a settle and the
    finding that grades it can never disagree about which row won.
    """
    out: dict[str, tuple[float, str]] = {}
    slug = getattr(tree, "own_slug", None)
    if not slug or store is None:
        return out
    ref = store.get_ref(kind="se", id=slug)
    if ref is None:
        return out
    cache = se_library._ReadCache(store)
    unit = se_compose.unit_label(cache, se_compose.LP_KEY)
    for name in names:
        node = tree.blocks.get(name)
        if node is None:
            continue
        cand = se_library._Candidate(str(slug), name, node, tree, ref.id)
        hit = se_library._resolve_star_value(cand, se_compose.LP_KEY, cache)
        if hit is None:
            continue
        row, _unit, provenance = hit
        value = se_library.value_row_number(row)
        if value is None:
            continue
        try:
            value_m = parse_quantity(f"{value} {unit}", "length")
        except BadInput:
            continue  # not a length — a mis-read row is worse than the default
        if not math.isfinite(value_m) or value_m <= 0.0:
            continue
        conditions = se_library.format_conditions(row.get("conditions"))
        source = f"material row {format_quantity(value_m, 'length')}"
        if conditions:
            source += f" at {conditions}"
        if provenance:
            source += f" ({provenance})"
        out[name] = (value_m, source)
    return out


def _lp_of(store: Any, tree: SeTree, geoms: dict[str, HelixGeometry]) -> dict[str, _Lp]:
    """Each helix's persistence length for the hinge springs: its
    ``material`` row (:func:`material_lp_m`) when the design has one, else
    the motif's own coded default."""
    out = {
        name: _Lp(
            geom.base_motif.persistence_length,
            f"coded {geom.base_motif.name} default",
            False,
        )
        for name, geom in geoms.items()
    }
    for name, (value_m, source) in material_lp_m(store, tree, sorted(geoms)).items():
        out[name] = _Lp(value_m, source, True)
    return out


def _bodies(tree: SeTree, geoms: dict[str, HelixGeometry]) -> list[_Body]:
    """One body per segment child, in helix then ``ord`` order. Refuses a
    helix with no children by name (module docstring)."""
    out: list[_Body] = []
    for helix in sorted(geoms):
        geom = geoms[helix]
        children = _segment_children(tree, helix)
        if not children:
            raise OpError(
                f"relax_chain: helix {helix!r} has no layout_chain children — "
                "run layout_chain first (the settle's rigid bodies ARE those "
                "segments; this op neither lays a helix out nor skips one "
                "silently)"
            )
        for node in children:
            record = node.chain or {}
            start, end = int(record["start"]), int(record["end"])
            if not (0 <= start <= end < geom.n_units):
                raise OpError(
                    f"relax_chain: segment {node.name!r} claims units "
                    f"[{start}, {end}] of a helix with {geom.n_units} — "
                    "re-run layout_chain to re-tile it"
                )
            nominal = segment_capsule(geom, start, end)
            _origin, euler, length_m = capsule_pose(nominal)
            carry = _rot(node.rot) @ _rot(euler).T
            a_nm = np.asarray(node.pose, dtype=float).reshape(3) * _NM_PER_M
            length_nm = length_m * _NM_PER_M
            axis = _rot(node.rot) @ np.array([0.0, 0.0, 1.0])
            out.append(
                _Body(
                    name=str(node.name),
                    helix=helix,
                    start=start,
                    end=end,
                    a_nm=a_nm,
                    b_nm=a_nm + length_nm * axis,
                    radius_nm=geom.motif.radius * _NM_PER_M,
                    length_nm=length_nm,
                    carry=carry,
                    movable=False,
                )
            )
    return out


def _attachment(
    body: _Body, index: int, geom: HelixGeometry, offset: int, forward: bool
) -> Attachment:
    """The :class:`~precis_chain.relax.Attachment` for one backbone exit.

    ``along`` is the exit's fractional position on the body axis; the
    offset vector is the exit's displacement from that axis point **in the
    nominal frame**, carried into the body's current placement. The exit
    itself is :func:`precis_chain.fibre.backbone_exit` at the strand's own
    azimuth (:func:`precis_se.chain.nucleic.strand_azimuth_rad` — the pair
    is groove-asymmetric, not antipodal, so a forward and a reverse domain
    at one offset take *different* azimuths).
    """
    span = body.end - body.start
    along = 0.0 if span <= 0 else (offset - body.start) / span
    a_n = geom.origin(body.start) * _NM_PER_M
    b_n = geom.origin(body.end) * _NM_PER_M
    exit_n = geom.exit(offset, forward) * _NM_PER_M
    off = body.carry @ (exit_n - (a_n + along * (b_n - a_n)))
    return Attachment(body=index, along=along, offset=_triple(off))


def _exit_point(
    body: _Body, bodies_nm: np.ndarray, index: int, att: Attachment
) -> np.ndarray:
    """Where ``att``'s exit ended up, in world nanometres — the kernel's own
    rule (:func:`_carry_rotation`) applied to the settled bodies."""
    a, b = bodies_nm[index, 0], bodies_nm[index, 1]
    base = a + att.along * (b - a)
    carry = _carry_rotation(_unit(body.b_nm - body.a_nm), _unit(b - a))
    return base + carry @ np.asarray(att.offset, dtype=float)


def _exit_tangent(
    body: _Body, bodies_nm: np.ndarray, index: int, att: Attachment, axial: float
) -> np.ndarray:
    """The direction the backbone leaves ``att``'s exit along, in the
    **settled** frame: the outward radial (exit minus axis point, carried
    like :func:`_exit_point` carries the exit itself) plus, on a terminal
    unit, the helix's axis direction the strand runs on out of the helix
    (``axial`` = :func:`_axial_sign`). An equal blend — the backbone at a
    helix end leaves the last pair diagonally, not flat in its plane.
    Zero when the exit has no outward direction at all (on-axis)."""
    a, b = bodies_nm[index, 0], bodies_nm[index, 1]
    axis = _unit(b - a)
    carry = _carry_rotation(_unit(body.b_nm - body.a_nm), axis)
    radial = carry @ np.asarray(att.offset, dtype=float)
    norm = float(np.linalg.norm(radial))
    if norm < 1e-12:
        return np.zeros(3)
    return radial / norm + axial * axis


@dataclass
class _Loop:
    """One loop the settle pulls on, and the domain row that owns its
    curve (the LATER domain — the one carrying ``loop_before_nt``)."""

    after: DomainSpec
    n_nt: int
    c_m: float
    spring: LoopSpring
    body_a: int
    body_b: int
    att_a: Attachment
    att_b: Attachment
    #: The axial component each exit tangent carries: ``+1``/``-1`` = the
    #: body's axis direction the backbone leaves along when the exit sits
    #: on the helix's **terminal** unit (a hairpin or a tail caps the helix
    #: end — gripe 457929), ``0`` = a mid-helix exit that leaves sideways.
    axial_a: float = 0.0
    axial_b: float = 0.0


def _axial_sign(
    geom: HelixGeometry, offset: int, forward: bool, *, leaving: bool
) -> float:
    """The signed axis direction a backbone exit points **out of** the helix
    along, or ``0.0`` when the exit is not on a terminal unit.

    A strand runs ``+t`` when forward and ``-t`` when reverse. Leaving the
    helix (the loop's start) the backbone keeps going in the strand's own
    direction; entering it (the loop's end) the outward direction is the
    strand's direction reversed. Only the helix's own last unit counts —
    a segment boundary mid-helix is a tiling seam, not an end.
    """
    strand_dir = 1.0 if forward else -1.0
    outward = strand_dir if leaving else -strand_dir
    at_end = offset == (geom.n_units - 1 if outward > 0 else 0)
    return outward if at_end else 0.0


def _loops(
    tree: SeTree, geoms: dict[str, HelixGeometry], bodies: list[_Body]
) -> list[_Loop]:
    """A loop spring per consecutive domain pair, pinned at the two
    backbone exits, resting at the ``(n + 1)``-bond contour
    (:func:`precis_chain.loop.contour` — a 0-nt crossover is a real
    connection with one bond of reach).

    No groove-asymmetry allowance here, deliberately: ``chain_loop_short``
    adds :func:`precis_se.chain.nucleic.backbone_frustration_m` to decide
    whether a loop *can* reach, which is a tolerance on a verdict. A
    spring's rest length is the contour itself — the settle should pull the
    exits to where the backbone actually holds them, not to where the
    check would forgive them.
    """
    index: dict[tuple[str, int], int] = {}
    for i, body in enumerate(bodies):
        for offset in range(body.start, body.end + 1):
            index[(body.helix, offset)] = i
    out: list[_Loop] = []
    tables = group_domains(list(getattr(tree, "domains", []) or []))
    for route in tables.by_strand.values():
        for before, after in itertools.pairwise(route):
            geom_a, geom_b = geoms.get(before.helix), geoms.get(after.helix)
            if geom_a is None or geom_b is None:
                continue  # chain_dangling_domain's report, not this op's
            hit_a = index.get((before.helix, before.exit_offset))
            hit_b = index.get((after.helix, after.entry_offset))
            if hit_a is None or hit_b is None:
                continue  # an offset past the helix's end — ditto
            i, j = hit_a, hit_b
            att_a = _attachment(
                bodies[i], i, geom_a, before.exit_offset, before.forward
            )
            att_b = _attachment(bodies[j], j, geom_b, after.entry_offset, after.forward)
            n_nt = after.loop_before_nt or 0
            c_m = geom_b.motif.contour_per_unit
            out.append(
                _Loop(
                    after=after,
                    n_nt=n_nt,
                    c_m=c_m,
                    spring=LoopSpring(
                        a=att_a,
                        b=att_b,
                        rest_length=contour(n_nt, c_m * _NM_PER_M),
                    ),
                    body_a=i,
                    body_b=j,
                    att_a=att_a,
                    att_b=att_b,
                    axial_a=_axial_sign(
                        geom_a, before.exit_offset, before.forward, leaving=True
                    ),
                    axial_b=_axial_sign(
                        geom_b, after.entry_offset, after.forward, leaving=False
                    ),
                )
            )
    return out


@dataclass
class _Walker:
    """One state-owning block in this settle: its body index, the state
    whose pose slot receives the settled placement, and the frame facts
    the write-back needs (the block's world rotation and where bead 0
    sits in its local frame)."""

    name: str
    state: str
    body: int
    rot: np.ndarray
    lo_z_m: float


def _walker_body(
    tree: SeTree, node: Any, state: design_states.BlockState
) -> tuple[_Body, _Walker]:
    """The rigid body for a state-owning block — ``se-walker-light-protocol``'s
    "the walker body is one more rigid body in ``relax_bundle``": its axis
    is the block's local ``z`` through its origin, spanning its envelope's
    ``z`` extent (the state's own envelope when it overrides the block's),
    its radius half the wider of the other two extents. Refuses, by name,
    a chain block (a helix's segments are already bodies — declare states
    on the walker BODY), a child block (this round settles top-level
    walkers; their parts move with them as children) and a block with no
    parsable envelope (a body needs an extent)."""
    if chain_role(node) is not None:
        raise OpError(
            f"relax_chain(state=): block {node.name!r} is a chain "
            f"{chain_role(node)} — declare the walker's states on its BODY "
            "block (the rigid part the legs hang off), not on a helix, strand "
            "or segment"
        )
    if node.parent is not None:
        raise OpError(
            f"relax_chain(state=): block {node.name!r} is a child of "
            f"{node.parent!r} — a state-settled walker must be top-level "
            "(its own parts move with it as ITS children)"
        )
    envelope = state.envelope or node.envelope
    if not envelope:
        raise OpError(
            f"relax_chain(state=): block {node.name!r} has no envelope — the "
            "settle needs the walker body's extent (set_envelope)"
        )
    try:
        lo, hi = cad_dsl.build(cad_dsl.parse(envelope)).aabb_local()
    except (cad_dsl.DslError, ValueError) as exc:
        raise OpError(
            f"relax_chain(state=): block {node.name!r}'s envelope does not build: {exc}"
        ) from exc
    lo_v = np.asarray(lo, dtype=float).reshape(3)
    hi_v = np.asarray(hi, dtype=float).reshape(3)
    length_m = max(float(hi_v[2] - lo_v[2]), _WALKER_MIN_EXTENT_M)
    radius_m = (
        max(float(hi_v[0] - lo_v[0]), float(hi_v[1] - lo_v[1]), _WALKER_MIN_EXTENT_M)
        / 2.0
    )
    rot = _rot(node.rot)
    origin = np.asarray(node.pose, dtype=float).reshape(3)
    a_m = origin + rot @ np.array([0.0, 0.0, float(lo_v[2])])
    axis = rot @ np.array([0.0, 0.0, 1.0])
    body = _Body(
        name=str(node.name),
        helix="",
        start=0,
        end=0,
        a_nm=a_m * _NM_PER_M,
        b_nm=(a_m + length_m * axis) * _NM_PER_M,
        radius_nm=radius_m * _NM_PER_M,
        length_nm=length_m * _NM_PER_M,
        carry=np.eye(3),
        movable=True,
        walker=True,
    )
    return body, _Walker(
        name=str(node.name), state=state.name, body=-1, rot=rot, lo_z_m=float(lo_v[2])
    )


@dataclass
class _Leg:
    """A tethered strand's spring: the walker's anchor port to the leg's
    first (foot) domain's entry exit, resting at the tether's contour."""

    strand: str
    walker: int
    foot: int
    spring: LoopSpring


def _unit_index(bodies: list[_Body]) -> dict[tuple[str, int], int]:
    index: dict[tuple[str, int], int] = {}
    for i, body in enumerate(bodies):
        if body.walker:
            continue
        for offset in range(body.start, body.end + 1):
            index[(body.helix, offset)] = i
    return index


def _legs(
    tree: SeTree,
    geoms: dict[str, HelixGeometry],
    bodies: list[_Body],
    walkers: dict[str, _Walker],
) -> list[_Leg]:
    """One loop spring per anchored strand whose anchor block is a walker
    in THIS settle and whose foot (first) domain sits on a laid-out helix —
    the item's "body-side end tied to a named attachment site by a loop
    spring at the leg's free-nucleotide contour". A free leg (its foot
    lifted by the state's occupancy, so :func:`group_domains` shows no
    route) has nothing to spring to and gets no spring; a strand anchored
    to a block that is not in this settle is left alone (the base
    ``relax_chain`` settles the track, not the walker)."""
    index = _unit_index(bodies)
    tables = group_domains(list(getattr(tree, "domains", []) or []))
    out: list[_Leg] = []
    for strand in sorted(tree.blocks):
        node = tree.blocks[strand]
        if chain_role(node) != STRAND_ROLE:
            continue
        anchor = (node.chain or {}).get("anchor")
        if not anchor:
            continue
        walker = walkers.get(str(anchor.get("block")))
        if walker is None:
            continue
        wnode = tree.blocks[walker.name]
        port = wnode.ports.get(str(anchor.get("port")))
        if port is None:
            raise OpError(
                f"relax_chain(state=): strand {strand!r} is anchored at "
                f"{walker.name}.{anchor.get('port')}, and {walker.name!r} has no "
                f"such port — add_port it, or realize_chain sites=[...] on the "
                "body's helix"
            )
        if port.pose is None:
            raise OpError(
                f"relax_chain(state=): anchor port {walker.name}.{port.name} has "
                "no pose — set_port_pose it (the tether needs a point to pull from)"
            )
        route = tables.by_strand.get(strand) or []
        if not route:
            continue
        foot = route[0]
        geom = geoms.get(foot.helix)
        j = index.get((foot.helix, foot.entry_offset))
        if geom is None or j is None:
            continue  # chain_dangling_domain's report, not this op's
        att_foot = _attachment(bodies[j], j, geom, foot.entry_offset, foot.forward)
        wb = bodies[walker.body]
        p_nm = (
            np.asarray(wnode.pose, dtype=float).reshape(3)
            + walker.rot @ np.asarray(port.pose, dtype=float).reshape(3)
        ) * _NM_PER_M
        axis = _unit(wb.b_nm - wb.a_nm)
        along = float(np.dot(p_nm - wb.a_nm, axis)) / wb.length_nm
        offset = p_nm - (wb.a_nm + along * (wb.b_nm - wb.a_nm))
        att_body = Attachment(body=walker.body, along=along, offset=_triple(offset))
        rest = contour(
            int(anchor.get("nt") or 0), geom.motif.contour_per_unit * _NM_PER_M
        )
        out.append(
            _Leg(
                strand=strand,
                walker=walker.body,
                foot=j,
                spring=LoopSpring(a=att_body, b=att_foot, rest_length=rest),
            )
        )
    return out


def _write_state_poses(
    tree: SeTree, bodies: list[_Body], settled: np.ndarray, walkers: dict[str, _Walker]
) -> int:
    """Each walker's settled placement → ``node.pending_state_poses[state]``
    as the block's own ``{xyz, rot}`` (world = parent-relative for a
    top-level block), for :func:`precis_se.handler._materialize_states`
    to store through :func:`precis.design.states.set_state_pose`. The
    tree's own pose is NOT touched: a state's pose is an overlay the
    ``args={'state': ...}`` read applies, never the block's default."""
    written = 0
    for walker in walkers.values():
        i = walker.body
        body = bodies[i]
        a = settled[i, 0] / _NM_PER_M
        axis_now = _unit(settled[i, 1] - settled[i, 0])
        carry = _carry_rotation(_unit(body.b_nm - body.a_nm), axis_now)
        rot = carry @ walker.rot
        origin = a - rot @ np.array([0.0, 0.0, walker.lo_z_m])
        node = tree.blocks[walker.name]
        pending = dict(node.pending_state_poses or {})
        pending[walker.state] = {
            "xyz": [float(v) for v in origin],
            "rot": [float(v) for v in euler_rad_from_matrix(rot)],
        }
        node.pending_state_poses = pending
        written += 1
    return written


def _hinges(
    bodies: list[_Body], geoms: dict[str, HelixGeometry], lp: dict[str, _Lp]
) -> list[Hinge]:
    """A welded hinge between consecutive segments of one helix, at the
    worm-like-chain constant :func:`precis_chain.relax.hinge_stiffness` —
    ``Lp / (2 L)``, a ratio, so it reads the same in metres or nanometres.
    The store-read Lp enters by replacing the motif's own field, so the
    formula stays the kernel's single copy."""
    out: list[Hinge] = []
    for i in range(len(bodies) - 1):
        a, b = bodies[i], bodies[i + 1]
        if a.walker or b.walker or a.helix != b.helix or a.end + 1 != b.start:
            continue
        motif = dataclasses.replace(
            geoms[a.helix].motif, persistence_length=lp[a.helix].value_m
        )
        length_m = max(a.length_nm, 1e-6) / _NM_PER_M
        out.append(
            Hinge(body_a=i, body_b=i + 1, stiffness=hinge_stiffness(motif, length_m))
        )
    return out


def _write_back(
    tree: SeTree,
    bodies: list[_Body],
    settled: np.ndarray,
    geoms: dict[str, HelixGeometry],
) -> int:
    """Settled bead positions → each movable child's pose/rot, stamped
    ``origin='proposed'``.

    The axis is re-scaled to the body's own length before writing: the
    kernel's rigid-body term is a stiff *spring*, so a settled body is a
    percent or so off its rest length, and the stored envelope (a ``cyl``
    of that length, :func:`precis_se.chain.layout.segment_envelope`) is a
    fact about the motif rather than a solver output. Re-imposing the
    length keeps pose and envelope consistent without rewriting the
    envelope.
    """
    written = 0
    for i, body in enumerate(bodies):
        if not body.movable or body.walker:
            continue
        node = tree.blocks[body.name]
        a = settled[i, 0] / _NM_PER_M
        axis = _unit(settled[i, 1] - settled[i, 0])
        b = a + (body.length_nm / _NM_PER_M) * axis
        parent = tree.blocks[body.helix]
        capsule = local_capsule(
            Capsule(a, b, geoms[body.helix].motif.radius), parent.pose, parent.rot
        )
        pose, rot = segment_pose(capsule)
        node.local_pose, node.local_rot = pose, rot
        node.origins["pose"] = "proposed"
        written += 1
    compose_world_pose(tree)
    return written


def _write_curves(bodies: list[_Body], loops: list[_Loop], settled: np.ndarray) -> int:
    """Each placed loop's sampled curve onto the LATER domain row's
    ``meta.loop_curve`` — points in **metres**, from the **settled** exits.

    This is the seam ``se-nucleic-realize-export`` reads to tell a placed
    loop from an unplaced one, so a row with no curve must stay
    distinguishable from one with a curve: nothing here ever writes an
    empty list. The exit tangents are :func:`_exit_tangent`: the outward
    radial direction (exit minus the body's axis point — the backbone
    leaves the duplex sideways mid-helix) blended with the helix axis on a
    terminal unit, so a hairpin or a tail caps the helix end instead of
    bowing out flat in the last pair's plane (gripe 457929).
    :func:`precis_chain.loop.loop_curve` only uses their directions.
    """
    written = 0
    for loop in loops:
        p = _exit_point(bodies[loop.body_a], settled, loop.body_a, loop.att_a)
        q = _exit_point(bodies[loop.body_b], settled, loop.body_b, loop.att_b)
        tp = _exit_tangent(
            bodies[loop.body_a], settled, loop.body_a, loop.att_a, loop.axial_a
        )
        tq = _exit_tangent(
            bodies[loop.body_b], settled, loop.body_b, loop.att_b, loop.axial_b
        )
        if float(np.linalg.norm(tp)) < 1e-12 or float(np.linalg.norm(tq)) < 1e-12:
            continue  # an on-axis exit has no outward direction to leave along
        curve = loop_curve(
            p / _NM_PER_M,
            tp,
            q / _NM_PER_M,
            -tq,
            loop.n_nt,
            loop.c_m,
            samples=LOOP_CURVE_SAMPLES,
        )
        loop.after.loop_curve = [[float(v) for v in point] for point in curve.points]
        written += 1
    return written


def _iters(op: dict[str, Any]) -> int:
    raw = op.get("iters")
    if raw is None:
        return DEFAULT_ITERS
    try:
        iters = int(raw)
    except (TypeError, ValueError) as exc:
        raise OpError(
            f"relax_chain 'iters' must be a whole number, got {raw!r}"
        ) from exc
    if not 1 <= iters <= MAX_ITERS:
        raise OpError(
            f"relax_chain 'iters' must be between 1 and {MAX_ITERS}, got {iters}"
        )
    return iters


def _summary(
    result: BundleResult,
    bodies: list[_Body],
    loops: list[_Loop],
    lp: dict[str, _Lp],
    gap_m: float,
    gap_owner: str,
    written: int,
    curves: int,
    *,
    walkers: dict[str, _Walker] | None = None,
    legs: list[_Leg] | None = None,
    stations: int = 0,
) -> str:
    sourced = sorted(name for name, entry in lp.items() if entry.from_store)
    if sourced:
        which = "; ".join(f"{name}: {lp[name].source}" for name in sourced)
        others = len(lp) - len(sourced)
        lp_line = f"Lp {which}" + (
            f"; {others} helix/helices on coded defaults" if others else ""
        )
    else:
        lp_line = "Lp " + ", ".join(sorted({entry.source for entry in lp.values()}))
    return (
        f"relax_chain: settled {sum(1 for b in bodies if not b.walker)} segment "
        f"bodies over {len(lp)} "
        f"helices with {len(loops)} loop spring(s) — {result.n_steps} step(s), "
        f"max force {result.max_force:.3g}, "
        f"{'converged' if result.converged else 'NOT converged'}; "
        f"{lp_line}; excluded volume at min_gap "
        f"{format_quantity(gap_m, 'length')} (helix {gap_owner!r}); wrote "
        f"{written} proposed pose(s) and {curves} loop curve(s)"
        + (
            "; station settle: "
            + ", ".join(f"{w.name}@{w.state}" for w in (walkers or {}).values())
            + f" as rigid bodies with {len(legs or [])} leg tether(s), "
            f"{stations} per-state pose(s) stored"
            if walkers
            else ""
        )
    )


def op_relax_chain(
    store: Any,
    tree: SeTree,
    op: dict[str, Any],
    state: dict[str, design_states.BlockState] | None = None,
) -> str:
    """Apply the ``relax_chain`` op (module docstring) and return its
    summary line.

    ``state`` (``{block name: BlockState}``, resolved by
    :func:`precis_se.state_arg.resolve_state_arg` from the op's
    ``state={block: state name}`` key) turns this into a **station
    settle** (``se-walker-light-protocol``): the states' occupancy is
    applied to a transient copy of the domain rows
    (:func:`~precis_se.chain.occupancy.apply_occupancy`), every
    state-owning block joins the bundle as one more rigid body
    (:func:`_walker_body`), its tethered strands pull it to their
    footholds (:func:`_legs`), and its settled placement is queued for the
    state's own pose slot (:func:`_write_state_poses`) rather than written
    to the tree — the tree's rows and the block's default pose come out of
    a station settle untouched, and no loop curve is written (a curve is
    a fact about the declared route, not about one station). With no
    ``move=`` a station settle moves the walker ALONE (the track is the
    fixed frame the legs pull against); segments named by ``move=`` still
    settle and write back as usual.

    Raises :class:`~precis_se.ops.OpError` before any mutation on every
    refusal path.
    """
    strays = sorted(set(op) - _ALLOWED_KEYS)
    if strays:
        raise OpError(
            f"relax_chain: unknown key(s) {', '.join(strays)} — takes move "
            "('all' or helix/segment block names), iters (FIRE steps, "
            f"default {DEFAULT_ITERS}) and state ({{block: state name}})"
        )
    resolved = dict(state or {})
    if op.get("state") is not None and not resolved:
        raise OpError(
            "relax_chain: 'state' must be resolved against the saved design's "
            "declared states before this op runs (put the design first, then "
            "edit it with the relax)"
        )
    saved_domains = tree.domains
    occupancy = merged_occupancy(resolved) if resolved else {}
    if occupancy:
        tree.domains = apply_occupancy(saved_domains, occupancy)
    try:
        return _settle(store, tree, op, resolved)
    finally:
        tree.domains = saved_domains


def _settle(
    store: Any,
    tree: SeTree,
    op: dict[str, Any],
    resolved: dict[str, design_states.BlockState],
) -> str:
    iters = _iters(op)
    geoms = _helices(tree)
    bodies = _bodies(tree, geoms)
    walkers: dict[str, _Walker] = {}
    for name in sorted(resolved):
        body, walker = _walker_body(tree, tree.blocks[name], resolved[name])
        walker.body = len(bodies)
        bodies.append(body)
        walkers[name] = walker
    if walkers and op.get("move") is None:
        # A station settle moves the walker alone: the track is the fixed
        # frame its legs pull against, so the layout's revisable segments
        # are NOT the default movers here (they are in a plain settle) —
        # else a foothold would come to the leg instead of the body going
        # to the foothold. move= still names segments explicitly.
        movable: set[str] = set()
    else:
        movable = _movable_names(tree, op, bodies)
    for body in bodies:
        body.movable = body.walker or body.name in movable
    lp = _lp_of(store, tree, geoms)
    hinges = _hinges(bodies, geoms, lp)
    loops = _loops(tree, geoms, bodies)
    legs = _legs(tree, geoms, bodies, walkers)
    # The excluded-volume clearance is the same number ``chain_clash``
    # measures against — the tightest ``min_gap`` any helix declares — so a
    # settle and the check that grades it cannot disagree.
    gap_owner = min(geoms.values(), key=lambda g: g.min_gap_m)
    # Sanctioned contacts, matching ``chain_clash``'s own exemptions: the
    # two segments a loop joins are *supposed* to touch. Hinge-welded pairs
    # are already exempt inside the kernel.
    skip = sorted(
        {tuple(sorted((loop.body_a, loop.body_b))) for loop in loops}
        | {tuple(sorted((leg.walker, leg.foot))) for leg in legs}
    )
    pins = [
        Pin(body=i, end=end, target=_triple((body.a_nm, body.b_nm)[end]))
        for i, body in enumerate(bodies)
        if not body.movable
        for end in (0, 1)
    ]
    start = np.stack([np.stack([b.a_nm, b.b_nm]) for b in bodies])
    try:
        result = relax_bundle(
            start,
            hinges,
            [loop.spring for loop in loops] + [leg.spring for leg in legs],
            pins,
            np.array([b.radius_nm for b in bodies], dtype=float),
            iters,
            ev_tol=gap_owner.min_gap_m * _NM_PER_M,
            skip_pairs=[(i, j) for i, j in skip if i != j],
            tol=TOL_NM,
        )
    except ValueError as exc:  # pragma: no cover — the vetting above precedes it
        raise OpError(f"relax_chain: the settle refused this bundle: {exc}") from exc
    written = _write_back(tree, bodies, result.bodies, geoms)
    curves = 0 if walkers else _write_curves(bodies, loops, result.bodies)
    stations = _write_state_poses(tree, bodies, result.bodies, walkers)
    return _summary(
        result,
        bodies,
        loops,
        lp,
        gap_owner.min_gap_m,
        gap_owner.name,
        written,
        curves,
        walkers=walkers,
        legs=legs,
        stations=stations,
    )
