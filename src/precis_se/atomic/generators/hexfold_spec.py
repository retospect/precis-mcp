"""The ``hexfold`` generator — a ``.hx`` *spec text* as the params payload
(docs/backlog/hexfold-integration.md). ``hexfold`` is the standalone,
numpy-only package that owns the topology side of the seam: a `.hx` file
carries *topology only* — lattice, instances (``sheet``/``tube``/``cone``/
``fullerene``), defects, holes, ``bond``/``fuse`` attachments, nanobud
menus — and the bond graph plus a deterministic stick-model seed are
*derived*. precis owns geometry and storage; the spec string stored in
``topology["spec"]`` is the regeneration input, the coordinates are a
preview.

hexfold is vendored at ``src/hexfold/`` (spec: ``src/hexfold/spec.md``).

**Bond orders**: the same sp²-sheet Pauling convention as
:mod:`~precis_se.atomic.generators.sp2`'s CNT family — sp²–sp² bonds get
``order = 4/3``, ``kind="aromatic"``; any bond touching an sp³ atom (a
bond-mode attachment site, e.g. a ``[2+2]`` nanobud seam) gets
``order=1.0``, ``kind="pairwise"``, so no atom's declared valence budget
is silently over-summed. Per-atom ``hyb`` tags (``"sp2"``/``"sp3"``)
come straight from hexfold's net into
:attr:`GeneratedBlock.hybridizations`.

**fidelity** (spec §25.1): ``params.fidelity`` picks the tier —
``"check"`` runs ``hexfold.check`` instead of building; the rendered
report goes back as the block's provenance with
:attr:`GeneratedBlock.dry_run` set, so ``prepare_generate`` echoes it and
mints nothing (the agent's edit/check loop). ``"stick"`` (the default)
builds and relaxes a preview geometry. ``"geo"``/``"emt"``/``"ml"`` are
precis's physics relaxation ladder (spec §15.2) and are not wired into
this generator yet. ``dry_run`` is accepted as a deprecated alias:
``dry_run=True`` means ``fidelity="check"``; passing both is only
allowed when they agree.
"""

from __future__ import annotations

import dataclasses
import math
from typing import Any

import numpy as np

import hexfold
from hexfold.build import Net, build
from hexfold.build import Port as HxPort
from hexfold.canon import canonical_json
from hexfold.check import check, geometry_findings
from hexfold.extent import measures as hx_measures
from hexfold.lattice import SP3_IDEAL_DEG
from hexfold.report import BuildError, Finding, HexfoldError, Profile, Report, Severity
from hexfold.stick import _angle_springs, stick, stick_relax_pinned
from precis_se.atomic.generators._types import (
    GeneratedBlock,
    GeneratedMeasure,
    GeneratedPort,
    GeneratorError,
    fmt_length_A,
)
from precis_se.atomic.generators.sp2 import VDW_MARGIN_A

#: Pauling bond order for a fully delocalized sp² sheet — the same value
#: the CNT generator uses (3 × 4/3 = 4 exactly).
_SP2_BOND_ORDER = 4.0 / 3.0

#: Fidelity tiers this generator actually builds.
_SUPPORTED_FIDELITIES = ("check", "stick")

#: Physics tiers named in spec §15.2 but not wired into this generator —
#: precis's relaxation ladder runs those as background jobs, not here.
_UNWIRED_FIDELITIES = ("geo", "emt", "ml")


#: The lattice tag every hexfold port carries (GeneratedPort.lattice): the
#: sp2 hexagonal lattice; rims are its port type.
_LATTICE = "sp2-hex"


def _rim_type_str(port: HxPort) -> str | None:
    """``"z12"`` / ``"a10"`` from ``Port.rim_type`` (SPEC 10), ``None`` for
    a mixed rim."""
    rt = port.rim_type
    return None if rt is None else f"{rt[0]}{rt[1]}"


def _rim_payload(port: HxPort) -> dict[str, Any]:
    """The hexfold instance of the typed port payload (GeneratedPort
    docstring): one rim, its edge-word, dangling count and rim type."""
    return {
        "kind": "rim",
        "word": port.word,
        "N": port.size,
        "type": _rim_type_str(port),
    }


def _se_port_name(hx_name: str) -> str:
    """The se port name for a hexfold port path.

    hexfold qualifies rim names as ``<instance>.<rim>`` (``h.out``) as soon
    as a spec has more than one instance; se's ``add_port`` reserves the
    dot for its ``block.port`` endpoint syntax and refuses dotted names.
    ``h.out`` → ``h_out``; a single-instance spec's bare ``in``/``out``
    pass through unchanged. The hexfold path is kept verbatim as
    ``topology.ports[<se name>].hx``.
    """
    return hx_name.replace(".", "_")


def _resolve_fidelity(params: dict[str, Any]) -> str:
    """``params["fidelity"]``, with ``params["dry_run"]`` honoured as a
    deprecated alias (``dry_run=True`` ⇒ ``fidelity="check"``,
    ``dry_run=False`` ⇒ ``fidelity="stick"``). Raises if both are given
    and disagree, or if the resolved tier isn't ``check``/``stick``."""
    fidelity = params.get("fidelity")
    dry_run = params.get("dry_run")
    if dry_run is not None:
        implied = "check" if dry_run else "stick"
        if fidelity is None:
            fidelity = implied
        elif fidelity != implied:
            raise GeneratorError(
                f"fidelity={fidelity!r} and dry_run={dry_run!r} disagree; pass only one"
            )
    if fidelity is None:
        fidelity = "stick"
    if fidelity in _UNWIRED_FIDELITIES:
        raise GeneratorError(f"fidelity={fidelity!r} is not wired yet, use check|stick")
    if fidelity not in _SUPPORTED_FIDELITIES:
        raise GeneratorError(
            f"fidelity must be one of {_SUPPORTED_FIDELITIES + _UNWIRED_FIDELITIES}, "
            f"got {fidelity!r}"
        )
    return fidelity


