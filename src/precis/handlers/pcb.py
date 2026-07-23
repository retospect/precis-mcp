"""PcbHandler — the electronics / PCB design kind (ADR 0042).

A ``pcb`` design is a slug-addressed ref whose graph lives in the dedicated
``pcb_*`` tables (components+pins / instances / nets / netconns). The agent
**authors it in batch** and **reads it as a traversable graph** — never
pixels. The verbs map onto the seven-verb surface:

- ``put``    — create / extend a design (``id=`` slug; ``args={components,
  nets, connections}`` — see :meth:`PcbHandler.put`). Re-runnable.
- ``get``    — list designs (no id); a design's netlist TOC (``id=slug``); one
  instance's neighbourhood (``id='slug#U3'`` — its pins, the net on each, and
  the connected instances); a net's members (``id='slug@SCL'``); the *eyes*
  (``view='crossings'|'ratsnest'|'drc'|'trace'|'proximity'|'measures'|
  'feasibility'``); or an *export* (``view='bom'|'cpl'|'netlist'|'dsn'|
  'mechanical'`` writes a JLCPCB fab artifact; ``view='route'`` runs the
  Freerouting place↔route round-trip — :mod:`precis.pcb.export` /
  :mod:`precis.pcb.route`).
- ``search`` — over design names + descriptions (the one summary card).
- ``delete`` — soft-retire a whole design.

Routing (Freerouting) and gerbers are *rented* downstream of the IR; export is
the only place the design leaves the relational graph. See ``precis-pcb-help``.
"""

from __future__ import annotations

import json
import logging
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, ClassVar

from precis.config import load_config
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.pcb import export as pcb_export
from precis.pcb import eyes, place, ratsnest
from precis.pcb import route as pcb_route
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit

log = logging.getLogger(__name__)

#: The "eyes" — analytic, computed-on-read (Slice 4/5).
_PROBE_VIEWS = (
    "crossings",
    "ratsnest",
    "drc",
    "trace",
    "proximity",
    "measures",
    "feasibility",
)
#: Pure file exporters off the IR (Slice 6).
_EXPORT_VIEWS = ("bom", "cpl", "netlist", "dsn", "mechanical")
#: The rented autorouter round-trip (Slice 6, gated on Freerouting).
_ROUTE_VIEWS = ("route",)
_OTHER_VIEWS = ("links",)
_VIEWS = (*_PROBE_VIEWS, *_EXPORT_VIEWS, *_ROUTE_VIEWS, *_OTHER_VIEWS)


class PcbHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="pcb",
        title="PCB",
        description=(
            "Electronics/PCB design (ADR 0042) — a netlist + placement graph "
            "the LLM authors in batch and reads as a traversable graph, never "
            "pixels. put creates/extends a design (id=slug, args={components:"
            "[{refdes,label,part?,pins:[{name,pad?,tags?}],x?,y?,layer?,roles?}],"
            " nets:[{name,class?,current?}], connections:[{net,refdes,pin}]}); "
            "get lists designs, a design's netlist TOC (id=slug), one "
            "instance's neighbourhood (id='slug#U3'), a net's members "
            "(id='slug@SCL'), the eyes (view='crossings'|'ratsnest'|'drc'|"
            "'trace'|'proximity'|'measures'|'feasibility'), or an export "
            "(view='bom'|'cpl'|'netlist'|'dsn'|'mechanical' writes a JLCPCB "
            "fab artifact; view='route' runs the Freerouting place↔route "
            "round-trip), all with args={...}; "
            "put(args={'autoplace':{'iters':N}}) auto-places to minimise "
            "crossings (fixed parts pinned); search over names; delete "
            "soft-retires. "
            "Postgres-canonical; routing/gerbers are downstream export. "
            "See precis-pcb-help."
        ),
        supports_get=True,
        supports_put=True,
        supports_search=True,
        supports_search_hits=True,
        supports_delete=True,
        is_numeric=False,
        id_required=False,
        views=_VIEWS,
    )

    def __init__(self, *, hub: Hub) -> None:
        if hub.store is None:
            raise InitError("pcb: store required")
        self.store = hub.store
        self.embedder = hub.embedder

    # ── put ──────────────────────────────────────────────────────────
    def put(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        title: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput(
                "put(kind='pcb') requires id= (the design slug)",
                next=(
                    "put(kind='pcb', id='sensor-node', args={'components': "
                    "[{'refdes':'U1','label':'ESP32-C3','pins':[{'name':'SCL'},"
                    "{'name':'SDA'}]}], 'nets':[{'name':'I2C_SCL','class':'i2c'}],"
                    " 'connections':[{'net':'I2C_SCL','refdes':'U1','pin':'SCL'}]})"
                ),
            )
        slug = str(id).strip()
        args = args or {}
        components = list(args.get("components") or [])
        nets = list(args.get("nets") or [])
        connections = list(args.get("connections") or [])
        measures = list(args.get("measures") or [])
        features = list(args.get("features") or [])
        autoplace = args.get("autoplace")
        meta = args.get("meta") if isinstance(args.get("meta"), dict) else None
        ttl = (title or slug).strip() or slug

        try:
            ref, created, counts = self.store.pcb_apply(
                slug=slug,
                title=ttl,
                components=components,
                nets=nets,
                connections=connections,
                measures=measures,
                features=features,
                meta=meta,
            )
        except ValueError as exc:
            raise BadInput(f"pcb: {exc}") from exc

        if autoplace:
            return self._autoplace(
                ref, autoplace if isinstance(autoplace, dict) else {}
            )

        verb = "created" if created else "extended"
        design = self.store.pcb_load(ref.id)
        extra = f", +{counts['measures']} measure(s)" if counts["measures"] else ""
        if counts["features"]:
            extra += f", +{counts['features']} feature(s)"
        head = (
            f"# {slug} — {verb}: +{counts['components']} part(s), "
            f"+{counts['nets']} net(s), +{counts['conns']} conn(s){extra}  "
            f"(now {len(design['instances'])} part(s), {len(design['nets'])} net(s))"
        )
        return Response(body=head + "\n" + self._toc(design))

    # ── get ──────────────────────────────────────────────────────────
    def get(  # type: ignore[override]
        self,
        *,
        id: str | int | None = None,
        view: str | None = None,
        args: dict[str, Any] | None = None,
        **_kw: Any,
    ) -> Response:
        if id is None or (isinstance(id, str) and id.strip() in ("", "/")):
            return self._render_list()
        s = str(id).strip()

        # sub-paths: slug#REFDES (instance neighbourhood) / slug@NET (net members)
        if "#" in s:
            slug, refdes = s.split("#", 1)
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug.strip())
            return self._render_instance(ref.id, refdes.strip())
        if "@" in s:
            slug, net = s.split("@", 1)
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug.strip())
            return self._render_net(ref.id, net.strip())

        ref = resolve_live_slug_ref(self.store, kind="pcb", id=s)
        if view is not None:
            return self._render_view(ref.id, view, args or {})
        design = self.store.pcb_load(ref.id)
        head = (
            f"# {ref.slug} — {len(design['instances'])} part(s), "
            f"{len(design['nets'])} net(s)"
        )
        return Response(body=head + "\n" + self._toc(design))

    # ── auto-place (ADR 0042 §9) ─────────────────────────────────────
    def _place_and_store(
        self,
        ref_id: int,
        *,
        iters: int,
        seed: int,
        measures: list[dict[str, Any]],
    ) -> tuple[place.PlaceResult, int]:
        """Anneal over the current graph and persist the placement + the
        ``last_place`` meta stamp. Shared by the put-time autoplace and the
        route round-trip's per-pass re-place (one copy of the store logic)."""
        graph = self.store.pcb_graph(ref_id)
        res = place.autoplace(
            graph["instances"],
            graph["nets"],
            measures=measures,
            iters=iters,
            seed=seed,
        )
        moved = self.store.pcb_set_placement(
            ref_id,
            res.positions,
            meta={
                "last_place": {
                    "crossings": res.crossings_after,
                    "length_mm": round(res.length_after, 2),
                    "objective": round(res.objective_after, 2),
                    "iters": res.iters,
                }
            },
        )
        return res, moved

    def _autoplace(self, ref: Any, opts: dict[str, Any]) -> Response:
        measures = self.store.pcb_measures_list(ref.id)
        res, moved = self._place_and_store(
            ref.id,
            iters=int(opts.get("iters") or 1500),
            seed=int(opts.get("seed") or 0),
            measures=measures,
        )
        return Response(
            body=(
                f"# autoplaced {ref.slug} — {moved} part(s) moved, {res.iters} iters\n"
                f"crossings: {res.crossings_before} → {res.crossings_after}\n"
                f"ratsnest:  {res.length_before:g} → {res.length_after:g} mm\n"
                f"objective: {res.objective_before:g} → {res.objective_after:g}\n"
                "Next: get(view='crossings') to inspect, get(view='feasibility') "
                "for the H/V via estimate, or pin parts with fixed= and re-run."
            )
        )

    # ── the eyes (ADR 0042 §8) ───────────────────────────────────────
    def _render_view(self, ref_id: int, view: str, args: dict[str, Any]) -> Response:
        if view not in _VIEWS:
            raise BadInput(
                f"unknown pcb view {view!r}",
                next=f"view= one of {list(_VIEWS)}, or omit for the netlist TOC",
            )
        if view in _EXPORT_VIEWS:
            return self._render_export(ref_id, view, args)
        if view in _ROUTE_VIEWS:
            return self._render_route(ref_id, args)
        if view == "links":
            # Graph-completeness audit item 1 (OPEN-ITEMS.md 🕸️) — sweep of
            # every Handler-direct kind alongside the paper fix.
            from precis.handlers._links_render import render_links_view

            ref = self.store.fetch_refs_by_ids([ref_id]).get(ref_id)
            if ref is None:
                raise NotFound(f"pcb id={ref_id} not found")
            return render_links_view(self.store, ref, sense="pcb")
        graph = self.store.pcb_graph(ref_id)
        if view in ("crossings", "ratsnest", "feasibility"):
            placed = {
                i["refdes"]: (float(i["x"]), float(i["y"]))
                for i in graph["instances"]
                if i["x"] is not None and i["y"] is not None
            }
            wires = ratsnest.build_airwires(placed, graph["nets"])
            if view == "feasibility":
                f = place.route_feasibility(wires)
                return Response(
                    body=(
                        f"# route feasibility (estimate, not real routing)\n"
                        f"airwires: {f['airwires']}  (H {f['h_layer']} / "
                        f"V {f['v_layer']})\n"
                        f"residual same-layer crossings: {f['residual_crossings']}\n"
                        f"≈ vias needed: {f['vias_estimate']}\n"
                        "Note: a coarse H/V Manhattan estimate; the rented "
                        "router (Slice 6) is authoritative."
                    )
                )
            if view == "ratsnest":
                rows = [
                    {"net": w.net, "from": w.a, "to": w.b, "len_mm": f"{w.length:g}"}
                    for w in wires
                ]
                return Response(
                    body=f"# ratsnest — {len(wires)} airwire(s), "
                    f"{ratsnest.total_length(wires):g} mm total "
                    f"({len(graph['instances']) - len(placed)} unplaced part(s) "
                    "excluded)\n"
                    + render_agent_table(rows, schema=["net", "from", "to", "len_mm"])
                )
            xs = ratsnest.crossings(wires)
            rows = [
                {
                    "net_a": a.net,
                    "wire_a": f"{a.a}-{a.b}",
                    "net_b": b.net,
                    "wire_b": f"{b.a}-{b.b}",
                }
                for a, b in xs
            ]
            head = (
                f"# crossings — {len(xs)} (the pre-routing objective; "
                f"plane nets excluded). ratsnest {ratsnest.total_length(wires):g} mm"
            )
            if not xs:
                return Response(body=head + "\n(no crossings — planar so far ✓)")
            return Response(
                body=head
                + "\n"
                + render_agent_table(
                    rows, schema=["net_a", "wire_a", "net_b", "wire_b"]
                )
            )
        if view == "drc":
            findings = eyes.drc_lite(graph)
            if not findings:
                return Response(body="# DRC-lite — no findings ✓")
            rows = [
                {
                    "severity": f["severity"],
                    "code": f["code"],
                    "where": f["where"],
                    "message": f["message"],
                }
                for f in findings
            ]
            return Response(
                body=f"# DRC-lite — {len(findings)} finding(s)\n"
                + render_agent_table(
                    rows, schema=["severity", "code", "where", "message"]
                )
            )
        if view == "proximity":
            a, b = str(args.get("a") or ""), str(args.get("b") or "")
            if not (a and b):
                raise BadInput(
                    "view='proximity' needs args.a and args.b (refdes)",
                    next="get(kind='pcb', id='slug', view='proximity', "
                    "args={'a':'U1','b':'C1'})",
                )
            try:
                pr = eyes.proximity(graph, a, b)
            except KeyError as exc:
                raise BadInput(str(exc)) from exc
            return Response(body=f"{a} ↔ {b}: {pr['gap_mm']:g} mm (centroid)")
        if view == "trace":
            net = str(args.get("net") or "")
            if not net:
                raise BadInput(
                    "view='trace' needs args.net (a net name)",
                    next="get(kind='pcb', id='slug', view='trace', "
                    "args={'net':'I2C_SCL'})",
                )
            try:
                tr = eyes.trace(graph, net)
            except KeyError as exc:
                raise BadInput(str(exc)) from exc
            path = " → ".join(
                f"{p['net']}" + (f" (via {p['via']})" if p["via"] != "—" else "")
                for p in tr["path"]
            )
            ends = ", ".join(tr["ends"]) or "—"
            return Response(body=f"# trace from {net}\n{path}\nends: {ends}")
        # measures
        measures = self.store.pcb_measures_list(ref_id)
        if not measures:
            return Response(
                body="no measures on this design\n\nNext: add them via "
                "put(kind='pcb', id='slug', args={'measures':[{'metric':"
                "'separation','operands':[{'role':'sensitive'},{'role':'noisy'}],"
                "'goal':10,'strength':'soft','reason':'keep the opamp off the FET'}]})"
            )
        results = eyes.evaluate_measures(graph, measures)
        rows = [
            {
                "metric": r["metric"],
                "strength": r["strength"],
                "goal": "" if r["goal"] is None else f"{r['goal']:g}",
                "value": "" if r["value"] is None else f"{r['value']:g}",
                "verdict": r["verdict"],
                "reason": (r["reason"] or "")[:40],
            }
            for r in results
        ]
        return Response(
            body=f"# measures — {len(results)}\n"
            + render_agent_table(
                rows,
                schema=["metric", "strength", "goal", "value", "verdict", "reason"],
            )
        )

    # ── exporters (ADR 0042 §6) ──────────────────────────────────────
    def _export_model(self, ref_id: int) -> dict[str, Any]:
        """The normalised export IR (placement detail + net membership)."""
        return pcb_export.export_model(
            self.store.pcb_load(ref_id), self.store.pcb_graph(ref_id)
        )

    def _export_dir(self, slug: str) -> Path:
        """Where artifacts land: ``<PRECIS_CORPUS_DIR>/pcb/<slug>/`` when the
        corpus root is set, else a temp dir. Override per-call with
        args={'dir': '...'}."""
        root = load_config().corpus_dir
        base = Path(root) / "pcb" if root else Path(tempfile.gettempdir())
        out = base / slug
        out.mkdir(parents=True, exist_ok=True)
        return out

    def _outline_from_features(self, ref_id: int) -> list[list[float]] | None:
        for f in self.store.pcb_features_list(ref_id):
            geom = f.get("geom") or {}
            if str(f.get("ftype") or "") == "outline" and isinstance(
                geom.get("path"), list
            ):
                return [[float(p[0]), float(p[1])] for p in geom["path"]]
        return None

    def _render_export(self, ref_id: int, view: str, args: dict[str, Any]) -> Response:
        """Write a fab artifact (BOM / CPL / KiCad netlist / Specctra DSN /
        mechanical profile) off the IR. Pure — no binary needed; the file lands
        under the corpus (or a temp dir)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None else str(ref_id)
        model = self._export_model(ref_id)
        raw_dir = args.get("dir")
        out_dir = Path(str(raw_dir)).expanduser() if raw_dir else self._export_dir(slug)
        out_dir.mkdir(parents=True, exist_ok=True)

        # one table per view — extension + builder together, so they can't
        # drift apart (was an ext map + a parallel if/elif chain).
        builders: dict[str, tuple[str, Callable[[], str]]] = {
            "bom": ("csv", lambda: pcb_export.bom_csv(model)),
            "cpl": ("csv", lambda: pcb_export.cpl_csv(model)),
            "netlist": ("net", lambda: pcb_export.kicad_netlist(model, name=slug)),
            "dsn": (
                "dsn",
                lambda: pcb_export.specctra_dsn(
                    model,
                    footprints=self.store.pcb_footprints_for(ref_id),
                    outline=self._outline_from_features(ref_id),
                    name=slug,
                ),
            ),
            "mechanical": (
                "json",
                lambda: json.dumps(
                    pcb_export.mechanical_profile(
                        model, self.store.pcb_features_list(ref_id)
                    ),
                    indent=2,
                ),
            ),
        }
        ext, build = builders[view]
        content = build()

        path = out_dir / f"{slug}.{ext}"
        path.write_text(content)
        warns = self._export_warnings(model, view)
        head = f"# exported {slug} → {view.upper()}\n{path}  ({len(content):,} bytes)"
        if warns:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in warns)
        # Echo a short preview so the agent sees the shape without re-reading.
        preview = "\n".join(content.splitlines()[:12])
        return Response(body=head + "\n\n```\n" + preview + "\n```")

    def _export_warnings(self, model: dict[str, Any], view: str) -> list[str]:
        out = []
        if view in ("cpl", "dsn"):  # route never reaches here (_render_route)
            up = pcb_export.unplaced(model)
            if up:
                out.append(
                    f"{len(up)} unplaced part(s) excluded: {', '.join(up[:8])}"
                    + ("…" if len(up) > 8 else "")
                    + " — run autoplace first"
                )
        if view in ("bom", "cpl"):
            ml = pcb_export.missing_lcsc(model)
            if ml:
                out.append(
                    f"{len(ml)} part(s) without an LCSC number "
                    f"(not JLCPCB-assemblable): {', '.join(ml[:8])}"
                    + ("…" if len(ml) > 8 else "")
                )
        return out

    def _render_route(self, ref_id: int, args: dict[str, Any]) -> Response:
        """The §9 place↔route round-trip via Freerouting headless. Re-places
        (escalating annealing) and re-routes until the route completes or
        ``max_passes`` is hit. Degrades to a single ``.dsn``-only pass when no
        router is installed (the gate is at this step only)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None else str(ref_id)
        # max(1,…): '0' is truthy, and 0 passes would report a .dsn that was
        # never written (the write lives inside the pass loop).
        max_passes = max(1, int(args.get("max_passes") or 3))
        base_iters = int(args.get("iters") or 1500)
        raw_dir = args.get("dir")
        out_dir = Path(str(raw_dir)).expanduser() if raw_dir else self._export_dir(slug)

        measures = self.store.pcb_measures_list(ref_id)

        def place_fn(iters: int, seed: int) -> dict[str, Any]:
            res, _moved = self._place_and_store(
                ref_id, iters=iters, seed=seed, measures=measures
            )
            return {"crossings_after": res.crossings_after}

        footprints = self.store.pcb_footprints_for(ref_id)
        outline = self._outline_from_features(ref_id)

        def dsn_fn(model: dict[str, Any]) -> str:
            return pcb_export.specctra_dsn(
                model, footprints=footprints, outline=outline, name=slug
            )

        rt = pcb_route.place_route_round_trip(
            lambda: self._export_model(ref_id),
            place_fn,
            dsn_fn,
            out_dir,
            max_passes=max_passes,
            base_iters=base_iters,
            name=slug,
        )
        rows = [
            {
                "pass": h["pass"],
                "iters": h["iters"],
                "crossings": ""
                if h["crossings_after"] is None
                else h["crossings_after"],
                "routed": "✓" if h["routed_ok"] else ("skip" if h["skipped"] else "✗"),
                "unrouted": "" if h["unrouted"] is None else h["unrouted"],
            }
            for h in rt.history
        ]
        if rt.route.skipped:
            tail = (
                "No Freerouting backend — emitted the .dsn only. Install it and "
                "set PRECIS_FREEROUTING_JAR, then re-run view='route'. The .dsn "
                "also opens in the EasyEDA/KiCad router as a manual escape hatch."
            )
        elif rt.ok:
            tail = (
                f"Routed ✓ → {rt.route.ses}\n"
                "Next: import the .ses into KiCad and run kicad-cli for gerbers + "
                "the BOM/CPL (view='bom', view='cpl') to order at JLCPCB."
            )
        else:
            tail = (
                f"Route incomplete after {rt.passes} pass(es) "
                f"({rt.route.unrouted} unrouted). Add area / relax density / pin "
                "more parts and re-run, or open the .dsn in a manual router.\n"
                f"log: {rt.route.log_tail[-400:]}"
            )
        head = f"# route {slug} — {rt.passes} pass(es), {'ok' if rt.ok else 'not complete'}\n{rt.dsn}"
        return Response(
            body=head
            + "\n"
            + render_agent_table(
                rows, schema=["pass", "iters", "crossings", "routed", "unrouted"]
            )
            + "\n"
            + tail
        )

    # ── delete ───────────────────────────────────────────────────────
    def delete(  # type: ignore[override]
        self, *, id: str | int | None = None, **_kw: Any
    ) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='pcb') requires id= (the design slug)")
        ref = resolve_live_slug_ref(self.store, kind="pcb", id=str(id).strip())
        counts = self.store.pcb_delete(ref.id)
        n = counts.get("pcb_instances", 0)
        return Response(body=f"retired pcb design {ref.slug} ({n} instance(s))")

    # ── search ───────────────────────────────────────────────────────
    def search(  # type: ignore[override]
        self,
        *,
        q: str | None = None,
        mode: str | None = None,
        page_size: int = 20,
        **_kw: Any,
    ) -> Response:
        if q is None or not str(q).strip():
            raise BadInput(
                "search(kind='pcb') requires q=",
                next="search(kind='pcb', q='I2C sensor node')",
            )
        q = str(q)
        triples = self._card_search(q, query_vec=None, mode=mode, page_size=page_size)
        if not triples:
            return Response(body=f"no pcb designs match {q!r}")
        rows = []
        for _block, ref, _score in triples:
            handle = handle_registry.try_format("pcb", ref.id, chunk=False) or "—"
            rows.append({"handle": handle, "design": ref.slug, "title": ref.title})
        return Response(
            body=f"# {len(triples)} pcb design(s) for {q!r}\n"
            + render_agent_table(rows, schema=["handle", "design", "title"])
        )

    def search_hits(  # type: ignore[override]
        self,
        *,
        q: str,
        page_size: int = 10,
        query_vec: list[float] | None = None,
        mode: str | None = None,
        **_kw: Any,
    ) -> list[SearchHit]:
        triples = self._card_search(
            q, query_vec=query_vec, mode=mode, page_size=page_size
        )
        self.store.bump_salience([b.id for b, _r, _s in triples])
        out: list[SearchHit] = []
        for block, ref, score in triples:
            text = (getattr(block, "text", "") or "").strip()
            preview = text if len(text) <= 200 else text[:199].rstrip() + "…"
            out.append(
                SearchHit(
                    score=float(score),
                    kind="pcb",
                    title=ref.title or ref.slug or "",
                    preview=preview,
                    slug=ref.slug,
                    ref_id=ref.id,
                    dedupe_key=f"pcb:{ref.slug or ref.id}",
                    uhandle=handle_registry.try_format("pcb", ref.id, chunk=False),
                )
            )
        return out

    def _card_search(
        self,
        q: str,
        *,
        query_vec: list[float] | None,
        mode: str | None,
        page_size: int,
    ) -> list[Any]:
        if not (q and q.strip()):
            return []
        if (mode or "").strip().lower() == "lexical":
            query_vec = None
        elif query_vec is None:
            query_vec = embed_query(self.embedder, q)
        return self.store.search_blocks(
            q=q,
            query_vec=query_vec,
            mode=mode,
            kind="pcb",
            limit=page_size,
            max_distance=SEMANTIC_DISTANCE_FLOOR,
            card_kinds=("card_combined",),
        )

    # ── rendering helpers ────────────────────────────────────────────
    def _render_list(self) -> Response:
        designs = self.store.list_refs(kind="pcb", order_by="id_desc", limit=50)
        if not designs:
            return Response(
                body="no pcb designs yet\n\nNext: put(kind='pcb', id='sensor-node', "
                "args={'components': [...], 'nets': [...], 'connections': [...]})"
            )
        rows = [{"design": r.slug, "title": r.title} for r in designs]
        return Response(
            body=f"# {len(designs)} pcb design(s)\n"
            + render_agent_table(rows, schema=["design", "title"])
        )

    def _toc(self, design: dict[str, list[dict[str, Any]]]) -> str:
        parts = []
        irows = [
            {
                "refdes": i["refdes"],
                "part": i["label"],
                "lcsc": i["part_lcsc"] or "—",
                "layer": i["layer"],
                "pose": self._pose(i),
                "roles": ",".join(i["roles"]) or "—",
            }
            for i in design["instances"]
        ]
        parts.append(
            "## parts\n"
            + render_agent_table(
                irows, schema=["refdes", "part", "lcsc", "layer", "pose", "roles"]
            )
        )
        nrows = [
            {
                "net": n["name"],
                "class": n["net_class"] or "—",
                "fanout": n["fanout"],
                "I": "" if n["est_current_a"] is None else f"{n['est_current_a']:g}A",
                "w": "" if n["width_mm"] is None else f"{n['width_mm']:g}mm",
            }
            for n in design["nets"]
        ]
        parts.append(
            "## nets\n"
            + render_agent_table(nrows, schema=["net", "class", "fanout", "I", "w"])
        )
        return "\n".join(parts)

    def _pose(self, i: dict[str, Any]) -> str:
        if i["x"] is None or i["y"] is None:
            return "unplaced"
        pose = f"@{i['x']:g},{i['y']:g}"
        if i["rot"]:
            pose += f" r{i['rot']:g}"
        if i["fixed"]:
            pose += f" 📌{i['fixed']}"
        return pose

    def _render_instance(self, ref_id: int, refdes: str) -> Response:
        nb = self.store.pcb_instance_neighbors(ref_id, refdes)
        if nb is None:
            raise NotFound(f"pcb instance {refdes!r} not found in this design")
        rows = [
            {
                "pin": p["pin"],
                "pad": p["pad"] or "—",
                "tags": ",".join(p["tags"]) or "—",
                "net": p["net"] or "(nc)",
                "neighbors": ",".join(p["neighbors"]) or "—",
            }
            for p in nb["pins"]
        ]
        return Response(
            body=f"# {refdes} — {len(nb['pins'])} pin(s)\n"
            + render_agent_table(
                rows, schema=["pin", "pad", "tags", "net", "neighbors"]
            )
        )

    def _render_net(self, ref_id: int, name: str) -> Response:
        net = self.store.pcb_net_members(ref_id, name)
        if net is None:
            raise NotFound(f"pcb net {name!r} not found in this design")
        rows = [
            {"refdes": m["refdes"], "pin": m["pin"], "tags": ",".join(m["tags"]) or "—"}
            for m in net["members"]
        ]
        cls = net["net_class"] or "—"
        return Response(
            body=f"# net {name} (class {cls}) — {len(net['members'])} pin(s)\n"
            + render_agent_table(rows, schema=["refdes", "pin", "tags"])
        )
