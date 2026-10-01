"""`precis_se.atomic.catalogue` — the DB-backed hexfold catalogue store.

The round-trip tests are ordinary; the ones that matter are
`test_resolve_edge_ignores_a_measured_row_by_default` and its
`trust_measured` twin, which pin the step 6 slice 1 ruling
(`docs/backlog/hexfold-integration.md`, gripe 456641): a measured row is
stored and readable but must not outrank the pinned wildcard, because
`measure_environment`'s seam radius tracks the measurement tube's length
rather than its `EnvKey`, and this table is shared by every design.
"""

from __future__ import annotations

import dataclasses

import pytest

from hexfold import catalogue as hx
from precis.store import Store
from precis_se.atomic.catalogue import DbCatalogueStore, for_store


@pytest.fixture
def cat(store: Store) -> DbCatalogueStore:
    return DbCatalogueStore(store)


def _pinned_z_stick() -> hx.EdgeMotif:
    """The zigzag/stick wildcard from `seed_rows` — the row the ruling
    says a measurement must not displace."""
    rows = [
        r
        for r in hx.seed_rows()
        if r.key.rim_type == "z" and r.key.rung == "stick" and r.key.N is None
    ]
    assert len(rows) == 1, "seed_rows should hold exactly one z/stick wildcard"
    return rows[0]


def _measured_z(N: int, seam_radius: int) -> hx.EdgeMotif:
    """A plausible `source="measured"` row for a concrete dangling count —
    exactly what a `warm_edge` call would deposit."""
    pinned = _pinned_z_stick()
    return dataclasses.replace(
        pinned,
        key=dataclasses.replace(pinned.key, N=N),
        seam_radius=seam_radius,
        source="measured",
        coverage="full",
    )


# ---------- storage ----------


def test_seed_loads_the_wildcard_rows_and_is_idempotent(cat: DbCatalogueStore) -> None:
    expected = len(hx.seed_rows())
    assert cat.seed() == expected
    first = cat.rows("edge")
    assert len(first) == expected
    cat.seed()
    assert len(cat.rows("edge")) == expected, "seed must not duplicate rows"


def test_get_round_trips_an_edge_row(cat: DbCatalogueStore) -> None:
    row = _pinned_z_stick()
    cat.put(row)
    got = cat.get(row.key)
    assert got == row, "to_dict/from_dict must round-trip through JSONB unchanged"


def test_get_round_trips_bulk_and_seam_rows(cat: DbCatalogueStore) -> None:
    """`zone` is the only row-type discriminator the table carries, so
    each zone must hydrate back into its own dataclass."""
    bulk = hx.BulkCell(
        key=hx.EnvKey(zone="bulk", kind="tube", nm=(5, 0), rung="stick"),
        radius_A=3.9,
        pitch_A=4.26,
        bond_axial_A=1.42,
        bond_circ_A=1.42,
        angle_mean_deg=120.0,
        atoms_per_period=20,
        measured_on="2026-09-29",
        source="measured",
        hexfold_version=hx.__version__,
    )
    seam = hx.SeamMotif(
        key=hx.EnvKey(zone="seam", seam=("z", "z", 10, 0), rung="stick"),
        motif="z-z",
        rings={6: 20, 5: 0},
        strain=(0.02, 1.5),
        hits=1,
        source="join",
    )
    cat.put(bulk)
    cat.put(seam)
    assert cat.get(bulk.key) == bulk
    assert cat.get(seam.key) == seam
    assert isinstance(cat.get(bulk.key), hx.BulkCell)
    assert isinstance(cat.get(seam.key), hx.SeamMotif)


def test_put_is_first_wins_unless_forced(cat: DbCatalogueStore) -> None:
    original = _pinned_z_stick()
    cat.put(original)
    clash = dataclasses.replace(original, seam_radius=999)

    cat.put(clash)
    got = cat.get(original.key)
    assert isinstance(got, hx.EdgeMotif)
    assert got.seam_radius == original.seam_radius, "put must be first-wins"

    cat.put(clash, force=True)
    forced = cat.get(original.key)
    assert isinstance(forced, hx.EdgeMotif)
    assert forced.seam_radius == 999


def test_rows_filters_by_zone(cat: DbCatalogueStore) -> None:
    cat.seed()
    assert cat.rows("bulk") == []
    assert len(cat.rows("edge")) == len(hx.seed_rows())
    assert len(cat.rows()) == len(hx.seed_rows())


# ---------- the measured-row gate (step 6 slice 1 ruling) ----------


def test_a_measured_row_is_stored_and_readable_but_withheld_from_rows(
    cat: DbCatalogueStore,
) -> None:
    measured = _measured_z(N=10, seam_radius=22)
    cat.put(measured)

    assert cat.get(measured.key) == measured, "get() must not hide a measured row"
    assert cat.measured("edge") == [measured]
    assert measured not in cat.rows("edge"), (
        "rows() feeds resolve_edge and must withhold measured rows by default"
    )


def test_resolve_edge_ignores_a_measured_row_by_default(
    cat: DbCatalogueStore,
) -> None:
    """The ruling, end to end: a measured row for the exact `(rim_type,
    N)` being resolved would normally win `resolve_edge`'s first branch.
    It must not — the pinned wildcard stands."""
    cat.seed()
    pinned = _pinned_z_stick()
    cat.put(_measured_z(N=10, seam_radius=22))

    row, label = hx.resolve_edge(cat, "z", 10, "stick")
    assert row is not None
    assert label == "pinned z", f"expected the pinned wildcard, got {label!r}"
    assert row.seam_radius == pinned.seam_radius


def test_trust_measured_lets_a_measured_row_win(store: Store) -> None:
    """The gate is a flag, not a deletion — the same store built
    `trust_measured=True` resolves to the measurement. This is what
    gripe 456641's three fixes would unlock; nothing in the product sets
    it today."""
    cat = DbCatalogueStore(store, trust_measured=True)
    cat.seed()
    cat.put(_measured_z(N=10, seam_radius=22))

    row, label = hx.resolve_edge(cat, "z", 10, "stick")
    assert row is not None
    assert label == "exact z10"
    assert row.seam_radius == 22


def test_for_store_returns_a_seeded_untrusting_store(store: Store) -> None:
    cat = for_store(store)
    assert cat.trust_measured is False
    assert len(cat.rows("edge")) == len(hx.seed_rows())