def _canonical_frame(
    coords: np.ndarray, *, inverse: dict[str, Any] | None = None
) -> tuple[np.ndarray, str]:
    """Move the stick coordinates into the frame the block's ``cyl:r<>h<>``
    envelope is read in, and return that envelope.

    ``envelope_fit`` (:mod:`precis_se.atomic.validate`) reads a cylinder
    envelope in the block's local frame as **the z axis, z ∈ [0, h],
    radially centred on the origin** — the frame the ``cnt`` generator
    emits into. Until 2026-09-27 this generator described a cylinder about
    a PCA axis through the centroid but left the atoms where the stick
    relaxation put them, so only a tube that happened to lie along z ever
    fit (a sheet protruded 28 Å, C60 1.7 Å — dogfood). Coordinates are
    derived, not part of the format (module docstring), so the fix is to
    put the atoms where the envelope says they are: the principal axis
    that gives the smallest containing cylinder becomes +z (sign fixed so
    its largest-magnitude component is positive), the radial centroid the
    origin, and the lowest atom sits one vdW margin above z=0. Radius =
    max radial distance + margin, height = axial extent + 2× margin, so
    every atom is inside the margin by construction. Deterministic for a
    given input (``eigh`` is)."""
    centroid = coords.mean(axis=0)
    centered = coords - centroid
    cov = centered.T @ centered
    _w, vecs = np.linalg.eigh(cov)
    best: tuple[float, np.ndarray, float, float, np.ndarray] | None = None
    for k in range(3):
        axis = vecs[:, k]
        pivot = int(np.argmax(np.abs(axis)))
        if axis[pivot] < 0:
            axis = -axis
        along = centered @ axis
        perp = centered - np.outer(along, axis)
        radius = float(np.linalg.norm(perp, axis=1).max()) + VDW_MARGIN_A
        height = float(along.max() - along.min()) + 2.0 * VDW_MARGIN_A
        volume = radius * radius * height
        if best is None or volume < best[0] - 1e-9:
            best = (volume, axis, radius, height, along)
    assert best is not None
    _volume, e3, radius, height, along = best
    # e1: the world axis least aligned with e3, made orthonormal; e2 closes
    # a right-handed basis. Rows of ``rot`` are the new axes.
    world = np.eye(3)[int(np.argmin(np.abs(e3)))]
    e1 = world - float(world @ e3) * e3
    e1 /= np.linalg.norm(e1)
    e2 = np.cross(e3, e1)
    rot = np.stack([e1, e2, e3])
    framed = centered @ rot.T
    framed[:, 2] += VDW_MARGIN_A - float(along.min())
    if inverse is not None:
        # Row-vector inverse of the SAME applied transform; never PCA on read.
        shift = np.array([0.0, 0.0, VDW_MARGIN_A - float(along.min())])
        inverse.update(Q=rot, b=centroid - shift @ rot)
    return framed, f"cyl:r{fmt_length_A(radius)}h{fmt_length_A(height)}"


#: A rim whose centroid sits closer to the body centroid than this fraction
#: of the rim's own radius has no outward direction (a sheet's ``rim`` is
#: the whole boundary and encloses the centroid; the residual is the
#: flat-perturbed seed's z-jitter, ~1e-3 of the radius). Below it the
#: direction is the canonical-frame axis, deterministic, not noise
#: (gr454488 residual 2). A tube end or cap rim sits ≥ 0.3 radii out.
_DEGENERATE_RIM_FRACTION = 0.05


def _port_direction(coords: np.ndarray, atoms: tuple[int, ...]) -> list[float]:
    rim_atoms = coords[list(atoms)]
    rim = rim_atoms.mean(axis=0)
    direction = rim - coords.mean(axis=0)
    norm = float(np.linalg.norm(direction))
    rim_radius = (
        float(np.linalg.norm(rim_atoms - rim, axis=1).max()) if len(atoms) else 0.0
    )
    if norm < 1e-9 or norm < _DEGENERATE_RIM_FRACTION * rim_radius:
        return [0.0, 0.0, 1.0]
    return [float(x) for x in direction / norm]


def _internal_message(exc: Exception) -> str:
    """A hexfold crash that is not a :class:`HexfoldError` is a compiler
    bug, not a spec error -- say so, with the exception text, instead of
    letting the dispatcher print ``internal error … (see server log)``,
    which the caller cannot read."""
    return (
        f"hexfold internal error while compiling the spec: "
        f"{type(exc).__name__}: {exc} -- the spec parsed; this is a hexfold "
        "bug, not a spec mistake. File a gripe with the spec text."
    )


