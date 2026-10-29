"""Currency on measures — slice 1 of docs/backlog/measure-currency-units.md
(gr510555): ``measures.currency``/``price_year`` recorded as reported, ranges
and rankings within one currency, legacy NULL rows read through their
reported/canonical token, and the sha + frozen contract unchanged. Real PG
(the ``store`` fixture); the migration tests replay the chain to 0247 then
apply 0249.
"""

from __future__ import annotations

import re
import shutil
import tempfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import psycopg
import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers import _measure_render as render
from precis.handlers.measure import MeasureHandler
from precis.store import Migrator, Store
from precis.store._measures_ops import (
    ANY_YEAR,
    MeasureSpec,
    reset_money_tables_cache,
    row_interval,
)
from precis.taxonomy.money import CPI_SERIES, row_currency
from precis.taxonomy.nodes import slugify
from tests.workers._helpers import seed_chunk, seed_ref

MIGRATIONS_DIR = Path(__file__).parent.parent / "src" / "precis" / "migrations"


# ── builders ───────────────────────────────────────────────────────────────


def _taxon(
    store: Store,
    name: str,
    *,
    unit: str | None = None,
    display: str | None = None,
    dimension_kind: str | None = None,
    parent: int | None = None,
) -> int:
    meta: dict[str, Any] = {
        "name": name,
        "norm_name": name.lower(),
        "slug": slugify(name),
        "definition": f"{name}.",
        "aliases": [],
        "status": "proposed",
    }
    if unit is not None:
        meta["canonical_unit"] = unit
    if display is not None:
        meta["display_unit"] = display
    if dimension_kind is not None:
        meta["dimension_kind"] = dimension_kind
    ref = store.insert_ref(kind="taxon", slug=None, title=name, meta=meta)
    if parent is not None:
        store.add_link(src_ref_id=ref.id, dst_ref_id=parent, relation="specialises")
    return ref.id


@pytest.fixture
def w(store: Store) -> dict[str, Any]:
    """A currency measurand (unit cost, USD/t canonical) under a ``measurand``
    start node, a second one per-year, a plain mass measurand, a quest the
    subject paper serves, and a chunk to anchor in."""
    d: dict[str, Any] = {}
    d["start"] = _taxon(store, "measurand")
    d["cost"] = _taxon(
        store,
        "unit cost",
        unit="USD/t",
        display="USD/t",
        dimension_kind="currency",
        parent=d["start"],
    )
    d["mass"] = _taxon(store, "catalyst mass", unit="kg", parent=d["start"])
    d["quest"] = store.insert_ref(kind="quest", slug=None, title="cheap H2").id
    d["paper"] = seed_ref(store, title="a TEA paper", kind="paper")
    store.add_link(src_ref_id=d["paper"], dst_ref_id=d["quest"], relation="serves")
    d["chunk"] = seed_chunk(
        store, ref_id=d["paper"], text="cost was 1000 EUR/t in 2015 prices."
    )
    return d


def _put(
    store: Store,
    w: dict[str, Any],
    key: str,
    literal: str,
    *,
    unit: str | None = None,
    **kw: Any,
) -> Any:
    return store.insert_measure(
        MeasureSpec(
            measurand_ref_id=w[key],
            literal=literal,
            subject_ref_id=w["paper"],
            reported_unit=unit,
            **kw,
        ),
        actor="reader",
        model="m1",
    )


def _row(store: Store, measure_id: int) -> dict[str, Any]:
    return store.measure_detail(measure_id)


@pytest.fixture
def h(store: Store) -> MeasureHandler:
    return MeasureHandler(hub=Hub(store=store))


# ── store: the columns ────────────────────────────────────────────────────


