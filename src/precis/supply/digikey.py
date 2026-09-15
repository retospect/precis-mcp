"""Digi-Key Product Information v4 — the one free, self-serve stock feed.

Register a developer app on developer.digikey.com against a My DigiKey
account (no commercial arrangement, no order history required), enable the
Product Information API, and set ``PRECIS_DIGIKEY_CLIENT_ID`` and
``PRECIS_DIGIKEY_CLIENT_SECRET``. Auth is OAuth2 **client credentials**:
one token request, cached until it expires, no user interaction.

**What it is good for, and what it isn't.** Digi-Key's hardware catalogue
is real but partial for our purposes: machine screws, standoffs and
threaded inserts in the small metric sizes, thinning out above M5 and
largely absent for structural sizes. A miss here means "Digi-Key does not
list it", never "nobody stocks it" — which is why the caller pairs every
answer with the curated availability tier rather than replacing it.

**Matching is by keyword**, because we hold a standards designation and
they hold manufacturer part numbers. Every quote says so
(``match_confidence='keyword'``): the top hit for "ISO 4762 M4x12" may be
the right screw in the wrong grade, or a bag of 100.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any

import httpx

from precis.supply.base import StockQuote, now

_TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
_SEARCH_URL = "https://api.digikey.com/products/v4/search/keyword"

#: Seconds of slack on the token's own lifetime — refresh a little early
#: rather than discover expiry as a 401 in the middle of a search.
_TOKEN_SKEW_S = 60.0

_TIMEOUT_S = 15.0


class DigiKeyAdapter:
    """One process-wide adapter; the access token is cached on it."""

    name = "digikey"

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: float = 0.0

    # -- configuration ---------------------------------------------------

    @staticmethod
    def _credentials() -> tuple[str | None, str | None]:
        return (
            os.environ.get("PRECIS_DIGIKEY_CLIENT_ID"),
            os.environ.get("PRECIS_DIGIKEY_CLIENT_SECRET"),
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
            "developer.digikey.com (free, self-serve) and enable the Product "
            "Information API"
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
            response.raise_for_status()
            payload = response.json()
            self._token = str(payload["access_token"])
            self._expires_at = (
                time.monotonic() + float(payload.get("expires_in", 600)) - _TOKEN_SKEW_S
            )
            return self._token

    # -- search ----------------------------------------------------------

    def search(self, designation: str, *, limit: int = 5) -> list[StockQuote]:
        client_id, _ = self._credentials()
        response = httpx.post(
            _SEARCH_URL,
            json={"Keywords": designation, "Limit": max(1, min(int(limit), 50))},
            headers={
                "Authorization": f"Bearer {self._access_token()}",
                "X-DIGIKEY-Client-Id": client_id or "",
                "X-DIGIKEY-Locale-Site": "DE",
                "X-DIGIKEY-Locale-Currency": "EUR",
            },
            timeout=_TIMEOUT_S,
        )
        response.raise_for_status()
        return [
            q
            for q in (self._one(p) for p in response.json().get("Products") or [])
            if q is not None
        ][:limit]

    @staticmethod
    def _one(product: dict[str, Any]) -> StockQuote | None:
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
            currency=str(first.get("Currency") or "EUR"),
            url=product.get("ProductUrl"),
            retrieved=now(),
        )
