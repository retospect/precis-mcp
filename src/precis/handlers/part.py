"""PartHandler — the LCSC/JLCPCB catalog kind.

A ``part`` is reference data in the ``parts`` catalog table, addressed by
its LCSC **C-number**, e.g. ``get(kind='part', id='C25804')``.
It is **ingest-only** — populated by ``precis pcb refresh-parts`` / the
``parts_refresh`` worker (JLCPCB Open API cursor walk, falling back to the
community ``jlcparts`` SQLite dump absent API credentials), never by
``put``. Selection prefers **JLCPCB-assemblable, high-turnover** parts.

On a catalog miss ``get`` falls back to a **live** C-number lookup through
the JLCPCB Open API (:func:`precis.pcb.jlc_api.live_lookup`, gr485250) and
``search`` to a keyword query (:mod:`precis.pcb.jlc_library` -- the Open API
has no keyword search, the one website call left); the hit is upserted into
``parts`` on the way out. Live misses are negative-cached briefly; a wedged endpoint degrades
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

import hashlib
import json
import logging
import re
from collections.abc import Callable
from typing import Any, ClassVar

from precis.dispatch import Hub, InitError
from precis.errors import BadInput, Gone, NotFound, Upstream
from precis.format import render_agent_table
from precis.handlers._eye import is_eye_view
from precis.handlers._links_render import render_links_section
from precis.handlers._numeric_ref import BATCH_ID_CAP, BATCH_VIEW_PAGE_CHARS
from precis.pcb import catalog, jlc_api, jlc_library
from precis.pcb._http import VendorError
from precis.pcb.catalog import min_unit_price
from precis.pcb.datasheet_facts import part_facts, render_brief, render_facts
from precis.pcb.datasheets import datasheet_state, pull_in_flight
from precis.pcb.datasheets import enqueue_facts_one as queue_facts
from precis.pcb.datasheets import enqueue_one as queue_pull
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.utils import handle_registry

log = logging.getLogger(__name__)


#: Query tokens that don't name a part type — mount/package words a
#: PARTIAL row may match without being the asked-for part (gr519680:
#: 'solar cell SMD' surfaced tactile switches on 'smd' alone).
_PART_GENERIC_TERMS = frozenset(
    {
        "smd",
        "smt",
        "dip",
        "tht",
        "axial",
        "radial",
        "surface",
        "mount",
        "through",
        "hole",
        "pcs",
        "reel",
    }
)
#: Parametric / package-code fragments ('5.5v', '0.47f', '10k', '0402',
#: '16pin') — a value, never a part type.
_PART_VALUE_TOKEN = re.compile(r"^[\d.]+[a-zµΩω]*$", re.I)

#: Alternate spellings a part-type term may carry in a catalog
#: description — the PARTIAL row's must-match-one set so a solar cell
#: filed under 'photovoltaic' or 'SMD Modules' still counts (gr519680).
_PART_TYPE_SYNONYMS: dict[str, tuple[str, ...]] = {
    "solar": ("photovoltaic", "solar cell", "solar panel"),
    "photovoltaic": ("solar",),
    "supercapacitor": ("supercap", "super capacitor", "ultracapacitor", "edlc"),
    "supercap": ("supercapacitor", "super capacitor", "ultracapacitor"),
}


def _type_terms(q: str) -> list[str]:
    """The q= tokens that name a part type — everything that isn't a
    mount/package word or a parametric value — plus each one's catalog-
    description alternates. Feeds ``parts_search``'s required= set."""
    out: list[str] = []
    for tok in q.split():
        t = tok.strip(".,;:()[]").lower()
        if not t or t in _PART_GENERIC_TERMS or _PART_VALUE_TOKEN.match(t):
            continue
        if t not in out:
            out.append(t)
        for alt in _PART_TYPE_SYNONYMS.get(t, ()):
            if alt not in out:
                out.append(alt)
    return out


