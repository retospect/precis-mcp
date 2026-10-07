"""Transient supplier records and persisted identity links, kept separate.

Reto declined API-data import: only identity/link fields persist. No supplier
response cache; single component reads fetch live with attribution. Manufacturer
datasheets are independent evidence through the existing datasheet ingest path.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from precis.supply.base import SupplierError, now

LINK_FIELDS = (
    "supplier",
    "supplier_part_number",
    "manufacturer",
    "mpn",
    "product_url",
    "datasheet_url",
    "match_confidence",
    "first_linked_at",
)


class IdentityRefused(RuntimeError):
    """Safe identity diagnostic without request/credential information."""


def supplier_adapter(name: str) -> Any:
    from precis.supply.digikey import DigiKeyAdapter
    from precis.supply.farnell import FarnellAdapter
    from precis.supply.mouser import MouserAdapter

    registry = {
        "digikey": DigiKeyAdapter,
        "farnell": FarnellAdapter,
        "mouser": MouserAdapter,
    }
    if name not in registry:
        raise IdentityRefused("unsupported linked supplier")
    return registry[name]()


def select_record(
    records: list[dict[str, Any]], mpn: str, confirmed: str | None
) -> dict[str, Any]:
    matches = [
        item
        for item in records
        if (
            str(item.get("supplier_part_number") or "").casefold()
            == confirmed.casefold()
            if confirmed
            else bool(mpn) and str(item.get("mpn") or "").casefold() == mpn.casefold()
        )
    ]
    if len(matches) != 1:
        raise IdentityRefused(
            "no unique exact MPN/confirmed supplier part number; confirm a supplier SKU"
        )
    return {
        **matches[0],
        "match_confidence": "confirmed_supplier_part_number"
        if confirmed
        else "exact_mpn",
    }


def identity_link(
    supplier: str, record: dict[str, Any], retrieved: datetime
) -> dict[str, Any]:
    link = {key: record.get(key) for key in LINK_FIELDS}
    link.update(supplier=supplier, first_linked_at=retrieved.isoformat())
    if not link["supplier_part_number"] or not link["mpn"] or not link["manufacturer"]:
        raise IdentityRefused("supplier record lacks manufacturer/MPN/part number")
    for field in ("product_url", "datasheet_url"):
        url = link[field]
        if not url and field == "datasheet_url":
            continue
        parsed = urlsplit(str(url))
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise IdentityRefused(
                "supplier link requires public HTTPS URLs without credentials"
            )
    return link


def safe_error(exc: Exception) -> str:
    return (
        str(exc)
        if isinstance(exc, (SupplierError, IdentityRefused))
        else type(exc).__name__
    )


def sources_lines(meta: dict[str, Any]) -> list[str]:
    links = meta.get("supplier_links") or []
    if not links:
        return []
    lines = ["", "## Sources"]
    for link in links:
        lines.append(
            f"- {link['supplier']} {link['supplier_part_number']}: [{link['manufacturer']} {link['mpn']}]({link['product_url']}) · matched by {link['match_confidence']} · first linked {link['first_linked_at']}"
        )
        if link.get("datasheet_url"):
            lines.append(
                f"  manufacturer datasheet: [{link['datasheet_url']}]({link['datasheet_url']})"
            )
    return lines


def live_lines(meta: dict[str, Any]) -> list[str]:
    lines = []
    for link in meta.get("supplier_links") or []:
        name = link["supplier"]
        try:
            adapter = supplier_adapter(name)
            why = adapter.configured()
            if why:
                raise IdentityRefused(why)
            record = adapter.record(
                link["mpn"], supplier_part_number=link["supplier_part_number"]
            )
            lines += [
                "",
                f"## live from {name}, retrieved {now().isoformat()}, not stored",
                f"Source: [{name} {record['supplier_part_number']}]({record['product_url']})",
            ]
            for key in (
                "description",
                "category_path",
                "parameters",
                "price_breaks",
                "currency",
                "stock",
            ):
                value = record.get(key)
                lines.append(
                    f"{key}: {json.dumps(value, ensure_ascii=False) if isinstance(value, (list, dict)) else value if value is not None else 'not supplied'}"
                )
        except Exception as exc:
            lines += [
                "",
                f"## {name} live supplier",
                f"supplier unreachable: {safe_error(exc)} · {name}; not stored",
            ]
    return lines or ["no supplier links recorded"]
