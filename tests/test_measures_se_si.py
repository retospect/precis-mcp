"""Migration 0236: the SE charge and dipole measurands (seeded by 0182 in
``e`` and ``D``) move to coherent SI the 0188 way, through
``measure_unit_compat`` rows keyed ``('taxon', slug)``.

Real PG. The ``store`` fixture's per-test TRUNCATE leaves ``refs`` empty, so
each test replays 0174 + 0182 (the taxonomy as prod has it before 0236),
writes what it needs, then replays 0236 (idempotent by design).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import psycopg
import pytest
from psycopg.rows import dict_row

from precis.store import Store
from precis.taxonomy.nodes import validate_taxon_meta

E = Decimal("1.602176634e-19")
DEBYE = Decimal("3.335640951981520495755767145e-30")


def _run_migration(name: str) -> None:
    from precis.store.migrate import _execute_dump_sql
    from tests.conftest import MIGRATIONS_DIR, _active_dsn

    sql = (MIGRATIONS_DIR / name).read_text(encoding="utf-8")
    with psycopg.connect(_active_dsn(), autocommit=True) as conn:
        with conn.cursor() as cur:
            _execute_dump_sql(cur, sql)


@pytest.fixture
def seeded(store: Store) -> Store:
    _run_migration("0174_taxon_seed.sql")
    _run_migration("0182_se_measurand_seed.sql")
    return store


def _taxon(store: Store, slug: str) -> tuple[int, dict[str, Any]]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT ref_id, meta FROM refs WHERE kind = 'taxon' "
            "AND retired_at IS NULL AND meta ->> 'slug' = %s",
            (slug,),
        ).fetchone()
    assert row is not None, slug
    return int(row[0]), dict(row[1])


def _subject(store: Store) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO refs (kind, title) VALUES ('material', 'zz 0236 subject') "
            "RETURNING ref_id"
        ).fetchone()
    assert row is not None
    return int(row[0])


def _measure(
    store: Store,
    subject: int,
    measurand: int,
    num: float,
    *,
    err: float | None = None,
    unit: str | None = "e",
) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
            " reported_unit, value_num, value_err, value_form, run_key, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'point', %s, 'survey-extract') "
            "RETURNING id",
            (subject, measurand, f"{num} {unit or ''}", unit, num, err, f"run:{num}"),
        ).fetchone()
    assert row is not None
    return int(row[0])


def _row(store: Store, mid: int) -> dict[str, Any]:
    with store.pool.connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            got = cur.execute("SELECT * FROM measures WHERE id = %s", (mid,)).fetchone()
    assert got is not None
    return dict(got)


def test_compat_rows_are_exact_and_agree_with_pint(store: Store) -> None:
    from precis.taxonomy.measure_units import make_converter

    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT legacy_key, legacy_unit, si_unit, factor, si_offset "
            "FROM measure_unit_compat WHERE legacy_table = 'taxon' ORDER BY 1"
        ).fetchall()
    assert rows == [
        ("dipole-moment", "D", "C*m", DEBYE, 0),
        ("net-partial-charge", "e", "C", E, 0),
    ]
    for _key, legacy, si, factor, _off in rows:
        assert make_converter(legacy, si).value(1.0) == pytest.approx(
            float(factor), rel=1e-15
        )


def test_seed_starts_scaled_and_0236_rebases_both_taxa(seeded: Store) -> None:
    _, charge = _taxon(seeded, "net-partial-charge")
    _, dipole = _taxon(seeded, "dipole-moment")
    assert (charge["canonical_unit"], dipole["canonical_unit"]) == ("e", "D")
    _run_migration("0236_se_charge_dipole_si.sql")
    _, charge = _taxon(seeded, "net-partial-charge")
    _, dipole = _taxon(seeded, "dipole-moment")
    assert (charge["canonical_unit"], charge["display_unit"]) == ("C", "e")
    assert (dipole["canonical_unit"], dipole["display_unit"]) == ("C*m", "D")
    # the slice-0 unit guard accepts what 0236 leaves
    assert validate_taxon_meta(charge) == charge
    assert validate_taxon_meta(dipole) == dipole


def test_rebase_converts_a_live_e_measure_to_coulombs_exactly(seeded: Store) -> None:
    charge_id, _ = _taxon(seeded, "net-partial-charge")
    subject = _subject(seeded)
    old = _measure(seeded, subject, charge_id, 0.77, err=0.01)

    _run_migration("0236_se_charge_dipole_si.sql")

    before = _row(seeded, old)
    assert before["superseded_by"] is not None
    new = _row(seeded, int(before["superseded_by"]))
    assert new["supersedes"] == old and new["superseded_by"] is None
    assert new["actor"] == "migration-0236"
    assert new["value_num"] == float(Decimal("0.77") * E)
    assert new["value_err"] == float(Decimal("0.01") * E)
    assert new["reported_unit"] == "e"  # the unit the literal is in
    assert new["literal"] == before["literal"]
    assert new["created_at"] == before["created_at"]
    conv = new["meta"]["conversion"]
    assert (conv["from"], conv["to"]) == ("e", "C")
    assert Decimal(str(conv["factor"])) == E
    assert new["meta"]["legacy_actor"] == "survey-extract"


def test_0236_is_idempotent(seeded: Store) -> None:
    charge_id, _ = _taxon(seeded, "net-partial-charge")
    _measure(seeded, _subject(seeded), charge_id, 0.5)
    _run_migration("0236_se_charge_dipole_si.sql")
    with seeded.pool.connection() as conn:
        snap = conn.execute(
            "SELECT id, superseded_by FROM measures ORDER BY id"
        ).fetchall()
    taxa = (_taxon(seeded, "net-partial-charge"), _taxon(seeded, "dipole-moment"))

    _run_migration("0236_se_charge_dipole_si.sql")

    with seeded.pool.connection() as conn:
        again = conn.execute(
            "SELECT id, superseded_by FROM measures ORDER BY id"
        ).fetchall()
    assert again == snap
    assert (
        _taxon(seeded, "net-partial-charge"),
        _taxon(seeded, "dipole-moment"),
    ) == taxa


def test_unitless_guard_refuses_a_bare_number_on_the_rebased_taxa(
    seeded: Store,
) -> None:
    _run_migration("0236_se_charge_dipole_si.sql")
    subject = _subject(seeded)
    for slug in ("net-partial-charge", "dipole-moment"):
        rid, _ = _taxon(seeded, slug)
        with pytest.raises(psycopg.errors.CheckViolation, match="no unit given"):
            _measure(seeded, subject, rid, 1.0, unit=None)
        # a stated unit is fine
        _measure(seeded, subject, rid, 1.0, unit="C" if "charge" in slug else "C*m")


def test_unit_guard_still_refuses_an_unrelated_unit_change(seeded: Store) -> None:
    _run_migration("0236_se_charge_dipole_si.sql")
    charge_id, _ = _taxon(seeded, "net-partial-charge")
    _measure(seeded, _subject(seeded), charge_id, 1e-19, unit="C")
    with seeded.pool.connection() as conn:
        # transaction-local, like 0236's own: the pool connection rolls back
        conn.execute("SELECT set_config('precis.allow_unit_rebase', 'on', true)")
        with pytest.raises(psycopg.errors.CheckViolation, match="live measures"):
            conn.execute(
                'UPDATE refs SET meta = meta || \'{"canonical_unit": "e"}\' '
                "WHERE ref_id = %s",
                (charge_id,),
            )


def test_0236_refuses_when_a_scan_fails_and_changes_nothing(seeded: Store) -> None:
    charge_id, _ = _taxon(seeded, "net-partial-charge")
    with seeded.pool.connection() as conn:
        # hand-edited to a unit that is neither the old one nor SI
        conn.execute(
            'UPDATE refs SET meta = meta || \'{"canonical_unit": "mC"}\' '
            "WHERE ref_id = %s",
            (charge_id,),
        )
    dipole_before = _taxon(seeded, "dipole-moment")
    with pytest.raises(psycopg.Error, match="neither the old unit nor SI"):
        _run_migration("0236_se_charge_dipole_si.sql")
    # one transaction: the dipole taxon was not re-based either
    assert _taxon(seeded, "dipole-moment") == dipole_before
    assert dipole_before[1]["canonical_unit"] == "D"
