"""Migration 0305: the survey-extraction measurand seed (gr632955).

The ALCHEM agreement diff (td632753) found 192 of 203 unmapped ALCHEM
measurand terms had no precis measurand taxon — ``drop:no_fitting_measurand``
was 85% of survey_extract reader drops. 0305 seeds the standard
electrochem/materials quantities; the variants (the three specialised
overpotentials, vacancy formation energy) specialise their parent measurand,
never the root (me628309).

Real PG. The ``store`` fixture's per-test TRUNCATE leaves ``refs`` empty, so
each test replays 0174 (the ``measurand`` start node) + 0305; a replay
inserts nothing, and a DB without the start node inserts nothing.
"""

from __future__ import annotations

from typing import Any

import psycopg
import pytest

from precis.reading.concepts import normalize_name
from precis.store import Store
from precis.taxonomy.nodes import slugify, taxon_card_text, validate_taxon_meta


def _run_migration(name: str) -> None:
    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            _execute_dump_sql(cur, sql)


_SEED = "0305_survey_measurand_seed.sql"

_SEEDED = (
    "overpotential",
    "onset-potential",
    "tafel-slope",
    "exchange-current-density",
    "charge-transfer-resistance",
    "double-layer-capacitance",
    "cell-voltage",
    "coulombic-efficiency",
    "capacity-retention",
    "cycle-life",
    "specific-capacity",
    "dissolution-rate",
    "tof",
    "ecsa",
    "nucleation-overpotential",
    "coverage-transition-overpotential",
    "overpotential-gap",
    "band-gap",
    "formation-energy",
    "vacancy-formation-energy",
    "d-band-center",
    "bet-surface-area",
    "particle-size",
    "pore-volume",
    "pore-size",
    "interplanar-spacing",
    "bond-length",
    "lattice-parameter",
    "xrd-peak-position",
    "raman-peak-position",
    "ftir-band-position",
    "mass-fraction",
    "oxidation-state",
    "coordination-number",
    "areal-loading",
)

#: Slug -> the measurand slug it specialises instead of the start node.
_PARENTS = {
    "nucleation-overpotential": "overpotential",
    "coverage-transition-overpotential": "overpotential",
    "overpotential-gap": "overpotential",
    "vacancy-formation-energy": "formation-energy",
}

#: canonical -> display spot checks (the put-guard rule: canonical is the
#: si_vector's coherent SI unit, the printed unit lives on display_unit).
_UNITS = {
    "overpotential": ("V", "mV"),
    "tafel-slope": ("V", "mV"),
    "charge-transfer-resistance": ("ohm", "ohm"),
    "double-layer-capacitance": ("F/m^2", "uF/cm^2"),
    "specific-capacity": ("A*s/kg", "mAh/g"),
    "ecsa": ("m^2", "cm^2"),
    "band-gap": ("J", "eV"),
    "bet-surface-area": ("m^2/kg", "m^2/g"),
    "raman-peak-position": ("1/m", "cm^-1"),
    "xrd-peak-position": ("deg", "deg"),
    "coulombic-efficiency": ("1", "%"),
    "cycle-life": ("count", "count"),
    "areal-loading": ("kg/m^2", "mg/cm^2"),
    "tof": ("1/s", "1/s"),
    "dissolution-rate": ("1/s", "%/h"),
}

_HIGHER_IS_BETTER = {
    "overpotential": False,
    "nucleation-overpotential": False,
    "tafel-slope": False,
    "charge-transfer-resistance": False,
    "dissolution-rate": False,
    "exchange-current-density": True,
    "coulombic-efficiency": True,
    "capacity-retention": True,
    "cycle-life": True,
    "specific-capacity": True,
    "tof": True,
    "ecsa": True,
    "bet-surface-area": True,
}


@pytest.fixture
def taxonomy(store: Store) -> Store:
    """The measurand start node (0174), then the survey seed."""
    _run_migration("0174_taxon_seed.sql")
    _run_migration(_SEED)
    return store


def _taxa(store: Store) -> list[tuple[int, str, dict[str, Any]]]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT ref_id, title, meta FROM refs WHERE kind = 'taxon' "
            "AND retired_at IS NULL AND meta->>'slug' = ANY(%s) ORDER BY ref_id",
            (list(_SEEDED),),
        ).fetchall()
    return [(int(r[0]), str(r[1]), dict(r[2])) for r in rows]


def test_seed_inserts_each_measurand_once_and_is_idempotent(
    taxonomy: Store,
) -> None:
    store = taxonomy
    first = _taxa(store)
    assert sorted(m["slug"] for _i, _t, m in first) == sorted(_SEEDED)
    _run_migration(_SEED)
    assert _taxa(store) == first  # replay inserts nothing

    by_slug = {m["slug"]: (rid, title, m) for rid, title, m in first}

    root = next(
        rid
        for rid in store.taxon_start_nodes_reached(first[0][0])
        if (ref := store.get_ref(kind="taxon", id=rid))
        and (ref.meta or {}).get("slug") == "measurand"
    )

    for rid, title, meta in first:
        slug = meta["slug"]
        assert meta["status"] == "proposed", slug
        assert validate_taxon_meta(meta, check_si_unit=True) == meta, slug
        assert slug == slugify(title), slug
        assert meta["norm_name"] == normalize_name(title), slug
        assert meta.get("dimension_kind"), slug
        want_parent = _PARENTS.get(slug)
        want_rid = by_slug[want_parent][0] if want_parent else root
        assert [p for p, _ax in store.taxon_parents(rid)] == [want_rid], slug
        with store.pool.connection() as conn:
            row = conn.execute(
                "SELECT text FROM chunks WHERE ref_id = %s AND ord = -1", (rid,)
            ).fetchone()
        assert row is not None, slug
        assert row[0] == taxon_card_text(title, meta["definition"], meta["aliases"]), (
            slug
        )

    for slug, (canon, display) in _UNITS.items():
        _rid2, _t, meta = by_slug[slug]
        assert (meta.get("canonical_unit"), meta.get("display_unit")) == (
            canon,
            display,
        ), slug
    for slug, want in _HIGHER_IS_BETTER.items():
        _rid2, _t, meta = by_slug[slug]
        assert meta.get("higher_is_better") is want, slug

    _rid_ovp, _t, ovp = by_slug["overpotential"]
    assert ovp.get("required_conditions") == ["product", "current-density"]
    _rid_tof, _t, tof = by_slug["tof"]
    assert tof.get("required_conditions") == ["product"]


def test_seed_is_a_noop_without_the_measurand_root(store: Store) -> None:
    """A database whose taxonomy is gone inserts nothing — never unrooted
    nodes, never a failed chain."""
    _run_migration(_SEED)
    assert _taxa(store) == []
