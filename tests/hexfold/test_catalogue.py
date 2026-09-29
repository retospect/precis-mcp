"""``hexfold.catalogue`` -- environment-keyed rows for seam/bulk geometry
(SPEC §26, `docs/backlog/hexfold-integration.md` ruling step 6, slice 1).

Behaviour-preserving: seed rows restate `hexfold.join`'s own module
constants (a test pins the two equal), and `compose` with no ``catalogue``
argument is untouched (`tests/hexfold/test_join.py` already covers that).
This file covers the new surface only -- the store, the lookup, the
measurement library function, `compose`'s catalogue-aware radius/threshold
resolution, and `hexfold.chain.CachedBackend`.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from hexfold import __version__
from hexfold.build import build
from hexfold.catalogue import (
    _MEASURE_FLOOR,
    BulkCell,
    CatalogueError,
    EdgeMotif,
    EnvKey,
    MemoryStore,
    SeamMotif,
    measure_environment,
    resolve_edge,
    seed_rows,
)
from hexfold.chain import CachedBackend, StubBackend
from hexfold.cli import main
from hexfold.join import (
    _LEAK_THRESH,
    SEAM_RADIUS,
    _adjacency,
    _shells_from,
    _thresh_for,
    block_from_net,
    compose,
)
from hexfold.join import LEAK_THRESH_GEO as _LEAK_THRESH_GEO
from hexfold.lattice import tube_radius
from hexfold.stick import stick_info


def _resolved(text: str):
    net = build(text, strict=False)
    pos, _max_force = stick_info(net)
    return block_from_net(net, pos)


# ---------- EnvKey ----------


def test_envkey_canonicalises_nm_and_hashes_stably() -> None:
    a = EnvKey(zone="bulk", kind="tube", nm=(0, 5))
    b = EnvKey(zone="bulk", kind="tube", nm=(5, 0))
    assert a.nm == b.nm == (5, 0)
    assert a.hash() == b.hash()
    assert a.hash() == EnvKey(zone="bulk", kind="tube", nm=(5, 0)).hash()


def test_envkey_hash_differs_on_every_field() -> None:
    base = EnvKey(zone="edge", rim_type="z", N=8, rung="stick")
    assert base.hash() != EnvKey(zone="edge", rim_type="a", N=8, rung="stick").hash()
    assert base.hash() != EnvKey(zone="edge", rim_type="z", N=9, rung="stick").hash()
    assert base.hash() != EnvKey(zone="edge", rim_type="z", N=8, rung="geo").hash()


# ---------- rows: JSON round trip ----------


def test_row_round_trips() -> None:
    key = EnvKey(
        zone="bulk", kind="tube", nm=(8, 0), rung="stick", relaxer="stick@0.2.0"
    )
    bulk = BulkCell(
        key=key,
        radius_A=3.1315,
        pitch_A=4.26,
        bond_axial_A=1.42,
        bond_circ_A=1.42,
        angle_mean_deg=120.0,
        atoms_per_period=32,
        measured_on="2026-09-28",
        source="measured",
        hexfold_version=__version__,
    )
    assert BulkCell.from_dict(bulk.to_dict()) == bulk

    edge = EdgeMotif(
        key=EnvKey(zone="edge", rim_type="z", N=8, rung="stick", relaxer="stick@0.2.0"),
        profile={0: (0.1, 0.006, 6.5), 1: (0.002, 0.0001, 0.06)},
        noise=(0.0001, 0.01),
        thresh=(0.0003, 0.04),
        seam_radius=4,
        leak_thresh=(0.0001, 0.02),
        shells=22,
        measured_on="2026-09-28",
        coverage="full",
        source="measured",
        hexfold_version=__version__,
    )
    assert EdgeMotif.from_dict(edge.to_dict()) == edge

    seam = SeamMotif(
        key=EnvKey(zone="seam", seam=("z", "a", 12, 0), rung="stick"),
        motif="adapter",
        rings={5: 6, 7: 6},
        strain=(0.01, 0.5),
        hits=3,
        source="join",
    )
    assert SeamMotif.from_dict(seam.to_dict()) == seam


# ---------- seed_rows ----------


def test_seed_rows_restate_join_constants() -> None:
    rows = seed_rows()
    assert len(rows) == 6
    by_key = {(r.key.rim_type, r.key.rung): r for r in rows}
    assert set(by_key) == {
        (rt, rung) for rt in ("z", "a", None) for rung in ("stick", "geo")
    }
    for rim_type in ("z", "a", None):
        expected_radius = (
            SEAM_RADIUS[rim_type] if rim_type is not None else SEAM_RADIUS["z"]
        )
        stick_row = by_key[(rim_type, "stick")]
        geo_row = by_key[(rim_type, "geo")]
        assert stick_row.seam_radius == expected_radius
        assert geo_row.seam_radius == expected_radius
        rt = (rim_type, 0) if rim_type is not None else None
        assert stick_row.leak_thresh == _thresh_for(rt, _LEAK_THRESH)
        assert geo_row.leak_thresh == _thresh_for(rt, _LEAK_THRESH_GEO)


# ---------- resolve_edge ----------


def _edge_row(rim_type: str | None, n: int | None, rung: str, radius: int) -> EdgeMotif:
    return EdgeMotif(
        key=EnvKey(zone="edge", rim_type=rim_type, N=n, rung=rung, relaxer="test@0"),
        profile={},
        noise=(0.0, 0.0),
        thresh=(0.001, 0.1),
        seam_radius=radius,
        leak_thresh=(0.001, 0.1),
        shells=radius,
        measured_on="2026-09-28",
        coverage="full",
        source="measured",
        hexfold_version=__version__,
    )


def test_resolve_edge_order_exact_nearest_wildcard_never_down() -> None:
    store = MemoryStore()
    store.put(_edge_row("z", 6, "stick", radius=1))
    store.put(_edge_row("z", 20, "stick", radius=9))
    store.put(_edge_row("z", None, "stick", radius=8))  # wildcard

    exact, label = resolve_edge(store, "z", 6, "stick")
    assert exact is not None and exact.key.N == 6
    assert label == "exact z6"

    # never-down: N=8 sits between the two measured rows -- must pick the
    # larger (conservative), never fall back to the smaller z6 row.
    nearest, label = resolve_edge(store, "z", 8, "stick")
    assert nearest is not None and nearest.key.N == 20
    assert label == "nearest z20"

    wildcard, label = resolve_edge(store, "z", 25, "stick")
    assert wildcard is not None and wildcard.key.N is None
    assert label == "pinned z"

    none_row, label = resolve_edge(store, "a", 8, "stick")
    assert none_row is None
    assert label == "none"

    # rung is part of the match: a geo query never sees the stick rows
    geo_row, label = resolve_edge(store, "z", 6, "geo")
    assert geo_row is None
    assert label == "none"


# ---------- measure_environment ----------


def test_measure_environment_z8_stick() -> None:
    # pinned by exact equality, not "<= some bound": this is today's
    # calibration of the 3x noise multiplier and the trusted-depth gate
    # (catalogue.py, both documented next to the constants) over a
    # (8,0)-len=6 free stick relax -- a later change to either knob must
    # show up here by name, not shift every catalogue row silently.
    edge, bulk = measure_environment((8, 0), rung="stick")
    assert edge.seam_radius == 4
    assert edge.coverage == "full"

    floor_dl, floor_dtheta = _MEASURE_FLOOR["stick"]
    # absolute pin: a fixed 10x margin over the code's own stick floor
    # constants -- the plan's originally-specified multiplier (SPEC §26
    # plan, "thresholds max(10x noise, floor)"), applied to the pinned
    # floor rather than this run's own noise sample.  Unlike `edge.thresh`
    # (derived from *this run's* noise -- widening the calibration widens
    # the pin with it, so it alone proves nothing), this bound cannot move
    # just because a future measurement recalibrates.
    dl_ceiling = floor_dl * 10
    dtheta_ceiling = floor_dtheta * 10
    for s, v in edge.profile.items():
        if s > edge.seam_radius and s <= edge.shells - 3:
            assert v[1] < dl_ceiling and v[2] < dtheta_ceiling, (s, v)
            assert v[1] < edge.thresh[0] and v[2] < edge.thresh[1], (s, v, edge.thresh)

    stub_radius = tube_radius(8, 0)
    stub_pitch = StubBackend().pitch_A("tube", (8, 0))
    assert stub_pitch is not None
    assert abs(bulk.radius_A - stub_radius) / stub_radius < 0.02
    assert abs(bulk.pitch_A - stub_pitch) / stub_pitch < 0.02
    assert bulk.atoms_per_period == 32


def test_measure_environment_a5_stick() -> None:
    edge, _bulk = measure_environment((5, 5), rung="stick")
    assert edge.seam_radius == 3
    assert edge.coverage == "lower-bound"


def test_measure_environment_cap_raises() -> None:
    with pytest.raises(CatalogueError):
        measure_environment((12, 0), rung="stick")


def test_measure_environment_mixed_rim_raises() -> None:
    with pytest.raises(CatalogueError):
        measure_environment((7, 3), rung="stick")


# ---------- compose(catalogue=...) ----------


def test_compose_catalogue_row_moves_exactly_its_shells_and_reports_source() -> None:
    spec1 = "hexfold 0.2\na: tube(8,0, len=10)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    pa, pb = a.ports["out"], b.ports["in"]

    store = MemoryStore()
    store.put(_edge_row("z", 8, "stick", radius=3))

    comp = compose(a, pa, b, pb, k=0, catalogue=store)
    assert comp.seam["radius"] == {"a": 3, "b": 3}
    assert comp.seam["radius_source"] == {"a": "exact z8", "b": "exact z8"}
    assert comp.seam["rung"] == "stick"

    n_a = len(a.elements)
    total_n = n_a + len(b.elements)
    dist_a = _shells_from(set(pa.atoms), _adjacency(a.bonds))
    dist_b = _shells_from(set(pb.atoms), _adjacency(b.bonds))
    expected = np.zeros(total_n, dtype=bool)
    for o, d in dist_a.items():
        if d <= 3:
            expected[o] = True
    for o, d in dist_b.items():
        if d <= 3:
            expected[n_a + o] = True
    assert np.array_equal(comp.movable, expected)


def test_compose_narrower_catalogue_radius_emits_finding() -> None:
    # the store's z8 row (radius=3) is narrower than the pinned
    # SEAM_RADIUS["z"] (8) it would otherwise fall back to -- a measured
    # row winning over a wider wildcard must not be silent.
    spec1 = "hexfold 0.2\na: tube(8,0, len=10)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    pa, pb = a.ports["out"], b.ports["in"]

    store = MemoryStore()
    store.put(_edge_row("z", 8, "stick", radius=3))

    comp = compose(a, pa, b, pb, k=0, catalogue=store)
    assert comp.seam["radius"] == {"a": 3, "b": 3}  # the narrow row still wins
    narrowed = [f for f in comp.findings if f.code == "seam.radius.narrowed"]
    assert len(narrowed) == 2
    data = dict(narrowed[0].data)
    assert data["measured"] == 3
    assert data["pinned"] == SEAM_RADIUS["z"]
    assert data["source"] == "exact z8"


def test_compose_explicit_seam_radius_still_wins_over_catalogue() -> None:
    spec1 = "hexfold 0.2\na: tube(8,0, len=10)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    pa, pb = a.ports["out"], b.ports["in"]

    store = MemoryStore()
    store.put(_edge_row("z", 8, "stick", radius=3))

    comp = compose(a, pa, b, pb, k=0, catalogue=store, seam_radius={"a": 1})
    assert comp.seam["radius"]["a"] == 1
    assert comp.seam["radius_source"]["a"] == "explicit"
    # b has no explicit override, so it still reads the catalogue row
    assert comp.seam["radius"]["b"] == 3
    assert comp.seam["radius_source"]["b"] == "exact z8"


def test_compose_with_no_catalogue_is_unaffected() -> None:
    # today's path (no catalogue, default rung) still gives the exact
    # numbers `tests/hexfold/test_join.py` pins.
    spec1 = "hexfold 0.2\na: tube(8,0, len=3)\n"
    a = _resolved(spec1)
    b = _resolved(spec1)
    comp = compose(a, a.ports["out"], b, b.ports["in"], k=0)
    assert comp.seam["radius"] == {"a": SEAM_RADIUS["z"], "b": SEAM_RADIUS["z"]}
    assert comp.seam["radius_source"] == {"a": "table", "b": "table"}
    assert comp.seam["rung"] == "stick"


# ---------- CachedBackend ----------


def test_cached_backend_prefers_bulk_row_and_falls_back() -> None:
    store = MemoryStore()
    row = BulkCell(
        key=EnvKey(
            zone="bulk",
            kind="tube",
            nm=(8, 0),
            rung="stick",
            relaxer=f"stick@{__version__}",
        ),
        radius_A=9.99,
        pitch_A=42.0,
        bond_axial_A=1.42,
        bond_circ_A=1.42,
        angle_mean_deg=120.0,
        atoms_per_period=32,
        measured_on="2026-09-28",
        source="measured",
        hexfold_version=__version__,
    )
    store.put(row)
    backend = CachedBackend(store)

    assert backend.pitch_A("tube", (8, 0)) == 42.0
    # no row for (9, 0): falls back to StubBackend
    assert backend.pitch_A("tube", (9, 0)) == StubBackend().pitch_A("tube", (9, 0))
    # rim/catalogue are pure delegation, never touch the store
    assert backend.catalogue("tube") == StubBackend().catalogue("tube")
    assert backend.rim("tube", (8, 0), "in") == StubBackend().rim("tube", (8, 0), "in")
    assert backend.fixed_length_A("tube", (8, 0)) == StubBackend().fixed_length_A(
        "tube", (8, 0)
    )


# ---------- MemoryStore JSON ----------


def test_memory_store_json_round_trip() -> None:
    store = MemoryStore.seeded()
    restored = MemoryStore.from_json(store.to_json())
    assert {r.key.hash() for r in restored.rows()} == {
        r.key.hash() for r in store.rows()
    }
    for row in store.rows():
        assert restored.get(row.key) == row


def test_memory_store_from_json_bad_json_raises() -> None:
    with pytest.raises(CatalogueError):
        MemoryStore.from_json("not json")


# ---------- CLI ----------


def test_cli_catalogue_json_lists_seed_rows(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["catalogue", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert len(out["rows"]) == 6
    assert {r["type"] for r in out["rows"]} == {"edge"}


def test_cli_catalogue_measure_writes_file(
    tmp_path, capsys: pytest.CaptureFixture[str]
) -> None:
    f = tmp_path / "cat.json"
    assert main(["catalogue", str(f), "--measure", "tube(5,5)", "--rung", "stick"]) == 0
    capsys.readouterr()
    assert f.exists()
    data = json.loads(f.read_text(encoding="utf-8"))
    kinds = {r["type"] for r in data["rows"]}
    assert kinds == {"edge", "bulk"}

    # a second CLI run over the same file sees the persisted rows too
    assert main(["catalogue", str(f), "--json"]) == 0
    out2 = json.loads(capsys.readouterr().out)
    assert len(out2["rows"]) == len(data["rows"])
