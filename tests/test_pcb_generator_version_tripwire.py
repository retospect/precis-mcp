"""A generator's output cannot change without its version changing.

The store decides no-op vs. re-expand on ``(generator, version, params)``
and never compares the emitted rows, so an output change shipped without a
:data:`precis.pcb.generators.VERSIONS` bump leaves every board authored at
the old version serving stale copper (pb345846 kept 54 removed B.Cu tracks,
docs/backlog/pcb-generator-version-is-a-manual-bump-with-no-tripwire.md).
This file pins a digest of each registered generator's expansion to its
version. When it fails: bump the generator's entry in ``VERSIONS`` and
replace the pinned ``(version, digest)`` with the pair the failure prints.
"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from precis.pcb import generators
from tests._canon import assert_close
from tests._canon import digest as canon_digest

#: Fixed inputs per generator: a bare field, and one that emits a sink part
#: (version 3 changed only the sink instances, so a sink-free case would not
#: have seen it).
_CASES: dict[str, list[dict[str, Any]]] = {
    "ewod_pad_array": [
        {"grid": [3, 3]},
        {
            "grid": [4, 4],
            "sink_grid": {
                "part": "C0000",
                "channels_per_sink": 16,
                "channel_pins": [f"HVOUT{i}" for i in range(1, 17)],
                "power": {"VDD": "VDD_LOGIC", "GND": "GND"},
            },
        },
        # An 8x8 field, whose plaza lattice (rows/cols 1, 4, 7) puts the via
        # box off the electrode centroid: version 10 moved only the sink
        # seed from one to the other, and the 4x4 case has them coincide.
        {
            "grid": [8, 8],
            "sink_grid": {
                "part": "C0000",
                "channels_per_sink": 64,
                "channel_pins": [f"HVOUT{i}" for i in range(1, 65)],
            },
        },
    ],
}

#: generator -> (version, digest over every case's expansion). Re-pin both
#: together, never the digest alone.
_PINNED: dict[str, tuple[int, str]] = {
    "ewod_pad_array": (10, "1b32f1cd558ddf44"),
}


def _digest(generator: str) -> str:
    # Strict on everything (counts, ids, nets, copper, params, version) except
    # polygon vertex lists, which tests/_canon.py reduces to area/bounds/
    # centroid/ring count at 1e-4. The only insensitivity this adds is arc
    # vertex count, which depends on platform libm (macOS vs glibc, gr495146).
    h = hashlib.sha256()
    for params in _CASES[generator]:
        exp = generators.expand(generator, "G1", params)
        emitted = {
            "components": exp.components,
            "nets": exp.nets,
            "connections": exp.connections,
            "footprints": exp.footprints,
            "features": exp.features,
            "copper": exp.copper,
            "net_classes": exp.net_classes,
            "measures": exp.measures,
            "canonical_params": exp.canonical_params,
        }
        h.update(canon_digest(emitted).encode("utf-8"))
    return h.hexdigest()[:16]


def test_every_registered_generator_is_pinned():
    assert set(generators._REGISTRY) == set(_PINNED) == set(_CASES)
    assert set(generators.VERSIONS) == set(generators._REGISTRY)


@pytest.mark.parametrize("generator", sorted(_PINNED))
def test_output_changes_only_with_a_version_bump(generator):
    version, digest = _PINNED[generator]
    actual = _digest(generator)
    current = generators.VERSIONS[generator]
    assert (current, actual) == (version, digest), (
        f"{generator}: expansion output or version changed — pinned "
        f"({version}, {digest!r}), now ({current}, {actual!r}). If the output "
        f"changed, bump VERSIONS[{generator!r}] and re-pin both values."
    )


def test_expansion_carries_the_versions_table_entry():
    for generator, cases in _CASES.items():
        exp = generators.expand(generator, "G1", cases[0])
        assert exp.version == generators.VERSIONS[generator]


def test_stale_generators_lists_only_rows_behind_the_code():
    current = generators.VERSIONS["ewod_pad_array"]
    rows = {
        "OLD": {"generator": "ewod_pad_array", "version": current - 1},
        "NOW": {"generator": "ewod_pad_array", "version": current},
        "GONE": {"generator": "retired_generator", "version": 1},
    }
    assert generators.stale_generators(rows) == [
        ("OLD", "ewod_pad_array", current - 1, current)
    ]
    note = generators.stale_generator_note(generators.stale_generators(rows))
    assert "OLD (ewod_pad_array)" in note
    assert "put the same generators entry again" in note
    assert generators.stale_generator_note([]) == ""


def _fixed_points(exp: generators.GeneratorExpansion) -> list[tuple[float, float]]:
    pts: list[tuple[float, float]] = []
    for row in exp.copper:
        geom = row["geom"]
        if row["ctype"] == "via":
            pts.append((geom["x"], geom["y"]))
        else:
            for seg in geom["segments"]:
                pts.append((seg["start"][0], seg["start"][1]))
                pts.append((seg["end"][0], seg["end"][1]))
    return pts


def test_fixed_copper_travels_with_the_array_anchor():
    """A part's own footprint copper is never separable from it (Reto
    2026-10-02): an array at (50, 50) emits every via/stub endpoint, ledger
    via point and sink seed exactly (50, 50) from the origin array's."""
    sink: dict[str, Any] = _CASES["ewod_pad_array"][1]
    origin = generators.expand("ewod_pad_array", "G1", dict(sink))
    moved = generators.expand("ewod_pad_array", "G1", {**sink, "x": 50.0, "y": 50.0})
    o_pts, m_pts = _fixed_points(origin), _fixed_points(moved)
    assert len(o_pts) == len(m_pts) > 0
    assert any(r["ctype"] == "via" for r in origin.copper)
    for (ox, oy), (mx, my) in zip(o_pts, m_pts, strict=True):
        assert (mx - ox, my - oy) == pytest.approx((50.0, 50.0))
    for o, m in zip(
        origin.ledger["pads"].values(), moved.ledger["pads"].values(), strict=True
    ):
        if "via" in o:
            assert m["via"]["x"] - o["via"]["x"] == pytest.approx(50.0)
            assert m["via"]["y"] - o["via"]["y"] == pytest.approx(50.0)
    assert len(origin.components) == len(moved.components) > 1
    for o_c, m_c in zip(origin.components[1:], moved.components[1:], strict=True):
        assert (m_c["x"] - o_c["x"], m_c["y"] - o_c["y"]) == pytest.approx((50.0, 50.0))
    # Every moved via/stub point sits inside the array's own mask_open field.
    poly = moved.features[0]["geom"]["polygon"]
    xs, ys = [p[0] for p in poly], [p[1] for p in poly]
    for vx, vy in m_pts:
        assert min(xs) <= vx <= max(xs) and min(ys) <= vy <= max(ys)


