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

from typing import Any

import numpy as np

import hexfold
from hexfold.build import Net, build
from hexfold.build import Port as HxPort
from hexfold.canon import canonical_json
from hexfold.check import check, geometry_findings
from hexfold.extent import measures as hx_measures
from hexfold.report import BuildError, HexfoldError, Report
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


def _canonical_frame(coords: np.ndarray) -> tuple[np.ndarray, str]:
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
            provenance=report.render(verbose=True),
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
    return _block_from_net(net, stick(net), spec=spec, report=report, fidelity=fidelity)


def _block_from_net(
    net: Net,
    raw_coords: np.ndarray,
    *,
    spec: str,
    report: Report,
    fidelity: str,
    extra_topology: dict[str, Any] | None = None,
    provenance_tail: str = "",
) -> GeneratedBlock:
    """Mint the block from a built ``net`` and its coordinates: the
    canonical frame and envelope, bond orders/kinds, ports, rings, length
    measures, the topology record and the provenance line. Shared by
    :func:`build_hexfold` (``stick(net)`` coordinates) and
    :mod:`~precis_se.atomic.generators.hexfold_scene` (tethered ones);
    ``report`` is the caller's merged findings, ``extra_topology`` extra
    topology keys, and ``provenance_tail`` is appended to the provenance
    line."""
    coords, envelope = _canonical_frame(np.asarray(raw_coords, dtype=float))
    elements = [a.element for a in net.atoms]
    hybridizations = [a.hyb for a in net.atoms]
    sp3 = {i for i, a in enumerate(net.atoms) if a.hyb == "sp3"}
    bonds = [
        (
            i,
            j,
            _SP2_BOND_ORDER if i not in sp3 and j not in sp3 else 1.0,
            "aromatic" if i not in sp3 and j not in sp3 else "pairwise",
        )
        for i, j, _o in net.bonds
    ]

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
    provenance = (
        f"hexfold {hexfold.__version__} spec ({len(net.atoms)} atoms, "
        f"{len(net.bonds)} bonds; rings {rings}); fidelity={fidelity} "
        f"seed={net.seed_kind}; coordinates derived, not part of the format"
        f"{provenance_tail}"
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
    )
