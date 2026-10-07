"""Mouser Search API v1: attributed live stock, no content persistence.

Register MyMouser and request Search API approval; put the emailed key in
PRECIS_MOUSER_API_KEY via /secrets. Published limits: 30 calls/minute,
1000/day, 50 parts/call. API stock has no warehouse field; do not infer a
European warehouse from account currency. Prices retain their actual currency.
Terms prohibit caching/recording results, so no import/cache path exists.
Docs: https://api.mouser.com/api/docs/V1
"""

from __future__ import annotations

import math
import re
from typing import Any

import httpx

from precis.secrets import get_secret
from precis.supply._http import protect_request_logs
from precis.supply.base import StockQuote, SupplierError, now

_URL = "https://api.mouser.com/api/v1/search/keyword"


class MouserAdapter:
    name = "mouser"

    def configured(self) -> str | None:
        if not get_secret("PRECIS_MOUSER_API_KEY"):
            return "PRECIS_MOUSER_API_KEY not set — register MyMouser and request Search API access; add approved key on /secrets"
        return None

    def search(self, designation: str, *, limit: int = 5) -> list[StockQuote]:
        protect_request_logs()
        parts = self._parts(designation, limit=limit)
        out = []
        for part in parts:
            raw = str(part.get("AvailabilityInStock") or "").strip()
            match = re.fullmatch(r"\d+", raw)
            if match:
                quantity = int(raw)
            else:
                match = re.fullmatch(
                    r"([0-9]+(?:,[0-9]{3})*)\s+In\s*Stock",
                    str(part.get("Availability") or ""),
                    flags=re.I,
                )
                if not match:
                    raise ValueError("supplier stock quantity missing or unrecognized")
                quantity = int(match[1].replace(",", ""))
            prices = part.get("PriceBreaks") or []
            first = min(prices, key=lambda p: int(p["Quantity"])) if prices else {}
            price = str(first.get("Price") or "")
            # Do not guess decimal conventions or convert a currency symbol.
            parsed = re.fullmatch(r"[$€£¥]?\s*(\d+(?:\.\d+)?)", price)
            amount = float(parsed[1]) if parsed else None
            if amount is not None and not math.isfinite(amount):
                amount = None
            out.append(
                StockQuote(
                    supplier=self.name,
                    sku=str(part["MouserPartNumber"]),
                    description=str(part.get("Description") or ""),
                    quantity=quantity,
                    unit_price=amount,
                    currency=str(first.get("Currency") or "unknown"),
                    url=part.get("ProductDetailUrl"),
                    retrieved=now(),
                    match_confidence="exact_mpn"
                    if str(part.get("ManufacturerPartNumber") or "").casefold()
                    == designation.casefold()
                    else "keyword",
                )
            )
        return out[:limit]

    def _parts(self, designation: str, *, limit: int) -> list[dict[str, Any]]:
        protect_request_logs()
        response = httpx.post(
            _URL,
            params={"apiKey": get_secret("PRECIS_MOUSER_API_KEY")},
            json={
                "SearchByKeywordRequest": {
                    "keyword": designation,
                    "records": min(max(limit, 1), 50),
                    "startingRecord": 0,
                    "searchOptions": "None",
                    "searchWithYourSignUpLanguage": "false",
                }
            },
            timeout=3,
        )
        if response.is_error:
            raise SupplierError("search", response.status_code)
        payload = response.json()
        if payload.get("Errors"):
            raise ValueError("supplier API error")  # body may contain query credentials
        parts = (payload.get("SearchResults") or {}).get("Parts")
        if not isinstance(parts, list):
            raise ValueError("missing supplier parts")
        return parts

    def record(
        self, mpn: str, *, supplier_part_number: str | None = None
    ) -> dict[str, Any]:
        from precis.supply.live import select_record

        records = []
        for p in self._parts(supplier_part_number or mpn, limit=50):
            records.append(
                {
                    "supplier_part_number": p.get("MouserPartNumber"),
                    "manufacturer": p.get("Manufacturer"),
                    "mpn": p.get("ManufacturerPartNumber"),
                    "product_url": p.get("ProductDetailUrl"),
                    "datasheet_url": p.get("DataSheetUrl"),
                    "description": p.get("Description"),
                    "category_path": p.get("Category"),
                    "parameters": p.get("ProductAttributes"),
                    "price_breaks": p.get("PriceBreaks"),
                    "currency": "per price break",
                    "stock": {
                        "quantity": p.get("AvailabilityInStock"),
                        "availability": p.get("Availability"),
                        "region": None,
                        "warehouse": None,
                    },
                }
            )
        return select_record(records, mpn, supplier_part_number)
