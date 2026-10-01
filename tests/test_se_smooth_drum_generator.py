"""The ``smooth_drum`` generator (precis_se.atomic.generators.smooth_drum)."""

from __future__ import annotations

import time

import numpy as np
import pytest

from hexfold import radii
from precis.cad.dsl import parse
from precis_se.atomic.generate import ingest_envelope
from precis_se.atomic.generators import GENERATORS, GeneratedBlock
from precis_se.atomic.generators._types import GeneratorError
from precis_se.atomic.generators.smooth_drum import build_smooth_drum
from precis_se.atomic.generators.tpms import _MEAN_BOND_MAX_A, _MEAN_BOND_MIN_A


@pytest.fixture(scope="module")
def drum() -> tuple[GeneratedBlock, float]:
    t0 = time.perf_counter()
    block = build_smooth_drum({"neck": 24, "wall": 90})
    return block, time.perf_counter() - t0


def test_registered() -> None:
    assert GENERATORS["smooth_drum"] is build_smooth_drum


def test_default_drum_topology(drum: tuple[GeneratedBlock, float]) -> None:
    block, seconds = drum
    t = block.topology
    assert t["family"] == "smooth_drum"
    assert t["neck"] == [24, 0] and t["wall"] == [90, 0]
    assert t["pentagons"] == 12 and t["heptagons"] == 12
    assert set(t["rings"]) <= {5, 6, 7}
    assert t["relaxed"] is True
    assert t["n_atoms"] == len(block.coords) == len(block.elements)
    assert t["n_bonds"] == len(block.bonds)
    assert _MEAN_BOND_MIN_A < t["bond_mean_A"] < _MEAN_BOND_MAX_A
    assert t["bond_min_A"] > 1.25 and t["bond_max_A"] < 1.75
    assert t["theta_p_max_deg"] <= radii.THETA_P_C60_DEG
    assert t["fillet_radius_A"] > 0
    # 4 s on the host, ~22 s in the (slower) dev container
    assert seconds < 60.0, seconds
    assert all(kind == "aromatic" for *_rest, kind in block.bonds)


def test_no_nonbonded_clash(drum: tuple[GeneratedBlock, float]) -> None:
    block, _ = drum
    x = block.coords
    bonded = {(min(i, j), max(i, j)) for i, j, *_ in block.bonds}
    # grid-bucket neighbour search, numpy only
    cell = 2.0
    keys = np.floor(x / cell).astype(np.int64)
    buckets: dict[tuple[int, int, int], list[int]] = {}
    for idx, k in enumerate(map(tuple, keys.tolist())):
        buckets.setdefault(k, []).append(idx)
    clashes = 0
    for (cx, cy, cz), members in buckets.items():
        near = [
            j
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
            for dz in (-1, 0, 1)
            for j in buckets.get((cx + dx, cy + dy, cz + dz), [])
        ]
        for i in members:
            d = np.linalg.norm(x[near] - x[i], axis=1)
            for j, dist in zip(near, d, strict=True):
                if j > i and dist < 2.0 and (i, j) not in bonded:
                    clashes += 1
    assert clashes == 0


def test_meridian_frame_agrees_with_atoms(drum: tuple[GeneratedBlock, float]) -> None:
    block, _ = drum
    mer = np.asarray(block.topology["surface_meridian"], dtype=float)
    assert mer.ndim == 2 and mer.shape[1] == 2 and len(mer) > 50
    r = np.hypot(block.coords[:, 0], block.coords[:, 1])
    z = block.coords[:, 2]
    # nearest meridian point per atom, in chunks
    worst = 0.0
    per_atom = []
    for lo in range(0, len(r), 500):
        dr = r[lo : lo + 500, None] - mer[None, :, 0]
        dz = z[lo : lo + 500, None] - mer[None, :, 1]
        dist = np.sqrt(dr**2 + dz**2).min(axis=1)
        per_atom.append(dist)
        worst = max(worst, float(dist.max()))
    # the free sheet rim stretches past the target's end point (the net is
    # not pinned tangentially), so the max is rim-dominated; the bulk is not
    near = np.concatenate(per_atom)
    assert float(near.mean()) < 1.0, near.mean()
    assert float(np.percentile(near, 95)) < 2.0
    assert worst < 6.0, worst
    assert abs(float(z.min())) < 1e-9  # base at z = 0, the cyl convention


def test_envelope_parses_and_covers_atoms(drum: tuple[GeneratedBlock, float]) -> None:
    block, _ = drum
    spec = parse(ingest_envelope(block.envelope))
    assert spec.alias == "cyl"
    r = np.hypot(block.coords[:, 0], block.coords[:, 1])
    assert float(r.max()) < spec.params["r"] * 1e10
    assert spec.params["h"] * 1e10 == pytest.approx(
        float(block.coords[:, 2].max()), abs=1e-3
    )


def test_rim_ports(drum: tuple[GeneratedBlock, float]) -> None:
    block, _ = drum
    assert block.ports
    n_edge = int(
        (
            np.bincount(
                np.asarray([b[:2] for b in block.bonds]).ravel(),
                minlength=len(block.coords),
            )
            == 2
        ).sum()
    )
    assert len(block.ports) == n_edge
    p = block.ports[0]
    assert p.name == "rim1" and p.roles == ["covalent", "sp2-rim"]
    assert p.direction[2] == 0.0
    atom = block.coords[p.atom_index]
    assert np.dot(p.direction[:2], atom[:2]) > 0.0


def test_unrelaxed_flag() -> None:
    block = build_smooth_drum({"neck": 24, "wall": 90, "relax": False})
    assert block.topology["relaxed"] is False
    assert block.topology["pentagons"] == 12


@pytest.mark.parametrize(
    "params, needle",
    [
        ({"wall": 90}, "neck"),
        ({"neck": 24}, "wall"),
        ({"neck": 24.5, "wall": 90}, "integer"),
        ({"neck": "x", "wall": 90}, "integer"),
        ({"neck": 24, "wall": 24}, "wall"),
        ({"neck": 4, "wall": 90}, "neck"),
        ({"neck": 24, "wall": 90, "stalk_length_A": 0}, "stalk_length_A"),
        ({"neck": 24, "wall": 90, "sheet_radius_A": -1}, "sheet_radius_A"),
        ({"neck": 24, "wall": 90, "relax": "yes"}, "relax"),
        ({"neck": 12, "wall": 20}, "fillet"),
    ],
)
def test_bad_params_raise(params: dict[str, object], needle: str) -> None:
    with pytest.raises(GeneratorError, match=needle):
        build_smooth_drum(params)
