"""The stock port (`precis.supply`) — the "is it actually buyable" half of
``se-off-the-shelf-fabrication.md``'s selection signal.

No network: the Digi-Key adapter is exercised against a stubbed
``httpx``, because what is worth testing is the *shape* of the answer —
that a missing credential says which one, that an outage does not sink
the other suppliers, and that a quote carries the caveat that it was
matched by keyword.

Credentials now resolve through :mod:`precis.secrets` (env -> vault ->
``~/.secrets/pw/<name>`` file -> default), so an unset env var no longer
means "absent" on a machine that happens to have a real file or a bound
store — the autouse fixture below pins the file layer to an empty
``tmp_path`` and unbinds any store, making the module hermetic.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from precis import secrets as vault
from precis import supply
from precis.supply import base
from precis.supply.digikey import DigiKeyAdapter


@pytest.fixture(autouse=True)
def _no_ambient_credentials(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> None:
    """Keep the vault/file layers of ``get_secret`` out of these tests unless
    a test opts in — see ``tests/test_secrets_resolver.py`` for the pattern."""
    monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
    vault.bind_store(None)
    vault.invalidate()


def _quote(**kw: Any) -> base.StockQuote:
    defaults: dict[str, Any] = {
        "supplier": "test",
        "sku": "X1",
        "description": "M4x12 socket cap",
        "quantity": 10,
        "unit_price": 0.12,
        "currency": "EUR",
        "url": None,
        "retrieved": datetime(2026, 9, 15, tzinfo=UTC),
    }
    return base.StockQuote(**{**defaults, "match_confidence": "keyword", **kw})


class TestQuoteShape:
    def test_a_quote_line_carries_the_caveat_and_the_timestamp(self) -> None:
        line = _quote().line()
        assert "10 in stock" in line
        assert "matched by keyword" in line
        assert "2026-09-15" in line

    def test_zero_stock_is_a_real_answer_not_an_absence(self) -> None:
        assert _quote(quantity=0).in_stock is False
        assert "0 in stock" in _quote(quantity=0).line()

    def test_one_in_stock_is_in_stock(self) -> None:
        """The boundary is one, not two — the last unit on the shelf is
        still the difference between buyable and not."""
        assert _quote(quantity=1).in_stock is True

    def test_a_priceless_quote_still_reports_its_stock(self) -> None:
        assert "no price" in _quote(unit_price=None).line()


class TestConfiguration:
    def test_a_missing_credential_names_itself(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("PRECIS_DIGIKEY_CLIENT_ID", raising=False)
        monkeypatch.delenv("PRECIS_DIGIKEY_CLIENT_SECRET", raising=False)
        why = DigiKeyAdapter().configured()
        assert why is not None
        assert "PRECIS_DIGIKEY_CLIENT_ID" in why
        assert "developer.digikey.com" in why

    def test_half_a_credential_is_not_configured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An id with no secret authenticates nothing — reporting it ready
        would turn a config mistake into an auth error at query time."""
        monkeypatch.setenv("PRECIS_DIGIKEY_CLIENT_ID", "id")
        monkeypatch.delenv("PRECIS_DIGIKEY_CLIENT_SECRET", raising=False)
        why = DigiKeyAdapter().configured()
        assert why is not None and "PRECIS_DIGIKEY_CLIENT_SECRET" in why
        assert "PRECIS_DIGIKEY_CLIENT_ID" not in why  # names only what's missing

    def test_both_credentials_present_means_ready(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_DIGIKEY_CLIENT_ID", "id")
        monkeypatch.setenv("PRECIS_DIGIKEY_CLIENT_SECRET", "secret")
        assert DigiKeyAdapter().configured() is None
        assert supply.unavailable_reason() is None

    def test_unconfigured_reports_why_rather_than_an_empty_result(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A missing key and a part nobody stocks must not read alike."""
        monkeypatch.delenv("PRECIS_DIGIKEY_CLIENT_ID", raising=False)
        monkeypatch.delenv("PRECIS_DIGIKEY_CLIENT_SECRET", raising=False)
        why = supply.unavailable_reason()
        assert why is not None and "digikey" in why

    def test_credentials_resolve_through_the_file_layer_with_no_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
    ) -> None:
        """With env unset, a file under ``PRECIS_SECRETS_FILE_DIR`` (the
        resolver's fallback below the vault) is enough to be configured —
        proof the adapter goes through ``get_secret`` rather than a raw
        ``os.environ`` read."""
        monkeypatch.delenv("PRECIS_DIGIKEY_CLIENT_ID", raising=False)
        monkeypatch.delenv("PRECIS_DIGIKEY_CLIENT_SECRET", raising=False)
        monkeypatch.setenv("PRECIS_SECRETS_FILE_DIR", str(tmp_path))
        (tmp_path / "PRECIS_DIGIKEY_CLIENT_ID").write_text("id\n", encoding="utf-8")
        (tmp_path / "PRECIS_DIGIKEY_CLIENT_SECRET").write_text(
            "secret\n", encoding="utf-8"
        )
        assert DigiKeyAdapter().configured() is None


class _Boom:
    name = "boom"

    def configured(self) -> str | None:
        return None

    def search(self, designation: str, *, limit: int = 5) -> list[base.StockQuote]:
        raise RuntimeError("supplier is down")


class _Fine:
    name = "fine"

    def configured(self) -> str | None:
        return None

    def search(self, designation: str, *, limit: int = 5) -> list[base.StockQuote]:
        return [
            _quote(supplier="fine", quantity=7),
            _quote(supplier="fine", quantity=99),
        ]


class TestAggregation:
    def test_one_supplier_down_does_not_sink_the_others(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(base, "adapters", lambda: [_Boom(), _Fine()])
        quotes = base.quote("ISO 4762 M4x12")
        assert [q.quantity for q in quotes] == [99, 7]  # best stocked first

    def test_nothing_listed_is_an_empty_list_not_an_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(base, "adapters", lambda: [_Boom()])
        assert base.quote("ISO 4762 M99x900") == []


class _Response:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class TestDigiKeyParsing:
    def test_a_product_row_becomes_a_quote(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_DIGIKEY_CLIENT_ID", "id")
        monkeypatch.setenv("PRECIS_DIGIKEY_CLIENT_SECRET", "secret")
        adapter = DigiKeyAdapter()
        calls: list[str] = []

        def fake_post(url: str, **kw: Any) -> _Response:
            calls.append(url)
            if "oauth2" in url:
                return _Response({"access_token": "tok", "expires_in": 600})
            return _Response(
                {
                    "Products": [
                        {
                            "QuantityAvailable": 4213,
                            "Description": {"ProductDescription": "SCREW M4"},
                            "ProductVariations": [
                                {
                                    "DigiKeyProductNumber": "DK-1",
                                    "Currency": "EUR",
                                    "StandardPricing": [{"UnitPrice": 0.19}],
                                }
                            ],
                        },
                        {"Description": {"ProductDescription": "no quantity"}},
                    ]
                }
            )

        monkeypatch.setattr("precis.supply.digikey.httpx.post", fake_post)
        quotes = adapter.search("ISO 4762 M4x12")
        assert len(quotes) == 1  # the row with no quantity is dropped, not guessed
        assert quotes[0].quantity == 4213
        assert quotes[0].sku == "DK-1"
        assert quotes[0].unit_price == pytest.approx(0.19)
        assert any("oauth2" in c for c in calls)

    def test_the_token_is_fetched_once_per_adapter(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PRECIS_DIGIKEY_CLIENT_ID", "id")
        monkeypatch.setenv("PRECIS_DIGIKEY_CLIENT_SECRET", "secret")
        adapter = DigiKeyAdapter()
        tokens = 0

        def fake_post(url: str, **kw: Any) -> _Response:
            nonlocal tokens
            if "oauth2" in url:
                tokens += 1
                return _Response({"access_token": "tok", "expires_in": 600})
            return _Response({"Products": []})

        monkeypatch.setattr("precis.supply.digikey.httpx.post", fake_post)
        adapter.search("a")
        adapter.search("b")
        assert tokens == 1
