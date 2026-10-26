"""money_refresh — slice 2 of ``docs/backlog/measure-currency-units.md``
(gr510555): the ECB/World Bank parsers, the guarded upserts, the size
caps and the ``money_refresh`` job-type dispatch. Fixtures only — no
network (the fetch seam is always injected); DB tests ride the
``store`` fixture (real PG, the 0255 tables already migrated).
"""

from __future__ import annotations

import contextlib
import io
import json
import zipfile
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from precis.store.store import Store
from precis.workers import money_refresh
from precis.workers.job_types import get_job_type, known_job_types
from precis.workers.job_types import money_refresh as money_jt
from precis.workers.money_refresh import MoneyRefreshError

FIXTURES = Path(__file__).parent / "fixtures" / "money"
ECB_ZIP = (FIXTURES / "eurofxref-hist-sample.zip").read_bytes()
WB_JSON = json.loads((FIXTURES / "worldbank-cpi-usa.json").read_text(encoding="utf-8"))


def _fetch(url: str) -> bytes:
    """The fixture-backed fetch used everywhere below."""
    if url == money_refresh.ECB_HIST_URL:
        return ECB_ZIP
    if url == money_refresh.WB_CPI_URL:
        return json.dumps(WB_JSON).encode()
    raise AssertionError(f"unexpected url {url}")


def _zip_csv(text: str, member: str = "eurofxref-hist.csv") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, text)
    return buf.getvalue()


class _FakeCtx:
    """Minimal DispatchContext stand-in capturing dispatcher side effects."""

    def __init__(self, store: Any = None, params: dict[str, Any] | None = None) -> None:
        self.store = store if store is not None else object()
        self.meta = {"params": params or {}}
        self.chunks: list[tuple[str, str]] = []
        self.meta_set: dict[str, Any] = {}
        self.failure: str | None = None

    def append_chunk(self, kind: str, text: str) -> None:
        self.chunks.append((kind, text))

    def set_meta(self, **kw: Any) -> None:
        self.meta_set.update(kw)

    def record_failure(self, msg: str) -> None:
        self.failure = msg


def _count(store: Store, table: str) -> int:
    with store.tx() as conn:
        row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
    assert row is not None
    return int(row[0])


# ── parsers (no DB) ──────────────────────────────────────────────────


def test_parse_ecb_hist_fixture() -> None:
    """The sample covers 2012-06-29, the weekend gap, ISK's suspension
    and an empty trailing cell — every gap shape the real file has."""
    rows = money_refresh.parse_ecb_hist(ECB_ZIP)
    by_key = {(c, d): r for c, d, r in rows}
    # 4 dates x 6 currencies, minus ISK's four N/A cells and one empty
    # NOK cell = 19 quotes.
    assert len(rows) == 19
    assert by_key[("USD", date(2012, 6, 29))] == Decimal("1.259")
    assert by_key[("USD", date(2012, 7, 2))] == Decimal("1.2593")
    assert by_key[("NOK", date(2012, 6, 28))] == Decimal("7.5465")
    # The weekend gap: no rows at all for 2012-06-30 / 2012-07-01.
    assert {d for _, d, _ in rows} == {
        date(2012, 6, 27),
        date(2012, 6, 28),
        date(2012, 6, 29),
        date(2012, 7, 2),
    }
    # ISK is N/A through the whole sample — its suspension gap.
    assert "ISK" not in {c for c, _, _ in rows}


def test_parse_ecb_hist_faults() -> None:
    with pytest.raises(MoneyRefreshError, match="not a zip"):
        money_refresh.parse_ecb_hist(b"not a zip")
    with pytest.raises(MoneyRefreshError, match="one csv member"):
        money_refresh.parse_ecb_hist(_zip_csv("Date,USD\n", member="other.txt"))
    with pytest.raises(MoneyRefreshError, match="header"):
        money_refresh.parse_ecb_hist(_zip_csv("Day,USD\n2020-01-02,1\n"))
    with pytest.raises(MoneyRefreshError, match="bad date"):
        money_refresh.parse_ecb_hist(_zip_csv("Date,USD,\nJune 1,1.2\n"))
    with pytest.raises(MoneyRefreshError, match="bad rate"):
        money_refresh.parse_ecb_hist(_zip_csv("Date,USD,\n2020-01-02,abc\n"))
    with pytest.raises(MoneyRefreshError, match="non-positive"):
        money_refresh.parse_ecb_hist(_zip_csv("Date,USD,\n2020-01-02,0\n"))
    with pytest.raises(MoneyRefreshError, match="bad currency"):
        money_refresh.parse_ecb_hist(_zip_csv("Date,USDD,\n2020-01-02,1\n"))
    with pytest.raises(MoneyRefreshError, match="no rates"):
        money_refresh.parse_ecb_hist(_zip_csv("Date,USD,\n2020-01-02,N/A\n"))


