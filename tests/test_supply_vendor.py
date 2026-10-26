"""``vendor:`` pull foundations — ``PartRecord``, ``may_store``, the alias
map, and the JLCPCB adapter over ``jlc_api.live_lookup``.

The supply half of spreadsheet-tables slice 4 (docs/backlog/
spreadsheet-tables.md §6). No network and no database: ``live_lookup``'s
``client`` seam takes a fake ``component_info``, and a stub store records
``parts_import`` calls — the real contract, exercised end to end. The
credentials fixture pins the secrets resolver to an empty dir, same
pattern as ``tests/test_supply.py``.
"""

from __future__ import annotations

from typing import Any, cast

import pytest

from precis import secrets as vault
from precis.pcb import jlc_api
from precis.supply import base
from precis.supply.base import (
    Adapter,
    LookupUnsupported,
    PartRecord,
    SupplierError,
)
from precis.supply.digikey import DigiKeyAdapter
from precis.supply.farnell import FarnellAdapter
from precis.supply.jlcpcb import JlcPcbAdapter
from precis.supply.mouser import MouserAdapter
from precis.utils.http import VendorError


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Pin the file layer of ``get_secret`` to an empty dir, unbind any
    store, and clear the JLCPCB miss cache — hermetic like test_supply."""
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    for name in (
        jlc_api.APP_ID_SECRET,
        jlc_api.ACCESS_KEY_SECRET,
        jlc_api.SECRET_KEY_SECRET,
    ):
        monkeypatch.delenv(name, raising=False)
    vault.bind_store(None)
    vault.invalidate()
    jlc_api.reset_live_cache()


@pytest.fixture
def creds(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(jlc_api.APP_ID_SECRET, "test-app-id")
    monkeypatch.setenv(jlc_api.ACCESS_KEY_SECRET, "test-access-key")
    monkeypatch.setenv(jlc_api.SECRET_KEY_SECRET, "test-secret-key")


class _Store:
    """``live_lookup``'s whole store surface: it upserts the hit row."""

    def __init__(self) -> None:
        self.imported: list[dict[str, Any]] = []

    def parts_import(self, rows: list[dict[str, Any]]) -> None:
        self.imported.extend(rows)


class _Client:
    """``JlcApiClient`` shape at the ``component_info`` seam — no network."""

    available = True

    def __init__(self, rows: dict[str, dict[str, Any] | None]) -> None:
        self._rows = rows
        self.calls: list[str] = []

    def component_info(self, lcsc: str) -> dict[str, Any] | None:
        self.calls.append(lcsc)
        return self._rows.get(lcsc)


def _jlcpcb(store: _Store | None, client: Any = None) -> JlcPcbAdapter:
    return JlcPcbAdapter(store=cast(Any, store), client=cast(Any, client))


def _row(**kw: Any) -> dict[str, Any]:
    """A ``normalize_api_row`` dict as ``live_lookup`` returns it."""
    row: dict[str, Any] = {
        "lcsc": "C25804",
        "mfr": "Samsung",
        "mfr_part": "CL05B104KO5NNNC",
        "description": "100nF 16V X7R 0402",
        "jlcpcb_assemblable": True,
        "basic": True,
        "stock": 500000,
        "price": [
            {"qFrom": 1, "qTo": 100, "price": 0.0023},
            {"qFrom": 101, "qTo": None, "price": 0.0019},
        ],
        "package": "0402",
        "height_mm": None,
        "params": None,
        "datasheet_url": "https://example.invalid/ds.pdf",
    }
    row.update(kw)
    return row


class TestPartRecord:
    def test_unit_price_at_picks_the_break_for_the_quantity(self) -> None:
        rec = PartRecord(
            supplier="x",
            sku="S1",
            price_breaks=((1, 0.05), (100, 0.04), (1000, 0.03)),
        )
        assert rec.unit_price_at(1) == pytest.approx(0.05)
        assert rec.unit_price_at(99) == pytest.approx(0.05)
        assert rec.unit_price_at(100) == pytest.approx(0.04)
        assert rec.unit_price_at(50_000) == pytest.approx(0.03)

    def test_below_the_first_break_takes_its_price(self) -> None:
        """The lowest rung is the only price on offer — not an error."""
        rec = PartRecord(supplier="x", sku="S1", price_breaks=((10, 0.05),))
        assert rec.unit_price_at(1) == pytest.approx(0.05)

    def test_no_prices_is_none_not_zero(self) -> None:
        """Zero would read as "free"; there is simply no ladder."""
        assert PartRecord(supplier="x", sku="S1").unit_price_at(5) is None


