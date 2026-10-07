"""Digi-Key Product Information v4 — the one free, self-serve stock feed.

Register a developer app on developer.digikey.com against a My DigiKey
account (no commercial arrangement, no order history required), enable the
Product Information API, and paste the resulting client ID and secret onto
the precis-web ``/secrets`` page (they resolve through the DB vault as
``PRECIS_DIGIKEY_CLIENT_ID`` / ``PRECIS_DIGIKEY_CLIENT_SECRET`` —
:func:`precis.secrets.get_secret`); an env var or a
``~/.secrets/pw/<name>`` file still works as a local override, per the
resolver order in ``src/precis/secrets.py``'s module docstring. Auth is
OAuth2 **client credentials**: one token request, cached until it expires,
no user interaction.

**What it is good for, and what it isn't.** Digi-Key's hardware catalogue
is real but partial for our purposes: machine screws, standoffs and
threaded inserts in the small metric sizes, thinning out above M5 and
largely absent for structural sizes. A successful zero-hit means only "0 products for this keyword",
never proof that the catalogue lacks the part — which is why the caller pairs every
answer with the curated availability tier rather than replacing it.

**Matching is explicit.** Fastener searches remain ``keyword``; a
returned manufacturer MPN equal to the query is ``exact_mpn``. A keyword
hit can still have the wrong grade, finish or pack quantity. Site/currency
are configurable (IE/EUR default); the site selects a country catalogue
with restrictions, not a warehouse. Public v4 stock has no location field.
Persisted API enrichment is refused; only confirmed identity links are stored.
"""

from __future__ import annotations

import os
import threading
import time
from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import httpx

from precis.secrets import get_secret
from precis.supply.base import StockQuote, SupplierError, now
from precis.supply.catalog import metric_parameters, validate_filters

_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"
_CATEGORIES_URL = "https://api.digikey.com/products/v4/search/categories"


@dataclass(frozen=True)
class CategorySnapshot:
    categories: list[dict[str, Any]]
    retrieved: datetime


#: Seconds of slack on the token's own lifetime — refresh a little early
#: rather than discover expiry as a 401 in the middle of a search.
_TOKEN_SKEW_S = 60.0

_TIMEOUT_S = 3.0