def test_parse_ecb_hist_unzipped_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(money_refresh, "MAX_UNZIPPED_BYTES", 100)
    with pytest.raises(MoneyRefreshError, match="cap"):
        money_refresh.parse_ecb_hist(ECB_ZIP)


def test_parse_worldbank_cpi_fixture() -> None:
    rows = money_refresh.parse_worldbank_cpi(WB_JSON)
    # 2025's null is skipped; five real observations remain.
    assert len(rows) == 5
    assert {s for s, _, _ in rows} == {money_refresh.CPI_SERIES}
    by_year = dict((y, v) for _, y, v in rows)
    assert by_year[2024] == Decimal("143.857336014607")
    assert by_year[1960] == Decimal("13.5630607803976")
    assert all(isinstance(y, int) for _, y, _ in rows)


def test_parse_worldbank_cpi_faults() -> None:
    with pytest.raises(MoneyRefreshError, match=r"\[meta, rows\]"):
        money_refresh.parse_worldbank_cpi({"rows": []})
    with pytest.raises(MoneyRefreshError, match="no observations"):
        money_refresh.parse_worldbank_cpi([{}, [{"date": "2025", "value": None}]])
    with pytest.raises(MoneyRefreshError, match="bad row"):
        money_refresh.parse_worldbank_cpi([{}, ["not-a-dict"]])
    with pytest.raises(MoneyRefreshError, match="bad row"):
        money_refresh.parse_worldbank_cpi([{}, [{"date": "1800", "value": 1}]])


def test_parse_worldbank_cpi_meta_faults() -> None:
    """Meta that disagrees with the body is a silent short read, so it
    faults — ``pages > 1``, a row count under ``total``, or malformed
    meta fields."""
    one_row = [{"date": "2024", "value": 140.0}]
    with pytest.raises(MoneyRefreshError, match="pages"):
        money_refresh.parse_worldbank_cpi([{"pages": 2, "total": 1}, one_row])
    with pytest.raises(MoneyRefreshError, match="short read"):
        money_refresh.parse_worldbank_cpi([{"pages": 1, "total": 7}, one_row])
    with pytest.raises(MoneyRefreshError, match="bad meta"):
        money_refresh.parse_worldbank_cpi([{"pages": "x"}, one_row])


def test_default_fetch_aborts_oversize_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``MAX_RESPONSE_BYTES`` is a wire cap: ``_default_fetch`` streams
    and aborts past it — the oversized body is never fully read."""
    chunks_yielded: list[int] = []

    class _BigResp:
        status_code = 200
        headers: dict[str, str] = {}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int = 0) -> Any:
            for i in range(10):
                chunks_yielded.append(i)
                yield b"x" * 1024

    @contextlib.contextmanager
    def _stream(client: Any, method: str, url: str, **kw: Any) -> Any:
        yield _BigResp()

    @contextlib.contextmanager
    def _client(*a: Any, **k: Any) -> Any:
        yield object()

    monkeypatch.setenv("PRECIS_RATE_LIMIT", "0")  # no limiter store here
    monkeypatch.setattr(money_refresh, "http_client", _client)
    monkeypatch.setattr(money_refresh, "safe_stream", _stream)
    monkeypatch.setattr(money_refresh, "MAX_RESPONSE_BYTES", 4 * 1024)
    with pytest.raises(MoneyRefreshError, match="cap"):
        money_refresh._default_fetch("https://ecb.test/feed.zip")
    # Aborted mid-stream: five 1 KiB chunks read, five never pulled.
    assert chunks_yielded == [0, 1, 2, 3, 4]


def test_default_fetch_declared_size_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ``Content-Length`` over the cap aborts before a single byte."""

    class _DeclaredBig:
        status_code = 200
        headers = {"Content-Length": str(50 * 1024 * 1024)}

        def raise_for_status(self) -> None:
            return None

        def iter_bytes(self, chunk_size: int = 0) -> Any:
            raise AssertionError("body must never be read")
            yield  # pragma: no cover

    @contextlib.contextmanager
    def _stream(client: Any, method: str, url: str, **kw: Any) -> Any:
        yield _DeclaredBig()

    @contextlib.contextmanager
    def _client(*a: Any, **k: Any) -> Any:
        yield object()

    monkeypatch.setenv("PRECIS_RATE_LIMIT", "0")
    monkeypatch.setattr(money_refresh, "http_client", _client)
    monkeypatch.setattr(money_refresh, "safe_stream", _stream)
    with pytest.raises(MoneyRefreshError, match="cap"):
        money_refresh._default_fetch("https://ecb.test/feed.zip")


