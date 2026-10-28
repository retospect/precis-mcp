"""Private six-period straight Y: analytic local ribbons, no relaxation."""

from __future__ import annotations

import copy
import math
from dataclasses import replace
from typing import Any, cast

import numpy as np
import pytest

import hexfold.join as join
from hexfold.build import Atom, Net
from hexfold.check import Relaxed, geometry_findings
from hexfold.ids import AtomPath
from hexfold.join import Block, _ZigzagSegment, compose_k3
from hexfold.lattice import Lattice, Site
from hexfold.report import Profile, Report, Severity
from hexfold.text import Spec


def ribbon() -> tuple[Block, _ZigzagSegment]:
    """Honeycomb strip, two guard periods around six selected D sites.

    In Å: period sqrt(3)*sigma, zigzag S halfway in tangent/half sigma
    outward. Nearest neighbours supply the exact finite honeycomb graph.
    """
    sigma = 1.42
    period = math.sqrt(3) * sigma
    rows = {
        "D": (0, 0),
        "S": (0.5, 0.5),
        "A": (0.5, 1.5),
        "B": (0, 2),
        "C": (0, 3),
        "E": (0.5, 3.5),
        "F": (0.5, 4.5),
        "G": (0, 5),
    }
    ids: dict[tuple[str, int], int] = {}
    points: list[tuple[float, float, float]] = []
    for row, (x, y) in rows.items():
        for i in range(-1, 7):
            ids[row, i] = len(points)
            points.append(((i + x) * period, y * sigma, 0))
    coords = np.array(points)
    bonds = tuple(
        (i, j, 1)
        for i in range(len(points))
        for j in range(i + 1, len(points))
        if np.isclose(np.linalg.norm(coords[i] - coords[j]), sigma)
    )
    rings = []
    for i in range(-1, 6):
        for pattern in (
            (
                ("S", i),
                ("D", i + 1),
                ("S", i + 1),
                ("A", i + 1),
                ("B", i + 1),
                ("A", i),
            ),
            (("B", i), ("A", i), ("B", i + 1), ("C", i + 1), ("E", i), ("C", i)),
            (
                ("E", i),
                ("C", i + 1),
                ("E", i + 1),
                ("F", i + 1),
                ("G", i + 1),
                ("F", i),
            ),
        ):
            rings.append(tuple(ids[p] for p in pattern))
    walk = [ids["S", -1]]
    for i in range(6):
        walk.extend((ids["D", i], ids["S", i]))
    segment = _ZigzagSegment(
        "edge", tuple(walk), tuple(ids["D", i] for i in range(6)), (walk[0], walk[-1])
    )
    return Block(("C",) * len(points), coords, bonds, tuple(rings), {}, sigma), segment


def inputs() -> tuple[tuple[Block, ...], tuple[_ZigzagSegment, ...]]:
    pairs = [ribbon() for _ in range(3)]
    # Distinct input poses ensure rigid frame extraction is exercised.
    for j, (block, _) in enumerate(pairs):
        theta = 0.31 * j
        rot = np.array(
            [
                [math.cos(theta), 0, math.sin(theta)],
                [0, 1, 0],
                [-math.sin(theta), 0, math.cos(theta)],
            ]
        )
        block.coords = block.coords @ rot.T + [j, -2 * j, j / 3]
    return tuple(p[0] for p in pairs), tuple(p[1] for p in pairs)


def call(
    blocks: tuple[Block, ...], rims: tuple[_ZigzagSegment, ...]
) -> join._K3Composite:
    return compose_k3(
        blocks, rims, seam_type="k3-sp2-120-z", dihedrals_deg=(120, 120, 120)
    )


def as_net(result: join._K3Composite) -> Net:
    return Net(
        atoms=tuple(
            Atom(AtomPath("Y", Site(i, 0, 0)), i, "C", "sp2", "Y")
            for i in range(len(result.elements))
        ),
        bonds=result.bonds,
        rings=result.rings,
        ports=(),
        regions=(),
        report=Report(),
        spec=Spec(),
        lattice=Lattice(),
        seed3=tuple(
            (float(row[0]), float(row[1]), float(row[2])) for row in result.coords
        ),
        seed_kind="mixed",
    )


