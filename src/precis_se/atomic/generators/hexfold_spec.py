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
from hexfold.build import build
from hexfold.canon import canonical_json
from hexfold.check import check
from hexfold.report import BuildError, HexfoldError
from hexfold.stick import stick
from precis_se.atomic.generators._types import (
    GeneratedBlock,
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


def _envelope(coords: np.ndarray) -> str:
    """Bounding cylinder about the principal axis, the same shape the CNT
    generator emits (``cyl:r<h>``) — radius = max distance off the first
    PCA axis plus the vdW margin, height = the axis extent plus 2× the
    margin. Deterministic: the axis sign is fixed so its
    largest-magnitude component is positive."""
    centroid = coords.mean(axis=0)
    centered = coords - centroid
    cov = centered.T @ centered
    _w, vecs = np.linalg.eigh(cov)
    axis = vecs[:, -1]
    pivot = int(np.argmax(np.abs(axis)))
    if axis[pivot] < 0:
        axis = -axis
    along = centered @ axis
    perp = centered - np.outer(along, axis)
    radius = float(np.linalg.norm(perp, axis=1).max()) + VDW_MARGIN_A
    height = float(along.max() - along.min()) + 2.0 * VDW_MARGIN_A
    return f"cyl:r{fmt_length_A(radius)}h{fmt_length_A(height)}"


def _port_direction(coords: np.ndarray, atoms: tuple[int, ...]) -> list[float]:
    rim = coords[list(atoms)].mean(axis=0)
    direction = rim - coords.mean(axis=0)
    norm = float(np.linalg.norm(direction))
    if norm < 1e-9:
        return [0.0, 0.0, 1.0]
    return [float(x) for x in direction / norm]


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
        report = check(spec, geometry=geometry)
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

    coords = stick(net)
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
            )
        )

    rings: dict[int, int] = {}
    for member_ring in net.rings:
        rings[len(member_ring)] = rings.get(len(member_ring), 0) + 1

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
            }
            for _name, p in net.ports
        },
        "regions": {name: list(ords) for name, ords in net.regions},
        "report": net.report.to_dict(),
        "seed_kind": net.seed_kind,
        "n_atoms": len(net.atoms),
        "n_bonds": len(net.bonds),
        "rings": rings,
    }
    provenance = (
        f"hexfold {hexfold.__version__} spec ({len(net.atoms)} atoms, "
        f"{len(net.bonds)} bonds; rings {rings}); fidelity={fidelity} "
        f"seed={net.seed_kind}; coordinates derived, not part of the format"
    )
    return GeneratedBlock(
        envelope=_envelope(coords),
        ports=ports,
        topology=topology,
        provenance=provenance,
        elements=elements,
        coords=coords,
        bonds=bonds,
        hybridizations=hybridizations,
    )