# ── the pass against real PG ─────────────────────────────────────────


def test_refresh_writes_and_is_idempotent(store: Store) -> None:
    counts = money_refresh.run_money_refresh(store, fetch=_fetch)
    assert counts == {"ecb": 19, "worldbank": 5}
    assert _count(store, "fx_rates") == 19
    assert _count(store, "price_index") == 5
    with store.tx() as conn:
        row = conn.execute(
            "SELECT per_eur_avg, n_days, first_date, last_date "
            "FROM fx_rates_annual WHERE currency = 'USD'"
        ).fetchone()
    assert row is not None
    avg, n_days, first, last = row
    assert n_days == 4
    assert first == date(2012, 6, 27) and last == date(2012, 7, 2)
    assert float(avg) == pytest.approx((1.2593 + 1.259 + 1.2418 + 1.2478) / 4)
    # Second identical run: same counts, still no duplicate rows.
    assert money_refresh.run_money_refresh(store, fetch=_fetch) == counts
    assert _count(store, "fx_rates") == 19
    assert _count(store, "price_index") == 5


def test_revision_upsert_updates_value(store: Store) -> None:
    """A restated value lands as an upsert, not a second row (the CPI
    revision path); fetched_at bumps."""
    money_refresh.run_money_refresh(store, sources=["ecb"], fetch=_fetch)
    # A same-size parse with one restated quote: upsert, not a new row.
    revised = _zip_csv(
        "Date,USD,JPY,GBP,CHF,ISK,NOK,\n"
        "2012-07-02,9.99,100.51,0.8041,1.2015,N/A,7.5255,\n"
        "2012-06-29,1.259,100.13,0.8068,1.203,N/A,7.533,\n"
        "2012-06-28,1.2418,98.6,0.79885,1.201,N/A,7.5465,\n"
        "2012-06-27,1.2478,99.49,0.7999,1.2011,N/A,\n"
    )
    counts = money_refresh.run_money_refresh(
        store, sources=["ecb"], fetch=lambda url: revised
    )
    assert counts == {"ecb": 19}
    with store.tx() as conn:
        row = conn.execute(
            "SELECT per_eur FROM fx_rates "
            "WHERE currency = 'USD' AND rate_date = '2012-07-02'"
        ).fetchone()
    assert row is not None
    assert Decimal(str(row[0])) == Decimal("9.99")
    assert _count(store, "fx_rates") == 19


def _stamps(store: Store) -> tuple[Any, Any, Any]:
    """``fetched_at`` per cache row + the refresh log, for change diffs."""
    with store.tx() as conn:
        fx = conn.execute(
            "SELECT currency, rate_date, fetched_at FROM fx_rates ORDER BY 1, 2"
        ).fetchall()
        cpi = conn.execute(
            "SELECT series, year, fetched_at FROM price_index ORDER BY 1, 2"
        ).fetchall()
        log = conn.execute(
            "SELECT source, refreshed_at, rows_written "
            "FROM money_refresh_log ORDER BY 1"
        ).fetchall()
    return fx, cpi, log