def _circle(r: float, quad_segs: int) -> list[list[float]]:
    from shapely.geometry import Point

    ring = Point(1.0, 2.0).buffer(r, quad_segs=quad_segs).exterior.coords
    return [[x, y] for x, y in ring]


def test_arc_vertex_count_does_not_change_the_digest():
    # At the pad resolution (~950 vertices) a one-or-two vertex libm drift
    # moves the area ~1e-8; quad_segs 237 vs 238 is the same class of change.
    # (quad_segs 8 vs 9 is a 1e-2 area change, a real resolution change.)
    a, b = _circle(1.0, 237), _circle(1.0, 238)
    assert len(a) != len(b)
    assert canon_digest({"poly": a}) == canon_digest({"poly": b})
    assert canon_digest({"poly": _circle(1.0, 8)}) != canon_digest(
        {"poly": _circle(1.0, 9)}
    )


def test_a_resized_moved_or_added_pad_changes_the_digest():
    base = {"pads": [{"id": "1", "poly": _circle(1.0, 64)}]}
    resized = {"pads": [{"id": "1", "poly": _circle(1.01, 64)}]}
    moved = {
        "pads": [{"id": "1", "poly": [[x + 0.01, y] for x, y in _circle(1.0, 64)]}]
    }
    added = {"pads": [*base["pads"], {"id": "2", "poly": _circle(1.0, 64)}]}
    for changed in (resized, moved, added):
        assert canon_digest(changed) != canon_digest(base)


def test_assert_close_tolerates_float_noise_only():
    assert_close({"a": [1.0, 2.0], "n": 3}, {"a": [1.00001, 2.0], "n": 3})
    with pytest.raises(AssertionError):
        assert_close({"a": 1.0}, {"a": 1.1})
    with pytest.raises(AssertionError):
        assert_close({"n": 3}, {"n": 4})