def build_hexfold(params: dict[str, Any]) -> GeneratedBlock:
    """``{"spec": "<.hx text>", "fidelity"?: "check"|"stick", "geometry"?:
    bool}`` — build the spec into atoms/bonds/ports (``fidelity="stick"``,
    the default), or return its rendered check report
    (``fidelity="check"``; ``geometry`` defaults true there). ``dry_run``
    is a deprecated alias for ``fidelity="check"``/``"stick"``."""
    spec = params.get("spec")
    if not isinstance(spec, str) or not spec.strip():
        raise GeneratorError("hexfold needs 'spec' (a .hx spec text)")

    fidelity = _resolve_fidelity(params)

    if fidelity == "check":
        geometry = bool(params.get("geometry", True))
        try:
            report = check(spec, geometry=geometry)
        except HexfoldError as exc:
            # A ParseError renders as ``line:col: message`` -- the one
            # error a check-mode caller iterates on, so it must reach them
            # as text, not as ``internal error … (see server log)``
            # (dogfood 2026-09-27).
            raise GeneratorError(str(exc)) from exc
        except (ValueError, KeyError, IndexError) as exc:
            raise GeneratorError(_internal_message(exc)) from exc
        return GeneratedBlock(
            envelope="",
            ports=[],
            topology={},
            provenance=report.render(verbose=True, agent=True),
            elements=[],
            coords=np.zeros((0, 3), dtype=np.float64),
            bonds=[],
            dry_run=True,
        )

    try:
        net = build(spec, strict=True)
    except BuildError as exc:
        raise GeneratorError(exc.report.render(verbose=True)) from exc
    except HexfoldError as exc:  # ParseError and friends carry no Report
        raise GeneratorError(str(exc)) from exc
    except (ValueError, KeyError, IndexError) as exc:
        raise GeneratorError(_internal_message(exc)) from exc

    # The persisted record carries the same geometry tier the check echo
    # shows (bond/angle deviations, geom.summary) — a reader of
    # ``## generated`` could not otherwise tell whether the minted
    # geometry is sane (gr454488 residual 3).
    report = net.report.merge(Report(tuple(geometry_findings(net))))
    return _block_from_net(
        net,
        stick(net),
        spec=spec,
        report=report,
        fidelity=fidelity,
        terminate=terminate_mode(params),
    )


#: ``params.terminate`` for every hexfold-family generator: cap every
#: under-coordinated carbon with H (``"H"``, the default), leave the join
#: ports' rim atoms bare but cap the rest (``"ports-open"``), or cap nothing
#: (``"none"``).
TERMINATE_MODES = ("H", "ports-open", "none")
TERMINATED_TAG = "terminated:h"


def terminate_mode(params: dict[str, Any]) -> str:
    """The ``terminate`` mode an op asks for, validated against
    :data:`TERMINATE_MODES`; absent means ``"H"``."""
    mode = params.get("terminate", "H")
    if mode not in TERMINATE_MODES:
        raise GeneratorError(
            f"terminate must be one of {list(TERMINATE_MODES)}; got {mode!r}"
        )
    return str(mode)


def _h_directions(unit: np.ndarray, missing: int, hyb: str) -> list[np.ndarray]:
    """Unit directions for ``missing`` H atoms on a carbon whose present
    bonds point along the rows of ``unit``: the missing-bond bisector for
    one, and the lattice/tetrahedral completion for two."""
    away = -unit.sum(axis=0)
    norm = float(np.linalg.norm(away))
    away = away / norm if norm > 1e-9 else np.array([0.0, 0.0, 1.0])
    if missing == 1:
        return [away]
    # a perpendicular to the present bonds' plane (or any, for one bond)
    if len(unit) >= 2:
        n = np.cross(unit[0], unit[1])
    else:
        probe = np.array([1.0, 0.0, 0.0])
        if abs(float(unit[0] @ probe)) > 0.9:
            probe = np.array([0.0, 1.0, 0.0])
        n = np.cross(unit[0], probe)
    n /= max(float(np.linalg.norm(n)), 1e-12)
    if hyb == "sp3":
        # two tetrahedral completions straddle the bisector out of the
        # present bonds' plane
        half = math.radians(SP3_IDEAL_DEG / 2.0)
        return [away * math.cos(half) + s * n * math.sin(half) for s in (1.0, -1.0)]
    # sp2 with one bond: the two in-plane completions at 120 deg from it
    side = np.cross(n, unit[0])
    side /= max(float(np.linalg.norm(side)), 1e-12)
    return [-unit[0] * 0.5 + s * side * math.sqrt(3.0) / 2.0 for s in (1.0, -1.0)]


def _terminate_open_edges(
    net: Net,
    coords: np.ndarray,
    mode: str,
    *,
    eligible_hosts: frozenset[int] | None = None,
) -> tuple[list[int], list[np.ndarray]]:
    """Which carbons get an H and where: every atom short of its
    valence (3 for sp2, 4 for sp3), skipping ports' rim atoms under
    ``"ports-open"``. Returns parallel lists of host ordinals and H
    positions (``sigma_CH_A`` along the completion directions)."""
    if mode == "none":
        return [], []
    skip: set[int] = set()
    if mode == "ports-open":
        for _name, port in net.ports:
            skip.update(port.dangling or port.atoms)
    degree = [0] * len(net.atoms)
    nbrs: list[list[int]] = [[] for _ in net.atoms]
    for i, j, _o in net.bonds:
        degree[i] += 1
        degree[j] += 1
        nbrs[i].append(j)
        nbrs[j].append(i)
    hosts: list[int] = []
    positions: list[np.ndarray] = []
    for a in net.atoms:
        if eligible_hosts is not None and a.ord not in eligible_hosts:
            continue
        if a.element != "C" or a.hyb not in ("sp2", "sp3") or a.ord in skip:
            continue
        want = 4 if a.hyb == "sp3" else 3
        missing = want - degree[a.ord]
        if missing <= 0 or degree[a.ord] == 0:
            continue
        vec = coords[nbrs[a.ord]] - coords[a.ord]
        unit = vec / np.linalg.norm(vec, axis=1)[:, None]
        for direction in _h_directions(unit, min(missing, 2), a.hyb):
            hosts.append(a.ord)
            positions.append(coords[a.ord] + net.lattice.sigma_CH_A * direction)
    return hosts, positions