class TestStorePolicy:
    @pytest.mark.parametrize("cls", [DigiKeyAdapter, FarnellAdapter, MouserAdapter])
    def test_live_read_vendors_may_not_store(self, cls: Any) -> None:
        adapter = cls()
        assert adapter.may_store is False

    @pytest.mark.parametrize("cls", [DigiKeyAdapter, FarnellAdapter, MouserAdapter])
    def test_live_read_vendors_have_no_exact_lookup(self, cls: Any) -> None:
        """``LookupUnsupported`` — not ``None``, which a caller could
        cache as "the vendor doesn't stock it"."""
        with pytest.raises(LookupUnsupported, match="keyword search"):
            cls().lookup("S1")

    def test_jlcpcb_is_the_store_vendor(self) -> None:
        assert _jlcpcb(_Store()).may_store is True

    def test_every_adapter_satisfies_the_protocol(self) -> None:
        for adapter in base.adapters(store=cast(Any, _Store())):
            assert isinstance(adapter, Adapter)


class TestRegistry:
    def test_no_store_keeps_lookup_only_vendors_out(self) -> None:
        """``quote``/``unavailable_reason`` call ``adapters()`` bare: a
        store-less JLCPCB could only report "no store" on every fastener
        search — it stays out of keyword aggregation."""
        assert {a.name for a in base.adapters()} == {
            "digikey",
            "farnell",
            "mouser",
        }

    def test_a_store_admits_jlcpcb(self) -> None:
        names = {a.name for a in base.adapters(store=cast(Any, _Store()))}
        assert names == {"digikey", "farnell", "mouser", "jlcpcb"}

    @pytest.mark.parametrize(
        "typed",
        ["jlcpcb", "JLCPCB", "jlc", "lcsc", "LCSC", "LCSC.com", "lcsc.com"],
    )
    def test_alias_map_resolves_jlcpcb_spellings(self, typed: str) -> None:
        adapter = base.vendor(typed, store=cast(Any, _Store()))
        assert isinstance(adapter, JlcPcbAdapter)

    @pytest.mark.parametrize(
        ("typed", "name"),
        [("digi-key", "digikey"), ("element14", "farnell"), ("mouser", "mouser")],
    )
    def test_alias_map_resolves_the_live_read_vendors(
        self, typed: str, name: str
    ) -> None:
        adapter = base.vendor(typed)
        assert adapter is not None and adapter.name == name

    def test_an_unknown_vendor_resolves_none(self) -> None:
        assert base.vendor("acme") is None

    def test_a_store_less_jlcpcb_still_resolves_so_configured_can_say_why(
        self,
    ) -> None:
        adapter = base.vendor("lcsc")
        assert isinstance(adapter, JlcPcbAdapter)
        why = adapter.configured()
        assert why is not None and "store" in why.casefold()


class TestJlcConfigured:
    def test_no_store_names_the_store(self, creds: None) -> None:
        why = JlcPcbAdapter().configured()
        assert why is not None and "store" in why.casefold()

    def test_store_without_credentials_names_the_secrets(self) -> None:
        why = _jlcpcb(_Store()).configured()
        assert why is not None and "JLCPCB_APP_ID" in why

    def test_store_and_credentials_is_ready(self, creds: None) -> None:
        assert _jlcpcb(_Store()).configured() is None

    def test_an_injected_available_client_is_configured_without_a_store(
        self,
    ) -> None:
        adapter = JlcPcbAdapter(client=cast(Any, _Client({})))
        assert adapter.configured() is None

    def test_an_injected_unavailable_client_reports_itself(self) -> None:
        class _Down:
            available = False

        adapter = JlcPcbAdapter(store=cast(Any, _Store()), client=cast(Any, _Down()))
        why = adapter.configured()
        assert why is not None and "client" in why