class DigiKeyAdapter:
    """Access-token memo only; supplier response data is never cached."""

    name = "digikey"

    def __init__(self) -> None:
        self.site = os.environ.get("PRECIS_DIGIKEY_SITE", "IE").strip().upper()
        self.currency = os.environ.get("PRECIS_DIGIKEY_CURRENCY", "EUR").strip().upper()
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    # -- configuration ---------------------------------------------------

    @staticmethod
    def _credentials() -> tuple[str | None, str | None]:
        # No store argument: the runtime factory binds the boot store, so
        # the MCP server, precis-web, and the `precis tools` CLI all reach
        # the vault; an env var still overrides per get_secret's resolution
        # order.
        return (
            get_secret("PRECIS_DIGIKEY_CLIENT_ID"),
            get_secret("PRECIS_DIGIKEY_CLIENT_SECRET"),
        )

    def configured(self) -> str | None:
        client_id, secret = self._credentials()
        if client_id and secret:
            return None
        missing = [
            name
            for name, value in (
                ("PRECIS_DIGIKEY_CLIENT_ID", client_id),
                ("PRECIS_DIGIKEY_CLIENT_SECRET", secret),
            )
            if not value
        ]
        return (
            f"{' and '.join(missing)} not set — register a developer app at "
            "developer.digikey.com (free, self-serve), enable the Product "
            "Information API, and paste the credentials on the precis-web "
            "/secrets page (or an env var / ~/.secrets/pw/<name> file, as a "
            "local override)"
        )

    # -- auth ------------------------------------------------------------

    def _access_token(self) -> str:
        with self._lock:
            if self._token and time.monotonic() < self._expires_at:
                return self._token
            client_id, secret = self._credentials()
            response = httpx.post(
                _TOKEN_URL,
                data={
                    "client_id": client_id,
                    "client_secret": secret,
                    "grant_type": "client_credentials",
                },
                timeout=_TIMEOUT_S,
            )
            self._check_status(response, "token")
            payload = response.json()
            self._token = str(payload["access_token"])
            self._expires_at = (
                time.monotonic() + float(payload.get("expires_in", 600)) - _TOKEN_SKEW_S
            )
            return self._token

    # -- search ----------------------------------------------------------

    def _headers(self, *, currency: str | None = None) -> dict[str, str]:
        client_id, _ = self._credentials()
        return {
            "Authorization": f"Bearer {self._access_token()}",
            "X-DIGIKEY-Client-Id": client_id or "",
            "X-DIGIKEY-Locale-Site": self.site,
            "X-DIGIKEY-Locale-Language": "en",
            "X-DIGIKEY-Locale-Currency": currency or self.currency,
        }

    def categories(self, category_id: int | None = None) -> CategorySnapshot:
        """Uncached category list/detail, with its retrieval UTC stamp."""
        validate_filters(category_id, None)
        url = _CATEGORIES_URL + (f"/{category_id}" if category_id is not None else "")
        response = httpx.get(url, headers=self._headers(), timeout=_TIMEOUT_S)
        self._check_status(response, "categories")
        payload = response.json()
        entries = (
            payload["Categories"] if category_id is None else [payload["Category"]]
        )
        if not isinstance(entries, list) or any(
            not isinstance(item, dict) for item in entries
        ):
            raise ValueError("invalid category response")
        return CategorySnapshot(deepcopy(entries), now())

    def _keyword(
        self,
        designation: str,
        *,
        limit: int = 5,
        category_id: int | None = None,
        parameters: dict[str, list[str]] | None = None,
        currency: str | None = None,
    ) -> dict[str, Any]:
        validate_filters(category_id, parameters)
        body: dict[str, Any] = {
            "Keywords": designation,
            "Limit": max(1, min(int(limit), 50)),
        }
        if category_id is not None:
            filters: dict[str, Any] = {"CategoryFilter": [{"Id": str(category_id)}]}
            if parameters:
                filters["ParameterFilterRequest"] = {
                    "CategoryFilter": {"Id": str(category_id)},
                    "ParameterFilters": [
                        {
                            "ParameterId": int(key),
                            "FilterValues": [{"Id": value} for value in values],
                        }
                        for key, values in parameters.items()
                    ],
                }
            body["FilterOptionsRequest"] = filters
        response = httpx.post(
            _SEARCH_URL,
            json=body,
            headers=self._headers(currency=currency),
            timeout=_TIMEOUT_S,
        )
        self._check_status(response, "search")
        payload: dict[str, Any] = response.json()
        if not isinstance(payload, dict) or not isinstance(
            payload.get("Products"), list
        ):
            raise ValueError("invalid keyword response")
        return payload

    def parameter_options(self, category_id: int) -> list[dict[str, Any]]:
        """Current category-scoped filter IDs/labels; never cache stock quotes."""
        return list(
            (
                self._keyword("", category_id=category_id, limit=1).get("FilterOptions")
                or {}
            ).get("ParametricFilters")
            or []
        )

    def fastener_parameters(
        self,
        category_id: int,
        *,
        thread: str,
        pitch: float | None,
        length: float | None,
        length_parameter: str = "Length - Below Head",
    ) -> dict[str, list[str]]:
        return metric_parameters(
            self.parameter_options(category_id),
            thread=thread,
            pitch=pitch,
            length=length,
            category_id=category_id,
            length_parameter=length_parameter,
        )

    def search(
        self,
        designation: str,
        *,
        limit: int = 5,
        category_id: int | None = None,
        parameters: dict[str, list[str]] | None = None,
    ) -> list[StockQuote]:
        payload = self._keyword(
            designation, limit=limit, category_id=category_id, parameters=parameters
        )
        return [
            replace(q, match_confidence="exact_mpn", ships_to=self.site)
            if str(p.get("ManufacturerProductNumber") or "").casefold()
            == designation.casefold()
            else replace(q, ships_to=self.site)
            for p in payload["Products"]
            if (
                q := self._one(
                    p,
                    currency=str(
                        (payload.get("SearchLocaleUsed") or {}).get("Currency")
                        or "unknown"
                    ),
                )
            )
            is not None
        ][:limit]

    def product_record(
        self, mpn: str, *, supplier_part_number: str | None = None
    ) -> tuple[dict[str, Any], dict[str, Any], str]:
        """Live identity lookup in configured currency; refuse unconfirmed keywords.

        Return product, actual locale and match confidence. A caller-provided
        supplier number confirms only that exact variation, not the first hit.
        """
        from precis.supply.live import IdentityRefused

        payload = self._keyword(supplier_part_number or mpn, limit=50)
        products = payload["Products"]
        if supplier_part_number:
            matches = [
                p
                for p in products
                if any(
                    str(v.get("DigiKeyProductNumber") or "").casefold()
                    == supplier_part_number.casefold()
                    for v in p.get("ProductVariations") or []
                )
            ]
            confidence = "confirmed_supplier_part_number"
        else:
            matches = [
                p
                for p in products
                if str(p.get("ManufacturerProductNumber") or "").casefold()
                == mpn.casefold()
            ]
            confidence = "exact_mpn"
        if len(matches) != 1:
            candidates = [
                {
                    "sku": v.get("DigiKeyProductNumber"),
                    "description": (p.get("Description") or {}).get(
                        "ProductDescription"
                    ),
                }
                for p in products
                for v in p.get("ProductVariations") or []
            ][:5]
            raise IdentityRefused(
                f"no unique confirmed supplier product; candidates={candidates}"
            )
        product = dict(matches[0])
        category_id = (product.get("Category") or {}).get("CategoryId")
        path: list[dict[str, Any]] = []

        def walk(entries: list[dict[str, Any]], parent: list[dict[str, Any]]) -> None:
            nonlocal path
            for entry in entries:
                current = parent + [
                    {"id": entry["CategoryId"], "name": entry.get("Name")}
                ]
                if entry["CategoryId"] == category_id:
                    path = current
                    return
                walk(entry.get("Children") or [], current)

        walk(self.categories().categories, [])
        if not path:
            raise IdentityRefused("supplier product category path unavailable")
        product["precis_category_path"] = path
        return product, payload.get("SearchLocaleUsed") or {}, confidence

    def record(
        self, mpn: str, *, supplier_part_number: str | None = None
    ) -> dict[str, Any]:
        product, locale, confidence = self.product_record(
            mpn, supplier_part_number=supplier_part_number
        )
        variants = product.get("ProductVariations") or []
        if supplier_part_number:
            variants = [
                v
                for v in variants
                if str(v.get("DigiKeyProductNumber") or "").casefold()
                == supplier_part_number.casefold()
            ]
        if not variants:
            from precis.supply.live import IdentityRefused

            raise IdentityRefused("no matching supplier variation")
        return {
            "supplier_part_number": variants[0]["DigiKeyProductNumber"],
            "manufacturer": (product.get("Manufacturer") or {}).get("Name"),
            "mpn": product.get("ManufacturerProductNumber"),
            "product_url": product.get("ProductUrl"),
            "datasheet_url": product.get("DatasheetUrl"),
            "match_confidence": confidence,
            "description": (product.get("Description") or {}).get("ProductDescription"),
            "category_path": product.get("precis_category_path"),
            "parameters": product.get("Parameters"),
            "currency": locale.get("Currency") or "unknown",
            "price_breaks": [
                {
                    "supplier_part_number": v["DigiKeyProductNumber"],
                    "currency": locale.get("Currency") or "unknown",
                    "breaks": v.get("StandardPricing") or [],
                }
                for v in variants
            ],
            "stock": {
                "quantity": product.get("QuantityAvailable"),
                "region": None,
                "warehouse": None,
            },
        }

    @staticmethod
    def _check_status(response: httpx.Response, stage: str) -> None:
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SupplierError(stage, response.status_code) from exc

    @staticmethod
    def _one(
        product: dict[str, Any], *, currency: str = "unknown"
    ) -> StockQuote | None:
        """One API product → a quote, or ``None`` when the row is missing
        the only field this exists to report."""
        quantity = product.get("QuantityAvailable")
        if quantity is None:
            return None
        variations = product.get("ProductVariations") or []
        first = variations[0] if variations else {}
        breaks = first.get("StandardPricing") or []
        price = float(breaks[0]["UnitPrice"]) if breaks else None
        return StockQuote(
            supplier="digikey",
            sku=str(
                first.get("DigiKeyProductNumber")
                or product.get("ManufacturerProductNumber")
                or "?"
            ),
            description=str(
                (product.get("Description") or {}).get("ProductDescription") or ""
            ),
            quantity=int(quantity),
            unit_price=price,
            currency=str(first.get("Currency") or currency),
            url=product.get("ProductUrl"),
            retrieved=now(),
        )