#: ``_resolve_edge_caps`` — two cap-bearing carbons whose placed H
#: collide and that sit within this many lattice spacings are "within
#: bonding reach": the (tethered) stick pass leaves converging rims
#: ~1.7-1.8 sigma (2.4-2.6 A at sigma 1.42) apart — the non-bonded
#: repulsion floor plus angle-spring pressure — and GFN2-xTB then closes
#: them into ordinary C-C bonds (relax1013 campaign, gr486406). Two
#: sigma is the hexagon's para distance — the width of one missing bond
#: across a rim gap; an authored opening sits wider still (~2 sigma +
#: vdW margins).
_RING_CLOSE_REACH_SIGMA = 2.0

#: Each resolution pass adds at least one ring-closure bond or removes at
#: least one rim atom, so the loop converges or exhausts this bound and
#: the build refuses.
_TERMINATE_MAX_PASSES = 16

#: After a resolution change a short local stick polish settles the
#: touched neighbourhood (a closed pair relaxes to an ordinary bond
#: length): atoms within this many bonds of a changed site may move,
#: everything else stays put — so the authored (pinned) registrations a
#: scene caller holds stay exact, and the polish cannot rewrite the far
#: field.
_TERMINATE_POLISH_HOPS = 3
_TERMINATE_POLISH_ITERS = 800


def _cap_clash_pairs(
    coords: np.ndarray,
    hosts: list[int],
    positions: list[np.ndarray],
) -> dict[tuple[int, int], tuple[float, set[int]]]:
    """Carbon pairs implicated by clashing caps: ``(a, b) -> (worst cap
    distance, {cap hosts})``. A cap-vs-carbon clash yields the host and
    the carbon it landed on; a cap-vs-cap clash the two hosts (a cap on
    the same host as another never counts — a =CH2 pair is legal)."""
    profile = Profile.DEFAULT
    pairs: dict[tuple[int, int], tuple[float, set[int]]] = {}
    if not positions:
        return pairs
    h_xyz = np.asarray(positions, dtype=float)

    def note(a: int, b: int, d: float, cap_hosts: set[int]) -> None:
        key = (min(a, b), max(a, b))
        best, old = pairs.get(key, (math.inf, set()))
        pairs[key] = (min(best, d), old | cap_hosts)

    for k, (host, p) in enumerate(zip(hosts, h_xyz)):
        d_c = np.linalg.norm(coords - p, axis=1)
        d_c[host] = np.inf
        for other in np.flatnonzero(d_c < profile.clash_bar("C", "C")):
            note(host, int(other), float(d_c[other]), {host})
        d_h = np.linalg.norm(h_xyz[k + 1 :] - p, axis=1)
        for m in np.flatnonzero(d_h < profile.clash_bar("H", "H")):
            other_host = hosts[k + 1 + int(m)]
            if other_host != host:
                note(host, other_host, float(d_h[m]), {host, other_host})
    return pairs


def _net_minus_atoms(
    net: Net, coords: np.ndarray, drop: set[int]
) -> tuple[Net, np.ndarray, dict[int, int]]:
    """``net`` without the atoms in ``drop`` (and non-lattice atoms left
    orphaned — an authored cap cannot survive its host), ordinals
    renumbered, every ordinal-bearing field remapped. Rings containing a
    removed atom are dropped; ``sheets`` face indices follow. Returns the
    reduced net, reduced coords and the old→new ordinal map."""
    lat_el = set(net.lattice.elements)
    drop = set(drop)
    for i, j, _o in net.bonds:
        if i in drop and net.atoms[j].element not in lat_el:
            drop.add(j)
        elif j in drop and net.atoms[i].element not in lat_el:
            drop.add(i)
    renum = {a.ord: n for n, a in enumerate(a for a in net.atoms if a.ord not in drop)}
    atoms = tuple(
        dataclasses.replace(a, ord=renum[a.ord]) for a in net.atoms if a.ord in renum
    )

    def rem(ords: tuple[int, ...]) -> tuple[int, ...]:
        return tuple(renum[o] for o in ords if o in renum)

    ports = []
    for name, port in net.ports:
        ring = rem(port.atoms)
        if not ring:
            raise GeneratorError(
                f"termination trimming emptied port {name!r}'s rim — an "
                "authored interface cannot be cut away"
            )
        ports.append(
            (
                name,
                dataclasses.replace(port, atoms=ring, dangling=rem(port.dangling)),
            )
        )
    ringmap: dict[int, int] = {}
    rings: list[tuple[int, ...]] = []
    for idx, ring in enumerate(net.rings):
        if all(o in renum for o in ring):
            ringmap[idx] = len(rings)
            rings.append(rem(ring))
    reduced = dataclasses.replace(
        net,
        atoms=atoms,
        bonds=tuple(
            sorted(
                (renum[i], renum[j], o)
                for i, j, o in net.bonds
                if i in renum and j in renum
            )
        ),
        rings=tuple(rings),
        ports=tuple(ports),
        regions=tuple((n, rem(ords)) for n, ords in net.regions),
        seams=tuple(dataclasses.replace(s, atoms=rem(s.atoms)) for s in net.seams),
        attach=tuple(
            (renum[i], renum[j]) for i, j in net.attach if i in renum and j in renum
        ),
        consumed_rims=tuple(rem(r) for r in net.consumed_rims),
        term_rims=tuple((rem(a), b, be) for a, b, be in net.term_rims),
        seam_rims=tuple((rem(a), b, be) for a, b, be in net.seam_rims),
        sheet_atoms=tuple((n, rem(o)) for n, o in net.sheet_atoms),
        sheets=tuple(
            (n, tuple(ringmap[f] for f in faces if f in ringmap))
            for n, faces in net.sheets
        ),
        seed3=(
            tuple(row for o, row in enumerate(net.seed3) if o in renum)
            if net.seed3 is not None
            else None
        ),
    )
    return reduced, coords[sorted(renum)], renum


