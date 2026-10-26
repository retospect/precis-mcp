"""JLCPCB/LCSC exact lookups — the one vendor whose numbers may persist.

:func:`precis.pcb.jlc_api.live_lookup` already does the live C-number
fetch (``getComponentDetailByCode``), the ``parts`` upsert and the
five-minute miss cache; this adapter maps that normalized row onto
:class:`~precis.supply.base.PartRecord` so a ``vendor:`` pull sees one
answer shape across suppliers. JLCPCB is the ``may_store`` vendor of the
licence ruling (Reto 2026-10-26, option 3) — identity *and* stock/price
may persist; the read-only rulings for the other adapters stand (package
docstring).

A :class:`~precis.store.Store` is required because ``live_lookup``
upserts, so ``configured()`` reports a store-less adapter as missing
rather than looking credential-broken. ``client`` is the same test seam
``live_lookup`` exposes — this file never needs the network either.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from precis.supply.base import (
    LookupUnsupported,
    PartRecord,
    StockQuote,
    SupplierError,
    now,
)
from precis.utils.coerce import to_int

if TYPE_CHECKING:
    from precis.pcb.jlc_api import JlcApiClient
    from precis.store import Store


class JlcPcbAdapter:
    """``Adapter`` over JLCPCB's exact C-number endpoint.

    No keyword search exists on this API — ``search`` refuses rather
    than returning a misleading empty list (a zero-hit reads as
    catalogue absence).
    """

    name = "jlcpcb"
    may_store = True
    #: ``live_lookup`` upserts into ``parts``, so the adapter is out of
    #: :func:`~precis.supply.base.adapters` when no store is bound;
    #: :func:`~precis.supply.base.vendor` still resolves it so
    #: ``configured()`` can say why.
    needs_store = True

    def __init__(
        self, *, store: Store | None = None, client: JlcApiClient | None = None
    ) -> None:
        self._store = store
        self._client = client

    def configured(self) -> str | None:
        client = self._client
        if client is not None:
            if client.available:
                return None
            return (
                "the injected JLCPCB client is unavailable — its "
                "credentials did not resolve"
            )
        if self._store is None:
            return (
                "JLCPCB lookups need a Store — live_lookup upserts the row into `parts`"
            )
        from precis.pcb.jlc_api import credentials_available

        if not credentials_available(store=self._store):
            return (
                "JLCPCB_APP_ID / JLCPCB_ACCESS_KEY / JLCPCB_SECRET_KEY not all "
                "set — Open API app at open.jlcpcb.com; add the three on /secrets"
            )
        return None

    def search(self, designation: str, *, limit: int = 5) -> list[StockQuote]:
        raise LookupUnsupported(
            "jlcpcb matches by exact C-number (lookup), not keyword — a "
            "zero-hit list would read as catalogue absence"
        )

    def lookup(self, sku: str) -> PartRecord | None:
        """The C-number's identity plus live stock/price; ``None`` on a
        real miss (``live_lookup``'s negative cache applies). A missing
        store or credentials raises :class:`SupplierError` — the same
        skip-and-name shape a transport failure produces.

        The client is built lazily and kept, so credentials are read once
        per adapter rather than per lookup. An injected client with no
        store takes the read-only ``component_info`` path — the ``parts``
        upsert only runs through ``live_lookup``."""
        why = self.configured()
        if why is not None:
            raise SupplierError("lookup", None, reason=why)
        from precis.pcb import jlc_api
        from precis.utils.http import VendorError

        client = self._client
        store = self._store
        try:
            if store is None:
                # An injected available client is configured() by itself;
                # read-only — no parts upsert, no miss cache. Same
                # casefolding live_lookup applies before component_info.
                row = (
                    client.component_info(str(sku).strip().upper())
                    if client is not None
                    else None
                )
            else:
                if client is None:
                    client = self._client = jlc_api.JlcApiClient(store=store)
                row = jlc_api.live_lookup(store, sku, client=client)
        except VendorError as exc:
            raise SupplierError("lookup", exc.status, reason=str(exc)) from exc
        if row is None:
            return None
        return _record(row)


def _record(row: dict[str, Any]) -> PartRecord:
    """One ``normalize_api_row`` dict → :class:`PartRecord` — the spec's
    mapping: ``mfr``→manufacturer, ``mfr_part``→mpn, ``price``→
    ``price_breaks``; ``basic`` and ``jlcpcb_assemblable`` ride in
    ``extra`` — JLCPCB-only flags, not columns on the shared shape."""
    return PartRecord(
        supplier="jlcpcb",
        sku=str(row["lcsc"]),
        manufacturer=row.get("mfr"),
        mpn=row.get("mfr_part"),
        description=str(row.get("description") or ""),
        package=row.get("package"),
        datasheet_url=row.get("datasheet_url"),
        lifecycle=None,  # the detail endpoint carries no lifecycle field
        stock=to_int(row.get("stock")),
        price_breaks=_price_breaks(row.get("price")),
        currency="USD",
        retrieved=now(),
        extra={
            key: value
            for key, value in (
                ("basic", row.get("basic")),
                ("jlcpcb_assemblable", row.get("jlcpcb_assemblable")),
            )
            if value is not None
        },
    )


def _price_breaks(raw: Any) -> tuple[tuple[int, float], ...]:
    """The ``price`` column's two spellings → ``(min_qty, unit_price)``
    ascending: the detail endpoint's ``componentPrices``
    (``[{"qFrom": 1, "qTo": 100, "price": 0.0023}]``) and the jlcparts
    text form (``"1-49:0.0204,5000-:0.0108"`` via ``parse_v2_price``)."""
    if isinstance(raw, str):
        from precis.pcb.catalog import parse_v2_price

        raw = parse_v2_price(raw)
    if not isinstance(raw, list):
        return ()
    out: list[tuple[int, float]] = []
    for brk in raw:
        if not isinstance(brk, dict):
            continue
        qty_raw = next(
            (
                brk[k]
                for k in ("qFrom", "startNumber", "ladder")
                if brk.get(k) is not None
            ),
            None,
        )
        price_raw = next(
            (brk[k] for k in ("price", "productPrice") if brk.get(k) is not None),
            None,
        )
        if qty_raw is None or price_raw is None:
            # A break with no quantity is not a "from 1" break — skip it.
            continue
        try:
            qty = int(qty_raw)
            price = float(price_raw)
        except (TypeError, ValueError):
            continue
        out.append((qty, price))
    return tuple(sorted(out))
