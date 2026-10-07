"""Farnell/element14 Product Search: live, attributed warehouse inventory.

Self-serve Partner Portal registration supplies PRECIS_FARNELL_API_KEY.
PRECIS_FARNELL_STORE defaults to ie.farnell.com; uk/de stores are supported.
The inventory response group supplies breakdowns, not currency-backed prices.
Never count both a breakdown and its aggregate. API terms prohibit content
storage, so this adapter has no result cache or component import path.
Docs: https://partner.element14.com/search_api/Inventory_Example
"""

from __future__ import annotations

import os
from typing import Any

import httpx

from precis.secrets import get_secret
from precis.supply._http import protect_request_logs
from precis.supply.base import StockQuote, SupplierError, now

_URL = "https://api.element14.com/catalog/products"
_STORES = {"ie.farnell.com": "IE", "uk.farnell.com": "GB", "de.farnell.com": "DE"}


class FarnellAdapter:
    name = "farnell"

    def __init__(self) -> None:
        self.store = os.environ.get("PRECIS_FARNELL_STORE", "ie.farnell.com").strip()

    def configured(self) -> str | None:
        if self.store not in _STORES:
            return "PRECIS_FARNELL_STORE must be ie.farnell.com, uk.farnell.com or de.farnell.com"
        if not get_secret("PRECIS_FARNELL_API_KEY"):
            return "PRECIS_FARNELL_API_KEY not set — register at partner.element14.com; add key on /secrets"
        return None

    def search(self, designation: str, *, limit: int = 5) -> list[StockQuote]:
        protect_request_logs()
        products = self._products(designation, limit=limit, group="inventory")
        quotes = []
        for product in products:
            quotes.extend(self._quotes(product, designation))
        return quotes  # aggregation selects after home-region sorting

    def _products(
        self, designation: str, *, limit: int, group: str
    ) -> list[dict[str, Any]]:
        protect_request_logs()
        response = httpx.get(
            _URL,
            params={
                "versionNumber": "1.4",
                "term": "any:" + designation,
                "storeInfo.id": self.store,
                "resultsSettings.offset": 0,
                "resultsSettings.numberOfResults": min(max(limit, 1), 50),
                "resultsSettings.responseGroup": group,
                "callInfo.responseDataFormat": "json",
                "callInfo.apiKey": get_secret("PRECIS_FARNELL_API_KEY"),
            },
            timeout=3,
        )
        if response.is_error:
            raise SupplierError("search", response.status_code)
        payload = response.json()
        result = payload.get("keywordSearchReturn")
        if payload.get("error") or not isinstance(result, dict):
            raise ValueError("supplier API error or missing search response")
        products = result.get("products")
        if products is None and result.get("numberOfResults") == 0:
            return []
        if not isinstance(products, list):
            raise ValueError("missing supplier products")
        return products

    def record(
        self, mpn: str, *, supplier_part_number: str | None = None
    ) -> dict[str, Any]:
        from precis.supply.live import select_record

        query = supplier_part_number or mpn
        products = self._products(query, limit=50, group="large")
        records = []
        for p in products:
            datasheets = p.get("datasheets") or []
            records.append(
                {
                    "supplier_part_number": str(p["sku"]),
                    "manufacturer": p.get("brandName") or p.get("vendorName"),
                    "mpn": p.get("translatedManufacturerPartNumber"),
                    "product_url": p.get("productURL"),
                    "datasheet_url": datasheets[0].get("url") if datasheets else None,
                    "description": p.get("displayName"),
                    "category_path": p.get("categoryName"),
                    "parameters": p.get("attributes"),
                    "price_breaks": p.get("prices"),
                    "currency": p.get("currency")
                    or "not supplied by API; store " + self.store,
                }
            )
        selected = select_record(records, mpn, supplier_part_number)
        inventory = self._products(
            selected["supplier_part_number"], limit=50, group="inventory"
        )
        matching = [
            p
            for p in inventory
            if str(p.get("sku")) == selected["supplier_part_number"]
        ]
        selected["stock"] = matching[0].get("stock") if len(matching) == 1 else None
        if len(matching) == 1 and not selected.get("product_url"):
            selected["product_url"] = matching[0].get("productURL")
        return selected

    def _quotes(self, product: dict[str, Any], query: str) -> list[StockQuote]:
        stock = product.get("stock") or {}
        rows = stock.get("breakdown")
        detailed = bool(rows)
        if not rows:
            rows = stock.get("regionalBreakdown")
        if not rows:
            if stock.get("level") is None:
                raise ValueError("supplier stock quantity missing")
            rows = [{"level": stock["level"]}]
        out = []
        for row in rows:
            quantity = row.get("inv" if detailed else "level")
            if quantity is None or int(quantity) < 0:
                raise ValueError("supplier stock quantity invalid")
            reported = row.get("region") if detailed else row.get("warehouse")
            region = {"UK": "GB", "GB": "GB", "IE": "IE", "DE": "DE", "EU": "EU"}.get(
                reported
            )
            warehouse = row.get("warehouse")
            if warehouse == "-":
                warehouse = None
            out.append(
                StockQuote(
                    supplier=self.name,
                    sku=str(product["sku"]),
                    description=str(product.get("displayName") or ""),
                    quantity=int(quantity),
                    unit_price=None,
                    currency="unknown",
                    url=product.get("productURL"),
                    retrieved=now(),
                    match_confidence="exact_mpn"
                    if str(
                        product.get("translatedManufacturerPartNumber") or ""
                    ).casefold()
                    == query.casefold()
                    else "keyword",
                    region=region,
                    warehouse=warehouse,
                    ships_from=region,
                    ships_to=_STORES[self.store],
                )
            )
        return out
