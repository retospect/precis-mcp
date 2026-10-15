"""PartHandler — the LCSC/JLCPCB catalog kind.

A ``part`` is reference data in the ``parts`` catalog table, addressed by
its LCSC **C-number**, e.g. ``get(kind='part', id='C25804')``.
It is **ingest-only** — populated by ``precis pcb refresh-parts`` / the
``parts_refresh`` worker (JLCPCB Open API cursor walk, falling back to the
community ``jlcparts`` SQLite dump absent API credentials), never by
``put``. Selection prefers **JLCPCB-assemblable, high-turnover** parts.

On a catalog miss ``get``/``search`` fall back to a **live** query against
JLCPCB's public parts-listing endpoint (:mod:`precis.pcb.jlc_library`,
gr485250): the dump lacks the Extended range below ~C6.37M and beyond, so a
C-number the catalog doesn't know (or a free-text query with no local hit)
is asked of the site directly and the hit is upserted into ``parts`` on the
way out. Live misses are negative-cached briefly; a wedged endpoint degrades
to the catalog's own answer with the failure noted.

Slice 1 ships read-only access (``get`` one part, ``search`` the catalog) over
whatever the importer has loaded; the turnover ranking + lazy
``easyeda2kicad`` footprint fetch land in Slice 2.

**A part in the mesh** (Reto 2026-10-02, knowledge-mesh-6: lazy part
refs; items inside a design stay addressed through their design). The
catalog is ~300k (v1 dump) to ~2M (v2 dump) rows replaced daily, so a part becomes a ref only on
first use:

- Identity: ``refs(kind='part', slug=<C-no>)`` plus
  ``ref_identifiers('lcsc', <C-no>)``, chunkless, titled from the catalog
  row at mint time; handle ``pn<ref_id>``.
  :meth:`Store.ensure_part_ref` finds or mints it, race-safe on the
  ``lcsc`` identifier's primary key. It mints only for a C-number present
  in ``parts``; an existing ref is returned whether or not its row is
  still there.
- Mint doors: the add-mode generic link doors (``apply_link_ops``,
  ``NumericRefHandler.link``) through
  :func:`~precis.utils.link_target.mint_lazy_link_target`.
  ``parse_link_target`` never mints, because it also serves unlink,
  ``like=`` and put-time ``link=``; its ``NotFound`` for an unminted part
  names ``link()``.
- ``component realized-by part`` goes through ``ComponentHandler.link``,
  which refuses ``made-of``/``contains`` (they keep their ``put`` doors).
- A datasheet's ``part_lcsc`` dual-writes ``meta.part_lcsc`` (nanopub,
  docx, latex read it) and ``datasheet-of``; outside the catalog with no
  part ref it keeps the meta and skips the edge.
- A board ``contains`` one part ref per placed C-number, with
  ``links.meta = {refdes, qty}``, reconciled at the end of every
  ``pcb_apply`` (``PcbMixin._pcb_reconcile_part_edges``) and backfilled by
  ``precis pcb link-parts``. A C-number outside the catalog gets no edge
  unless a part ref already exists for it (the ``datasheet_pull`` job mints
  one, flagged ``meta.uncatalogued``, to record a failed pull); revisit if a hand-authored board needs a dropped part (the alternative
  is minting from the board's own snapshot).
- ``get`` appends the ref's ring; after the catalog swap drops the row it
  renders the ref with "no longer in the catalog" instead of ``NotFound``.
- A part's datasheet is pulled automatically when a board uses its C-number
  (``precis.pcb.datasheets``; the ``datasheet_pull`` job): ``get`` shows the
  linked ``datasheet-of`` datasheet, or the recorded pull failure and its
  reason. A failed pull mints the part ref even for a C-number outside the
  catalog, so the reason has a place to live.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._eye import is_eye_view
from precis.handlers._links_render import render_links_section
from precis.pcb import catalog, jlc_library
from precis.pcb._http import VendorError
from precis.pcb.catalog import min_unit_price
from precis.pcb.datasheet_facts import part_facts, render_facts
from precis.pcb.datasheets import datasheet_state
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils import handle_registry

log = logging.getLogger(__name__)


def _datasheet_line(state: dict[str, Any]) -> str:
    """The part's datasheet: the linked ref, the recorded pull failure with
    its reason, or "not pulled yet" (``precis.pcb.datasheets``)."""
    ds = state["datasheet"]
    if ds is not None:
        slug = ds[1] or ds[0]
        line = f"datasheet: {slug}  get(kind='datasheet', id='{slug}')"
        pull = state["pull"]
        if pull is not None and pull.get("status") == "failed":
            line += f"; pull failed, reason {pull.get('reason')}"
            if pull.get("detail"):
                line += f" ({pull['detail']})"
        return line
    pull = state["pull"]
    if pull is None:
        return (
            "datasheet: not pulled yet (a pcb design that uses this part "
            "queues the pull)"
        )
    if pull.get("status") == "failed":
        line = f"datasheet: pull failed, reason {pull.get('reason')}"
        if pull.get("detail"):
            line += f" ({pull['detail']})"
        if pull.get("url"):
            line += f"; url {pull['url']}"
        return line + f"; at {pull.get('at')}"
    return f"datasheet: pulled {pull.get('at')} but the datasheet ref is gone"


class PartHandler(Handler):
    #: catalogue part numbers / vendor ids: the agent-write secret gate (dispatch) skips this kind.
    stores_opaque_text: ClassVar[bool] = True
    spec: ClassVar[KindSpec] = KindSpec(
        kind="part",
        title="Part",
        description=(
            "LCSC/JLCPCB catalog part — reference data addressed "
            "by LCSC C-number (get(kind='part', id='C25804'); a pasted "
            "lcsc.com/jlcpcb.com product URL works too). Catalog-loaded "
            "(jlcparts dump + JLCPCB library top-up); a catalog miss on a "
            "C-number is looked up LIVE on jlcpcb.com and catalogued, and "
            "search falls back to the same live keyword search. Linkable: "
            "link(target='part:C25804') mints its ref on first use. "
            "search(kind='part', q='0.1uF 0402 X7R') filters "
            "to JLCPCB-assemblable parts and prefers Basic + high-turnover + "
            "cheap. Used by a pcb design to pick manufacturable parts. "
            "view='facts' reads the extracted datasheet pin table / "
            "required externals (precis.pcb.datasheet_facts). "
            "See precis-part-select-help."
        ),
        supports_get=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,
        views=("facts",),
    )

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return ["facts"]

    def __init__(
        self,
        *,
        hub: Hub,
        live_lookup: Callable[..., Any] | None = None,
        live_search: Callable[..., Any] | None = None,
    ) -> None:
        if hub.store is None:
            raise InitError("part: store required")
        self.store = hub.store
        #: Test seams — ``live_lookup``: ``(store, lcsc) -> normalized row |
        #: None``; ``live_search``: ``(store, q, page_size=) -> rows``.
        #: Both default to the real endpoint (:mod:`precis.pcb.jlc_library`).
        self._live_lookup = live_lookup
        self._live_search = live_search

    def _live(self, lcsc: str) -> dict[str, Any] | None:
        """The injectable live lookup; resolves to the real endpoint
        (:func:`precis.pcb.jlc_library.live_lookup`) when no test double
        was injected."""
        fn = self._live_lookup or jlc_library.live_lookup
        return fn(self.store, lcsc)

    # ── get ──────────────────────────────────────────────────────────
    def get(
        self, *, id: str | int | None = None, view: str | None = None, **_kw: Any
    ) -> Response:
        if view is not None:
            if is_eye_view(view):
                return self.eye(id=id, extent=view)
            if view != "facts":
                raise BadInput(
                    f"kind='part' has no view {view!r}",
                    next="get(kind='part', id='C…', view='facts') — the "
                    "datasheet-extracted pin table / required externals",
                )
            if id is None or not str(id).strip():
                raise BadInput(
                    "get(kind='part', view='facts') requires id= (an LCSC C-number)"
                )
            lcsc = catalog.lcsc_id(id) or str(id).strip().upper()
            return Response(body=render_facts(part_facts(self.store, lcsc), lcsc=lcsc))
        if id is None or not str(id).strip():
            raise BadInput(
                "get(kind='part') requires id= (an LCSC C-number)",
                next="get(kind='part', id='C25804')  or  search(kind='part', q='...')",
            )
        lcsc = catalog.lcsc_id(id) or str(id).strip().upper()
        row = self.store.part_row(lcsc)
        live_note: str | None = None
        if row is None and catalog.lcsc_id(lcsc) is not None:
            # Catalog miss on a C-number: ask the live listing endpoint
            # (gr485250 — the dump lacks the Extended range). A hit is
            # upserted into ``parts`` by the lookup itself.
            try:
                if self._live(lcsc) is not None:
                    row = self.store.part_row(lcsc)
                else:
                    live_note = (
                        "the live jlcpcb.com parts listing was checked too — "
                        f"no {lcsc} there either"
                    )
            except VendorError as exc:
                live_note = f"the live jlcpcb.com lookup failed ({exc})"
        ref_id = self.store.part_ref_id(lcsc)
        ref = self.store.get_ref(kind="part", id=ref_id) if ref_id is not None else None
        if row is None and ref is None:
            raise NotFound(
                f"part {lcsc} not in the catalog",
                next=[
                    *([live_note] if live_note else []),
                    "search(kind='part', q='...') for alternatives "
                    "(live-searched too on a catalog miss)",
                ],
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
            if (ref.meta or {}).get("uncatalogued"):
                gone = f"{lcsc} is not in the parts catalog (no catalogue row yet); "
            else:
                gone = f"{lcsc} is no longer in the catalog (as of the last refresh); "
            body = f"# {lcsc} — {ref.title}\n{gone}its ref and links remain."
        body += "\n" + _datasheet_line(datasheet_state(self.store, lcsc))
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
            # A C-number query is a point lookup — the selector's
            # description-only tsv never matches one.
            direct = catalog.lcsc_id(q)
            if direct is not None:
                hit = self.store.part_row(direct)
                if hit is not None:
                    rows = [hit]
        live_rows: list[dict[str, Any]] | None = None
        live_err: str | None = None
        if not rows:
            # Live fallback (gr485250): one bounded keyword query against
            # the public listing endpoint; hits are catalogued on the way
            # out. A wedged endpoint degrades to the catalog's own answer
            # with the failure named, never to a hang.
            fn = self._live_search or jlc_library.live_search
            try:
                live_rows = fn(self.store, q, page_size=min(page_size, 25))
            except VendorError as exc:
                live_err = str(exc)
        if not rows and not live_rows:
            note = f"no assemblable parts match {q!r}"
            if live_err is not None:
                note += f"; the live jlcpcb.com lookup failed ({live_err})"
            elif live_rows is not None:
                note += " — the live jlcpcb.com listing found nothing either"
            return Response(
                body=note
                + "\n\n"
                + "Note: the parts catalog is populated by `precis pcb "
                "refresh-parts` (JLCPCB Open API, or the jlcparts dump as a "
                "fallback) — it may be empty until that has run."
            )
        out = []
        for r in rows or live_rows or []:
            price = min_unit_price(r.get("price"))
            out.append(
                {
                    "lcsc": r["lcsc"],
                    "mfr_part": r["mfr_part"] or "—",
                    "description": (r["description"] or "")[:50],
                    "basic": "yes" if r["basic"] else "no",
                    "stock": r["stock"] if r["stock"] is not None else "—",
                    "restocks": r.get("restock_count", "—"),
                    "package": r["package"] or "—",
                    "$ea": f"{price:.4g}" if price is not None else "—",
                }
            )
        head = (
            f"# {len(out)} assemblable part(s) for {q!r} (Basic + high-turnover first)"
            if rows
            else f"# {len(out)} LIVE part(s) for {q!r} — fetched from "
            "jlcpcb.com's listing just now and catalogued (not yet ranked by "
            "turnover; these include Extended parts the catalog lacks)"
        )
        return Response(
            body=head
            + "\n"
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