def _polish_termination(
    net: Net,
    coords: np.ndarray,
    sites: set[int],
    pinned: frozenset[int],
) -> tuple[np.ndarray, float, float]:
    """Settle the neighbourhood of ``sites`` (closed-pair endpoints,
    trimmed atoms' former neighbours): a short stick pass with only atoms
    within ``_TERMINATE_POLISH_HOPS`` bonds movable — and never a pinned
    (authored-registration) atom. Returns coords, max force, max
    displacement."""
    adjacency: list[set[int]] = [set() for _ in net.atoms]
    for i, j, _o in net.bonds:
        adjacency[i].add(j)
        adjacency[j].add(i)
    movable_set = set(sites)
    front = set(sites)
    for _hop in range(_TERMINATE_POLISH_HOPS):
        nxt = set().union(*(adjacency[a] for a in front)) if front else set()
        nxt -= movable_set
        movable_set |= nxt
        front = nxt
    movable_set -= set(pinned)
    if not movable_set or not net.bonds:
        return coords, 0.0, 0.0
    movable = np.zeros(len(net.atoms), dtype=float)
    movable[sorted(movable_set)] = 1.0
    lat_el = set(net.lattice.elements)
    brest = np.asarray(
        [
            net.lattice.sigma_A
            if net.atoms[i].element in lat_el and net.atoms[j].element in lat_el
            else net.lattice.sigma_CH_A
            for i, j, _o in net.bonds
        ]
    )
    springs = np.asarray(_angle_springs(net), dtype=float).reshape(-1, 3)
    pos, force = stick_relax_pinned(
        np.asarray(coords, dtype=float),
        np.asarray([(i, j) for i, j, _o in net.bonds], dtype=np.int64),
        brest,
        springs,
        net.lattice.sigma_A,
        iters=_TERMINATE_POLISH_ITERS,
        movable=movable,
    )
    if not np.isfinite(pos).all() or not math.isfinite(force):
        raise GeneratorError("termination polish returned nonfinite coordinates")
    return pos, float(force), float(np.linalg.norm(pos - coords, axis=1).max())


