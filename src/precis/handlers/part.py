"""PartHandler — the LCSC/JLCPCB catalog kind.

A ``part`` is reference data in the ``parts`` catalog table, addressed by
its LCSC **C-number**, e.g. ``get(kind='part', id='C25804')``. A part
becomes a ref lazily, on its first add-mode link
(:meth:`Store.ensure_part_ref`, docs/backlog/linkable-parts.md); ``get``
then appends the ref's link ring, and keeps rendering the ref after the
daily catalog swap drops its row.
It is **ingest-only** — populated by ``precis pcb refresh-parts`` / the
``parts_refresh`` worker (JLCPCB Open API cursor walk, falling back to the
community ``jlcparts`` SQLite dump absent API credentials), never by
``put``. Selection prefers **JLCPCB-assemblable, high-turnover** parts.

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
from precis.handlers._links_render import render_links_section
from precis.pcb.catalog import min_unit_price
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils import handle_registry

log = logging.getLogger(__name__)


class PartHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="part",
        title="Part",
        description=(
            "LCSC/JLCPCB catalog part — reference data addressed "
            "by LCSC C-number (get(kind='part', id='C25804')). Ingest-only "
            "(jlcparts dump); linkable: link(target='part:C25804') mints its "
            "ref on first use. search(kind='part', q='0.1uF 0402 X7R') filters "
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
    def get(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "get(kind='part') requires id= (an LCSC C-number)",
                next="get(kind='part', id='C25804')  or  search(kind='part', q='...')",
            )
        lcsc = str(id).strip().upper()
        row = self.store.part_row(lcsc)
        ref_id = self.store.part_ref_id(lcsc)
        ref = self.store.get_ref(kind="part", id=ref_id) if ref_id is not None else None
        if row is None and ref is None:
            raise NotFound(
                f"part {lcsc} not in the catalog",
                next="the parts catalog is populated by the parts_refresh worker "
                "(precis pcb refresh-parts); search(kind='part', q='...') once "
                "it has run",
            )
        if row is not None:
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
            body = render_agent_table([payload])
        else:
            assert ref is not None
            body = (
                f"# {lcsc} — {ref.title}\n"
                f"{lcsc} is no longer in the catalog (as of the last refresh); "
                "its ref and links remain."
            )
        if ref is not None:
            # the section arrives with its own leading blank line
            body += f"\nref: {handle_registry.format_handle('part', ref.id)}"
            body += render_links_section(self.store, ref, limit=12)
        return Response(body=body)

    # ── search ───────────────────────────────────────────────────────
    def search(
        self, *, q: str | None = None, page_size: int = 20, **_kw: Any
    ) -> Response:
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='part') requires q=",
                next="search(kind='part', q='0.1uF 0402 X7R 16V')",
            )
        q = str(q).strip()
        # JLCPCB-native selector: hard-filter to assemblable parts; rank
        # Basic-first then turnover (store.parts_search).
        rows = self.store.parts_search(q, limit=page_size)
        if not rows:
            return Response(
                body=f"no assemblable parts match {q!r}\n\n"
                "Note: the parts catalog is populated by `precis pcb "
                "refresh-parts` (JLCPCB Open API, or the jlcparts dump as a "
                "fallback) — it may be empty until that has run."
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