def _datasheet_line(state: dict[str, Any], *, in_flight: bool = False) -> str:
    """The part's datasheet: the linked ref, the recorded pull failure with
    its reason, or "not pulled yet" (``precis.pcb.datasheets``).

    ``in_flight`` names a queued/running ``datasheet_pull`` job: the job
    only writes its ``meta.datasheet_pull`` record when it *ends*, so
    without it a mid-pull part read as "not pulled yet" while the pull
    was already running — and the linked sheet could appear between this
    get and the agent's next call (gr519849)."""
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
        if in_flight:
            return (
                "datasheet: pull queued or running now — the job writes "
                "its outcome when it ends; recheck with "
                "get(kind='part', id=…) shortly"
            )
        return (
            "datasheet: not pulled yet (looking the part up queues the pull; "
            "or put(kind='datasheet', args={'url': …, 'part': …}))"
        )
    if pull.get("status") == "failed":
        line = f"datasheet: pull failed, reason {pull.get('reason')}"
        if pull.get("detail"):
            line += f" ({pull['detail']})"
        if pull.get("url"):
            line += f"; url {pull['url']}"
        return line + f"; at {pull.get('at')}"
    return f"datasheet: pulled {pull.get('at')} but the datasheet ref is gone"


def _category_keyword_paths(
    tree: list[dict[str, Any]], kw: str
) -> list[list[dict[str, Any]]]:
    """Every root→node chain whose node name contains ``kw``
    (case-insensitive) — the unmatched-id fallback for
    ``view='categories'``: 'solar' resolves to the taxonomy paths that
    hold it, which the walk can then drill."""
    out: list[list[dict[str, Any]]] = []
    needle = kw.strip().lower()
    if not needle:
        return out

    def walk(nodes: list[dict[str, Any]], chain: list[dict[str, Any]]) -> None:
        for n in nodes:
            c = [*chain, n]
            if needle in str(n["name"]).lower():
                out.append(c)
            walk(n["children"], c)

    walk(tree, [])
    return out


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
            "cheap; args={'category': 'Capacitors'} narrows to a "
            "category substring. Used by a pcb design to pick "
            "manufacturable parts. "
            "view='facts' reads the extracted datasheet pin table / "
            "required externals (precis.pcb.datasheet_facts); "
            "view='brief' is the same extraction's sourced, "
            "human-readable distillation (gr520243). "
            "view='footprint' returns the cached footprint row "
            "(pads/pin_map/courtyard/centroid/holes + a content sha) as "
            "JSON — the sandbox read path pcb.catalog() resolves "
            "through (gr514460); view='pinmap' returns only the "
            "pad-number → pin-name map, for writing connections "
            "(gr520331); view='categories' browses JLCPCB's live sort "
            "taxonomy — id='<first>/<second>' lists the leaf's parts "
            "(gr519793). See precis-part-select-help."
        ),
        supports_get=True,
        supports_search=True,
        is_numeric=False,
        id_required=False,
        views=("brief", "categories", "facts", "footprint", "pinmap"),
    )

    def accepted_views(self, *, id: Any = None) -> list[str]:
        return ["brief", "categories", "facts", "footprint", "pinmap"]

    def __init__(
        self,
        *,
        hub: Hub,
        live_lookup: Callable[..., Any] | None = None,
        live_search: Callable[..., Any] | None = None,
        live_categories: Callable[..., Any] | None = None,
    ) -> None:
        if hub.store is None:
            raise InitError("part: store required")
        self.store = hub.store
        self.hub = hub
        #: Test seams — ``live_lookup``: ``(store, lcsc) -> normalized row |
        #: None``; ``live_search``: ``(store, q, page_size=) -> rows``;
        #: ``live_categories``: ``(store, path) -> {tree, path,
        #: unmatched, parts, total}``.
        #: Defaults: :mod:`precis.pcb.jlc_api` / :mod:`precis.pcb.jlc_library`.
        self._live_lookup = live_lookup
        self._live_search = live_search
        self._live_categories = live_categories

    def _live(self, lcsc: str) -> dict[str, Any] | None:
        """The injectable live lookup; resolves to the real endpoint
        (:func:`precis.pcb.jlc_api.live_lookup`, the Open API) when no test double
        was injected."""
        fn = self._live_lookup or jlc_api.live_lookup
        return fn(self.store, lcsc)

    # ── get ──────────────────────────────────────────────────────────
    def get(
        self,
        *,
        id: str | int | list[str | int] | None = None,
        view: str | None = None,
        **_kw: Any,
    ) -> Response:
        if isinstance(id, (list, tuple)):
            return self._get_batch(list(id), view=view)
        if view is not None:
            if is_eye_view(view):
                return self.eye(id=id, extent=view)
            if view not in ("brief", "categories", "facts", "footprint", "pinmap"):
                raise BadInput(
                    f"kind='part' has no view {view!r}",
                    next="get(kind='part', id='C…', view='facts') — the "
                    "datasheet-extracted pin table / required externals; "
                    "view='brief' is its sourced human-readable "
                    "distillation; view='footprint' is the cached "
                    "footprint row as JSON; "
                    "view='pinmap' is only its pad-number → pin-name map; "
                    "view='categories' walks JLCPCB's sort taxonomy "
                    "(id='Capacitors/Supercapacitors' lists its parts)",
                )
            if view == "categories":
                return self._categories_view(id)
            if id is None or not str(id).strip():
                raise BadInput(
                    f"get(kind='part', view={view!r}) requires id= (an LCSC C-number)",
                    next=f"get(kind='part', id='C25804', view={view!r})",
                )
            lcsc = catalog.lcsc_id(id) or str(id).strip().upper()
            if view == "facts":
                return Response(
                    body=render_facts(part_facts(self.store, lcsc), lcsc=lcsc)
                )
            if view == "brief":
                return Response(
                    body=render_brief(part_facts(self.store, lcsc), lcsc=lcsc)
                )
            if view == "pinmap":
                return self._pinmap_view(lcsc)
            return self._footprint_view(lcsc)
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
        if row is not None and catalog.lcsc_id(lcsc) is not None:
            # datasheets come before a design uses the part (gr492689);
            # the facts read queues next to the pull — opening a part is
            # the lazy door prose mentions defer to (gr520243).
            job = self.hub.sibling("job")
            queue_pull(self.store, job, lcsc)
            queue_facts(self.store, job, lcsc)
        body += "\n" + _datasheet_line(
            datasheet_state(self.store, lcsc),
            in_flight=pull_in_flight(self.store, lcsc),
        )
        if ref is not None:
            # the section arrives with its own leading blank line
            body += f"\nref: {handle_registry.format_handle('part', ref.id)}"
            body += render_links_section(self.store, ref, limit=12)
        facts = part_facts(self.store, lcsc)
        if facts is not None and facts.brief is not None:
            # gr520243: the brief is the default view once it exists.
            body += "\n\n" + render_brief(facts, lcsc=lcsc)
        return Response(body=body)

    def _get_batch(self, ids: list[Any], *, view: str | None) -> Response:
        """``get(id=[...])``: the single-id read mapped over every id, in
        the order given (gr520100).

        The shared batch path (``_numeric_ref._get_batch``) covers the
        integer-id ref kinds; a part's public id is its LCSC C-number —
        catalog data, not a ``refs`` row — so the batch lives here. Each
        element takes the same spellings the singleton form does (``C…``
        any case, a pasted lcsc.com/jlcpcb.com URL); a miss is a one-line
        ``not found`` block, never a dropped id or an error for the whole
        call. Whole blocks page under :data:`BATCH_VIEW_PAGE_CHARS`,
        same as the numeric batch's view form — the unread tail is named
        as the ``next:`` call's id list.
        """
        if not ids:
            raise BadInput(
                "id=[] is empty",
                next="get(kind='part', id='C25804')  or  id=['C…', 'C…']",
            )
        if len(ids) > BATCH_ID_CAP:
            raise BadInput(
                f"id=[...] takes at most {BATCH_ID_CAP} ids, got {len(ids)}",
                next=f"split into calls of {BATCH_ID_CAP} or fewer",
            )
        lcses: list[str] = []
        for pos, raw in enumerate(ids):
            if isinstance(raw, bool) or not isinstance(raw, (str, int)):
                raise BadInput(
                    f"id[{pos}] must be a C-number string, got {type(raw).__name__}",
                    next="get(kind='part', id=['C25804', ...])",
                )
            lcsc = catalog.lcsc_id(raw) or str(raw).strip().upper()
            if lcsc not in lcses:
                lcses.append(lcsc)
        blocks: list[str] = []
        for lcsc in lcses:
            try:
                blocks.append(self.get(id=lcsc, view=view).body)
            except NotFound:
                blocks.append(f"{lcsc}  not found")
            except Gone:
                blocks.append(f"{lcsc}  deleted")
        shown: list[str] = []
        used = 0
        for block in blocks:
            if shown and used + len(block) > BATCH_VIEW_PAGE_CHARS:
                break
            shown.append(block)
            used += len(block) + 2  # the "\n\n" join
        rest = lcses[len(shown) :]
        body = f"# {len(lcses)} parts (batch"
        body += f", view={view!r})\n\n" if view is not None else ")\n\n"
        body += "\n\n".join(shown)
        if rest:
            body += (
                f"\n\n{len(rest)} more — next: get(kind='part', "
                f"id=[{', '.join(repr(c) for c in rest)}])"
            )
        return Response(body=body, result_count=len(shown))

    def _categories_view(self, id: Any) -> Response:
        """``view='categories'`` (gr519793) — JLCPCB's first/second-sort
        taxonomy, browsed live through the parts listing's
        ``sortAndCountVoList`` envelope (the Open API has no taxonomy).
        ``id`` is a ``'/'``-joined sort-name path; a clean walk to a
        leaf lists that category's live parts (catalogued on the way
        out, like a ``live_search`` hit). An unmatched tail
        substring-matches every category name — the "which category do
        solar cells live in" discovery path. The whole call is live, so
        a wedged endpoint is an :class:`Upstream` refusal, not a stale
        answer."""
        path = str(id).strip().strip("/") if id is not None else ""
        fn = self._live_categories or jlc_library.browse_categories
        try:
            res = fn(self.store, path or None)
        except VendorError as exc:
            raise Upstream(
                f"the live jlcpcb.com category browse failed ({exc})",
                next="retry the same call — the tree is one bounded "
                "request to the parts-listing endpoint",
            ) from exc
        tree: list[dict[str, Any]] = res["tree"]
        if not path:
            rows = [
                {
                    "first sort": n["name"],
                    "id": n["sort_id"],
                    "second sorts": len(n["children"]),
                    "parts": n["count"],
                }
                for n in tree
            ]
            return Response(
                body="# JLCPCB part categories — the live sort taxonomy "
                "jlcpcb.com browses\n"
                + render_agent_table(
                    rows, schema=["first sort", "id", "second sorts", "parts"]
                )
                + "\nnext: get(kind='part', view='categories', id='<first "
                "sort>') drills in; id='<first>/<second>' lists the leaf's "
                "live parts; an unmatched name substring-matches every "
                "category (id='solar')"
            )
        matched: list[dict[str, Any]] = res["path"]
        unmatched: str = res["unmatched"]
        if unmatched or not matched:
            kw = unmatched or path
            hits = _category_keyword_paths(tree, kw)
            if hits:
                lines = [f"# JLCPCB categories matching {kw!r}", ""]
                for chain in hits[:20]:
                    p = " / ".join(n["name"] for n in chain)
                    lines.append(f"- {p} — {chain[-1]['count']} parts")
                if len(hits) > 20:
                    lines.append(f"_(+{len(hits) - 20} more)_")
                lines.append(
                    "\nnext: get(kind='part', view='categories', "
                    "id='<path>') — a leaf path lists live parts"
                )
                return Response(body="\n".join(lines))
            where = " / ".join(n["name"] for n in matched)
            raise BadInput(
                f"no JLCPCB category matches {kw!r}"
                + (f" under {where}" if where else ""),
                options=(
                    [c["name"] for c in matched[-1]["children"]][:10]
                    if matched
                    else [n["name"] for n in tree][:10]
                ),
                next="get(kind='part', view='categories') — walk the "
                "tree top-down instead",
            )
        node = matched[-1]
        where = " / ".join(n["name"] for n in matched)
        kids = node["children"]
        if kids:
            rows = [
                {"second sort": c["name"], "id": c["sort_id"], "parts": c["count"]}
                for c in kids
            ]
            return Response(
                body=f"# {where} — {len(kids)} subcategories, "
                f"{node['count']} parts\n"
                + render_agent_table(rows, schema=["second sort", "id", "parts"])
                + f"\nnext: get(kind='part', view='categories', "
                f"id='{where}/<child>')"
            )
        rows = [
            {
                "lcsc": r["lcsc"],
                "mfr_part": r["mfr_part"] or "—",
                "description": (r["description"] or "")[:50],
                "basic": "yes" if r["basic"] else "no",
                "stock": r["stock"] if r["stock"] is not None else "—",
                "package": r["package"] or "—",
                "$ea": (
                    f"{unit_price:.4g}"
                    if (
                        unit_price := (
                            None
                            if catalog.has_no_buy(r.get("params"))
                            else min_unit_price(r.get("price"))
                        )
                    )
                    is not None
                    else "—"
                ),
            }
            for r in res["parts"]
        ]
        return Response(
            body=f"# {where} — {res['total']} part(s) live on jlcpcb.com, "
            f"{len(rows)} shown (catalogued just now)\n"
            + render_agent_table(
                rows,
                schema=[
                    "lcsc",
                    "mfr_part",
                    "description",
                    "basic",
                    "stock",
                    "package",
                    "$ea",
                ],
            )
            + "\nnext: get(kind='part', id='<C-no>') for one of these, or "
            "search(kind='part', q='...')"
        )

    def _footprint_view(self, lcsc: str) -> Response:
        """``view='footprint'`` — the cached ``part_footprints`` row as a
        JSON body (the sandbox's ``precis_client`` parses it): the
        ``normalize_footprint`` row shape plus ``package``/``height_mm``
        off ``parts``, the part's ``pn<ref>`` when it has one, and
        ``sha`` = sha256 of the canonical JSON of the geometry fields —
        the manifest's ``footprint_sha`` ("what was actually read",
        gr514460)."""
        row = self.store.part_footprint_get(lcsc)
        if row is None:
            raise NotFound(
                f"part {lcsc}: no cached footprint",
                next=(
                    f"put(kind='pcb', args={{'op': 'footprint', 'part': {lcsc!r}}})"
                    " fetches and caches it, or search(kind='part', q='...')"
                    " for another part"
                ),
            )
        geometry = {
            k: row.get(k) for k in ("pads", "pin_map", "courtyard", "centroid", "holes")
        }
        part = self.store.part_row(lcsc) or {}
        ref_id = self.store.part_ref_id(lcsc)
        payload = {
            "lcsc": lcsc,
            "part_id": (
                handle_registry.format_handle("part", ref_id)
                if ref_id is not None
                else None
            ),
            "package": part.get("package"),
            "height_mm": part.get("height_mm"),
            **geometry,
            "sha": hashlib.sha256(
                json.dumps(geometry, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest(),
        }
        return Response(body=json.dumps(payload, sort_keys=True))

    def _pinmap_view(self, lcsc: str) -> Response:
        """``view='pinmap'`` (gr520331) — only the pad-number → pin-name
        map off the cached footprint row, for a board-script author
        writing connections; the full pad geometry stays on
        ``view='footprint'``. Same JSON shape the sandbox parses."""
        row = self.store.part_footprint_get(lcsc)
        if row is None:
            raise NotFound(
                f"part {lcsc}: no cached footprint",
                next=(
                    f"put(kind='pcb', args={{'op': 'footprint', 'part': {lcsc!r}}})"
                    " fetches and caches it, or search(kind='part', q='...')"
                    " for another part"
                ),
            )
        pin_map = {
            str(pad): (
                str(e.get("name") or "") if isinstance(e, dict) else str(e or "")
            )
            for pad, e in (row.get("pin_map") or {}).items()
        }
        return Response(
            body=json.dumps({"lcsc": lcsc, "pin_map": pin_map}, sort_keys=True)
        )

    # ── search ───────────────────────────────────────────────────────
    def search(
        self,
        *,
        q: str | None = None,
        category: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='part') requires q=",
                next="search(kind='part', q='0.1uF 0402 X7R 16V')",
            )
        q = str(q).strip()
        category = str(category).strip() if category is not None else None
        # JLCPCB-native selector: hard-filter to assemblable parts; rank
        # Basic-first then turnover (store.parts_search).
        rows = self.store.parts_search(q, limit=page_size, category=category)
        # gr520325: soft-only rows are near-misses, not a hit — a query
        # with no row satisfying every soft term still reaches the live
        # lookup, as it did before the terms went soft. The store returns
        # every full match in its fetched window (even past the page
        # cut), so ``any(all_soft)`` here answers for the window, not
        # just the page.
        near_miss = bool(rows) and not any(r.get("all_soft", True) for r in rows)
        if not rows:
            # A C-number query is a point lookup — the selector's
            # description-only tsv never matches one.
            direct = catalog.lcsc_id(q)
            if direct is not None and category is None:
                hit = self.store.part_row(direct)
                if hit is not None:
                    rows = [hit]
        partial = False
        if not rows and len(q.split()) > 1:
            # No part matches every word: rank by terms matched, in-stock
            # first, so '16pin' vs a catalog '16P' still finds the part. A
            # single token (an MPN like TYPE-C-31-M-12 tokenizes into common
            # fragments) goes to the live lookup instead. The required=
            # set keeps a row only when it matched a part-type term
            # (gr519680): 'solar cell SMD' no longer surfaces tactile
            # switches that matched 'smd' alone — it falls through to the
            # live search.
            rows = self.store.parts_search(
                q,
                limit=page_size,
                partial=True,
                required=_type_terms(q),
                category=category,
            )
            partial = bool(rows)
        live_rows: list[dict[str, Any]] | None = None
        live_err: str | None = None
        if not rows or near_miss:
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
        # Live-merge ruling (gr520325 r2): when no local row satisfies
        # every soft term and the live lookup answers, the results MERGE —
        # live hits first, then the catalog near-misses, each labelled
        # with the soft term it failed. Local rows are never discarded.
        live_sorted = sorted(live_rows or [], key=lambda r: not (r["stock"] or 0))
        if near_miss:
            # A live hit that is already a catalog row would render as a
            # duplicate line — the local near-miss keeps its label.
            local_lcsc = {r["lcsc"] for r in rows}
            live_sorted = [r for r in live_sorted if r["lcsc"] not in local_lcsc]
        merged = near_miss and bool(live_sorted)
        shown = (live_sorted + rows) if merged else (rows or live_sorted)
        for r in shown:
            # a no-buy listing's ladder is a placeholder, not a price (gr519681)
            price = (
                None
                if r.get("no_buy") or catalog.has_no_buy(r.get("params"))
                else min_unit_price(r.get("price"))
            )
            desc = (r["description"] or "")[:50]
            miss = r.get("soft_miss")
            if miss:
                desc += " — near miss: " + " ".join(miss).upper()
            out.append(
                {
                    "lcsc": r["lcsc"],
                    "mfr_part": r["mfr_part"] or "—",
                    "description": desc,
                    "basic": "yes" if r["basic"] else "no",
                    "stock": r["stock"] if r["stock"] is not None else "—",
                    "restocks": r.get("restock_count", "—"),
                    "package": r["package"] or "—",
                    "$ea": f"{price:.4g}" if price is not None else "—",
                }
            )
        head = (
            f"# {len(out)} PARTIAL match(es) for {q!r} — no part matches every "
            "term; ranked in-stock first, then by terms matched"
            if partial
            else f"# {len(out)} part(s) for {q!r} — {len(live_sorted)} LIVE "
            "from jlcpcb.com's listing just now (catalogued, not yet "
            "turnover-ranked) + the nearest catalog rows, no local part "
            "satisfying every term"
            if merged
            else f"# {len(out)} assemblable part(s) for {q!r} "
            "(in stock, Basic + high-turnover first)"
            if rows
            else f"# {len(out)} LIVE part(s) for {q!r} — fetched from "
            "jlcpcb.com's listing just now and catalogued (not yet ranked by "
            "turnover; these include Extended parts the catalog lacks)"
        )
        tail = ""
        if near_miss and not live_rows:
            # The full ask missed locally AND live — the near-misses are
            # still the best answer; name the live outcome.
            tail = (
                f"\n\n(the live jlcpcb.com lookup failed: {live_err})"
                if live_err is not None
                else "\n\n(the live jlcpcb.com listing found nothing "
                "either — these are the nearest catalog rows)"
            )
        elif near_miss and live_rows and not live_sorted:
            # Live answered but every hit was already a catalog row —
            # the dedup emptied the merge; still name the live outcome.
            tail = f"\n\n(live lookup: {len(live_rows)} hit(s), all already listed)"
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
            + tail
        )


__all__ = ["PartHandler"]