def test_second_identical_run_rewrites_no_rows(store: Store) -> None:
    """``IS DISTINCT FROM``: an unchanged row is not rewritten, so
    ``fetched_at`` marks the last CHANGE — while ``money_refresh_log``
    still bumps to mark the last successful poll."""
    assert money_refresh.run_money_refresh(store, fetch=_fetch) == {
        "ecb": 19,
        "worldbank": 5,
    }
    fx1, cpi1, log1 = _stamps(store)
    money_refresh.run_money_refresh(store, fetch=_fetch)
    fx2, cpi2, log2 = _stamps(store)
    # Zero rewritten rows: every fetched_at survived the second poll.
    assert fx1 == fx2
    assert cpi1 == cpi2
    # Freshness still moved: one log row per source, later refreshed_at.
    assert [s for s, _, _ in log1] == ["ecb", "worldbank"]
    assert {s: w for s, _, w in log2} == {"ecb": 19, "worldbank": 5}
    for (s1, t1, _), (s2, t2, _) in zip(log1, log2):
        assert s1 == s2 and t2 > t1


def test_truncated_download_refused(store: Store) -> None:
    money_refresh.run_money_refresh(store, fetch=_fetch)
    stub = _zip_csv("Date,USD,\n2012-07-02,1.2593,\n")

    def _short(url: str) -> bytes:
        return stub if url == money_refresh.ECB_HIST_URL else _fetch(url)

    with pytest.raises(MoneyRefreshError, match="truncated"):
        money_refresh.run_money_refresh(store, fetch=_short)
    # The refused source left its rows; worldbank still committed.
    assert _count(store, "fx_rates") == 19
    assert _count(store, "price_index") == 5


def test_fetch_fault_leaves_tables_untouched(store: Store) -> None:
    def _down(url: str) -> bytes:
        raise ConnectionError("host unreachable")

    with pytest.raises(MoneyRefreshError, match="unreachable"):
        money_refresh.run_money_refresh(store, fetch=_down)
    assert _count(store, "fx_rates") == 0
    assert _count(store, "price_index") == 0


def test_response_size_cap(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(money_refresh, "MAX_RESPONSE_BYTES", 10)
    with pytest.raises(MoneyRefreshError, match="cap"):
        money_refresh.run_money_refresh(store, fetch=_fetch)
    assert _count(store, "fx_rates") == 0
    assert _count(store, "price_index") == 0


def test_sources_subset_and_unknown(store: Store) -> None:
    counts = money_refresh.run_money_refresh(store, sources=["worldbank"], fetch=_fetch)
    assert counts == {"worldbank": 5}
    assert _count(store, "fx_rates") == 0
    with pytest.raises(MoneyRefreshError, match="unknown source"):
        money_refresh.run_money_refresh(store, sources=["mars"], fetch=_fetch)


# ── job_type registration + dispatch ─────────────────────────────────


def test_job_type_registered() -> None:
    spec = get_job_type("money_refresh")
    assert spec is not None
    assert spec.name == "money_refresh"
    assert spec.compatible_executors == frozenset({"claude_inproc"})
    assert spec.requires == frozenset()
    assert callable(spec.dispatch)
    assert "money_refresh" in known_job_types()


def test_dispatch_with_injected_fetch(store: Store) -> None:
    ctx = _FakeCtx(store=store)
    money_jt._dispatch(ctx, money_jt.SPEC, fetch=_fetch)
    assert ctx.failure is None
    assert ctx.meta_set == {"ecb_rows": 19, "worldbank_rows": 5}
    assert any("ecb 19 rows" in t for _, t in ctx.chunks)
    assert _count(store, "fx_rates") == 19


def test_dispatch_sources_param(store: Store) -> None:
    ctx = _FakeCtx(store=store, params={"sources": ["worldbank"]})
    money_jt._dispatch(ctx, money_jt.SPEC, fetch=_fetch)
    assert ctx.failure is None
    assert ctx.meta_set == {"worldbank_rows": 5}
    assert _count(store, "fx_rates") == 0


def test_dispatch_records_failure(store: Store) -> None:
    def _down(url: str) -> bytes:
        raise ConnectionError("ecb down")

    ctx = _FakeCtx(store=store, params={"sources": ["ecb"]})
    money_jt._dispatch(ctx, money_jt.SPEC, fetch=_down)
    assert ctx.failure is not None and "ecb down" in ctx.failure
    assert _count(store, "fx_rates") == 0