def _resolve_edge_caps(
    net: Net,
    coords: np.ndarray,
    terminate: str,
    *,
    drop_bar: float | None,
    pinned: frozenset[int],
) -> tuple[
    Net,
    np.ndarray,
    list[int],
    list[np.ndarray],
    list[int],
    list[str],
    list[tuple[int, int]],
    float,
]:
    """Resolve cap collisions at converging rims before the H atoms are
    minted (gr486406). Returns the net and coords — atoms may have been
    trimmed and ring-closure bonds added — plus the surviving cap
    hosts/positions, dropped-cap hosts, trimmed atoms' paths, the closed
    pairs (final ordinals) and the largest polish displacement.

    Each pass places caps on the current net and collects the carbon
    pairs whose caps collide. In order: (1) a pair within
    ``_RING_CLOSE_REACH_SIGMA * sigma_A`` is bondable — drop the caps, add
    the C-C bond (a carbon that reaches degree four is relabelled sp3);
    (2) a pair out of reach loses its dangling rim atom(s) — trimmed out
    of the net, the newly exposed atoms re-capped on the next pass;
    authored interfaces (port rims, seam records, ``bond``-verb
    endpoints) and ``pinned`` atoms are never trimmed. (3) Any cap still
    inside the clash bar when a pass changes nothing — or the pass bound
    runs out — fails the build, naming the atoms. Every changed
    neighbourhood gets a short pinned stick polish so closed pairs sit at
    a real bond length.
    """
    work = net
    xyz = np.asarray(coords, dtype=float)
    pinned = frozenset(pinned)
    protected = set(pinned)
    for _name, port in work.ports:
        protected.update(port.atoms)
    for s in work.seams:
        protected.update(s.atoms)
    for i, j in work.attach:
        protected.update((i, j))
    lat_el = set(work.lattice.elements)
    reach = _RING_CLOSE_REACH_SIGMA * work.lattice.sigma_A
    trimmed: list[str] = []
    closed_paths: list[tuple[str, str]] = []
    polish_disp = 0.0

    def closed_pairs() -> list[tuple[int, int]]:
        by_path = {str(a.path): a.ord for a in work.atoms}
        return sorted(
            {
                (min(by_path[pa], by_path[pb]), max(by_path[pa], by_path[pb]))
                for pa, pb in closed_paths
                if pa in by_path and pb in by_path
            }
        )

    pairs: dict[tuple[int, int], tuple[float, set[int]]] = {}
    for _pass in range(_TERMINATE_MAX_PASSES):
        hosts, positions = _terminate_open_edges(work, xyz, terminate)
        dropped: list[int] = []
        if positions and drop_bar is not None:
            h_xyz = np.asarray(positions, dtype=float)
            keep: list[int] = []
            for k, (host, p) in enumerate(zip(hosts, h_xyz)):
                d_c = np.linalg.norm(xyz - p, axis=1)
                d_c[host] = np.inf
                d_h = np.linalg.norm(h_xyz - p, axis=1)
                d_h[k] = np.inf
                if min(float(d_c.min()), float(d_h.min())) < drop_bar:
                    dropped.append(host)
                else:
                    keep.append(k)
            hosts = [hosts[k] for k in keep]
            positions = [positions[k] for k in keep]
        pairs = _cap_clash_pairs(xyz, hosts, positions)
        if not pairs:
            return (
                work,
                xyz,
                hosts,
                positions,
                dropped,
                trimmed,
                closed_pairs(),
                polish_disp,
            )
        bonded = {frozenset((i, j)) for i, j, _o in work.bonds}
        degree = [0] * len(work.atoms)
        for i, j, _o in work.bonds:
            degree[i] += 1
            degree[j] += 1
        new_bonds: list[tuple[int, int]] = []
        to_trim: set[int] = set()
        for (a, b), (_d, cap_hosts) in sorted(pairs.items()):
            if (
                frozenset((a, b)) not in bonded
                and work.atoms[a].element in lat_el
                and work.atoms[b].element in lat_el
                and degree[a] < 4
                and degree[b] < 4
                and float(np.linalg.norm(xyz[a] - xyz[b])) <= reach
            ):
                new_bonds.append((a, b))
                degree[a] += 1
                degree[b] += 1
            else:
                to_trim.update(h for h in cap_hosts if h not in protected)
        if not new_bonds and not to_trim:
            break
        changed: set[int] = set()
        if to_trim:
            nbrs: list[set[int]] = [set() for _ in work.atoms]
            for i, j, _o in work.bonds:
                nbrs[i].add(j)
                nbrs[j].add(i)
            freed = {n for t in to_trim for n in nbrs[t]} - to_trim
            trimmed.extend(str(work.atoms[t].path) for t in sorted(to_trim))
            work, xyz, renum = _net_minus_atoms(work, xyz, to_trim)
            changed.update(renum[f] for f in freed if f in renum)
            pinned = frozenset(
                p for p in (renum.get(o) for o in pinned) if p is not None
            )
            protected = {r for o in protected if (r := renum.get(o)) is not None}
            new_bonds = [
                (renum[a], renum[b]) for a, b in new_bonds if a in renum and b in renum
            ]
        if new_bonds:
            closed_now = {(min(a, b), max(a, b), 1) for a, b in new_bonds}
            closed_paths.extend(
                (str(work.atoms[a].path), str(work.atoms[b].path)) for a, b in new_bonds
            )
            work = dataclasses.replace(
                work, bonds=tuple(sorted(set(work.bonds) | closed_now))
            )
            changed.update(o for a, b in new_bonds for o in (a, b))
            degree = [0] * len(work.atoms)
            for i, j, _o in work.bonds:
                degree[i] += 1
                degree[j] += 1
            touched = {o for a, b in new_bonds for o in (a, b)}
            flip = {
                a.ord
                for a in work.atoms
                if a.ord in touched and a.hyb == "sp2" and degree[a.ord] >= 4
            }
            if flip:
                work = dataclasses.replace(
                    work,
                    atoms=tuple(
                        dataclasses.replace(a, hyb="sp3") if a.ord in flip else a
                        for a in work.atoms
                    ),
                )
        xyz, _force, disp = _polish_termination(work, xyz, changed, pinned)
        polish_disp = max(polish_disp, disp)
    # stalled or out of passes: re-scan so the refusal names what remains
    hosts, positions = _terminate_open_edges(work, xyz, terminate)
    pairs = _cap_clash_pairs(xyz, hosts, positions)
    names = "; ".join(
        f"{work.atoms[a].path}~{work.atoms[b].path} ({d:.2f} A)"
        for (a, b), (d, _hs) in sorted(pairs.items())[:6]
    )
    raise GeneratorError(
        f"terminate.clash: {len(pairs)} cap clash(es) at converging rims "
        f"could not be resolved — closest atoms {names}. Pairs within "
        f"{reach:.2f} A were bond-closed and dangling rim atoms trimmed; "
        "what is left sits on an authored interface or beyond reach. "
        "Use terminate='none' and cap by hand, or open the seam wider."
    )


