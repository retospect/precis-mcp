"""Migration 0188 (measures Build A2, docs/backlog/measures-substrate.md "Store
SI, convert at the edges"): the legacy taxa move to coherent SI, their live
numeric rows are superseded by SI rows, ``rxn_values`` is dropped, and every
reader still shows the number it always did.

Real PG. The migration tests replay the chain up to 0187 (``fresh_db``), write
legacy rows through the compatibility views, snapshot every reader, apply 0188
and compare. The store-level tests use the ``store`` fixture, whose per-test
TRUNCATE leaves ``refs`` empty, so the legacy taxa there are minted lazily by
``precis_measure_taxon`` (the same path a baseline-built install takes).
"""

from __future__ import annotations

import re
import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import psycopg
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _measure_render as render
from precis.handlers.measure import MeasureHandler
from precis.handlers.quest import QuestHandler
from precis.store import Migrator, Store
from precis.store._measures_ops import MeasureSpec, legacy_value
from precis.taxonomy.measure_units import make_converter
from tests.workers._helpers import seed_ref

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"
NEW = "0188_measures_si.sql"


# ── builders ───────────────────────────────────────────────────────────────


def _chain_dir_without_0188(tmp: Path) -> None:
    for f in MIGRATIONS_DIR.glob("*.sql"):
        if f.name >= "0188":
            continue
        shutil.copy(f, tmp / f.name)


def _replay_to_0187(dsn: str) -> None:
    tmp = Path(tempfile.mkdtemp(prefix="precis_test_0188_"))
    try:
        _chain_dir_without_0188(tmp)
        Migrator(dsn, tmp).apply_all()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _apply_0188(dsn: str) -> None:
    Migrator(dsn, MIGRATIONS_DIR).apply_all()