class TestColumns:
    def test_the_token_stores_currency_year_and_a_scaled_remainder(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        # the spec's acceptance case: EUR2015/kg on a USD/t measurand stores
        # the amount x1000, in EUR at 2015 — never converted
        run = _put(store, w, "cost", "1", unit="EUR2015/kg")
        r = _row(store, run.output_id)
        assert (r["currency"], r["price_year"]) == ("EUR", 2015)
        assert r["value_num"] == pytest.approx(1000.0)
        assert r["reported_unit"] == "EUR2015/kg"

    def test_the_field_supplies_a_missing_year(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        run = _put(store, w, "cost", "1000", unit="EUR/t", price_year=2015)
        r = _row(store, run.output_id)
        assert (r["currency"], r["price_year"]) == ("EUR", 2015)

    def test_no_year_is_accepted_and_stays_null(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        run = _put(store, w, "cost", "500", unit="USD/t")
        r = _row(store, run.output_id)
        assert (r["currency"], r["price_year"]) == ("USD", None)

    def test_a_symbol_token_stores_its_iso_code(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        run = _put(store, w, "cost", "500", unit="$/t")
        assert _row(store, run.output_id)["currency"] == "USD"


class TestRefusals:
    def test_field_and_token_years_that_disagree(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        with pytest.raises(BadInput, match="disagrees"):
            _put(store, w, "cost", "1000", unit="EUR2015/t", price_year=2016)
        # the same year twice is fine
        run = _put(store, w, "cost", "1000", unit="EUR2015/t", price_year=2015)
        assert _row(store, run.output_id)["price_year"] == 2015

    def test_a_currency_measurand_needs_a_token(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        with pytest.raises(BadInput, match="no currency token") as err:
            _put(store, w, "cost", "1000", unit="kg")
        assert "reported_unit='EUR2015/t'" in str(err.value.next)

    def test_a_price_year_belongs_to_a_currency_figure(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        with pytest.raises(BadInput, match="belongs to a currency figure"):
            _put(store, w, "mass", "5", unit="kg", price_year=2015)
        with pytest.raises(BadInput, match="belongs to a currency figure"):
            _put(store, w, "mass", "five", price_year=2015, value_text="five")

    @pytest.mark.parametrize("year", [1899, 2200])
    def test_a_year_out_of_range_is_refused(
        self, store: Store, w: dict[str, Any], year: int
    ) -> None:
        with pytest.raises(BadInput, match="price year"):
            _put(store, w, "cost", "1000", unit="EUR/t", price_year=year)


# ── the write verb ────────────────────────────────────────────────────────


class TestPutRun:
    def _meta(self, w: dict[str, Any], **over: Any) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "measurand": "measurand/unit-cost",
            "subject": f"pa{w['paper']}",
            "reported_unit": "EUR/t",
            "tier": "asserted",
        }
        meta.update(over)
        return meta

    def test_price_year_through_meta(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        body = h.put(text="1000", meta=self._meta(w, price_year=2015)).body
        found = re.search(r"^mx(\d+) ", body, re.M)
        assert found is not None, body
        r = _row(store, int(found.group(1)))
        assert (r["currency"], r["price_year"]) == ("EUR", 2015)
        assert "no price year" not in body

    def test_no_price_year_gets_the_hint_line(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        body = h.put(text="1000", meta=self._meta(w)).body
        assert "no price year" in body
        assert "shown as reported, not converted" in body
        assert "price_year=" in body

    def test_a_non_int_price_year_is_refused(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        with pytest.raises(BadInput, match=r"meta\.price_year"):
            h.put(text="1000", meta=self._meta(w, price_year="early"))
        # a digit string is accepted as the int
        body = h.put(text="1000", meta=self._meta(w, price_year="2015")).body
        found = re.search(r"^mx(\d+) ", body, re.M)
        assert found is not None, body
        assert _row(store, int(found.group(1)))["price_year"] == 2015


# ── ranges and ranking stay within one currency ───────────────────────────


class TestWithinCurrency:
    def test_a_range_matches_only_the_bounds_currency(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        eur = _put(store, w, "cost", "1000", unit="EUR/t")
        _put(store, w, "cost", "950", unit="USD/t")  # in range, wrong currency
        _put(store, w, "cost", "500", unit="USD/t")  # out of range entirely
        found = store.search_measures(
            w["cost"], min_si=900, max_si=1100, bound_currency=("EUR", None)
        )
        assert [r["id"] for r in found.rows] == [eur.output_id]
        # the SQL range kept only 900..1100; the footer counts the in-range
        # row in another group
        assert found.other_currency == 1
        # no bound currency: the SQL path is unchanged
        found = store.search_measures(w["cost"], min_si=400, max_si=600)
        assert len(found.rows) == 1 and found.other_currency == 0

    def test_a_range_groups_on_the_price_year_too(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        # a NULL year is its own group: USD2010, USD2020 and yearless USD
        # are three groups, and the bound names one
        old = _put(store, w, "cost", "1000", unit="USD2010/t")
        _put(store, w, "cost", "980", unit="USD2020/t")
        _put(store, w, "cost", "960", unit="USD/t")
        found = store.search_measures(
            w["cost"], min_si=900, max_si=1100, bound_currency=("USD", 2010)
        )
        assert [r["id"] for r in found.rows] == [old.output_id]
        assert found.other_currency == 2
        # and the handler takes the year from the unit= token
        h = MeasureHandler(hub=Hub(store=store))
        body = h.search(
            property="measurand/unit-cost", min=900, max=1100, unit="USD2020/t"
        ).body
        assert f"mx{old.output_id}" not in body
        assert "2 row(s) in another currency or price year" in body

    def test_a_range_without_unit_spans_all_price_years(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        # no unit=: the taxon's currency bounds across every price year —
        # the rows never convert, but each labels its own year
        old = _put(store, w, "cost", "1000", unit="USD2010/t")
        new = _put(store, w, "cost", "980", unit="USD2020/t")
        bare = _put(store, w, "cost", "960", unit="USD/t")
        _put(store, w, "cost", "950", unit="EUR/t")  # in range, other currency
        found = store.search_measures(
            w["cost"], min_si=900, max_si=1100, bound_currency=("USD", ANY_YEAR)
        )
        assert {r["id"] for r in found.rows} == {
            old.output_id,
            new.output_id,
            bare.output_id,
        }
        assert found.other_currency == 1

    def test_the_handler_range_without_unit_labels_each_rows_year(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        old = _put(store, w, "cost", "1000", unit="USD2010/t")
        new = _put(store, w, "cost", "980", unit="USD2020/t")
        eur = _put(store, w, "cost", "950", unit="EUR/t")
        body = h.search(property="measurand/unit-cost", min=900, max=1100).body
        assert f"mx{old.output_id}" in body and "USD(2010)" in body
        assert f"mx{new.output_id}" in body and "USD(2020)" in body
        assert f"mx{eur.output_id}" not in body
        assert "1 row(s) in another currency or price year" in body

    def test_the_handler_search_names_the_bound_currency(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        eur = _put(store, w, "cost", "1000", unit="EUR/t")
        usd = _put(store, w, "cost", "950", unit="USD/t")
        body = h.search(
            property="measurand/unit-cost", min=900, max=1100, unit="EUR/t"
        ).body
        assert f"mx{eur.output_id}" in body
        assert f"mx{usd.output_id}" not in body
        assert "1 row(s) in another currency or price year left out" in body

    def test_best_measure_partitions_by_currency(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        eur = _put(store, w, "cost", "1000", unit="EUR/t")
        usd = _put(store, w, "cost", "50", unit="USD/t")
        groups = store.best_measure(w["cost"], serving=w["quest"], sense="min")
        by_cur = {(g["currency"], g["price_year"]): g for g in groups}
        assert set(by_cur) == {("EUR", None), ("USD", None)}
        # 50 USD is not the "cheapest" across both — each currency ranks alone
        assert by_cur[("EUR", None)]["best"]["id"] == eur.output_id
        assert by_cur[("USD", None)]["best"]["id"] == usd.output_id
        assert by_cur[("EUR", None)]["n"] == 1 and by_cur[("USD", None)]["n"] == 1

    def test_best_measure_partitions_by_price_year(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        old = _put(store, w, "cost", "1000", unit="USD2010/t")
        new = _put(store, w, "cost", "50", unit="USD2020/t")
        groups = store.best_measure(w["cost"], serving=w["quest"], sense="min")
        by_year = {g["price_year"]: g for g in groups}
        # one currency, two price years: two groups, never ranked together
        assert set(by_year) == {2010, 2020}
        assert by_year[2010]["best"]["id"] == old.output_id
        assert by_year[2020]["best"]["id"] == new.output_id

    def test_the_outlier_median_is_per_currency(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        # three USD rows make the median sample; an EUR row wildly off it is
        # not flagged — the median never mixes currencies
        for v in ("500", "520", "480"):
            _put(store, w, "cost", v, unit="USD/t")
        eur = _put(store, w, "cost", "9e9", unit="EUR/t")
        assert not (_row(store, eur.output_id)["meta"] or {}).get("escalation")
        usd = _put(store, w, "cost", "9e9", unit="USD/t")
        rules = [
            e["rule"]
            for e in (_row(store, usd.output_id)["meta"] or {}).get("escalation", [])
        ]
        assert "implausible_value" in rules

    def test_the_outlier_median_is_per_price_year(
        self, store: Store, w: dict[str, Any]
    ) -> None:
        # three USD2020 rows make the median sample; a USD2010 row wildly
        # off it is its own group and is not flagged
        for v in ("500", "520", "480"):
            _put(store, w, "cost", v, unit="USD2020/t")
        old = _put(store, w, "cost", "9e9", unit="USD2010/t")
        assert not (_row(store, old.output_id)["meta"] or {}).get("escalation")
        new = _put(store, w, "cost", "9e9", unit="USD2020/t")
        rules = [
            e["rule"]
            for e in (_row(store, new.output_id)["meta"] or {}).get("escalation", [])
        ]
        assert "implausible_value" in rules


# ── rendering ─────────────────────────────────────────────────────────────


class TestRender:
    def test_the_label_carries_the_currency_and_year(self) -> None:
        row = {
            "value_num": 1000.0,
            "value_form": "point",
            "currency": "EUR",
            "price_year": 2015,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row) == "1000 EUR(2015)/t"

    def test_the_remainder_converts_within_the_currency(self) -> None:
        row = {
            "value_num": 1000.0,
            "value_form": "point",
            "currency": "EUR",
            "price_year": 2015,
            "canonical_unit": "USD/t",
            "display_unit": "EUR/kg",
        }
        assert render.value_text(row) == "1 EUR(2015)/kg"
        assert render.value_text(row, unit="EUR/kg") == "1 EUR(2015)/kg"

    def test_a_row_with_no_year_shows_the_code_alone(self) -> None:
        row = {
            "value_num": 500.0,
            "value_form": "point",
            "currency": "USD",
            "price_year": None,
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row) == "500 USD/t"

    def test_a_legacy_null_row_reads_its_unit_token(self) -> None:
        row = {
            "value_num": 42.0,
            "value_form": "point",
            "reported_unit": "USD/t",
            "canonical_unit": "USD/t",
        }
        assert render.value_text(row) == "42 USD/t"

    def test_the_stored_unit_is_the_currency_and_canonical_remainder(self) -> None:
        row = {
            "currency": "EUR",
            "price_year": 2015,
            "canonical_unit": "USD/t",
        }
        assert render.stored_unit(row) == "EUR(2015)/t"
        assert render.stored_unit({"canonical_unit": "m"}) == "m"
        assert render.stored_unit({"canonical_unit": "1"}) == ""


# ── migration 0249: legacy rows, sha, frozen ──────────────────────────────


def _chain_dir_without_0249(tmp: Path) -> None:
    for f in MIGRATIONS_DIR.glob("*.sql"):
        if f.name >= "0249":
            continue
        shutil.copy(f, tmp / f.name)


def _legacy_currency_world(dsn: str) -> dict[str, int]:
    """Replay the chain to 0247 and write one USD/t cost row as it was always
    written (no currency columns)."""
    tmp = Path(tempfile.mkdtemp(prefix="precis_test_0249_"))
    try:
        _chain_dir_without_0249(tmp)
        Migrator(dsn, tmp).apply_all()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    def _one(conn: psycopg.Connection[Any], sql: str, params: tuple = ()) -> Any:
        row = conn.execute(sql, params).fetchone()
        assert row is not None
        return row[0]

    out: dict[str, int] = {}
    with psycopg.connect(dsn, autocommit=True) as conn:
        out["taxon"] = _one(
            conn,
            "INSERT INTO refs (kind, title, meta) VALUES ('taxon', 'unit cost', "
            '\'{"name": "unit cost", "slug": "unit-cost", '
            '"canonical_unit": "USD/t", "dimension_kind": "currency"}'
            "'::jsonb) RETURNING ref_id",
        )
        out["subject"] = _one(
            conn,
            "INSERT INTO refs (kind, title) VALUES ('paper', 'a paper') "
            "RETURNING ref_id",
        )
        out["measure"] = _one(
            conn,
            "INSERT INTO measures (subject_ref_id, measurand_ref_id, literal, "
            " reported_unit, value_num, value_form, run_key, actor) "
            "VALUES (%s, %s, '1000', 'USD/t', 1000, 'point', "
            " 'legacy:test', 'legacy') RETURNING id",
            (out["subject"], out["taxon"]),
        )
    return out


def test_0249_keeps_the_sha_and_reads_the_legacy_row(fresh_db: str) -> None:
    ids = _legacy_currency_world(fresh_db)
    with psycopg.connect(fresh_db) as conn:
        row = conn.execute(
            "SELECT precis_measure_sha(%s)", (ids["measure"],)
        ).fetchone()
        assert row is not None
        (sha_before,) = row
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    s = Store.connect(fresh_db)
    try:
        r = s.measure_detail(ids["measure"])
        # the legacy NULL row reads its token: USD from reported_unit, no year
        assert r["currency"] is None and r["price_year"] is None
        assert row_currency(r) == ("USD", None)
        assert row_interval(r) == (1000.0, 1000.0)
        assert render.value_text(r) == "1000 USD/t"
        # the same row still ranges under its canonical token's currency
        found = s.search_measures(
            ids["taxon"], min_si=900, max_si=1100, bound_currency=("USD", None)
        )
        assert [x["id"] for x in found.rows] == [ids["measure"]]
    finally:
        s.close()
    with psycopg.connect(fresh_db) as conn:
        row = conn.execute(
            "SELECT precis_measure_sha(%s)", (ids["measure"],)
        ).fetchone()
        assert row is not None
        (sha_after,) = row
        assert sha_after == sha_before


def test_0249_the_frozen_trigger_covers_the_new_columns(fresh_db: str) -> None:
    ids = _legacy_currency_world(fresh_db)
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
            conn.execute(
                "UPDATE measures SET price_year = 2015 WHERE id = %s",
                (ids["measure"],),
            )
        with pytest.raises(psycopg.errors.CheckViolation, match="append-only"):
            conn.execute(
                "UPDATE measures SET currency = 'EUR' WHERE id = %s",
                (ids["measure"],),
            )


# ── convert-on-read (slice 3, ruling 2026-10-28) ──────────────────────────

_NOW_YEAR = datetime.now(UTC).year


def _seed_money(store: Store, *, through_current: bool = True) -> None:
    """50 daily USD quotes for 2019 (the thin-year floor) and CPI points:
    2019 always, the current year unless ``through_current`` is False (the
    World Bank series lags — the falling-back case). 1300 USD at 2019
    prices prices at 1.12 USD/EUR and deflates by 121/110 to 1277 EUR at
    the current year."""
    rows = [(CPI_SERIES, 2019, "110")]
    if through_current:
        rows.append((CPI_SERIES, _NOW_YEAR, "121"))
    with store.pool.connection() as conn:
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO fx_rates (currency, rate_date, per_eur, source) "
                "VALUES ('USD', %s, 1.12, 'test')",
                [(date(2019, 1, 2) + timedelta(days=2 * i),) for i in range(50)],
            )
            cur.executemany(
                "INSERT INTO price_index (series, year, value, source) "
                "VALUES (%s, %s, %s, 'test')",
                rows,
            )


class TestConvertOnRead:
    def test_money_tables_loads_the_caches(self, store: Store) -> None:
        _seed_money(store)
        tables = store.money_tables()
        per_eur, n_days = tables.fx[("USD", 2019)]
        assert per_eur == pytest.approx(1.12) and n_days == 50
        assert tables.cpi[2019] == pytest.approx(110.0)
        assert tables.cpi[_NOW_YEAR] == pytest.approx(121.0)

    def test_search_converts_usd_to_eur_at_the_target_year(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        _seed_money(store)
        run = _put(store, w, "cost", "1300", unit="USD2019/t")
        body = h.search(property="measurand/unit-cost").body
        assert f"mx{run.output_id}" in body
        assert (
            f"1277 EUR({_NOW_YEAR})/t "
            "(1300 USD/t, 2019, FX: ECB annual mean 2019)" in body
        )

    def test_a_missing_current_year_falls_back_to_the_latest_cpi_year(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        # the World Bank series lags: no CPI for the current year, so the
        # target is 2019 and the note says so — priced by the FX leg only
        _seed_money(store, through_current=False)
        run = _put(store, w, "cost", "1300", unit="USD2019/t")
        body = h.search(property="measurand/unit-cost").body
        assert (
            "1161 EUR(2019)/t (1300 USD/t, 2019, FX: ECB annual mean 2019, "
            "latest CPI year)" in body
        )

    def test_get_notes_a_missing_fx_rate(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        _seed_money(store)
        run = _put(store, w, "cost", "100", unit="GBP2020/t")
        body = h.get(id=f"mx{run.output_id}").body
        assert "100 GBP(2020)/t (not converted: no ECB rate for GBP in 2020)" in body

    def test_a_eur_row_of_the_target_year_reads_unchanged(
        self, store: Store, w: dict[str, Any], h: MeasureHandler
    ) -> None:
        _seed_money(store)
        run = _put(store, w, "cost", "100", unit=f"EUR{_NOW_YEAR}/t")
        body = h.get(id=f"mx{run.output_id}").body
        assert f"100 EUR({_NOW_YEAR})/t (100 EUR/t, {_NOW_YEAR})" in body


# ── a mid-chain database (no 0253) must not break reads ───────────────────


def _chain_dir_without_0253(tmp: Path) -> None:
    for f in MIGRATIONS_DIR.glob("*.sql"):
        if f.name >= "0253":
            continue
        shutil.copy(f, tmp / f.name)


def test_money_tables_before_0253_falls_back_to_empty_and_is_not_cached(
    fresh_db: str,
) -> None:
    """At a mid-chain schema (0249 applied, 0253 not — the state
    test_measures_si's 0187/0188 readers run at is older still) the
    convert-on-read path must not fail: money_tables() answers an empty
    MoneyTables, a currency row shows as reported with the not-converted
    note, and the fallback is NOT cached — once 0253 lands, the next read
    sees the real tables."""

    def _one(conn: psycopg.Connection[Any], sql: str, params: tuple = ()) -> Any:
        row = conn.execute(sql, params).fetchone()
        assert row is not None
        return row[0]

    # This test uses fresh_db directly, so the conftest store fixture's
    # reset has not run — drop any tables a previous test's store cached.
    reset_money_tables_cache()
    tmp = Path(tempfile.mkdtemp(prefix="precis_test_0253_"))
    try:
        _chain_dir_without_0253(tmp)
        Migrator(fresh_db, tmp).apply_all()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    # Seed through the store, as the other tests do. The measurand sits under
    # the ``measurand`` start node so ``property=`` can address it; it is not
    # named "unit cost" because the chain already seeds a taxon at
    # ``measurand/unit-cost`` (a second node there makes the path ambiguous).
    s = Store.connect(fresh_db)
    try:
        start = _taxon(s, "measurand")
        cost = _taxon(
            s,
            "ore cost",
            unit="USD/t",
            display="USD/t",
            dimension_kind="currency",
            parent=start,
        )
        paper = seed_ref(s, title="p", kind="paper")
        measure = _put(
            s, {"cost": cost, "paper": paper}, "cost", "1300", unit="USD2019/t"
        ).output_id
    finally:
        s.close()

    s = Store.connect(fresh_db)
    try:
        assert s.money_tables().fx == {} and s.money_tables().cpi == {}
        body = (
            MeasureHandler(hub=Hub(store=s)).search(property="measurand/ore-cost").body
        )
        assert f"mx{measure}" in body
        assert "1300 USD(2019)/t (not converted: no ECB rate for USD in 2019)" in body
    finally:
        s.close()

    # The fallback must not stick: with 0253 applied and a CPI row seeded,
    # the next read serves the real tables.
    Migrator(fresh_db, MIGRATIONS_DIR).apply_all()
    with psycopg.connect(fresh_db, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO price_index (series, year, value, source) "
            "VALUES (%s, 2019, '110', 'test')",
            (CPI_SERIES,),
        )
    s = Store.connect(fresh_db)
    try:
        assert s.money_tables().cpi == {2019: 110.0}
    finally:
        s.close()