def test_six_period_y_degree_angles_bonds_clashes_and_determinism(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(*_a: object, **_k: object) -> None:
        pytest.fail("relaxation/pair composition must not run")

    monkeypatch.setattr(join, "_stick_relaxer", forbidden)
    monkeypatch.setattr(join, "compose", forbidden)
    import hexfold.stick as stick

    monkeypatch.setattr(stick, "stick_info", forbidden)
    blocks, rims = inputs()
    original = copy.deepcopy(blocks)
    result = call(blocks, rims)
    assert not result.findings
    assert len(result.seam_atoms) == 6
    adj = join._adjacency(result.bonds)
    for atom in result.seam_atoms:
        assert result.elements[atom] == "C" and result.hybridisation[atom] == "sp2"
        assert len(adj[atom]) == 3
        vectors = result.coords[adj[atom]] - result.coords[atom]
        assert np.linalg.norm(vectors, axis=1) == pytest.approx([1.42] * 3)
        unit = vectors / 1.42
        assert np.linalg.det(unit) == pytest.approx(0, abs=1e-12)
        assert unit @ unit.T == pytest.approx(np.full((3, 3), -0.5) + np.eye(3) * 1.5)
    for a, b, _kind in result.bonds:
        assert (
            abs(np.linalg.norm(result.coords[a] - result.coords[b]) - 1.42)
            <= Profile.DEFAULT.bond_tol_A
        )
    for before, after in zip(original, blocks):
        np.testing.assert_array_equal(before.coords, after.coords)
        assert before.bonds == after.bonds and before.rings == after.rings
    offset = 0
    for block, (rotation, shift) in zip(blocks, result.transforms):
        assert rotation.T @ rotation == pytest.approx(np.eye(3), abs=1e-12)
        assert np.linalg.det(rotation) == pytest.approx(1)
        assert result.coords[offset : offset + len(block.coords)] == pytest.approx(
            block.coords @ rotation.T + shift
        )
        offset += len(block.coords)
    again = call(blocks, rims)
    np.testing.assert_array_equal(result.coords, again.coords)
    assert result.bonds == again.bonds and result.rings == again.rings
    edge_set = {(a, b) for a, b, _ in result.bonds}
    for face in result.rings:
        assert all(
            tuple(sorted((a, b))) in edge_set for a, b in zip(face, face[1:] + face[:1])
        )
    assert sum(len(r) == 8 for r in result.rings) == 15
    findings = geometry_findings(
        as_net(result),
        relaxed=Relaxed(result.coords, 0, "deterministic-unrelaxed; force unmeasured"),
    )
    assert not any(
        f.code
        in {"geom.clash", "geom.seed_overlap", "geom.bond.long", "geom.bond.short"}
        for f in findings
    )
    # Existing checker assumes regular polygon angles; nonplanar 8-cycles
    # yield angle warnings, retained honestly rather than hidden/relaxed.
    assert {f.code for f in findings} == {"geom.summary", "geom.angle.dev"}
    assert all(f.severity != Severity.ERROR for f in findings)


@pytest.mark.parametrize(
    "angles",
    [
        (110, 120, 130),
        (120, 120, float("nan")),
        (120, 120, float("inf")),
        (120, 120),
        (True, 120, 120),
    ],
)
def test_bad_dihedrals_refuse_before_geometry(
    monkeypatch: pytest.MonkeyPatch, angles: tuple[float, ...]
) -> None:
    monkeypatch.setattr(
        join, "_segment_frame", lambda *_: pytest.fail("premature geometry access")
    )
    result = compose_k3(
        cast(tuple[Block, ...], (None,) * 3),
        cast(tuple[_ZigzagSegment, ...], (None,) * 3),
        seam_type="k3-sp2-120-z",
        dihedrals_deg=angles,
    )
    assert result.findings[0].code == "fit.unsolvable"
    assert result.coords.shape == (0, 3) and not result.bonds and not result.seam_atoms


@pytest.mark.parametrize("count", [2, 4, 5, 6])
def test_wrong_multiplicity_refused_before_inputs(count: int) -> None:
    result = compose_k3(
        cast(tuple[Block, ...], (None,) * count),
        cast(tuple[_ZigzagSegment, ...], (None,) * count),
        seam_type="k3-sp2-120-z",
        dihedrals_deg=(120,) * 3,
    )
    assert result.findings[0].code == "fit.unsolvable"
    assert dict(result.findings[0].data)["multiplicity"] == count
    assert not result.elements


@pytest.mark.parametrize(
    "selector,phase", [("unknown", 0), ("k3-sp2-120-z", 1), ("k3-sp2-120-z", True)]
)
def test_selector_and_phase_no_fallback(selector: str, phase: int) -> None:
    blocks, rims = inputs()
    result = compose_k3(
        blocks, rims, seam_type=selector, dihedrals_deg=(120,) * 3, phase=phase
    )
    assert result.findings[0].code == "fit.unsolvable"


@pytest.mark.parametrize(
    "bad",
    [
        "count",
        "endpoints",
        "armchair",
        "nonfinite",
        "curved",
        "degree",
        "spacing",
        "element",
        "sigma",
    ],
)
def test_bad_segment_refused_before_placement(
    monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    monkeypatch.setattr(
        join, "_place_k3", lambda *_: pytest.fail("premature placement/mint")
    )
    blocks, rims = inputs()
    block, rim = blocks[0], rims[0]
    if bad == "count":
        rim = replace(rim, dangling=rim.dangling[:-1])
    elif bad == "endpoints":
        rim = replace(rim, endpoints=(rim.atoms[-1], rim.atoms[0]))
    elif bad == "armchair":
        rim = replace(rim, dangling=rim.atoms[1:7])
    elif bad == "nonfinite":
        block.coords[0, 0] = np.nan
    elif bad == "curved":
        block.coords[rim.dangling[2], 2] += 0.1
    elif bad == "degree":
        block.bonds = tuple(b for b in block.bonds if rim.dangling[2] not in b[:2])
    elif bad == "spacing":
        block.coords *= 1.01
    elif bad == "element":
        block.elements = ("N",) + block.elements[1:]
    elif bad == "sigma":
        block.sigma = 1.5
    result = call(blocks, (rim, *rims[1:]))
    assert result.findings[0].code == (
        "fit.unsolvable" if bad == "sigma" else "port.mismatch"
    )
    assert not result.bonds and len(result.coords) == 0


@pytest.mark.parametrize("block_index", [0, 1, 2])
@pytest.mark.parametrize(
    "bad",
    [
        "negative_ring",
        "oversized_ring",
        "float_ring",
        "bool_ring",
        "invented_walk",
        "repeat_vertex",
        "unclosed_walk",
        "short_ring",
        "float_bond",
        "bool_bond",
        "negative_bond",
        "oversized_bond",
    ],
)
def test_every_copied_graph_index_and_face_validated_before_mint(
    monkeypatch: pytest.MonkeyPatch, block_index: int, bad: str
) -> None:
    monkeypatch.setattr(
        join,
        "_place_k3",
        lambda *_: pytest.fail("invalid graph reached placement/mint"),
    )
    blocks, rims = inputs()
    block = blocks[block_index]
    original = copy.deepcopy(blocks)
    # These fixtures deliberately violate the production integer graph types.
    face: list[Any] = list(block.rings[0])
    if bad == "negative_ring":
        face[0] = -1
    elif bad == "oversized_ring":
        # In block0 this aliases block2's first atom after concatenation.
        face[0] = 2 * len(block.elements)
    elif bad == "float_ring":
        face[0] = float(face[0])
    elif bad == "bool_ring":
        face[0] = True
    elif bad == "invented_walk":
        face = [0, 1, 2]  # valid local indices, no closed bond walk
    elif bad == "repeat_vertex":
        face.append(face[0])
    elif bad == "unclosed_walk":
        face = face[:-1]  # existing consecutive edges, missing closing bond
    elif bad == "short_ring":
        face = face[:2]
    elif bad.endswith("bond"):
        a, b, order = block.bonds[0]
        replacement: Any = {
            "float_bond": float(a),
            "bool_bond": True,
            "negative_bond": -1,
            "oversized_bond": len(block.elements),
        }[bad]
        block.bonds = ((replacement, b, order), *block.bonds[1:])
    if not bad.endswith("bond"):
        block.rings = (tuple(face), *block.rings[1:])
    malformed = copy.deepcopy(block)
    result = call(blocks, rims)
    assert result.findings[0].code == "port.mismatch"
    assert result.findings[0].severity == Severity.ERROR
    assert not result.seam_atoms and not result.elements and not result.bonds
    assert result.coords.shape == (0, 3)
    np.testing.assert_array_equal(block.coords, malformed.coords)
    assert block.bonds == malformed.bonds and block.rings == malformed.rings
    for i, untouched in enumerate(original):
        if i != block_index:
            np.testing.assert_array_equal(blocks[i].coords, untouched.coords)
            assert (
                blocks[i].bonds == untouched.bonds
                and blocks[i].rings == untouched.rings
            )