class TestJlcLookup:
    def test_a_normalized_row_becomes_a_part_record(self, creds: None) -> None:
        store = _Store()
        adapter = _jlcpcb(store, _Client({"C25804": _row()}))
        rec = adapter.lookup("C25804")
        assert rec is not None
        assert rec.supplier == "jlcpcb"
        assert rec.sku == "C25804"
        assert rec.manufacturer == "Samsung"
        assert rec.mpn == "CL05B104KO5NNNC"
        assert rec.description == "100nF 16V X7R 0402"
        assert rec.package == "0402"
        assert rec.datasheet_url == "https://example.invalid/ds.pdf"
        assert rec.lifecycle is None
        assert rec.stock == 500000
        assert rec.price_breaks == ((1, 0.0023), (101, 0.0019))
        assert rec.unit_price_at(50) == pytest.approx(0.0023)
        assert rec.unit_price_at(200) == pytest.approx(0.0019)
        assert rec.currency == "USD"
        assert rec.extra["basic"] is True
        assert rec.retrieved.tzinfo is not None
        # live_lookup's upsert ran — the row landed in `parts`.
        assert [r["lcsc"] for r in store.imported] == ["C25804"]

    def test_the_jlcparts_string_price_form_parses(self, creds: None) -> None:
        adapter = _jlcpcb(
            _Store(), _Client({"C1": _row(price="1-49:0.0204,5000-:0.0108")})
        )
        rec = adapter.lookup("C1")
        assert rec is not None
        assert rec.price_breaks == ((1, 0.0204), (5000, 0.0108))

    def test_the_native_jlcpcb_break_shape_parses(self, creds: None) -> None:
        """The detail endpoint's own spelling —
        ``{startNumber, endNumber, productPrice}``."""
        row = _row(
            price=[
                {"startNumber": 1, "endNumber": 29, "productPrice": "7.95"},
                {"startNumber": 30, "endNumber": 103, "productPrice": "6.15"},
                {"startNumber": 104, "endNumber": None, "productPrice": "5.58"},
            ]
        )
        adapter = _jlcpcb(_Store(), _Client({"C1": row}))
        rec = adapter.lookup("C1")
        assert rec is not None
        assert rec.price_breaks == ((1, 7.95), (30, 6.15), (104, 5.58))
        assert rec.unit_price_at(500) == pytest.approx(5.58)

    def test_a_break_without_a_quantity_is_skipped(self, creds: None) -> None:
        """A quantity-less break is not a "from 1" break — drop it, don't
        guess its floor."""
        row = _row(
            price=[
                {"endNumber": 100, "price": 0.5},
                {"startNumber": 50, "price": 0.4},
            ]
        )
        rec = _jlcpcb(_Store(), _Client({"C1": row})).lookup("C1")
        assert rec is not None and rec.price_breaks == ((50, 0.4),)

    def test_no_price_rows_give_empty_breaks(self, creds: None) -> None:
        adapter = _jlcpcb(_Store(), _Client({"C1": _row(price=None)}))
        rec = adapter.lookup("C1")
        assert rec is not None and rec.price_breaks == ()
        assert rec.unit_price_at(10) is None

    def test_a_miss_is_none_and_negative_cached(self, creds: None) -> None:
        client = _Client({})
        adapter = _jlcpcb(_Store(), client)
        assert adapter.lookup("C9999") is None
        assert adapter.lookup("C9999") is None
        assert client.calls == ["C9999"]  # second call served by the miss cache

    def test_lookup_without_a_store_raises_named_supplier_error(
        self, creds: None
    ) -> None:
        with pytest.raises(SupplierError, match="Store"):
            JlcPcbAdapter().lookup("C1")

    def test_lookup_without_credentials_raises_and_stays_uncached(self) -> None:
        # No injected client: the store path checks the secrets resolver.
        adapter = _jlcpcb(_Store())
        with pytest.raises(SupplierError):
            adapter.lookup("C1")
        with pytest.raises(SupplierError):  # a miss caches; a refusal must not
            adapter.lookup("C1")

    def test_a_vendor_failure_becomes_a_supplier_error(self, creds: None) -> None:
        class _Down:
            available = True

            def component_info(self, lcsc: str) -> dict[str, Any] | None:
                raise VendorError("jlcpcb", "HTTP 500", status=500)

        with pytest.raises(SupplierError) as excinfo:
            _jlcpcb(_Store(), _Down()).lookup("C1")
        assert excinfo.value.status == 500

    def test_search_refuses_rather_than_a_misleading_empty(self, creds: None) -> None:
        """JLCPCB matches by exact C-number; a zero-hit keyword list
        reads as catalogue absence — refuse instead."""
        with pytest.raises(LookupUnsupported, match="keyword"):
            _jlcpcb(_Store()).search("resistor")

    def test_injected_client_without_a_store_is_read_only(self, creds: None) -> None:
        """An available injected client answers lookups on its own —
        ``component_info`` directly, no ``parts`` upsert to fail on."""
        adapter = JlcPcbAdapter(client=cast(Any, _Client({"C9": _row(lcsc="C9")})))
        rec = adapter.lookup("C9")
        assert rec is not None and rec.sku == "C9"

    def test_the_client_is_built_once_and_reused(
        self, creds: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No injected client → one lazy ``JlcApiClient`` per adapter, so
        credentials resolve once rather than per lookup."""
        built: list[Any] = []

        def _factory(*, store: Any = None, **kw: Any) -> _Client:
            client = _Client({"C1": _row(lcsc="C1")})
            built.append(client)
            return client

        monkeypatch.setattr(jlc_api, "JlcApiClient", _factory)
        store = _Store()
        adapter = _jlcpcb(store)
        assert adapter.lookup("C1") is not None
        assert adapter.lookup("C1") is not None
        assert len(built) == 1
        assert [r["lcsc"] for r in store.imported] == ["C1", "C1"]
