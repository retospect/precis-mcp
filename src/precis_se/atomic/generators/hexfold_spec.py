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
from hexfold.stick import stick
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
    net: Net, coords: np.ndarray, mode: str
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
    join ports' rim atoms bare for a later fuse, ``"none"`` caps nothing."""
    inverse: dict[str, Any] = {}
    coords, envelope = _canonical_frame(
        np.asarray(raw_coords, dtype=float),
        inverse=inverse if target is not None else None,
    )
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
    h_hosts, h_positions = _terminate_open_edges(net, coords, terminate)
    h_clashes: list[tuple[float, int, int]] = []
    if h_positions:
        base = len(elements)
        carbon = coords
        coords = np.vstack([coords, np.asarray(h_positions, dtype=float)])
        # the envelope must hold the caps too: same frame, re-floored so
        # the lowest atom (now possibly an H) sits one vdW margin above
        # z = 0 as the envelope contract says, re-measured over every atom;
        # the carbons move by that one rigid z shift and nothing else
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
        # a placed H that lands inside another atom's clash bar is reported,
        # never dropped: two converging rims (a Y's seam end, a strip's
        # corner against a tube rim) can want H where there is no room
        profile = Profile.DEFAULT
        h_xyz = coords[base:]
        for k, (host, p) in enumerate(zip(h_hosts, h_xyz)):
            d_c = np.linalg.norm(carbon - p, axis=1)
            d_c[host] = np.inf
            for other in np.flatnonzero(d_c < profile.clash_bar("C", elements[0])):
                h_clashes.append((float(d_c[other]), base + k, int(other)))
            d_h = np.linalg.norm(h_xyz[k + 1 :] - p, axis=1)
            for m in np.flatnonzero(d_h < profile.clash_bar("H", "H")):
                h_clashes.append((float(d_h[m]), base + k, base + k + 1 + int(m)))
        h_clashes.sort()
        if h_clashes:
            worst = h_clashes[0]
            report = report.merge(
                Report(
                    (
                        Finding(
                            "terminate.clash",
                            Severity.WARN,
                            f"{len(h_clashes)} placed H within another atom's clash "
                            f"bar (closest {worst[0]:.2f} A, atoms {worst[1]} and "
                            f"{worst[2]}): converging open edges want H where there "
                            "is no room; the caps are kept, judge the edge before a "
                            "DFT handoff",
                            where=str(worst[1]),
                            data=(
                                ("count", len(h_clashes)),
                                ("min_A", round(worst[0], 3)),
                                ("pairs", [[i, j] for _d, i, j in h_clashes[:10]]),
                            ),
                        ),
                    )
                )
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
        "bond_A": net.lattice.sigma_CH_A,
        "why": "open edges capped after the relax and the judgement so renders, "
        "se reports and a DFT handoff see a closed-shell edge; the report "
        "describes the carbon net",
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
