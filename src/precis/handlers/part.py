"""PartHandler — the LCSC/JLCPCB catalog kind (ADR 0042 §5).

A ``part`` is reference data in the ``parts`` catalog table (NOT a ref;
addressed by its LCSC **C-number**, e.g. ``get(kind='part', id='C25804')``).
It is **ingest-only** — populated by the ``parts_refresh`` worker from the
``jlcparts`` dump (Slice 2), never by ``put``. Selection prefers
**JLCPCB-assemblable, high-turnover** parts (ADR 0042 §5).

Slice 1 ships read-only access (``get`` one part, ``search`` the catalog) over
whatever the importer has loaded; the turnover ranking + lazy
``easyeda2kicad`` footprint fetch land in Slice 2.
"""

from __future__ import annotations

import logging
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.pcb.catalog import min_unit_price
from precis.protocol import Handler, KindSpec
from precis.response import Response

log = logging.getLogger(__name__)


class PartHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="part",
        title="Part",
        description=(
            "LCSC/JLCPCB catalog part (ADR 0042 §5) — reference data addressed "
            "by LCSC C-number (get(kind='part', id='C25804')). Ingest-only "
            "(jlcparts dump). search(kind='part', q='0.1uF 0402 X7R') filters "
            "to JLCPCB-assemblable parts and prefers Basic + high-turnover + "
            "cheap. Used by a pcb design to pick manufacturable parts. "
            "See precis-part-select-help."
        ),
        supports_get=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("part: store required")
        self.store = hub.store

    # ── get ──────────────────────────────────────────────────────────
    def get(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "get(kind='part') requires id= (an LCSC C-number)",
                next="get(kind='part', id='C25804')  or  search(kind='part', q='...')",
            )
        lcsc = str(id).strip().upper()
        row = self.store.part_row(lcsc)
        if row is None:
            raise NotFound(
                f"part {lcsc} not in the catalog",
                next="the parts catalog is populated by the parts_refresh worker "
                "(precis pcb refresh-parts); search(kind='part', q='...') once "
                "it has run",
            )
        payload = {
            "lcsc": row["lcsc"],
            "mfr_part": row["mfr_part"],
            "description": row["description"],
            "assemblable": row["jlcpcb_assemblable"],
            "basic": row["basic"],
            "stock": row["stock"],
            "package": row["package"],
            "height_mm": row["height_mm"],
            "datasheet_url": row["datasheet_url"],
            "restocks": row["restock_count"],
        }
        return Response(body=render_agent_table([payload]))

    # ── search ───────────────────────────────────────────────────────
    def search(  # type: ignore[override]
        self, *, q: str | None = None, page_size: int = 20, **_kw: Any
    ) -> Response:
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='part') requires q=",
                next="search(kind='part', q='0.1uF 0402 X7R 16V')",
            )
        q = str(q).strip()
        # JLCPCB-native selector: hard-filter to assemblable parts; rank
        # Basic-first then turnover (ADR 0042 §5, store.parts_search).
        rows = self.store.parts_search(q, limit=page_size)
        if not rows:
            return Response(
                body=f"no assemblable parts match {q!r}\n\n"
                "Note: the parts catalog is populated by `precis pcb "
                "refresh-parts` (the jlcparts dump) — it may be empty until "
                "that has run."
            )
        out = []
        for r in rows:
            price = min_unit_price(r["price"])
            out.append(
                {
                    "lcsc": r["lcsc"],
                    "mfr_part": r["mfr_part"] or "—",
                    "description": (r["description"] or "")[:50],
                    "basic": "yes" if r["basic"] else "no",
                    "stock": r["stock"] if r["stock"] is not None else "—",
                    "restocks": r["restock_count"],
                    "package": r["package"] or "—",
                    "$ea": f"{price:.4g}" if price is not None else "—",
                }
            )
        return Response(
            body=f"# {len(out)} assemblable part(s) for {q!r} "
            "(Basic + high-turnover first)\n"
            + render_agent_table(
                out,
                schema=[
                    "lcsc",
                    "mfr_part",
                    "description",
                    "basic",
                    "stock",
                    "restocks",
                    "package",
                    "$ea",
                ],
            )
        )


__all__ = ["PartHandler"]