def _one(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> Any:
    with psycopg.connect(dsn, autocommit=True) as conn:
        row = conn.execute(sql, params or None).fetchone()
    return None if row is None else row[0]


def _all(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn, autocommit=True) as conn:
        return [tuple(r) for r in conn.execute(sql, params or None).fetchall()]


def _taxon_id(dsn: str, table: str, key: str) -> int:
    got = _one(
        dsn,
        "SELECT ref_id FROM refs WHERE kind = 'taxon' AND retired_at IS NULL "
        "AND meta -> 'legacy_source' ->> 'table' = %s "
        "AND meta -> 'legacy_source' ->> 'key' = %s",
        (table, key),
    )
    assert got is not None, f"no taxon for {table}.{key}"
    return int(got)


def _taxon_units(dsn: str, table: str, key: str) -> tuple[str | None, str | None]:
    row = _all(
        dsn,
        "SELECT meta ->> 'canonical_unit', meta ->> 'display_unit' FROM refs "
        "WHERE kind = 'taxon' AND meta -> 'legacy_source' ->> 'table' = %s "
        "AND meta -> 'legacy_source' ->> 'key' = %s",
        (table, key),
    )
    assert len(row) == 1
    return row[0]


@contextmanager
def _store(dsn: str) -> Iterator[Store]:
    s = Store.connect(dsn)
    try:
        yield s
    finally:
        s.close()


def _legacy_world(dsn: str) -> dict[str, int]:
    """Legacy rows through the views, at the 0187 state, covering both legacy
    tables, point / interval / text rows, set_by, a Build-B row with an error."""
    out: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        for key, kind, title in (
            ("mat", "material", "Al"),
            ("comp", "component", "bolt"),
            ("quest", "quest", "stiff things"),
        ):
            row = conn.execute(
                "INSERT INTO refs (kind, title) VALUES (%s, %s) RETURNING ref_id",
                (kind, title),
            ).fetchone()
            assert row is not None
            out[key] = int(row[0])
        # three of the legacy taxa are proposed properties minted at write time in
        # prod, not core seeds: register them as prod has them
        for prop, name, unit in (
            ("delta_length", "Delta Length", "Å"),
            ("persistence_length", "Persistence Length", "nm"),
        ):
            conn.execute(
                "INSERT INTO material_properties (prop_id, name, canonical_unit, "
                " dimension, value_type, status) VALUES (%s, %s, %s, 'length', "
                "'quantity', 'proposed') ON CONFLICT DO NOTHING",
                (prop, name, unit),
            )
        mv = (
            "INSERT INTO material_values (material_ref_id, property_id, value_num, "
            " value_low, value_high, value_text, set_by) VALUES (%s, %s, %s, %s, %s, %s, %s)"
        )
        for params in (
            ("youngs_modulus", 70.0, None, None, None, "agent"),
            ("youngs_modulus", 7.0, None, None, None, None),  # 7 * 1e9 / 1e9 drifts
            ("delta_length", 1.4, None, None, None, "agent"),
            ("persistence_length", None, 50.0, 59.0, None, None),  # NULL num
            ("elongation_at_break", 29.0, None, None, None, "agent"),  # 29 * 1e-2
            ("density", 2700.0, None, None, None, None),  # not converted
            ("youngs_modulus", None, None, None, "n/a", None),  # text: untouched
        ):
            conn.execute(mv, (out["mat"], *params))
        cv = (
            "INSERT INTO component_spec_values (component_ref_id, spec_id, value_num, "
            " value_low, value_high, set_by) VALUES (%s, %s, %s, %s, %s, %s)"
        )
        for cparams in (
            ("length", 12.5, None, None, "agent"),
            ("length", 7.0, None, None, None),
            ("across_flats", None, 10.0, 11.0, None),  # NULL num
            ("thread_pitch", 1.5, None, None, "agent"),
            ("mass", 0.012, None, None, "agent"),  # kg: untouched
        ):
            conn.execute(cv, (out["comp"], *cparams))
        length = _taxon_id(dsn, "component_specs", "length")
        # a Build-B-style row on a legacy taxon, with and without an error
        for lit, num, err in (("4.0 ± 0.5", 4.0, 0.5), ("9", 9.0, None)):
            conn.execute(
                "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
                " value_num, value_err, value_form, run_key, actor) "
                "VALUES (%s, %s, %s, %s, %s, 'point', %s, 'reader')",
                (out["comp"], length, lit, num, err, f"run:{lit}"),
            )
        conn.execute(
            "UPDATE refs SET meta = meta || '{\"higher_is_better\": true}' "
            "WHERE ref_id = %s",
            (length,),
        )
    return out


def _legacy_view_snapshot(dsn: str) -> dict[str, list[tuple[Any, ...]]]:
    """Every column of both compatibility views except ``id`` (a re-based row is
    a new row), in a stable order."""
    return {
        "material": _all(
            dsn,
            "SELECT material_ref_id, property_id, value_num, value_low, value_high, "
            "value_text, input_unit, conditions::text, maturity, method, source_ref_id, "
            "as_of, set_by, created_at, notes FROM material_values "
            "ORDER BY property_id, value_num NULLS LAST, value_low NULLS LAST, "
            "value_text NULLS LAST",
        ),
        "component": _all(
            dsn,
            "SELECT component_ref_id, spec_id, value_num, value_low, value_high, "
            "value_text, input_unit, conditions::text, maturity, method, source_ref_id, "
            "as_of, set_by, created_at, notes FROM component_spec_values "
            "ORDER BY spec_id, value_num NULLS LAST, value_low NULLS LAST, "
            "value_text NULLS LAST",
        ),
    }


@pytest.fixture
def at_0187(fresh_db: str) -> dict[str, int]:
    """A DB at the 0187 state with a legacy world; returns the ref ids."""
    _replay_to_0187(fresh_db)
    return _legacy_world(fresh_db)


# ── the compat table ───────────────────────────────────────────────────────


class TestCompatTable:
    def test_rows_match_the_legacy_registries_and_pint(self, fresh_db: str) -> None:
        """Every compat row names a real registry row whose unit is its
        legacy_unit; its factor is the one pint computes (USD has no pint
        unit); si_unit has the legacy unit's dimension."""
        _apply_0188(fresh_db)
        rows = _all(
            fresh_db,
            "SELECT legacy_table, legacy_key, legacy_unit, si_unit, factor, si_offset "
            "FROM measure_unit_compat ORDER BY 1, 2",
        )
        assert len(rows) == 30
        registry = {
            "material_properties": "SELECT canonical_unit FROM material_properties "
            "WHERE prop_id = %s",
            "component_specs": "SELECT canonical_unit FROM component_specs "
            "WHERE spec_id = %s",
            "rxn_properties": "SELECT canonical_unit FROM rxn_properties "
            "WHERE prop_id = %s",
        }
        absent = []
        for table, key, legacy, si, factor, offset in rows:
            have = _all(fresh_db, registry[table], (key,))
            if have:
                assert have == [(legacy,)], (table, key)
            else:
                absent.append(key)  # proposed in prod, not a core seed
            assert offset == 0
            conv = make_converter(legacy, si)
            assert conv.value(1.0) == pytest.approx(float(factor), rel=1e-12), (
                key,
                legacy,
            )
        assert sorted(absent) == ["delta_length", "persistence_length", "unit_length"]

    def test_a_table_and_key_pair_is_the_identity_not_a_ref_id(
        self, fresh_db: str
    ) -> None:
        _apply_0188(fresh_db)
        cols = _all(
            fresh_db,
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = 'measure_unit_compat' ORDER BY ordinal_position",
        )
        assert [c[0] for c in cols] == [
            "legacy_table",
            "legacy_key",
            "legacy_unit",
            "si_unit",
            "factor",
            "si_offset",
        ]
        pk = _all(
            fresh_db,
            "SELECT a.attname FROM pg_index i JOIN pg_attribute a "
            "ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey) "
            "WHERE i.indrelid = 'measure_unit_compat'::regclass AND i.indisprimary "
            "ORDER BY a.attname",
        )
        assert [p[0] for p in pk] == ["legacy_key", "legacy_table"]


# ── the migration ──────────────────────────────────────────────────────────


class TestMigration:
    def test_legacy_views_read_the_same_numbers_after(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        before = _legacy_view_snapshot(fresh_db)
        _apply_0188(fresh_db)
        after = _legacy_view_snapshot(fresh_db)
        assert after["material"] == before["material"]
        assert after["component"] == before["component"]
        assert len(before["material"]) == 7 and len(before["component"]) == 7

    def test_taxa_move_to_si_with_the_legacy_unit_as_display(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        assert _taxon_units(fresh_db, "component_specs", "length") == ("mm", None)
        _apply_0188(fresh_db)
        want = {
            ("component_specs", "length"): ("m", "mm"),
            ("component_specs", "head_angle"): ("rad", "deg"),
            ("material_properties", "youngs_modulus"): ("Pa", "GPa"),
            ("material_properties", "delta_length"): ("m", "Å"),
            ("material_properties", "elongation_at_break"): ("1", "%"),
            ("material_properties", "dielectric_strength"): ("V/m", "MV/m"),
            ("rxn_properties", "catalyst_loading"): ("1", "mol%"),
            ("rxn_properties", "price_per_gram"): (
                "USD/g",
                None,
            ),  # currency: no conversion
            # already SI, or with no SI form: untouched
            ("material_properties", "density"): ("kg/m3", None),
            ("component_specs", "mass"): ("kg", None),
            ("component_specs", "unit_cost"): ("USD", None),
            ("rxn_properties", "temperature"): ("K", None),
        }
        for (table, key), units in want.items():
            assert _taxon_units(fresh_db, table, key) == units, (table, key)

    def test_each_converted_row_is_superseded_by_a_new_si_row(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        old = {
            r[0]: r
            for r in _all(
                fresh_db,
                "SELECT m.id, m.value_num, m.created_at, m.literal, m.run_key, "
                "m.reported_unit, m.actor FROM measures m "
                "JOIN refs t ON t.ref_id = m.measurand_ref_id "
                "WHERE t.meta -> 'legacy_source' ->> 'key' = 'length'",
            )
        }
        _apply_0188(fresh_db)
        rows = _all(
            fresh_db,
            "SELECT l.id, n.id, l.superseded_by, l.superseded_at IS NOT NULL, "
            "n.supersedes, n.superseded_by, n.actor, n.value_num, n.created_at, "
            "n.literal, n.run_key, n.reported_unit, n.meta, l.value_num "
            "FROM measures l JOIN measures n ON n.supersedes = l.id "
            "JOIN refs t ON t.ref_id = l.measurand_ref_id "
            "WHERE t.meta -> 'legacy_source' ->> 'key' = 'length' ORDER BY l.id",
        )
        assert len(rows) == len(old) == 4  # 12.5, 7, 4.0 ± 0.5, 9
        for (
            lid,
            nid,
            sup_by,
            sup_at,
            sup,
            n_sup_by,
            actor,
            nval,
            ncreated,
            lit,
            rk,
            ru,
            meta,
            lval,
        ) in rows:
            assert sup_by == nid and sup_at and sup == lid and n_sup_by is None
            assert actor == "migration-0188"
            assert nval == pytest.approx(lval * 1e-3, rel=1e-12)
            # provenance kept
            assert (ncreated, lit, rk) == tuple(old[lid][i] for i in (2, 3, 4))
            # the SI row says what unit its literal is in (L recorded none)
            assert old[lid][5] is None and ru == "mm"
            assert meta["rebased_from"] == lid and meta["legacy_actor"] == old[lid][6]
            assert meta["conversion"] == {
                "from": "mm",
                "to": "m",
                "factor": 1e-3,
                "offset": 0,
            }
        # the text row on a converted taxon, the unconverted taxa: untouched
        untouched = _all(
            fresh_db,
            "SELECT count(*) FROM measures WHERE superseded_by IS NULL "
            "AND actor <> 'migration-0188' AND value_num IS NOT NULL",
        )
        # density 2700, mass 0.012 (and no other live legacy numeric row)
        assert untouched == [(2,)]
        text_row = _all(
            fresh_db,
            "SELECT superseded_by, value_text FROM measures WHERE value_text = 'n/a'",
        )
        assert text_row == [(None, "n/a")]

    def test_null_bounds_and_null_error_stay_null(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        _apply_0188(fresh_db)
        # interval rows (NULL value_num): num stays NULL, bounds scale
        nm = _all(
            fresh_db,
            "SELECT n.value_num, n.value_low, n.value_high, n.value_err "
            "FROM measures n JOIN refs t ON t.ref_id = n.measurand_ref_id "
            "WHERE n.actor = 'migration-0188' "
            "AND t.meta -> 'legacy_source' ->> 'key' = 'persistence_length'",
        )
        assert nm == [(None, pytest.approx(5e-8), pytest.approx(5.9e-8), None)]
        af = _all(
            fresh_db,
            "SELECT n.value_num, n.value_low, n.value_high, n.value_err "
            "FROM measures n JOIN refs t ON t.ref_id = n.measurand_ref_id "
            "WHERE n.actor = 'migration-0188' "
            "AND t.meta -> 'legacy_source' ->> 'key' = 'across_flats'",
        )
        assert af == [(None, pytest.approx(0.010), pytest.approx(0.011), None)]
        # point rows: bounds NULL; the error scales when present, stays NULL when not
        pts = {
            r[0]: r
            for r in _all(
                fresh_db,
                "SELECT n.literal, n.value_num, n.value_low, n.value_high, n.value_err "
                "FROM measures n JOIN refs t ON t.ref_id = n.measurand_ref_id "
                "WHERE n.actor = 'migration-0188' "
                "AND t.meta -> 'legacy_source' ->> 'key' = 'length'",
            )
        }
        assert pts["4.0 ± 0.5"][1:] == (
            pytest.approx(0.004),
            None,
            None,
            pytest.approx(0.0005),
        )
        assert pts["9"][1:] == (pytest.approx(0.009), None, None, None)

    def test_rows_from_both_legacy_tables_are_converted(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        _apply_0188(fresh_db)
        by_table = dict(
            _all(
                fresh_db,
                "SELECT t.meta -> 'legacy_source' ->> 'table', count(*) "
                "FROM measures n JOIN refs t ON t.ref_id = n.measurand_ref_id "
                "WHERE n.actor = 'migration-0188' GROUP BY 1",
            )
        )
        # material: youngs_modulus x2, delta_length, persistence_length,
        # elongation_at_break; component: length x4, across_flats, thread_pitch
        assert by_table == {"material_properties": 5, "component_specs": 6}

    def test_set_by_of_a_rebased_row_is_the_legacy_writer(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        _apply_0188(fresh_db)
        got = _all(
            fresh_db,
            "SELECT value_num, set_by FROM material_values "
            "WHERE property_id = 'youngs_modulus' AND value_num IS NOT NULL "
            "ORDER BY value_num",
        )
        assert got == [(7.0, None), (70.0, "agent")]

    def test_the_covered_kind_list_is_unchanged_so_no_trigger_is_rebuilt(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        sql = (
            "SELECT tgname, oid FROM pg_trigger WHERE NOT tgisinternal "
            "AND tgrelid IN ('refs'::regclass, 'links'::regclass, 'chunks'::regclass) "
            "AND tgname LIKE '%revision%' ORDER BY tgname"
        )
        before = _all(fresh_db, sql)
        _apply_0188(fresh_db)
        assert before and _all(fresh_db, sql) == before
        assert _one(
            fresh_db,
            "SELECT 'display_unit' = ANY (covered_meta) FROM kinds WHERE slug = 'taxon'",
        )
        assert _one(
            fresh_db,
            "SELECT last_error IS NULL FROM revision_trigger_state",
        )

    def test_rxn_values_is_dropped_and_the_compat_rows_are_there(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        assert _one(fresh_db, "SELECT to_regclass('rxn_values') IS NOT NULL")
        _apply_0188(fresh_db)
        assert _one(fresh_db, "SELECT to_regclass('rxn_values') IS NULL")
        assert _one(fresh_db, "SELECT count(*) FROM measure_unit_compat") == 30

    # -- nothing to convert -------------------------------------------------

    def test_an_empty_database_passes(self, fresh_db: str) -> None:
        _replay_to_0187(fresh_db)  # taxa seeded by 0174, no value rows at all
        assert _one(fresh_db, "SELECT count(*) FROM measures") == 0
        _apply_0188(fresh_db)
        assert _one(fresh_db, "SELECT count(*) FROM measures") == 0
        assert _taxon_units(fresh_db, "component_specs", "length") == ("m", "mm")

    def test_a_database_without_the_legacy_taxa_passes(self, fresh_db: str) -> None:
        _replay_to_0187(fresh_db)
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM refs WHERE kind = 'taxon' "
                "AND meta -> 'legacy_source' IS NOT NULL"
            )
        assert (
            _one(fresh_db, "SELECT count(*) FROM refs WHERE meta ? 'legacy_source'")
            == 0
        )
        _apply_0188(fresh_db)
        assert _one(fresh_db, "SELECT count(*) FROM measures") == 0
        assert _one(fresh_db, "SELECT count(*) FROM measure_unit_compat") == 30

    def test_some_taxa_absent_converts_the_rest(self, fresh_db: str) -> None:
        _replay_to_0187(fresh_db)
        ids = _legacy_world(fresh_db)
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            # drop the component-side taxa that have no rows, keep length/material
            conn.execute(
                "DELETE FROM refs WHERE kind = 'taxon' "
                "AND meta -> 'legacy_source' ->> 'table' = 'component_specs' "
                "AND meta -> 'legacy_source' ->> 'key' IN ('width', 'height', "
                "'min_bend_radius', 'max_working_pressure')"
            )
        before = _legacy_view_snapshot(fresh_db)
        _apply_0188(fresh_db)
        assert _legacy_view_snapshot(fresh_db) == before
        assert ids  # world built
        assert (
            _one(
                fresh_db,
                "SELECT count(*) FROM measures WHERE actor = 'migration-0188'",
            )
            == 11
        )

    def test_a_taxon_in_a_third_unit_aborts_naming_it(self, fresh_db: str) -> None:
        _replay_to_0187(fresh_db)
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{canonical_unit}', '\"cm\"') "
                "WHERE kind = 'taxon' AND meta -> 'legacy_source' ->> 'key' = 'length'"
            )
        try:
            with pytest.raises(psycopg.errors.RaiseException, match="neither"):
                _apply_0188(fresh_db)
        finally:
            # fresh_db's teardown replays the chain over whatever the test left
            with psycopg.connect(fresh_db, autocommit=True) as conn:
                conn.execute(
                    "UPDATE refs SET meta = jsonb_set(meta, '{canonical_unit}', "
                    "'\"mm\"') WHERE kind = 'taxon' "
                    "AND meta -> 'legacy_source' ->> 'key' = 'length'"
                )

    def test_a_different_display_unit_aborts_naming_the_taxon(
        self, fresh_db: str
    ) -> None:
        _replay_to_0187(fresh_db)
        length = _taxon_id(fresh_db, "component_specs", "length")
        _all(
            fresh_db,
            'UPDATE refs SET meta = meta || \'{"display_unit": "cm"}\' '
            "WHERE ref_id = %s RETURNING ref_id",
            (length,),
        )
        try:
            with pytest.raises(
                psycopg.errors.RaiseException,
                match=rf"{length} .*display_unit cm.*legacy_unit mm",
            ):
                _apply_0188(fresh_db)
        finally:
            _all(
                fresh_db,
                "UPDATE refs SET meta = meta - 'display_unit' "
                "WHERE ref_id = %s RETURNING ref_id",
                (length,),
            )

    def test_an_equal_display_unit_is_accepted(self, fresh_db: str) -> None:
        _replay_to_0187(fresh_db)
        _all(
            fresh_db,
            'UPDATE refs SET meta = meta || \'{"display_unit": "mm"}\' '
            "WHERE meta -> 'legacy_source' ->> 'key' = 'length' RETURNING ref_id",
        )
        _apply_0188(fresh_db)
        assert _taxon_units(fresh_db, "component_specs", "length") == ("m", "mm")

    def test_superseded_history_on_a_compat_taxon_aborts_naming_it(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        length = _taxon_id(fresh_db, "component_specs", "length")
        with psycopg.connect(fresh_db, autocommit=True) as conn:
            ids: list[int] = []
            for lit, val in (("20", 20.0), ("21", 21.0)):
                got = conn.execute(
                    "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
                    " value_num, value_form, run_key, actor) "
                    "VALUES (%s, %s, %s, %s, 'point', %s, 'reader') RETURNING id",
                    (at_0187["comp"], length, lit, val, f"run:{lit}"),
                ).fetchone()
                assert got is not None
                ids.append(int(got[0]))
            conn.execute(
                "UPDATE measures SET superseded_by = %s, superseded_at = now() "
                "WHERE id = %s",
                (ids[1], ids[0]),
            )
        try:
            with pytest.raises(
                psycopg.errors.RaiseException,
                match=rf"{length} .*1 superseded row",
            ):
                _apply_0188(fresh_db)
            assert _taxon_units(fresh_db, "component_specs", "length") == ("mm", None)
        finally:
            # append-only: only a test TRUNCATE can clear it for fresh_db's teardown
            with psycopg.connect(fresh_db, autocommit=True) as conn:
                conn.execute("SET precis.allow_measures_truncate = 'on'")
                conn.execute("TRUNCATE measures")

    def test_a_row_by_another_actor_in_inches_with_an_error_converts(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        length = _taxon_id(fresh_db, "component_specs", "length")
        old = _one(
            fresh_db,
            "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
            " reported_unit, value_num, value_err, value_form, run_key, actor) "
            "VALUES (%s, %s, '0.5 in', 'in', 12.7, 0.25, 'point', 'run:in', 'reader') "
            "RETURNING id",
            (at_0187["comp"], length),
        )
        _apply_0188(fresh_db)
        row = _all(
            fresh_db,
            "SELECT n.value_num, n.value_err, n.reported_unit, n.actor, n.literal, "
            "n.meta ->> 'legacy_actor', l.superseded_by = n.id "
            "FROM measures n JOIN measures l ON l.id = n.supersedes WHERE l.id = %s",
            (old,),
        )
        assert row == [
            (
                pytest.approx(0.0127),
                pytest.approx(0.00025),
                "in",  # what was printed is kept
                "migration-0188",
                "0.5 in",
                "reader",
                True,
            )
        ]
        # the legacy view still reports the writer and the unit-of-record value
        assert _all(
            fresh_db,
            "SELECT value_num, set_by FROM component_spec_values "
            "WHERE spec_id = 'length' AND set_by = 'reader' AND value_num = 12.7",
        ) == [(12.7, "reader")]

    def test_a_retired_duplicate_taxon_with_the_same_legacy_source_converts_too(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        live = _taxon_id(fresh_db, "component_specs", "length")
        dup = _one(
            fresh_db,
            "INSERT INTO refs (kind, title, meta, retired_at) "
            "SELECT 'taxon', 'Length (old)', meta, now() FROM refs WHERE ref_id = %s "
            "RETURNING ref_id",
            (live,),
        )
        _all(
            fresh_db,
            "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
            " value_num, value_form, run_key, actor) "
            "VALUES (%s, %s, '30', 30, 'point', 'run:dup', 'reader') RETURNING id",
            (at_0187["comp"], dup),
        )
        _apply_0188(fresh_db)
        for ref in (live, dup):
            assert _all(
                fresh_db,
                "SELECT meta ->> 'canonical_unit', meta ->> 'display_unit' "
                "FROM refs WHERE ref_id = %s",
                (ref,),
            ) == [("m", "mm")]
        assert _all(
            fresh_db,
            "SELECT value_num FROM measures WHERE measurand_ref_id = %s "
            "AND superseded_by IS NULL",
            (dup,),
        ) == [(pytest.approx(0.03),)]
        # the live taxon is still the one the legacy mint verbs resolve to
        assert (
            _one(fresh_db, "SELECT precis_measure_taxon('component_specs', 'length')")
            == live
        )

    def test_si_numbers_are_exact_through_the_migration_the_view_and_insert_measure(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        comp = at_0187["comp"]
        _all(
            fresh_db,
            "INSERT INTO component_spec_values (component_ref_id, spec_id, value_num) "
            "VALUES (%s, 'length', 0.9) RETURNING id",
            (comp,),
        )
        assert 0.9 * 1e-3 != 0.0009  # the float product is one ulp off
        _apply_0188(fresh_db)
        by_migration = _one(
            fresh_db,
            "SELECT n.value_num FROM measures n JOIN measures l ON l.id = n.supersedes "
            "WHERE l.value_num = 0.9",
        )
        with _store(fresh_db) as s:
            mid = s.component_value_insert(
                component_ref_id=comp, spec_id="length", value_num=0.9
            )
            length = _taxon_id(fresh_db, "component_specs", "length")
            run = s.insert_measure(
                MeasureSpec(length, "0.9", comp, reported_unit="mm"), actor="reader"
            )
            rows = {r["id"]: r["value_num"] for r in s.measures_for(comp)}
            by_view_trigger = rows[mid]
            by_insert_measure = rows[run.output_id]
        assert by_migration == by_view_trigger == by_insert_measure == 0.0009
        # and the view reads it back as written
        assert _all(
            fresh_db,
            "SELECT count(*) FROM component_spec_values WHERE value_num = 0.9",
        ) == [(3,)]

    def test_a_unitless_numeric_insert_on_a_converted_taxon_is_refused_by_the_table(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        _apply_0188(fresh_db)
        length = _taxon_id(fresh_db, "component_specs", "length")
        density = _taxon_id(fresh_db, "material_properties", "density")
        sql = (
            "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
            " reported_unit, value_num, value_text, value_form, run_key, actor) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'point', 'run:g', 'old-code') RETURNING id"
        )
        comp = at_0187["comp"]
        with pytest.raises(
            psycopg.errors.CheckViolation,
            match=r"no unit given for '5' on 'Length' \(canonical 'm'\); state the unit",
        ):
            _all(fresh_db, sql, (comp, length, "5", None, 5.0, None))
        assert _all(fresh_db, sql, (comp, length, "5", "mm", 0.005, None))  # stated
        assert _all(fresh_db, sql, (comp, length, "see dwg", None, None, "see dwg"))
        # a taxon with no compat row is not the guard's business
        assert _all(fresh_db, sql, (comp, density, "2700", None, 2700.0, None))
        # an error alone is numeric too (the backfill converts it): refused
        with pytest.raises(psycopg.errors.CheckViolation, match="no unit given"):
            _all(
                fresh_db,
                "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
                " value_err, value_form, run_key, actor) "
                "VALUES (%s, %s, '± 0.5', 0.5, 'not_established', 'run:e', 'old-code') "
                "RETURNING id",
                (comp, length),
            )

    # -- rxn_values refusal -------------------------------------------------

    def test_a_row_in_rxn_values_refuses_and_leaves_every_table_untouched(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        rxn = _one(
            fresh_db,
            "INSERT INTO refs (kind, title) VALUES ('rxn', 'amide') RETURNING ref_id",
        )
        _all(
            fresh_db,
            "INSERT INTO rxn_values (rxn_ref_id, property_id, value_num) "
            "VALUES (%s, 'yield', 83) RETURNING id",
            (rxn,),
        )

        def snapshot() -> dict[str, Any]:
            return {
                "measures": _all(fresh_db, "SELECT * FROM measures ORDER BY id"),
                "taxa": _all(
                    fresh_db,
                    "SELECT ref_id, meta FROM refs WHERE kind = 'taxon' ORDER BY ref_id",
                ),
                "rxn_values": _all(fresh_db, "SELECT * FROM rxn_values ORDER BY id"),
                "compat": _one(fresh_db, "SELECT to_regclass('measure_unit_compat')"),
                "ledger": _all(
                    fresh_db, "SELECT version FROM _migrations ORDER BY version"
                ),
                "views": _all(
                    fresh_db,
                    "SELECT md5(pg_get_viewdef('material_values'::regclass)), "
                    "md5(pg_get_viewdef('component_spec_values'::regclass))",
                ),
                "funcs": _all(
                    fresh_db,
                    "SELECT proname, md5(prosrc) FROM pg_proc WHERE proname IN "
                    "('precis_measure_taxon', 'precis_taxon_unit_guard', "
                    "'precis_legacy_value_insert', 'precis_measure_legacy_value') "
                    "ORDER BY proname",
                ),
                "covered": _one(
                    fresh_db, "SELECT covered_meta FROM kinds WHERE slug = 'taxon'"
                ),
            }

        before = snapshot()
        assert before["compat"] is None and len(before["rxn_values"]) == 1
        try:
            with pytest.raises(
                psycopg.errors.RaiseException, match="rxn_values holds 1"
            ):
                _apply_0188(fresh_db)
            assert snapshot() == before
            assert not any(v.startswith("0188") for (v,) in before["ledger"])
        finally:
            # fresh_db's teardown replays the chain over whatever the test left
            _all(fresh_db, "DELETE FROM rxn_values RETURNING id")


# ── the unit guard ─────────────────────────────────────────────────────────


class TestGuardBypass:
    def _rebase_attempt(
        self, dsn: str, ref_id: int, unit: str, *, flag: str | None
    ) -> None:
        with psycopg.connect(dsn) as conn:
            if flag is not None:
                conn.execute(
                    "SELECT set_config('precis.allow_unit_rebase', %s, true)", (flag,)
                )
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{canonical_unit}', to_jsonb(%s::text)) "
                "WHERE ref_id = %s",
                (unit, ref_id),
            )

    def test_the_rebase_happened_under_the_guard_and_cannot_repeat(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        length = _taxon_id(fresh_db, "component_specs", "length")
        # before 0188: live measures + a change is refused, flag or not
        for flag in (None, "on"):
            with pytest.raises(psycopg.errors.CheckViolation, match="live measures"):
                self._rebase_attempt(fresh_db, length, "m", flag=flag)
        _apply_0188(fresh_db)
        assert _taxon_units(fresh_db, "component_specs", "length") == ("m", "mm")
        # a second re-base, in any direction, with the setting on: refused
        for unit in ("mm", "km", "cm"):
            with pytest.raises(psycopg.errors.CheckViolation, match="live measures"):
                self._rebase_attempt(fresh_db, length, unit, flag="on")
        # and with it off
        with pytest.raises(psycopg.errors.CheckViolation, match="live measures"):
            self._rebase_attempt(fresh_db, length, "mm", flag=None)
        assert _taxon_units(fresh_db, "component_specs", "length") == ("m", "mm")

    def test_the_setting_alone_is_not_enough_for_a_taxon_with_no_compat_row(
        self, store: Store
    ) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        tax = store.insert_ref(
            kind="taxon",
            slug=None,
            title="gap",
            meta={"name": "gap", "slug": "gap", "canonical_unit": "K"},
        ).id
        store.insert_measure(
            MeasureSpec(tax, "300", paper, reported_unit="K"), actor="reader"
        )
        dsn = store.dsn
        assert dsn is not None
        with pytest.raises(psycopg.errors.CheckViolation, match="live measures"):
            self._rebase_attempt(dsn, tax, "mK", flag="on")

    def test_the_setting_is_transaction_local(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        _apply_0188(fresh_db)
        # a later session sees no setting left behind by the migration's txn
        assert _one(
            fresh_db,
            "SELECT coalesce(current_setting('precis.allow_unit_rebase', true), '')",
        ) in ("", "off")


class TestOffsetBranch:
    def test_an_affine_compat_row_converts_both_ways_and_in_insert_measure(
        self, store: Store
    ) -> None:
        dsn = store.dsn
        assert dsn is not None
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO material_properties (prop_id, name, canonical_unit, "
                " dimension, value_type, status) VALUES "
                "('zz_temp', 'Zz temp', 'degC', 'temperature', 'quantity', 'proposed')"
            )
            conn.execute(
                "INSERT INTO measure_unit_compat VALUES "
                "('material_properties', 'zz_temp', 'degC', 'K', 1, 273.15)"
            )
        try:
            mat = seed_ref(store, title="x", kind="material")
            store.material_value_insert(
                material_ref_id=mat, property_id="zz_temp", value_num=25.0
            )
            tax = _mint(store, "material_properties", "zz_temp")
            run = store.insert_measure(
                MeasureSpec(tax, "25 ± 2", mat, reported_unit="degC"), actor="reader"
            )
            rows = {r["id"]: r for r in store.measures_for(mat)}
            via_view, via_store = (
                v for _, v in sorted(rows.items(), key=lambda kv: kv[0])
            )
            assert via_view["value_num"] == via_store["value_num"] == 298.15
            assert via_store["value_err"] == 2.0  # an uncertainty scales, never shifts
            assert via_view["canonical_unit"] == "K"
            assert run.output_id in rows
            # back through the view: (298.15 - 273.15) / 1
            assert sorted(
                float(v["value_num"] or 0) for v in store.material_values_for_ref(mat)
            ) == [25.0, 25.0]
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    "DELETE FROM measure_unit_compat WHERE legacy_key = 'zz_temp'"
                )
                conn.execute(
                    "DELETE FROM material_properties WHERE prop_id = 'zz_temp'"
                )


class TestViewJoinIsDefensive:
    def test_a_taxon_not_in_the_compat_si_unit_passes_its_numbers_through(
        self, store: Store
    ) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        tax = _mint(
            store, "material_properties", "youngs_modulus"
        )  # Pa, compat GPa->Pa
        dsn = store.dsn
        assert dsn is not None
        with psycopg.connect(dsn, autocommit=True) as conn:
            # no live measures yet, so the canonical unit may change
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{canonical_unit}', '\"GPa\"') "
                "WHERE ref_id = %s",
                (tax,),
            )
            conn.execute(
                "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
                " value_num, value_form, run_key, actor) "
                "VALUES (%s, %s, '5', 5, 'point', 'run:x', 'reader')",
                (mat, tax),
            )
        # the factor (1e-9 back) is NOT applied: 5 stays 5, not 5e-9 or 5e9
        assert store.material_values_for_ref(mat)[0]["value_num"] == 5.0
        (row,) = store.measures_for(mat)
        assert row["legacy_factor"] is None  # the compat join does not match either


# ── Store SI: a number with no unit is refused ─────────────────────────────


def _mint(store: Store, table: str, key: str) -> int:
    with store.tx() as conn:
        row = conn.execute(
            "SELECT precis_measure_taxon(%s, %s, TRUE)", (table, key)
        ).fetchone()
    assert row is not None and row[0] is not None
    return int(row[0])


class TestUnitlessWriteRefused:
    def test_a_legacy_taxon_refuses_a_bare_number_naming_both_units(
        self, store: Store
    ) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        length = _mint(store, "component_specs", "length")
        with pytest.raises(BadInput) as err:
            store.insert_measure(MeasureSpec(length, "5", paper), actor="reader")
        msg = str(err.value)
        assert "'m'" in msg and "'mm'" in msg and "not guessed" in msg
        assert store.measures_for(paper) == []  # nothing written

    def test_a_dimensionless_canonical_one_is_refused_too(self, store: Store) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        eff = _mint(store, "rxn_properties", "yield")  # canonical '1', display '%'
        with pytest.raises(BadInput) as err:
            store.insert_measure(MeasureSpec(eff, "95", paper), actor="reader")
        assert "'1'" in str(err.value) and "'%'" in str(err.value)
        run = store.insert_measure(
            MeasureSpec(eff, "95", paper, reported_unit="%"), actor="reader"
        )
        (row,) = store.measures_for(paper)
        assert row["id"] == run.output_id and row["value_num"] == pytest.approx(0.95)
        # the canonical unit itself is a stated unit: 0.95 is 0.95
        store.insert_measure(
            MeasureSpec(eff, "0.95", paper, reported_unit="1"), actor="reader"
        )

    def test_caller_supplied_values_are_in_the_reported_unit_and_converted(
        self, store: Store
    ) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        volt = store.insert_ref(
            kind="taxon",
            slug=None,
            title="potential",
            meta={"name": "potential", "slug": "potential", "canonical_unit": "V"},
        ).id
        a = store.insert_measure(
            MeasureSpec(volt, "500", paper, reported_unit="mV", value_num=500.0),
            actor="reader",
        )
        b = store.insert_measure(
            MeasureSpec(volt, "500", paper, reported_unit="mV", value_num=0.5),
            actor="reader",
        )
        rows = {r["id"]: r["value_num"] for r in store.measures_for(paper)}
        assert rows[a.output_id] == pytest.approx(0.5)  # 500 mV
        assert rows[b.output_id] == pytest.approx(0.0005)  # 0.5 mV, as stated

    def test_a_taxon_without_a_display_unit_says_so(self, store: Store) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        temp = _mint(store, "rxn_properties", "temperature")  # K, no display
        with pytest.raises(
            BadInput,
            match=r"no unit given for '300' on .*\(canonical 'K'\); state the unit",
        ):
            store.insert_measure(MeasureSpec(temp, "300", paper), actor="reader")

    def test_interval_and_bound_literals_are_refused_the_same(
        self, store: Store
    ) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        length = _mint(store, "component_specs", "length")
        for lit in ("10–12", "<5", "~7"):
            with pytest.raises(BadInput, match="reported_unit"):
                store.insert_measure(MeasureSpec(length, lit, paper), actor="reader")

    def test_categorical_values_and_unit_less_taxa_still_land(
        self, store: Store
    ) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        length = _mint(store, "component_specs", "length")
        store.insert_measure(
            MeasureSpec(length, "see drawing", paper, value_text="see drawing"),
            actor="reader",
        )
        grade = _mint(store, "component_specs", "grade")  # categorical, no unit
        store.insert_measure(MeasureSpec(grade, "8.8", paper), actor="reader")
        assert len(store.measures_for(paper)) == 2

    def test_the_stated_unit_converts_exactly_through_the_compat_row(
        self, store: Store
    ) -> None:
        paper = seed_ref(store, title="p", kind="paper")
        length = _mint(store, "component_specs", "length")
        a = store.insert_measure(
            MeasureSpec(length, "5", paper, reported_unit="mm"), actor="reader"
        )
        b = store.insert_measure(
            MeasureSpec(length, "5", paper, reported_unit="m"), actor="reader"
        )
        rows = {r["id"]: r for r in store.measures_for(paper)}
        assert rows[a.output_id]["value_num"] == 5 * 1e-3
        assert rows[b.output_id]["value_num"] == 5.0
        assert rows[a.output_id]["meta"]["conversion"] == {"from": "mm", "to": "m"}


# ── the legacy writers and the lazily minted taxon ─────────────────────────


class TestLegacyWriters:
    def test_a_material_write_in_the_legacy_unit_stores_si_and_reads_back(
        self, store: Store
    ) -> None:
        mat = seed_ref(store, title="steel", kind="material")
        mid = store.material_value_insert(
            material_ref_id=mat, property_id="youngs_modulus", value_num=210.0
        )
        (view,) = store.material_values_for_ref(mat)
        assert view["id"] == mid and view["value_num"] == 210.0
        (row,) = store.measures_for(mat)
        assert row["value_num"] == pytest.approx(2.1e11, rel=1e-12)
        assert (row["reported_unit"], row["canonical_unit"], row["display_unit"]) == (
            "GPa",
            "Pa",
            "GPa",
        )

    def test_a_component_write_stores_si_and_reads_back(self, store: Store) -> None:
        comp = seed_ref(store, title="bolt", kind="component")
        store.component_value_insert(
            component_ref_id=comp, spec_id="length", value_num=12.5, set_by="agent"
        )
        (cur,) = store.component_values_for_ref(comp)
        assert cur["value_num"] == 12.5 and cur["set_by"] == "agent"
        (row,) = store.measures_for(comp)
        assert row["value_num"] == pytest.approx(0.0125, rel=1e-12)

    def test_90_degrees_read_back_as_exactly_90_through_the_view_and_python(
        self, store: Store
    ) -> None:
        # deg -> rad has an inexact factor; the read-back must still be 90
        comp = seed_ref(store, title="countersunk screw", kind="component")
        store.component_value_insert(
            component_ref_id=comp, spec_id="head_angle", value_num=90.0
        )
        (view,) = store.component_values_for_ref(comp)
        (row,) = store.measures_for(comp)
        assert row["value_num"] == pytest.approx(1.5707963267948966, rel=1e-15)
        via_python = legacy_value(
            row["value_num"], row["legacy_factor"], row["legacy_offset"]
        )
        assert view["value_num"] == via_python == 90.0

    def test_an_unconverted_taxon_is_written_as_before(self, store: Store) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        store.material_value_insert(
            material_ref_id=mat, property_id="density", value_num=2700.0
        )
        (row,) = store.measures_for(mat)
        assert row["value_num"] == 2700.0 and row["reported_unit"] is None

    def test_a_compat_taxon_not_in_si_refuses_the_legacy_write(
        self, store: Store
    ) -> None:
        mat = seed_ref(store, title="Al", kind="material")
        tax = _mint(store, "material_properties", "youngs_modulus")
        dsn = store.dsn
        assert dsn is not None
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "UPDATE refs SET meta = jsonb_set(meta, '{canonical_unit}', '\"GPa\"') "
                "WHERE ref_id = %s",
                (tax,),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="off by"):
            store.material_value_insert(
                material_ref_id=mat, property_id="youngs_modulus", value_num=210.0
            )

    def test_the_view_reads_back_what_was_written_to_15_digits(
        self, store: Store
    ) -> None:
        mat = seed_ref(store, title="steel", kind="material")
        comp = seed_ref(store, title="bolt", kind="component")
        for val in (0.1234567890123, 123.456789012345, 7.0, 29.0):
            store.component_value_insert(
                component_ref_id=comp,
                spec_id="length",
                value_num=val,  # mm
            )
            store.material_value_insert(
                material_ref_id=mat,
                property_id="youngs_modulus",
                value_num=val,  # GPa
            )
        want = sorted([0.1234567890123, 123.456789012345, 7.0, 29.0])
        got_c = sorted(
            float(r["value_num"] or 0) for r in store.component_values_for_ref(comp)
        )
        got_m = sorted(
            float(r["value_num"] or 0) for r in store.material_values_for_ref(mat)
        )
        assert got_c == want and got_m == want  # exact: all have <= 15 digits

    def test_a_minted_legacy_taxon_starts_in_si_with_the_legacy_display(
        self, store: Store
    ) -> None:
        tax = _mint(store, "material_properties", "youngs_modulus")
        ref = store.get_ref(kind="taxon", id=tax)
        assert ref is not None
        assert ref.meta["canonical_unit"] == "Pa"
        assert ref.meta["display_unit"] == "GPa"
        # idempotent: a second mint finds it
        assert _mint(store, "material_properties", "youngs_modulus") == tax

    def test_a_property_registered_after_0188_with_a_compat_row_is_minted_si(
        self, store: Store
    ) -> None:
        dsn = store.dsn
        assert dsn is not None
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "INSERT INTO material_properties (prop_id, name, canonical_unit, "
                " dimension, value_type, status) VALUES "
                "('zz_len', 'Zz len', 'mm', 'length', 'quantity', 'proposed')"
            )
            conn.execute(
                "INSERT INTO measure_unit_compat VALUES "
                "('material_properties', 'zz_len', 'mm', 'm', 1e-3, 0)"
            )
        try:
            mat = seed_ref(store, title="x", kind="material")
            store.material_value_insert(
                material_ref_id=mat, property_id="zz_len", value_num=5.0
            )
            (row,) = store.measures_for(mat)
            assert row["canonical_unit"] == "m" and row["display_unit"] == "mm"
            assert row["value_num"] == pytest.approx(5e-3)
            assert store.material_values_for_ref(mat)[0]["value_num"] == 5.0
        finally:
            with psycopg.connect(dsn, autocommit=True) as conn:
                conn.execute(
                    "DELETE FROM measure_unit_compat WHERE legacy_key = 'zz_len'"
                )
                conn.execute("DELETE FROM material_properties WHERE prop_id = 'zz_len'")


# ── rxn: the ported store ops ──────────────────────────────────────────────


class TestRxnOnMeasures:
    def test_a_yield_is_written_and_read_in_percent(self, store: Store) -> None:
        rxn = store.insert_ref(kind="rxn", slug="amide", title="amide", meta={}).id
        vid = store.rxn_value_insert(
            rxn_ref_id=rxn,
            property_id="yield",
            value_num=83.0,
            conditions={"solvent": "toluene"},
            maturity="commercial",
            method="measured",
            source_licence="CC-BY",
            set_by="agent",
            notes="n",
        )
        (m,) = store.measures_for(rxn)
        assert m["id"] == vid and m["value_num"] == pytest.approx(0.83)
        assert (m["reported_unit"], m["canonical_unit"], m["display_unit"]) == (
            "%",
            "1",
            "%",
        )
        (v,) = store.rxn_values_for_ref(rxn)
        assert v["property_id"] == "yield" and v["value_num"] == 83.0
        assert (
            v["conditions"] == {"solvent": "toluene"} and v["maturity"] == "commercial"
        )
        assert (v["method"], v["source_licence"], v["set_by"], v["notes"]) == (
            "measured",
            "CC-BY",
            "agent",
            "n",
        )

    def test_null_set_by_reads_back_null_and_text_and_range_values_land(
        self, store: Store
    ) -> None:
        rxn = store.insert_ref(kind="rxn", slug="r2", title="r2", meta={}).id
        store.rxn_value_insert(
            rxn_ref_id=rxn, property_id="yield", value_low=70.0, value_high=80.0
        )
        store.rxn_value_insert(
            rxn_ref_id=rxn, property_id="solvent", value_text="toluene"
        )
        by_prop = {v["property_id"]: v for v in store.rxn_values_for_ref(rxn)}
        assert by_prop["yield"]["set_by"] is None
        assert (by_prop["yield"]["value_low"], by_prop["yield"]["value_high"]) == (
            70.0,
            80.0,
        )
        assert by_prop["yield"]["value_num"] is None
        assert by_prop["solvent"]["value_text"] == "toluene"

    def test_price_per_gram_keeps_usd_per_g_with_no_conversion(
        self, store: Store
    ) -> None:
        rxn = store.insert_ref(kind="rxn", slug="r3", title="r3", meta={}).id
        store.rxn_value_insert(
            rxn_ref_id=rxn, property_id="price_per_gram", value_num=12.0
        )
        (m,) = store.measures_for(rxn)
        assert m["value_num"] == 12.0 and m["canonical_unit"] == "USD/g"
        assert store.rxn_values_for_ref(rxn)[0]["value_num"] == 12.0

    def test_search_values_filters_in_the_registry_unit(self, store: Store) -> None:
        a = store.insert_ref(
            kind="rxn", slug="a", title="a", meta={"reaction_class": "RXNO:1"}
        ).id
        b = store.insert_ref(
            kind="rxn", slug="b", title="b", meta={"reaction_class": "RXNO:1"}
        ).id
        for ref, val in ((a, 83.0), (b, 55.0)):
            store.rxn_value_insert(rxn_ref_id=ref, property_id="yield", value_num=val)
        hits = store.rxn_search_values(property_id="yield", min_val=80.0)
        assert [(h["rxn_title"], h["value_num"]) for h in hits] == [("a", 83.0)]
        both = store.rxn_search_values(property_id="yield", min_val=50.0, max_val=90.0)
        assert [h["value_num"] for h in both] == [55.0, 83.0]
        assert store.rxn_precedent_count("RXNO:1") == (2, 2)
        assert store.rxn_precedent_count("RXNO:2") == (0, 0)

    def test_a_pilot_measure_on_the_shared_taxon_is_not_a_reaction(
        self, store: Store
    ) -> None:
        rxn = store.insert_ref(
            kind="rxn", slug="rk", title="rk", meta={"reaction_class": "RXNO:7"}
        ).id
        store.rxn_value_insert(rxn_ref_id=rxn, property_id="yield", value_num=60.0)
        paper = seed_ref(store, title="a paper", kind="paper")
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE refs SET meta = meta || %s::jsonb WHERE ref_id = %s",
                ('{"reaction_class": "RXNO:7"}', paper),
            )
        eff = _mint(store, "rxn_properties", "yield")
        store.insert_measure(
            MeasureSpec(eff, "99", paper, reported_unit="%"), actor="reader"
        )
        hits = store.rxn_search_values(property_id="yield")
        assert [h["rxn_ref_id"] for h in hits] == [rxn]
        assert store.rxn_precedent_count("RXNO:7") == (1, 1)
        assert store.rxn_values_for_ref(paper) == []
        assert [v["value_num"] for v in store.rxn_values_for_ref(rxn)] == [60.0]

    def test_the_range_filter_has_the_build_b_tolerance(self, store: Store) -> None:
        rxn = store.insert_ref(kind="rxn", slug="rt", title="rt", meta={}).id
        store.rxn_value_insert(rxn_ref_id=rxn, property_id="yield", value_num=7.0)
        # 7.000000000000001 is what a naive back-conversion reads; it must still
        # meet the stored 7 % at both edges
        assert store.rxn_search_values(property_id="yield", min_val=7.000000000000001)
        assert store.rxn_search_values(property_id="yield", max_val=6.999999999999999)
        assert not store.rxn_search_values(property_id="yield", min_val=7.1)

    def test_set_by_of_a_rebased_row_is_the_legacy_writer(self) -> None:
        from precis.store._rxn_ops import _measure_to_value

        def row(legacy_actor: str) -> dict[str, Any]:
            return {
                "id": 1, "subject_ref_id": 2, "property_key": "yield",
                "value_num": 0.5, "value_low": None, "value_high": None,
                "value_text": None, "value_bool": None, "input_unit": None,
                "conditions": {}, "maturity": "lab", "method": None,
                "source_ref_id": None, "source_chunk": None, "source_url": None,
                "as_of": None, "created_at": None, "notes": None,
                "actor": "migration-0188", "legacy_factor": 0.01,
                "legacy_offset": 0.0, "meta": {"legacy_actor": legacy_actor},
            }  # fmt: skip

        assert _measure_to_value(row("agent"))["set_by"] == "agent"
        assert _measure_to_value(row("legacy"))["set_by"] is None
        assert _measure_to_value(row("agent"))["value_num"] == 50.0

    def test_an_unregistered_property_is_refused(self, store: Store) -> None:
        rxn = store.insert_ref(kind="rxn", slug="r4", title="r4", meta={}).id
        with pytest.raises(Exception, match="not registered"):
            store.rxn_value_insert(rxn_ref_id=rxn, property_id="no_such", value_num=1.0)


# ── readers: the number shown before 0188 equals the number shown after ────

_LEGACY_UNIT = {"length": "mm", "youngs_modulus": "GPa", "delta_length": "Å"}


def _shown(rows: list[dict[str, Any]], unit: str | None = None) -> list[str]:
    return sorted(render.value_text(r, unit=unit) for r in rows)


def _line_body(line: str) -> str:
    """A result line without its row handle (a re-based row has a new id)."""
    return re.sub(r"\bmx\d+\b", "mx", line)


class TestReadersShowTheSameNumber:
    """One test per reader of ``measures`` for converted taxa. ``before`` runs at
    the 0187 state (canonical mm / GPa / Å, no display unit), ``after`` at 0188
    (SI, display unit = the legacy one). The default rendering of a mm length is
    compared as is; the others in their legacy unit (the old default of a bare
    ``Å`` or ``GPa`` taxon was pint's automatic prefix)."""

    def test_search_measures_and_the_range_search_with_unit(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        def capture() -> dict[str, Any]:
            with _store(fresh_db) as s:
                tax = _taxon_id(fresh_db, "component_specs", "length")
                h = MeasureHandler(hub=Hub(store=s))
                rows = s.search_measures(tax).rows
                body = h.search(property=str(tax), min=8.0, max=13.0, unit="mm").body
                default = h.search(property=str(tax)).body
                return {
                    "default": _shown(rows),
                    "mm": _shown(rows, "mm"),
                    "ranged": sorted(_line_body(x) for x in body.splitlines()[1:]),
                    "all": sorted(_line_body(x) for x in default.splitlines()[1:]),
                    "n": len(rows),
                }

        before = capture()
        assert before["default"] == ["12.5 mm", "4 ± 0.5 mm", "7 mm", "9 mm"]
        _apply_0188(fresh_db)
        after = capture()
        assert after == before

    def test_best_measure_and_the_quest_measures_view(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        w = at_0187

        def capture() -> dict[str, Any]:
            with _store(fresh_db) as s:
                groups = s.best_measure(None, serving=w["quest"])
                quest = (
                    QuestHandler(hub=Hub(store=s))
                    .get(id=w["quest"], view="measures")
                    .body
                )
                g = next(x for x in groups if x["measurand"] == "Length")
                return {
                    "best": render.value_text(g["best"]),
                    "n": g["n"],
                    "view": [
                        line.split(" | ")[0]
                        for line in quest.splitlines()
                        if line.startswith("Length")
                    ],
                }

        with _store(fresh_db) as s:
            s.add_link(src_ref_id=w["comp"], dst_ref_id=w["quest"], relation="serves")
        before = capture()
        assert before["view"] == ["Length: 12.5 mm"] and before["best"] == "12.5 mm"
        _apply_0188(fresh_db)
        assert capture() == before

    def test_measure_detail_of_the_live_row_and_of_the_superseded_legacy_row(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        with _store(fresh_db) as s:
            tax = _taxon_id(fresh_db, "component_specs", "length")
            (old,) = [r for r in s.search_measures(tax).rows if r["literal"] == "12.5"]
            shown_before = render.value_text(s.measure_detail(old["id"]))
        _apply_0188(fresh_db)
        with _store(fresh_db) as s:
            legacy = s.measure_detail(old["id"])  # superseded, readable
            live = s.measure_detail(legacy["superseded_by"])
            assert (
                legacy["superseded_by"] == live["id"]
                and live["supersedes"] == old["id"]
            )
            assert shown_before == "12.5 mm"
            assert render.value_text(live) == shown_before
            # the old row's stored 12.5 is in mm; it is normalised to SI for display
            assert legacy["value_num"] == pytest.approx(0.0125, rel=1e-12)
            assert render.value_text(legacy) == shown_before
            assert [c["id"] for c in live["chain"]] == [old["id"], live["id"]]
            assert [c["live"] for c in live["chain"]] == [False, True]

    def test_status_all_does_not_compare_a_legacy_number_as_si(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        _apply_0188(fresh_db)
        with _store(fresh_db) as s:
            tax = _taxon_id(fresh_db, "component_specs", "length")
            h = MeasureHandler(hub=Hub(store=s))
            kw: dict[str, Any] = {"property": str(tax), "min": 12.0, "max": 13.0}
            # 12-13 mm: the live SI row, with or without status='all'
            assert h.search(unit="mm", **kw).body.startswith("# 1 measure(s)")
            assert h.search(unit="mm", status="all", **kw).body.startswith(
                "# 1 measure(s)"
            )
            # 10-15 m: the superseded legacy row stores 12.5 (mm); read as SI it
            # would match, so it must be left out of a ranged status='all' search
            far = h.search(
                property=str(tax), min=10.0, max=15.0, unit="m", status="all"
            ).body
        assert not far.startswith("# 1 measure") and "12.5" not in far

    def test_measures_for_carries_what_a_caller_needs_to_convert_at_the_edge(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        _apply_0188(fresh_db)
        with _store(fresh_db) as s:
            rows = [
                r
                for r in s.measures_for(at_0187["comp"])
                if (r["legacy_source"] or {}).get("key") == "length"
            ]
            assert len(rows) == 4
            for r in rows:
                assert r["canonical_unit"] == "m" and r["display_unit"] == "mm"
                assert r["legacy_unit"] == "mm"
            back = sorted(
                float(
                    legacy_value(r["value_num"], r["legacy_factor"], r["legacy_offset"])
                    or 0
                )
                for r in rows
            )
            assert back == [4.0, 7.0, 9.0, 12.5]
            # the superseded legacy rows come back in SI too (not off by 1000)
            every = s.measures_for(at_0187["comp"], include_superseded=True)
            assert (
                len(every) == 13
            )  # length 4+4, across_flats 1+1, thread_pitch 1+1, mass
            legacy = [r for r in every if r["superseded_by"] is not None]
            assert sorted(
                r["value_num"] for r in legacy if r["value_num"]
            ) == pytest.approx([0.0015, 0.004, 0.007, 0.009, 0.0125])

    def test_material_and_component_read_paths_through_the_views(
        self, fresh_db: str, at_0187: dict[str, int]
    ) -> None:
        def capture() -> dict[str, Any]:
            with _store(fresh_db) as s:

                def strip(rows: list[Any]) -> list[tuple[Any, ...]]:
                    out = [
                        (
                            r["property_id"] if "property_id" in r else r["spec_id"],
                            r["value_num"],
                            r["value_low"],
                            r["value_high"],
                            r["value_text"],
                            r["set_by"],
                        )
                        for r in rows
                    ]
                    return sorted(out, key=repr)

                cur = s.component_current_spec_values(at_0187["comp"])
                one = s.component_current_spec_value(at_0187["comp"], "across_flats")
                return {
                    "material": strip(s.material_values_for_ref(at_0187["mat"])),
                    "component": strip(s.component_values_for_ref(at_0187["comp"])),
                    "current": {
                        k: (v["value_num"], v["value_low"], v["value_high"])
                        for k, v in sorted(cur.items())
                    },
                    "current_one": (one["value_low"], one["value_high"])
                    if one
                    else None,
                    "mat_range": [
                        (r["property_id"], r["value_num"])
                        for r in s.material_search_values(
                            property_id="youngs_modulus", min_val=60.0, max_val=80.0
                        )
                    ],
                    "comp_range": sorted(
                        (r["spec_id"], r["value_num"])
                        for r in s.component_search_values(
                            spec_id="length", min_val=10.0, max_val=13.0
                        )
                    ),
                }

        before = capture()
        assert before["mat_range"] == [("youngs_modulus", 70.0)]
        assert before["comp_range"] == [("length", 12.5)]
        assert before["current_one"] == (10.0, 11.0)
        _apply_0188(fresh_db)
        assert capture() == before


# ── the legacy mint verbs convert: a convertible unit is re-based at mint ──


@contextmanager
def _minted(store: Store, table: str, key: str) -> Iterator[None]:
    """Remove the registry and compat rows a test mints (both tables survive the
    per-test TRUNCATE)."""
    dsn = store.dsn
    assert dsn is not None
    pk = {
        "material_properties": "prop_id",
        "component_specs": "spec_id",
        "rxn_properties": "prop_id",
    }[table]
    try:
        yield
    finally:
        with psycopg.connect(dsn, autocommit=True) as conn:
            conn.execute(
                "DELETE FROM measure_unit_compat WHERE legacy_table = %s "
                "AND legacy_key = %s",
                (table, key),
            )
            conn.execute(f"DELETE FROM {table} WHERE {pk} = %s", (key,))


def _compat_row(store: Store, table: str, key: str) -> tuple[Any, ...] | None:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT legacy_unit, si_unit, factor, si_offset FROM measure_unit_compat "
            "WHERE legacy_table = %s AND legacy_key = %s",
            (table, key),
        ).fetchone()
    return None if row is None else tuple(row)


class TestMintVerbsRebaseConvertibleUnits:
    def test_material_mint_in_nm_stores_metres_and_reads_back_what_was_written(
        self, store: Store
    ) -> None:
        with _minted(store, "material_properties", "zz_nm"):
            prop = store.material_property_mint(
                prop_id="zz_nm",
                name="Zz nm",
                canonical_unit="nm",
                dimension="length",
                value_type="quantity",
            )
            assert prop["canonical_unit"] == "nm"  # the registry keeps the unit given
            assert _compat_row(store, "material_properties", "zz_nm") == (
                "nm",
                "m",
                Decimal("1E-9"),
                Decimal(0),
            )
            mat = seed_ref(store, title="graphene", kind="material")
            store.material_value_insert(
                material_ref_id=mat, property_id="zz_nm", value_num=3.7
            )
            (view,) = store.material_values_for_ref(mat)
            assert view["value_num"] == 3.7  # exactly what was written
            (row,) = store.measures_for(mat)
            assert row["value_num"] == 3.7e-9  # metres, exact
            assert (row["canonical_unit"], row["display_unit"]) == ("m", "nm")

    def test_material_mint_in_celsius_applies_the_offset(self, store: Store) -> None:
        with _minted(store, "material_properties", "zz_c"):
            store.material_property_mint(
                prop_id="zz_c",
                name="Zz c",
                canonical_unit="degC",
                dimension="temperature",
                value_type="quantity",
            )
            assert _compat_row(store, "material_properties", "zz_c") == (
                "degC",
                "K",
                Decimal(1),
                Decimal("273.15"),
            )
            mat = seed_ref(store, title="x", kind="material")
            store.material_value_insert(
                material_ref_id=mat, property_id="zz_c", value_num=25.0
            )
            assert store.material_values_for_ref(mat)[0]["value_num"] == 25.0
            (row,) = store.measures_for(mat)
            assert row["value_num"] == 298.15 and row["canonical_unit"] == "K"

    def test_component_spec_mint_in_mpa(self, store: Store) -> None:
        with _minted(store, "component_specs", "zz_p"):
            store.component_spec_mint(
                spec_id="zz_p",
                name="Zz p",
                canonical_unit="MPa",
                dimension="pressure",
                value_type="quantity",
                category_id=None,
            )
            comp = seed_ref(store, title="hose", kind="component")
            store.component_value_insert(
                component_ref_id=comp, spec_id="zz_p", value_num=12.3
            )
            assert store.component_values_for_ref(comp)[0]["value_num"] == 12.3
            (row,) = store.measures_for(comp)
            assert row["value_num"] == 12.3e6 and row["canonical_unit"] == "Pa"

    def test_rxn_property_mint_in_percent(self, store: Store) -> None:
        with _minted(store, "rxn_properties", "zz_pct"):
            store.rxn_property_mint(
                prop_id="zz_pct",
                name="Zz pct",
                canonical_unit="%",
                dimension="dimensionless",
                value_type="quantity",
            )
            rxn = store.insert_ref(kind="rxn", slug="zzr", title="zzr", meta={}).id
            store.rxn_value_insert(rxn_ref_id=rxn, property_id="zz_pct", value_num=83.0)
            assert store.rxn_values_for_ref(rxn)[0]["value_num"] == 83.0
            (row,) = store.measures_for(rxn)
            assert row["value_num"] == 0.83 and row["canonical_unit"] == "1"

    def test_an_si_unit_or_a_unit_with_no_si_form_mints_with_no_compat_row(
        self, store: Store
    ) -> None:
        for key, unit in (("zz_k", "K"), ("zz_usd", "USD/g"), ("zz_ph", "pH")):
            with _minted(store, "material_properties", key):
                store.material_property_mint(
                    prop_id=key,
                    name=key,
                    canonical_unit=unit,
                    dimension="x",
                    value_type="quantity",
                )
                assert _compat_row(store, "material_properties", key) is None

    def test_a_second_mint_of_a_key_does_not_disturb_its_compat_row(
        self, store: Store
    ) -> None:
        from precis.store._measures_ops import register_legacy_unit

        with _minted(store, "material_properties", "zz_twice"):
            with store.tx() as conn:
                register_legacy_unit(conn, "material_properties", "zz_twice", "nm")
                register_legacy_unit(conn, "material_properties", "zz_twice", "mm")
            assert _compat_row(store, "material_properties", "zz_twice") == (
                "nm",
                "m",
                Decimal("1E-9"),
                Decimal(0),
            )

    def test_si_form_is_computed_like_the_seeded_rows(self) -> None:
        from precis.taxonomy.measure_units import si_form

        nm = si_form("nm")
        assert nm is not None and (nm.si_unit, nm.factor) == ("m", Decimal("1E-9"))
        c = si_form("°C")
        assert c is not None and (c.factor, c.offset) == (1, Decimal("273.15"))
        pct = si_form("%")
        assert pct is not None and (pct.si_unit, pct.factor) == ("1", Decimal("0.01"))
        for unit in ("m", "K", "Pa", "1", "USD", "pH", "", None):
            assert si_form(unit) is None