def _block_from_net(
    net: Net,
    raw_coords: np.ndarray,
    *,
    spec: str,
    report: Report,
    fidelity: str,
    extra_topology: dict[str, Any] | None = None,
    target: dict[str, Any] | None = None,
    target_flip: np.ndarray | None = None,
    provenance_tail: str = "",
    terminate: str = "H",
    drop_h_under_A: float | None = None,
    pinned: set[int] | frozenset[int] | None = None,
) -> GeneratedBlock:
    """Mint the block from a built ``net`` and its coordinates: the
    canonical frame and envelope, bond orders/kinds, ports, rings, length
    measures, the topology record and the provenance line. Shared by
    :func:`build_hexfold` (``stick(net)`` coordinates) and
    :mod:`~precis_se.atomic.generators.hexfold_scene` (tethered ones);
    ``report`` is the caller's merged findings, ``extra_topology`` extra
    topology keys, and ``provenance_tail`` is appended to the provenance
    line.

    **Termination is the last step** (Reto, 2026-10-07): with ``terminate``
    ``"H"`` every carbon short of its valence gets an H at ``sigma_CH_A``
    along its missing bond, after the relax and after the judgement, so
    the report still describes the carbon net while the stored atoms are
    capped for renders, se reports and a DFT handoff. The H atoms are
    appended after the carbons (ports, regions and findings keep their
    ordinals), recorded in ``topology["terminated"]`` and tagged
    :data:`TERMINATED_TAG` on the structure; ``"ports-open"`` leaves the
    join ports' rim atoms bare for a later fuse, ``"none"`` caps nothing.

    Where open rims converge (a Y's seam end, a fin's corner against the
    tube) independently placed caps collide. ``_resolve_edge_caps`` fixes
    the topology instead of minting colliding H (gr486406): cap pairs
    whose host carbons are within bonding reach
    (``_RING_CLOSE_REACH_SIGMA * sigma_A`` after the relax pass) are
    dropped and the C-C ring bond is added; dangling rim atoms whose caps
    clash out of reach are trimmed and the new rim re-capped; a clash on
    an authored interface (port rim, seam record, ``bond`` verb endpoint)
    or past the pass bound refuses the build. Each change is settled by
    a short stick polish over the touched neighbourhood — ``pinned``
    atoms (a caller's authored registration) never move — and every
    closure/trim is recorded in the report and on
    ``topology["terminated"]``.

    ``drop_h_under_A`` (default None, keep every cap) drops a placed H
    outright when any other atom sits under that distance — for seam
    rows whose converging rims would otherwise mint overlapping
    hydrogens: the bare open edge is honest, a 0.2 Å cap pair is not.
    Dropped caps are reported as ``terminate.dropped`` INFO with their
    host ordinals, and counted on ``topology["terminated"]["dropped"]``."""
    inverse: dict[str, Any] = {}
    coords, envelope = _canonical_frame(
        np.asarray(raw_coords, dtype=float),
        inverse=inverse if target is not None else None,
    )
    closed: list[tuple[int, int]] = []
    trimmed: list[str] = []
    h_dropped: list[int] = []  # host ordinals whose caps were dropped
    polish_disp = 0.0
    if terminate != "none":
        (
            net,
            coords,
            h_hosts,
            h_positions,
            h_dropped,
            trimmed,
            closed,
            polish_disp,
        ) = _resolve_edge_caps(
            net,
            coords,
            terminate,
            drop_bar=drop_h_under_A,
            pinned=frozenset(pinned or ()),
        )
    else:
        h_hosts, h_positions = [], []
    elements = [a.element for a in net.atoms]
    hybridizations = [a.hyb for a in net.atoms]
    sp3 = {i for i, a in enumerate(net.atoms) if a.hyb == "sp3"}
    not_carbon = {i for i, a in enumerate(net.atoms) if a.element != "C"}
    bonds = [
        (
            i,
            j,
            _SP2_BOND_ORDER if {i, j}.isdisjoint(sp3 | not_carbon) else 1.0,
            "aromatic" if {i, j}.isdisjoint(sp3 | not_carbon) else "pairwise",
        )
        for i, j, _o in net.bonds
    ]
    if h_dropped:
        report = report.merge(
            Report(
                (
                    Finding(
                        "terminate.dropped",
                        Severity.INFO,
                        f"{len(h_dropped)} placed H cap(s) dropped: another atom "
                        f"sits under {drop_h_under_A:g} A; those carbons stay bare",
                        data=(
                            ("count", len(h_dropped)),
                            ("hosts", sorted(set(h_dropped))),
                        ),
                    ),
                )
            )
        )
    if closed or trimmed:
        found: list[Finding] = []
        if closed:
            found.append(
                Finding(
                    "terminate.closed",
                    Severity.INFO,
                    f"{len(closed)} rim carbon pair(s) bond-closed where their "
                    "caps collided at a converging rim; a local stick polish "
                    "settled the junction",
                    data=(
                        ("count", len(closed)),
                        ("pairs", [list(p) for p in closed]),
                        (
                            "atoms",
                            [
                                [str(net.atoms[a].path), str(net.atoms[b].path)]
                                for a, b in closed
                            ],
                        ),
                        ("polish_max_disp_A", round(polish_disp, 3)),
                    ),
                )
            )
        if trimmed:
            found.append(
                Finding(
                    "terminate.trimmed",
                    Severity.INFO,
                    f"{len(trimmed)} dangling rim atom(s) trimmed where caps "
                    "collided out of bonding reach; the exposed rim was "
                    "re-capped",
                    data=(("count", len(trimmed)), ("atoms", trimmed)),
                )
            )
        report = report.merge(Report(tuple(found)))
    h_clashes: list[tuple[float, int, int]] = []
    if h_positions or closed or trimmed:
        base = len(elements)
        carbon = coords
        if h_positions:
            coords = np.vstack([coords, np.asarray(h_positions, dtype=float)])
        # the envelope must hold the caps too: same frame, re-floored so
        # the lowest atom (now possibly an H) sits one vdW margin above
        # z = 0 as the envelope contract says, re-measured over every atom;
        # the carbons move by that one rigid z shift and nothing else.
        # Re-measuring also covers the resolver's trims and polish.
        lift = VDW_MARGIN_A - float(coords[:, 2].min())
        if lift:
            coords[:, 2] += lift
            carbon = carbon + np.array([0.0, 0.0, lift])
            if inverse:
                inverse["b"] = inverse["b"] - np.array([0.0, 0.0, lift]) @ inverse["Q"]
        radius = float(np.linalg.norm(coords[:, :2], axis=1).max()) + VDW_MARGIN_A
        height = float(coords[:, 2].max()) + VDW_MARGIN_A
        envelope = f"cyl:r{fmt_length_A(radius)}h{fmt_length_A(height)}"
        elements.extend("H" for _ in h_positions)
        hybridizations.extend("s" for _ in h_positions)
        bonds.extend(
            (host, base + k, 1.0, "pairwise") for k, host in enumerate(h_hosts)
        )
        # hard gate, kept as the last word after resolution: a cap inside
        # the clash bar means the minted block carries colliding atoms
        profile = Profile.DEFAULT
        h_xyz = coords[base:]
        for k, (host, p) in enumerate(zip(h_hosts, h_xyz)):
            d_c = np.linalg.norm(carbon - p, axis=1)
            d_c[host] = np.inf
            for other in np.flatnonzero(d_c < profile.clash_bar("C", "C")):
                h_clashes.append((float(d_c[other]), base + k, int(other)))
            d_h = np.linalg.norm(h_xyz[k + 1 :] - p, axis=1)
            for m in np.flatnonzero(d_h < profile.clash_bar("H", "H")):
                h_clashes.append((float(d_h[m]), base + k, base + k + 1 + int(m)))
        h_clashes.sort()
        if h_clashes:
            worst = h_clashes[0]

            def _label(ord_: int) -> str:
                if ord_ < len(net.atoms):
                    return str(net.atoms[ord_].path)
                return f"H{ord_} on {net.atoms[h_hosts[ord_ - base]].path}"

            raise GeneratorError(
                f"terminate.clash: {len(h_clashes)} placed H cap(s) within "
                f"another atom's clash bar after rim resolution (closest "
                f"{worst[0]:.2f} A, {_label(worst[1])}~{_label(worst[2])}) — "
                "refusing to mint a colliding structure"
            )
    capped = bool(h_positions) or any(a.element == "H" for a in net.atoms)

    ports: list[GeneratedPort] = []
    se_names = [_se_port_name(p.name) for _n, p in net.ports]
    if len(set(se_names)) != len(se_names):
        # `.`→`_` is not injective (instance `a_b` port `c` vs `a` port
        # `b_c`); hexfold's rim vocabulary has no underscores today, so
        # this only fires if that changes -- loudly, not by overwriting
        # a topology.ports entry.
        dup = sorted({n for n in se_names if se_names.count(n) > 1})
        raise GeneratorError(f"hexfold port names collide as se ports: {dup}")
    for _name, port in net.ports:
        ring = list(port.dangling) if port.dangling else list(port.atoms)
        atom_index = ring[0]
        ports.append(
            GeneratedPort(
                name=_se_port_name(port.name),
                atom_index=atom_index,
                direction=_port_direction(coords, tuple(ring)),
                roles=["covalent"],
                expected_element=elements[atom_index],
                atoms=ring,
                lattice=_LATTICE,
                payload=_rim_payload(port),
            )
        )

    rings: dict[int, int] = {}
    for member_ring in net.rings:
        rings[len(member_ring)] = rings.get(len(member_ring), 0) + 1

    # length anchors (hexfold.extent.measures): sheet extents with the
    # snap cell as band, tube lengths with the snap period, tube radii as
    # points -- se L2 measures the user's relations stack up against
    measures = [
        GeneratedMeasure(
            name=m.name,
            value_A=m.value_A,
            min_A=m.min_A,
            max_A=m.max_A,
            reason=m.reason,
        )
        for m in hx_measures(net)
    ]

    topology: dict[str, Any] = {
        "hexfold": hexfold.__version__,
        "spec": spec,
        "canonical_json": canonical_json(net),
        "ports": {
            _se_port_name(p.name): {
                "hx": p.name,
                "atoms": list(p.dangling) if p.dangling else list(p.atoms),
                "word": p.word,
                "B": p.b,
                "lattice": _LATTICE,
                "type": _rim_type_str(p),
            }
            for _name, p in net.ports
        },
        "regions": {name: list(ords) for name, ords in net.regions},
        "report": report.to_dict(),
        "seed_kind": net.seed_kind,
        "n_atoms": len(net.atoms),
        "n_bonds": len(net.bonds),
        "rings": rings,
        "measures": [m.to_dict() for m in hx_measures(net)],
    }
    if extra_topology:
        topology.update(extra_topology)
    topology["terminated"] = {
        "element": "H",
        "mode": terminate,
        "count": len(h_positions),
        "hosts": sorted(set(h_hosts)),
        "clashes": len(h_clashes),
        "closed": [list(p) for p in closed],
        "trimmed": trimmed,
        "polish_max_disp_A": round(polish_disp, 4),
        "dropped": len(h_dropped),
        "bond_A": net.lattice.sigma_CH_A,
        "why": "open edges capped after the relax and the judgement so renders, "
        "se reports and a DFT handoff see a closed-shell edge; the report "
        "describes the carbon net; caps that collide at a converging rim "
        "are resolved by a C-C ring bond or a rim trim before minting, "
        "and a clash the rim cannot resolve fails the build",
    }
    if target is not None:
        if target_flip is None:
            raise GeneratorError("evaluated target requires its build-to-judge map")
        receipt = {
            **target,
            "map": {
                "convention": "row-vector y=x@Q+b",
                "Q": (inverse["Q"] @ np.diag(target_flip)).tolist(),
                "b_A": (inverse["b"] @ np.diag(target_flip)).tolist(),
            },
        }
        # The strict read-side check runs here too, so a receipt the read
        # would refuse (axis-crossing meridian, non-reflected map) fails
        # the generation as a typed refusal before anything is persisted,
        # not as a raw ValueError in finish_generate after the tree edit.
        from precis_se.atomic.surface_target import decode_target

        try:
            decode_target(receipt, permit_sheet_name=True)
        except ValueError as exc:
            raise GeneratorError(f"evaluated target receipt: {exc}") from exc
        topology["surface_target"] = receipt
    provenance = (
        f"hexfold {hexfold.__version__} spec ({len(net.atoms)} atoms, "
        f"{len(net.bonds)} bonds; rings {rings}); fidelity={fidelity} "
        f"seed={net.seed_kind}; coordinates derived, not part of the format"
        f"{provenance_tail}"
        + (f"; {len(h_positions)} open edge(s) H-terminated" if h_positions else "")
        + (
            f"; {len(closed)} rim pair(s) bond-closed, "
            f"{len(trimmed)} rim atom(s) trimmed at converging caps"
            if closed or trimmed
            else ""
        )
    )
    return GeneratedBlock(
        envelope=envelope,
        ports=ports,
        topology=topology,
        provenance=provenance,
        elements=elements,
        coords=coords,
        bonds=bonds,
        hybridizations=hybridizations,
        measures=measures,
        tags=[TERMINATED_TAG] if capped else [],
    )
