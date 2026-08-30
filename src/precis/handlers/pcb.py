"""PcbHandler — the electronics / PCB design kind.

A ``pcb`` design is a slug-addressed ref whose graph lives in the dedicated
``pcb_*`` tables (components+pins / instances / nets / netconns). The agent
**authors it in batch** and **reads it as a traversable graph** — never
pixels. The verbs map onto the seven-verb surface:

- ``put``    — create / extend a design (``id=`` slug; ``args={components,
  nets, connections, net_classes}`` — see :meth:`PcbHandler.put`).
  Re-runnable. Every design gets a default board (``pcb-guided-place-route``
  Slice 1 — one 4-layer FR-4 board, ``pcb_boards``) on first write; nets are
  electrical-only in v1 (``domain`` != ``'electrical'`` is rejected).
  ``args={'op': ...}`` is the pcb-guided-place-route Slice 10 tool surface:
  ``op='place'``/``op='route'`` **enqueue** a ``pcb_place``/``pcb_route``
  worker job (never compute inline — the optimizer measures ~880 moves/s,
  minutes per board) idempotent per (design, op, content-hash); ``op='move'``/
  ``'rip'``/``'pin_side'``/``'plane_net'``/``'class_rules'`` are cheap inline
  edits. See ``precis-route-help``.
- ``get``    — list designs (no id); a design's netlist TOC (``id=slug``,
  now with the board/stackup + net_classes + route-status summary); one
  instance's neighbourhood (``id='slug#U3'`` — its pins, the net on each, and
  the connected instances); a net's members (``id='slug@SCL'``); the *eyes*
  (``view='crossings'|'ratsnest'|'drc'|'trace'|'proximity'|'measures'|
  'feasibility'|'route-status'|'congestion'|'planes'``); ``view='svg'`` — a
  publication-quality vector figure (``args={'level':'board'|'sketch'|'fab'}``,
  see :mod:`precis.pcb.svg`); or an *export*
  (``view='bom'|'cpl'|'netlist'|'dsn'|'mechanical'|'gerber'`` writes a
  JLCPCB fab artifact — ``'gerber'`` is the full manufacturable bundle
  (gerbers + Excellon, zipped) off our own realizer/pads, never
  Freerouting/kicad-cli; ``view='route'`` runs the Freerouting
  place↔route round-trip, the demoted escape hatch —
  :mod:`precis.pcb.export` / :mod:`precis.pcb.route`).
- ``search`` — over design names + descriptions (the one summary card).
- ``delete`` — soft-retire a whole design.

The in-house topological place+route engine (:mod:`precis.pcb.optimize` /
:mod:`precis.pcb.realize`, run via the ``pcb_place``/``pcb_route`` jobs) is
the primary path; Freerouting/gerbers-via-DSN is the rented, demoted escape
hatch. Export is the one place the design leaves the relational graph.
See ``precis-pcb-help`` and ``precis-route-help``.
"""

from __future__ import annotations

import json
import logging
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from precis.config import load_config
from precis.dispatch import Hub, InitError
from precis.errors import BadInput, NotFound
from precis.format import render_agent_table
from precis.handlers._slug_ref_shared import resolve_live_slug_ref
from precis.pcb import cost as pcb_cost
from precis.pcb import drc as pcb_drc
from precis.pcb import export as pcb_export
from precis.pcb import eyes, gerber_view, padplace, place, ratsnest
from precis.pcb import gerber as pcb_gerber
from precis.pcb import ir as pcb_ir
from precis.pcb import planes as pcb_planes
from precis.pcb import realize as pcb_realize
from precis.pcb import route as pcb_route
from precis.pcb import session as pcb_session
from precis.pcb import silk as pcb_silk
from precis.pcb import svg as pcb_svg
from precis.pcb.capabilities import capability_for
from precis.pcb.rules import NetRules, resolve_net_rules
from precis.protocol import Handler, KindSpec
from precis.response import Response
from precis.store._mappers import SEMANTIC_DISTANCE_FLOOR
from precis.utils import handle_registry
from precis.utils.embed_query import embed_query
from precis.utils.search_merge import SearchHit

log = logging.getLogger(__name__)


def _export_date() -> str:
    """Today, UTC, for the silkscreen title block.

    :func:`precis.pcb.silk.build_title_block` refuses to invent a date and
    requires the caller to supply one, which is right for a library: a
    date on a board is a claim about that artwork. The handler is the
    layer that legitimately holds one — this IS the export, so the export
    date is a fact it has rather than a guess.

    There is no stored design revision to pair it with (``pcb_boards``
    carries ``board_id``/``name``/``stackup``/``fold_lines`` and no
    version or timestamp column), so the block renders name + date and
    omits the revision rather than fabricating one.
    """
    return datetime.now(UTC).date().isoformat()


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
#: Pure file exporters off the IR (Slice 6). ``gerber`` (pcb-fab-output-
#: unwired) is the odd one out here — it doesn't read ``_export_model``
#: (the netlist IR), it assembles :mod:`precis.pcb.gerber`'s copper+pad
#: model straight off the store, see :meth:`PcbHandler._render_gerber`.
_EXPORT_VIEWS = ("bom", "cpl", "netlist", "dsn", "mechanical", "gerber")
#: The rented autorouter round-trip (Slice 6, gated on Freerouting).
_ROUTE_VIEWS = ("route",)
#: pcb-guided-place-route Slice 1 — per-net route status (all-unrouted v1).
_STATUS_VIEWS = ("route-status",)
#: pcb-guided-place-route Slice 10 — the enqueued optimizer/realizer's
#: read-side: congestion warnings from the latest ``op='route'`` run, and
#: authored plane assignments.
_SESSION_VIEWS = ("congestion", "planes")
#: pcb-svg-render — publication-quality vector figures off the same
#: copper model Gerber writes from (:mod:`precis.pcb.svg`), plus the L3
#: rubber-band sketch off the IR. See :meth:`PcbHandler._render_svg`.
_RENDER_VIEWS = ("svg",)
_OTHER_VIEWS = ("links",)
_VIEWS = (
    *_PROBE_VIEWS,
    *_EXPORT_VIEWS,
    *_ROUTE_VIEWS,
    *_STATUS_VIEWS,
    *_SESSION_VIEWS,
    *_RENDER_VIEWS,
    *_OTHER_VIEWS,
)

#: pcb-guided-place-route Slice 10 tool surface — the two heavy ops that
#: enqueue a worker job (never compute inline) and the inline edits that
#: are cheap enough to run in the request path.
_JOB_OPS = ("place", "route")
_INLINE_EDIT_OPS = ("move", "rip", "pin_side", "plane_net", "class_rules")
_OPS = (*_JOB_OPS, *_INLINE_EDIT_OPS)


class PcbHandler(Handler):
    spec: ClassVar[KindSpec] = KindSpec(
        kind="pcb",
        title="PCB",
        description=(
            "Electronics/PCB design — a netlist + placement graph "
            "the LLM authors in batch and reads as a traversable graph, never "
            "pixels. put creates/extends a design (id=slug, args={components:"
            "[{refdes,label,part?,pins:[{name,pad?,tags?}],x?,y?,layer?,roles?}],"
            " nets:[{name,class?,current?,domain?}], connections:[{net,refdes,"
            "pin}], net_classes:{name:rules}}); nets default domain='electrical' "
            "(v1 rejects fluidic/thermal — schema-reserved). Every design gets "
            "a default 4-layer board (pcb_boards) on first write. "
            "get lists designs, a design's netlist TOC (id=slug, incl. board/"
            "stackup + net_classes + route-status summary), one "
            "instance's neighbourhood (id='slug#U3'), a net's members "
            "(id='slug@SCL'), the eyes (view='crossings'|'ratsnest'|'drc'|"
            "'trace'|'proximity'|'measures'|'feasibility'|'route-status'|"
            "'congestion'|'planes'), a vector figure (view='svg', "
            "args={'level':'board'|'sketch'|'fab','layers':[...],'include':[...]}), "
            "or an export (view='bom'|'cpl'|'netlist'|"
            "'dsn'|'mechanical'|'gerber' writes a JLCPCB fab artifact -- "
            "'gerber' is the full manufacturable bundle (gerbers+Excellon, "
            "zipped); view='route' runs "
            "the demoted Freerouting escape hatch), all with args={...}; "
            "put(args={'op':'place'}) / args={'op':'route'} ENQUEUE a worker "
            "job (never inline; idempotent per design+op+content-hash) — "
            "args={'autoplace':{...}} is a deprecated alias for op='place'; "
            "put(args={'op':'move'|'rip'|'pin_side'|'plane_net'|'class_rules'}) "
            "are cheap inline edits; search over names; delete soft-retires. "
            "Postgres-canonical; routing/gerbers are downstream export. "
            "See precis-pcb-help and precis-route-help."
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
        self.hub = hub
        self.store = hub.store
        self.embedder = hub.embedder

    # ── put ──────────────────────────────────────────────────────────
    def put(
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
        op = args.get("op")
        if op is not None:
            # pcb-guided-place-route Slice 10 tool surface: place/route/the
            # inline edits address an EXISTING design and never blend with
            # bulk netlist authoring in the same call (unlike the retired
            # `autoplace` alias below, which historically did) — keeps the
            # enqueue/edit paths from having to reason about a simultaneous
            # components/nets diff.
            ref = resolve_live_slug_ref(self.store, kind="pcb", id=slug)
            return self._dispatch_op(ref, str(op), args)
        components = list(args.get("components") or [])
        nets = list(args.get("nets") or [])
        connections = list(args.get("connections") or [])
        measures = list(args.get("measures") or [])
        features = list(args.get("features") or [])
        autoplace = args.get("autoplace")
        meta = args.get("meta") if isinstance(args.get("meta"), dict) else None
        net_classes = args.get("net_classes")
        ttl = (title or slug).strip() or slug

        self._reject_non_electrical(nets)

        try:
            # One transaction spans pcb_apply + the net_classes upsert (both
            # accept an external conn=) so a validation error in either half
            # (e.g. a blank net_class name) rolls back the whole put — never
            # a committed design followed by a BadInput implying nothing
            # happened (gr — pcb-guided-place-route Slice 1 review).
            with self.store.tx() as conn:
                ref, created, counts = self.store.pcb_apply(
                    slug=slug,
                    title=ttl,
                    components=components,
                    nets=nets,
                    connections=connections,
                    measures=measures,
                    features=features,
                    meta=meta,
                    conn=conn,
                )
                n_classes = 0
                if isinstance(net_classes, dict) and net_classes:
                    n_classes = self.store.pcb_upsert_net_classes(
                        ref.id, net_classes, conn=conn
                    )
        except ValueError as exc:
            raise BadInput(f"pcb: {exc}") from exc

        if autoplace:
            # Retired as a computed-inline behavior (pcb-guided-place-route
            # Slice 10) — enqueues the SAME `pcb_place` job `op='place'`
            # does; kept as a one-release alias so an existing caller's
            # `args={'autoplace':{...}}` still works, just asynchronously
            # now. See precis-route-help for the replacement.
            opts = autoplace if isinstance(autoplace, dict) else {}
            body = self._enqueue_op(ref, "place", opts)
            return Response(
                body=(
                    "⚠️  DEPRECATED: args={'autoplace':...} now ENQUEUES a "
                    "worker job instead of computing inline — use "
                    "args={'op':'place', ...} directly (same params, same "
                    "job); this alias will be removed. See precis-route-help.\n\n"
                    + body
                )
            )

        verb = "created" if created else "extended"
        design = self.store.pcb_load(ref.id)
        extra = f", +{counts['measures']} measure(s)" if counts["measures"] else ""
        if counts["features"]:
            extra += f", +{counts['features']} feature(s)"
        if n_classes:
            extra += f", +{n_classes} net_class(es)"
        head = (
            f"# {slug} — {verb}: +{counts['components']} part(s), "
            f"+{counts['nets']} net(s), +{counts['conns']} conn(s){extra}  "
            f"(now {len(design['instances'])} part(s), {len(design['nets'])} net(s))"
        )
        return Response(body=head + "\n" + self._toc(design))

    def _reject_non_electrical(self, nets: list[dict[str, Any]]) -> None:
        """v1 routes electrical nets only — ``domain`` is schema-reserved
        for fluidic/thermal co-design (pcb-guided-place-route). Nets
        without a ``domain`` key default to electrical (schema default)."""
        for n in nets:
            domain = n.get("domain")
            if domain is not None and str(domain).strip() != "electrical":
                raise BadInput(
                    f"pcb: net {n.get('name')!r} has domain={domain!r} — v1 routes "
                    "electrical nets only; fluidic/thermal are schema-reserved",
                    next="omit 'domain' (defaults to electrical) or use "
                    "domain='electrical'",
                )

    # ── get ──────────────────────────────────────────────────────────
    def get(
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

    # ── auto-place ─────────────────────────────────────
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

    # ── pcb-guided-place-route Slice 10 tool surface ────────────────────
    def _dispatch_op(self, ref: Any, op: str, args: dict[str, Any]) -> Response:
        """``put(args={'op': ..., ...})`` — the enqueued heavy ops
        (``place``/``route``, never computed inline) and the cheap inline
        edits. See precis-route-help for the full surface."""
        if op in _JOB_OPS:
            return Response(body=self._enqueue_op(ref, op, args))
        if op == "move":
            return self._op_move(ref, args)
        if op == "rip":
            return self._op_rip(ref, args)
        if op == "pin_side":
            return self._op_pin_side(ref, args)
        if op == "plane_net":
            return self._op_plane_net(ref, args)
        if op == "class_rules":
            return self._op_class_rules(ref, args)
        raise BadInput(
            f"unknown op {op!r}",
            options=list(_OPS),
            next=(
                "put(kind='pcb', id='slug', args={'op':'place'}) or "
                "args={'op':'route'} (enqueued jobs); or one of "
                f"{_INLINE_EDIT_OPS} for an inline edit — see precis-route-help"
            ),
        )

    def _enqueue_op(self, ref: Any, op: str, opts: dict[str, Any]) -> str:
        """Enqueue a ``pcb_place``/``pcb_route`` worker job — NEVER
        computes inline (the serve thread-pool starvation lesson; the
        optimizer measures ~880 moves/s, so minutes per board, not
        milliseconds). Idempotent per (design, op, content-hash): a
        re-submit against unchanged design state + params collapses onto
        the in-flight/prior job rather than minting a duplicate."""
        graph = self.store.pcb_graph(ref.id)
        stackup = (graph.get("board") or {}).get("stackup") or []
        if len(stackup) != 4:
            raise BadInput(
                f"pcb: {ref.slug!r} has a {len(stackup)}-layer stackup — v1 "
                "place/route only supports the default 4-layer board",
                next="a non-4-layer stackup is out of v1 scope (2-layer "
                "boards route power like signals, a different problem)",
            )
        params: dict[str, Any] = {"pcb_ref_id": ref.id}
        if opts.get("iters") is not None:
            params["iters"] = int(opts["iters"])
        if opts.get("seed") is not None:
            params["seed"] = int(opts["seed"])
        digest = pcb_session.content_hash(graph, params)
        job_resp = self.hub.sibling("job").put(
            job_type=f"pcb_{op}",
            executor="job_inproc",
            parent_id=ref.id,
            params=params,
            idem_key=f"pcb_{op}:{ref.id}:{digest}",
        )
        status_view = "route-status" if op == "route" else "crossings"
        return (
            f"# {op} {ref.slug} — enqueued\n{job_resp.body}\n\n"
            f"Next: get(kind='pcb', id='{ref.slug}', view='{status_view}') "
            "to check progress once the job lands."
        )

    def _op_move(self, ref: Any, args: dict[str, Any]) -> Response:
        refdes = str(args.get("refdes") or "").strip()
        if not refdes:
            raise BadInput(
                "op='move' needs args.refdes",
                next="args={'op':'move','refdes':'U1','x':10.0,'y':5.0,'rot':90}",
            )
        x, y, rot = args.get("x"), args.get("y"), args.get("rot")
        kwargs: dict[str, Any] = {}
        if x is not None:
            kwargs["x"] = float(x)
        if y is not None:
            kwargs["y"] = float(y)
        if rot is not None:
            kwargs["rot"] = float(rot)
        if "fixed" in args:
            kwargs["fixed"] = args["fixed"]
        if not kwargs:
            raise BadInput(
                "op='move' needs at least one of x / y / rot / fixed",
                next="args={'op':'move','refdes':'U1','fixed':'xy'}",
            )
        ok = self.store.pcb_move_instance(ref.id, refdes, **kwargs)
        if not ok:
            raise NotFound(f"pcb instance {refdes!r} not found in {ref.slug!r}")
        return Response(body=f"# {refdes} moved — {kwargs}")

    def _op_rip(self, ref: Any, args: dict[str, Any]) -> Response:
        net = str(args.get("net") or "").strip()
        if not net:
            raise BadInput(
                "op='rip' needs args.net",
                next="args={'op':'rip','net':'I2C_SCL'}",
            )
        ripped = self.store.pcb_rip_route(ref.id, net)
        if not ripped:
            return Response(body=f"net {net!r} has no route to rip (already unrouted)")
        return Response(
            body=f"# ripped {net} — sketch cleared, back to unrouted\n"
            "Next: put(args={'op':'route'}) to re-decide its topology, or "
            "args={'op':'pin_side'} to steer it first."
        )

    def _op_pin_side(self, ref: Any, args: dict[str, Any]) -> Response:
        net = str(args.get("net") or "").strip()
        a, b = str(args.get("a") or "").strip(), str(args.get("b") or "").strip()
        side = args.get("side")
        if not (net and a and b) or side is None:
            raise BadInput(
                "op='pin_side' needs args.net, args.a, args.b (each "
                "'REFDES.PIN'), args.side",
                next="args={'op':'pin_side','net':'I2C_SCL','a':'U1.SCL',"
                "'b':'R1.1','side':1}",
            )
        ok = self.store.pcb_pin_topology(ref.id, net, a, b, int(side))
        if not ok:
            raise NotFound(f"net {net!r} not found in {ref.slug!r}")
        return Response(
            body=f"# pinned {a} ↔ {b} on {net} to side={side}\n"
            "Takes effect on the next put(args={'op':'route'})."
        )

    def _op_plane_net(self, ref: Any, args: dict[str, Any]) -> Response:
        layer = str(args.get("layer") or "").strip()
        net = str(args.get("net") or "").strip()
        if not (layer and net):
            raise BadInput(
                "op='plane_net' needs args.layer and args.net",
                next="args={'op':'plane_net','layer':'In1.Cu','net':'GND'}",
            )
        design = self.store.pcb_load(ref.id)
        layer_names = [str(entry.get("name")) for entry in design["board"]["stackup"]]
        if layer not in layer_names:
            raise BadInput(
                f"layer {layer!r} is not in this board's stackup",
                options=layer_names,
            )
        # A layer is ONE sheet of copper, so two nets cannot both be poured
        # on it — a hard constraint, not a price. Without this, a second
        # op='plane_net' on the same layer was accepted, and
        # planes.plane_pours' "lowest net id wins" tie-break then silently
        # dropped one of them: the caller's instruction was stored, agreed
        # to, and never appeared on the board. Same failure the annealer's
        # one-net-per-plane-layer fix closed on the derived side.
        conflict = next(
            (
                row
                for row in self.store.pcb_planes_list(ref.id)
                if row["layer"] == layer and row["net"] != net
            ),
            None,
        )
        if conflict is not None:
            raise BadInput(
                f"layer {layer!r} already carries plane net {conflict['net']!r} "
                f"({conflict['source']}) — a layer is one sheet of copper, so "
                "reassign it or pick a different layer",
                options=[n for n in layer_names if n != layer],
            )
        plane_id = self.store.pcb_assign_plane(ref.id, layer, net)
        if not plane_id:
            raise NotFound(f"net {net!r} not found in {ref.slug!r}")
        return Response(
            body=f"# {net} assigned to plane layer {layer}\n"
            "Takes effect on the next put(args={'op':'route'}) — its pins "
            "fan out (dog-bone) instead of routing."
        )

    def _op_class_rules(self, ref: Any, args: dict[str, Any]) -> Response:
        name = str(args.get("name") or "").strip()
        rules = args.get("rules")
        if not name or not isinstance(rules, dict):
            raise BadInput(
                "op='class_rules' needs args.name and args.rules (a dict)",
                next="args={'op':'class_rules','name':'i2c',"
                "'rules':{'clearance_mm':0.2}}",
            )
        self.store.pcb_set_class_rules(ref.id, name, rules)
        return Response(body=f"# net class {name!r} rules set: {rules}")

    # ── the eyes ───────────────────────────────────────
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
        if view in _STATUS_VIEWS:
            return self._render_route_status(ref_id)
        if view == "congestion":
            return self._render_congestion(ref_id)
        if view == "planes":
            return self._render_planes(ref_id)
        if view == "drc":
            return self._render_drc(ref_id)
        if view == "svg":
            return self._render_svg(ref_id, args)
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

    # ── exporters ──────────────────────────────────────
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

    def _furniture_clearance_mm(self, stackup: list[dict[str, Any]]) -> float | None:
        """The clearance :meth:`_board_furniture` hands to
        :func:`precis.pcb.planes.cut_antipads` for a fiducial's antipad
        ring — resolved from :mod:`precis.pcb.capabilities` through
        :func:`precis.pcb.rules.resolve_net_rules`, the SAME resolver
        :meth:`_render_drc` already calls per-net a few lines down from
        its own ``capability_for`` (comment there, verbatim: "an arbitrary
        True is fine here; realize.py is the caller that resolves
        per-layer"). No net-class override applies to a fiducial (it
        carries no net), so this is the fab-floor figure
        ``resolve_net_rules`` falls back to absent one — the SAME floor
        :func:`precis.pcb.realize._pour_planes` folds into the
        ``clearance_mm`` it hands ``plane_pours`` for every OTHER
        antipad on the same pour. One number, one source, read the same
        way by both the realize-time cut and this render-time one.

        ``None`` for a stackup layer count :func:`precis.pcb.drc.
        process_for_stackup` has no capability row for, rather than
        raising: :meth:`_render_gerber`/:meth:`_render_fab_svg` (unlike
        :meth:`_render_drc`) never called ``capability_for`` at all
        before this ring existed, and a board that has not been routed
        yet — so carries no pour, hence nothing for ``cut_antipads`` to
        cut — must still export/preview cleanly on an unsupported layer
        count exactly as it did before. Any board that DOES carry a pour
        already passed this same ``capability_for`` call at REALIZE time
        (:func:`precis.workers.job_types.pcb_route._process_for_stackup`),
        so this can only return ``None`` when there is no pour to cut in
        the first place."""
        try:
            capability = capability_for(pcb_drc.process_for_stackup(stackup))
        except ValueError:
            return None
        return resolve_net_rules(
            "", layer_is_outer=True, fab_caps=capability
        ).clearance_mm

    def _board_furniture(
        self,
        ir: pcb_ir.PcbIR,
        pads: list[dict[str, Any]],
        *,
        vias: list[dict[str, Any]],
        instance_sides: dict[str, str],
        copper: list[dict[str, Any]],
        outline: list[list[float]] | list[tuple[float, float]] | None,
        layer_names: list[str],
        slug: str,
        clearance_mm: float | None,
        date: str | None = None,
    ) -> tuple[
        list[dict[str, Any]],
        dict[str, list[dict[str, Any]]],
        list[str],
        tuple[pcb_silk.SilkPlacement, ...],
        list[dict[str, Any]],
    ]:
        """Fiducials, title block and silkscreen, assembled ONCE.

        Returns ``(pads, silk_draws, warnings, census, copper)`` — ``pads``
        with the fiducial flashes folded in (they are ordinary
        ``model["pads"]`` entries, so copper and the swelled mask opening
        fall out of the existing pad pipeline untouched), ``census`` (one
        :class:`~precis.pcb.silk.SilkPlacement` per per-instance courtyard/
        pin-1/refdes item :func:`~precis.pcb.silk.build_silk` considered)
        for a caller wiring ``view='drc'`` (:meth:`_render_drc`) — the
        structured record :func:`precis.pcb.drc.check_silk_missing`/
        :func:`~precis.pcb.drc.check_silk_printability` read, rather than
        this function's own ``warnings`` prose — and ``copper``, which is
        the ``copper`` PARAMETER given back with any plane pour on the
        fiducials' layer antipadded around the fiducial discs just placed
        (:func:`precis.pcb.planes.cut_antipads`, called below once
        :func:`~precis.pcb.silk.build_fiducials` has run). A caller MUST
        use this returned ``copper``, not the one it passed in, for
        everything downstream (the gerber/DRC/fab-SVG model's
        ``"copper"`` key) — this function does not mutate the input list
        in place, so keeping the old reference silently un-does the cut
        this exists to make. ``clearance_mm`` is the caller's own
        resolved figure (:meth:`_furniture_clearance_mm`) for that same
        cut — ``None`` only when the board's stackup has no capability
        row at all (an unsupported layer count), which reports as a
        warning and leaves any pour on the fiducials' layer un-cut rather
        than raising, since a board in that state carries no pour to cut
        in the first place (that same docstring's own reasoning).

        **This exists because there are two render sites** —
        :meth:`_render_gerber` (the fab set a human orders from) and the
        fab SVG (the picture they inspect it with) — and this subsystem's
        recurring defect is one rule implemented at two call sites that
        then drift. A board whose picture shows fiducials the gerbers
        lack is exactly that bug, and it would look like a rendering
        nicety rather than a fab defect.

        **Order is load-bearing, not stylistic — and this paragraph
        describes what the code below actually does, not the reverse.**
        An EARLIER version of this function placed fiducials first (they
        are optical targets a machine is told to look at, so the instinct
        is "they should not have to move for anything else") — but a
        fiducial pinned to the outline's bottom-right corner then blocked
        every candidate in the title block's own margin ladder and
        silently dropped it from the rendered board (found by rendering
        and looking, not by any test; see the comment ahead of the title
        block call below). The title block and the S/N patch are each
        anchored to a specific outline-bbox corner with a short,
        fixed-order fallback ladder (:func:`precis.pcb.silk.
        build_title_block`/:func:`~precis.pcb.silk.build_sn_patch`) — they
        have almost nowhere else to go. Fiducials, by contrast,
        :func:`~precis.pcb.silk.build_fiducials` walks ALL four corners
        and is content with whichever three clear first, so THEY are the
        one board-level feature with real room to move around something
        already committed. The actual order below is therefore: **title
        block first, the S/N patch second** (against the title block,
        which becomes an obstacle for it, not merely an anchor — see the
        comment ahead of that call), **fiducials third** (checked against
        both), **and per-part silk last of all**, against everything the
        first three placed — the only ordering under which every board-
        level feature that CAN move out of the way for another one
        actually does.
        """
        warnings: list[str] = []
        if not outline:
            # Both builders need a bounding box. Say so rather than
            # returning a board that is quietly missing its fab targets.
            warnings.append(
                "no outline feature — skipped fiducials and the title block "
                "(both are placed relative to the board's bounding box)"
            )
            silk_only = pcb_silk.build_silk(
                ir, pads, vias=vias, instance_sides=instance_sides
            )
            warnings.extend(f"silk: {m}" for m in silk_only.dropped)
            warnings.extend(f"silk: {m}" for m in silk_only.relocated)
            return pads, silk_only.draws, warnings, silk_only.census, copper

        # **Title block FIRST, fiducials second.** Both want a corner of the
        # same bounding box, and only the fiducials can go somewhere else:
        # `build_fiducials` walks a list of corners and takes the first that
        # are clear, while `build_title_block` stacks at ONE corner and is
        # dropped outright if that corner is occupied.
        #
        # Placing fiducials first put one at the outline's bottom-right,
        # which blocked every title-block candidate inside its margin ladder
        # and silently dropped the block from the rendered board — found by
        # rendering and looking, not by any test. `build_title_block`'s own
        # docstring says to place it first; this is that instruction.
        title = pcb_silk.build_title_block(outline, pads, name=slug, date=date)
        warnings.extend(f"title block: {m}" for m in title.dropped)

        # The serial-number patch sits against the title block, so it is
        # placed immediately after it and before anything that would
        # compete for the same corner.
        # The title block is an OBSTACLE for the patch, not merely an anchor.
        # Passing only `title_bbox` positioned the patch relative to the
        # block but left nothing stopping it from overlapping — and a solid
        # silk box painted over the board name erases it, which is worse
        # than either element being dropped.
        sn = pcb_silk.build_sn_patch(
            outline,
            pads,
            title_bbox=title.bbox,
            avoid=(
                [pcb_silk.obstacle_from_bbox(title.bbox)]
                if title.bbox is not None
                else None
            ),
        )
        warnings.extend(f"S/N patch: {m}" for m in sn.dropped)

        fid_layer = layer_names[0] if layer_names else "F.Cu"
        # `obstacle_from_bbox` returns a pad-shaped dict, which is exactly
        # what `build_fiducials` checks its candidate corners against — so
        # the block becomes an ordinary obstacle rather than a special case.
        fid_obstacles = list(pads)
        for placed_bbox in (title.bbox, sn.bbox):
            if placed_bbox is not None:
                fid_obstacles.append(pcb_silk.obstacle_from_bbox(placed_bbox))
        fid = pcb_silk.build_fiducials(outline, fid_obstacles, layer=fid_layer)
        warnings.extend(f"fiducial: {m}" for m in fid.dropped)

        # `FiducialResult.plane_blockers` cut in HERE, at render time, off
        # the already-realized+persisted pours — `planes.plane_pours` ran
        # at REALIZE time, before these fiducials existed, so it could not
        # have antipadded them (see that dataclass's own docstring for the
        # timing gap). `cut_antipads` punches the same no-pour ring
        # `plane_pours` would have, sized off the SAME `clearance_mm` this
        # handler resolves from `capabilities.py` (the caller's own
        # docstring), around each fiducial's MASK opening (the blocker
        # discs are already sized to `mask_dia_mm`, not the bare copper).
        # Non-pour copper (tracks/vias/pads) passes through untouched.
        pour_items = [c for c in copper if c.get("ctype") == "pour"]
        if fid.plane_blockers and pour_items and clearance_mm is None:
            # No capability row for this stackup (see
            # `_furniture_clearance_mm`'s own docstring) — a board that
            # somehow carries a pour anyway (it should not: pouring one
            # requires the same lookup to have already succeeded at
            # realize time) is reported here rather than crashing a
            # render that otherwise has nothing to do with this cut.
            warnings.append(
                f"fiducial: no fab capability row for this stackup — "
                f"pour(s) on {fid_layer} left uncut around the fiducials"
            )
        elif fid.plane_blockers and pour_items:
            assert clearance_mm is not None  # narrowed by the branch above
            cut_pours = pcb_planes.cut_antipads(
                pour_items, fid.plane_blockers, clearance_mm=clearance_mm
            )
            copper = [c for c in copper if c.get("ctype") != "pour"] + cut_pours
            # The cut is geometry, not a promise — verify the invariant it
            # exists for (a fiducial's mask opening is clear of copper)
            # rather than trusting it silently. The one way this can
            # legitimately still fail: the antipad ring's own area falls
            # under `MIN_FRAGMENT_MM2` (`cut_antipads` reuses the SAME
            # noise floor `plane_pours` uses for a sliver fragment, shared
            # via `_pour_item`), so the hole is filtered out as buffering
            # noise and the pour re-solidifies over the fiducial — a real
            # fiducial-mask-opening-vs-fab-noise-floor collision, not a
            # timing gap this function can no longer close.
            for fx, fy in fid.fiducials:
                if any(
                    str(p.get("layer") or "") == fid_layer
                    and pcb_planes.point_in_pour(p, fx, fy)
                    for p in cut_pours
                ):
                    warnings.append(
                        f"fiducial: antipad ring at ({fx:.2f}, {fy:.2f}) on "
                        f"{fid_layer} was too small to survive as a real hole "
                        f"(< {pcb_planes.MIN_FRAGMENT_MM2}mm^2) — the pour "
                        "still covers this fiducial"
                    )

        pads = pads + fid.pads

        # Everything already placed becomes an obstacle for the per-part
        # labels, on BOTH sides: a fiducial is a copper/mask feature and
        # blocks silk wherever it lands.
        board_obstacles = list(fid.silk_keepouts)
        for placed_bbox in (title.bbox, sn.bbox):
            if placed_bbox is not None:
                board_obstacles.append(pcb_silk.obstacle_from_bbox(placed_bbox))
        reserved = {side: list(board_obstacles) for side in ("top", "bottom")}

        silk_result = pcb_silk.build_silk(
            ir, pads, vias=vias, instance_sides=instance_sides, reserved=reserved
        )
        warnings.extend(f"silk: {m}" for m in silk_result.dropped)
        warnings.extend(f"silk: {m}" for m in silk_result.relocated)

        draws = {side: list(items) for side, items in silk_result.draws.items()}
        draws.setdefault("top", []).extend(title.draws)
        draws.setdefault("top", []).extend(sn.draws)
        return pads, draws, warnings, silk_result.census, copper

    def _drc_pads(self, ref_id: int, layers: list[str]) -> list[dict[str, Any]]:
        """This design's pads, in :mod:`precis.pcb.gerber` model shape.

        Delegates to :func:`precis.pcb.realize.pads_for_ir` — pad geometry
        has exactly one definition, for the reasons that function's
        docstring records. Real per-pin geometry where a part's footprint
        is cached (:meth:`Store.pcb_footprints_for`, remapped onto refdes
        via :func:`precis.pcb.session.footprints_by_refdes` — the join
        ``PcbIR.instance_part_lcsc`` exists for); a package-family
        synthesized bound (``synthesized=True``) everywhere else, same as
        before. This is what makes ``view='gerber'``'s fallback (this
        design has no ``board_pads`` output yet) and ``view='svg'
        args={'level':'fab'}`` both describe the SAME board a fully-cached
        design's real ``export_fab`` call would.
        """
        graph = self.store.pcb_graph(ref_id)
        if not graph.get("nets"):
            return []
        ir = pcb_session.build_ir(graph)
        footprints = pcb_session.footprints_by_refdes(
            ir, self.store.pcb_footprints_for(ref_id)
        )
        return pcb_realize.pads_for_ir(ir, layers, footprints)

    def _drc_geometry(
        self, ref_id: int, layers: list[str]
    ) -> tuple[list[dict[str, Any]], dict[str, float]]:
        """DRC's pads (the same IR-sourced geometry :meth:`_drc_pads`
        builds, off ONE ``build_ir`` call here rather than two independent
        ones) plus each instance's real, pad-geometry-derived courtyard
        keep-out radius, keyed by refdes.

        **The radius is not computed here.**
        :func:`precis.pcb.ir.instance_keepout_radius_mm` is the definition
        used by every COPPER-side consumer — this view,
        ``OptimizeEngine._keepout_r`` and ``seed_placement``.

        **It is NOT the only courtyard in this subsystem, and this
        docstring claimed it was until 2026-08-29.** The courtyard actually
        DRAWN on the silkscreen comes from
        :func:`precis.pcb.silk._courtyard_reach_mm`, which is a separate
        formula: a SQUARE of half-extent ``instance_pad_radius + half_pad
        + margin``, against this function's CIRCLE of radius
        ``instance_pad_radius + PAD_BREATHING_MM`` floored at
        ``COURTYARD_MIN_SEPARATION_MM / 2``. Different shape, different
        size. So ``courtyard_overlap`` enforces one boundary while the
        board's silkscreen shows an assembler a different one — they are
        not required to agree (a placement-legality keep-out and a visual
        assembly aid are different questions), but nothing currently
        states the relationship or checks it, and a reader who believed
        the old "ONE definition" claim would not go looking. Whether they
        SHOULD be reconciled is tracked in
        ``docs/backlog/pcb-engine-plan.md``.

        A flat
        :data:`~precis.pcb.drc.DEFAULT_COURTYARD_RADIUS_MM` for EVERY
        instance made ``courtyard_overlap`` structurally dormant: it is
        smaller than any real part's derived keep-out, so placement always
        separates parts further than the flat check would ever flag — a
        TO-220's real several-millimetre courtyard was being checked
        against nothing. The floor is passed in rather than baked into
        ``ir.py`` because it is a cost-policy constant and ``ir.py`` sits
        below ``cost.py`` in the import order."""
        graph = self.store.pcb_graph(ref_id)
        ir = pcb_session.build_ir(graph)
        footprints = pcb_session.footprints_by_refdes(
            ir, self.store.pcb_footprints_for(ref_id)
        )
        pads = (
            pcb_realize.pads_for_ir(ir, layers, footprints) if graph.get("nets") else []
        )
        radii = pcb_ir.instance_keepout_radius_mm(
            ir, min_radius_mm=pcb_cost.COURTYARD_MIN_SEPARATION_MM / 2.0
        )
        radius_by_refdes = {
            str(ir.instance_refdes[i]): float(radii[i]) for i in range(ir.n_instances)
        }
        return pads, radius_by_refdes

    def _drc_drills(self, ref_id: int) -> list[dict[str, Any]]:
        """``model['drills']`` for :func:`precis.pcb.drc.
        check_npth_clearance` — ``mounting_hole`` features, the mechanical
        holes that check exists to protect (a screw hole is never a
        soldered lead, so it is unplated by construction). Before this,
        ``_render_drc`` never populated ``drills`` at all, so
        ``check_npth_clearance`` — wired into ``run_geometric_drc`` and
        reading ``model.get('drills')`` — was structurally incapable of
        firing regardless of design content, on every board, every seed.

        Field names match BOTH of ``model['drills']``'s existing consumers
        verbatim — :func:`precis.pcb.drc.check_npth_clearance`'s own
        ``{"x","y","dia_mm","plated"}`` reads and
        :func:`precis.pcb.gerber.excellon_files` /
        :func:`precis.pcb.padplace.place_footprint_pads`'s identical
        shape for a through-hole component pad's drill — so this never
        invents a third spelling.

        **A through-hole component pad's own drill is NOT included here**:
        DRC's pad source (:func:`precis.pcb.realize.pads_for_ir`, the IR
        path this view deliberately uses so pads and connectivity share
        ONE source — see :meth:`_render_drc`'s own comment) carries no
        drill/through-hole concept at all yet. Reaching into
        :mod:`precis.pcb.padplace`'s cached-footprint pad source instead
        would reintroduce exactly the "one rule, two call sites, drifted"
        pad-source split that comment already warns against — a distinct,
        deferred gap that belongs in ``realize.py``, not patched around
        here."""
        out: list[dict[str, Any]] = []
        for f in self.store.pcb_features_list(ref_id):
            if str(f.get("ftype") or "") != "mounting_hole":
                continue
            geom = f.get("geom") or {}
            dia = geom.get("diameter") or geom.get("d")
            x, y = f.get("x"), f.get("y")
            if dia is None or x is None or y is None:
                continue
            out.append(
                {"x": float(x), "y": float(y), "dia_mm": float(dia), "plated": False}
            )
        return out

    def _render_export(self, ref_id: int, view: str, args: dict[str, Any]) -> Response:
        """Write a fab artifact (BOM / CPL / KiCad netlist / Specctra DSN /
        mechanical profile / the gerber bundle) off the IR. Pure — no binary
        needed; the file lands under the corpus (or a temp dir)."""
        if view == "gerber":
            return self._render_gerber(ref_id, args)
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
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
        path.write_text(content, encoding="utf-8")
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

    def _render_gerber(self, ref_id: int, args: dict[str, Any]) -> Response:
        """Assemble + write the manufacturable fab bundle — gerbers +
        Excellon, zipped (:mod:`precis.pcb.gerber`) — closing
        ``docs/backlog/pcb-fab-output-unwired.md``'s "the export tail is
        unwired" gap. Unlike every other ``_EXPORT_VIEWS`` entry this does
        NOT read ``_export_model`` (that IR has no realized copper or pad
        geometry): the model here is ``{layers, outline, copper, pads,
        drills, silkscreen}`` — copper straight off ``pcb_copper`` (the
        realizer's own output shape, see :mod:`precis.pcb.realize`), pads
        newly placed by :mod:`precis.pcb.padplace` off each instance's
        pose + its cached footprint, and a GENERATED silkscreen
        (:mod:`precis.pcb.silk` — refdes labels, courtyard outlines, pin-1
        ticks, built off the same IR and checked against these same pads
        AND this same realized copper's vias, so nothing prints where a
        fab would scrape it off)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        board_id = int(board["board_id"])
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        copper = self.store.pcb_copper_list(board_id)

        warnings: list[str] = []
        outline = self._outline_from_features(ref_id)
        if outline is None:
            x0, y0, x1, y1 = pcb_export.board_bbox({"instances": design["instances"]})
            outline = [[x0, y0], [x1, y0], [x1, y1], [x0, y1]]
            warnings.append(
                "no 'outline' feature — the board edge is the placed-parts "
                "bounding box, not a real board outline"
            )
        if not copper:
            warnings.append(
                "no realized copper yet — run put(args={'op':'route'}) first, "
                "or the gerbers will carry pads/outline/drills only"
            )

        footprints = self.store.pcb_footprints_for(ref_id)
        graph = self.store.pcb_graph(ref_id)
        ir = pcb_session.build_ir(graph)
        pin_to_net = {
            (m["refdes"], m["pin"]): net["name"]
            for net in graph["nets"]
            for m in net["members"]
        }
        pads, drills = padplace.board_pads(
            design["instances"], footprints, layers=layer_names, pin_to_net=pin_to_net
        )
        placed = [
            i
            for i in design["instances"]
            if i.get("x") is not None and i.get("y") is not None
        ]
        has_pads = {
            i["refdes"]
            for i in placed
            if (footprints.get(str(i.get("part_lcsc") or "")) or {}).get("pads")
        }
        missing = sorted({i["refdes"] for i in placed} - has_pads)
        if missing:
            warnings.append(
                f"{len(missing)} placed part(s) have no cached footprint — pads "
                "fall back to SYNTHESIZED land patterns (bounds, not the real "
                f"part): {', '.join(missing[:8])}"
                + ("…" if len(missing) > 8 else "")
                + " (fetch via precis.pcb.footprint first)"
            )
            # `board_pads` (module docstring, verbatim) "contributes nothing"
            # for an instance with no cached footprint — an honest gap for
            # ITS own caller, but left as-is here that gap silently dropped
            # the missing part's pads from the fab set ENTIRELY rather than
            # marking them synthesized: a mixed design (the realistic case,
            # some parts cached, some not) came out looking clean because
            # export_fab's synthesized-pad check saw NOTHING to refuse, not
            # because the board was real. Fill in exactly the missing
            # refdes' pins from the SAME synthesized-or-real merge point
            # `_drc_pads`/DRC already uses (`pads_for_ir` -> `pad_geometry`)
            # so those pads carry `synthesized: True` and export_fab's
            # refusal actually fires. `placed_pin_ids` replays
            # `pads_for_ir`'s own "skip an unplaced pin" filter so the
            # zip stays index-aligned without a second geometry pass.
            footprints_by_refdes = pcb_session.footprints_by_refdes(ir, footprints)
            ir_pads = pcb_realize.pads_for_ir(ir, layer_names, footprints_by_refdes)
            placed_pin_ids = [
                pid for pid in range(ir.n_pins) if pcb_ir.pin_point(ir, pid) is not None
            ]
            missing_set = set(missing)
            pads = pads + [
                pad
                for pid, pad in zip(placed_pin_ids, ir_pads, strict=True)
                if str(ir.instance_refdes[int(ir.pin_instance[pid])]) in missing_set
            ]
        if not pads:
            # No placed instance contributed a pad at all (nothing above
            # could have added one either — `missing` would itself be
            # empty in that case). Kept as a last-resort safety net, same
            # as before: fall back to the SAME land patterns the router
            # routed to, so the fab set describes the board the engine
            # actually built, and let export_fab refuse to call it
            # manufacturable.
            pads = self._drc_pads(ref_id, layer_names)

        instance_sides = {
            str(i.get("refdes")): str(i.get("layer") or "top")
            for i in design["instances"]
        }
        vias = [c for c in copper if c.get("ctype") == "via"]
        pads, silk_draws, furniture_warnings, _silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=self._furniture_clearance_mm(board["stackup"]),
                date=_export_date(),
            )
        )
        warnings.extend(furniture_warnings)

        model: dict[str, Any] = {
            "layers": layer_names,
            "outline": outline,
            "copper": copper,
            "pads": pads,
            "drills": drills,
            "silkscreen": silk_draws,
        }
        try:
            files = pcb_gerber.export_fab(model, name=slug)
        except pcb_gerber.SynthesizedPadError as exc:
            raise BadInput(
                f"pcb: {exc} Use view='svg' args={{'level':'fab'}} to inspect "
                "the board as gerbers without exporting it."
            ) from exc
        blob = pcb_gerber.zip_fab(files)

        raw_dir = args.get("dir")
        out_dir = Path(str(raw_dir)).expanduser() if raw_dir else self._export_dir(slug)
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{slug}-fab.zip"
        path.write_bytes(blob)

        head = (
            f"# exported {slug} → GERBER (fab bundle)\n{path}  "
            f"({len(blob):,} bytes zipped, {len(files)} file(s))\n"
            f"pads: {len(pads)}  drills: {len(drills)}  copper item(s): {len(copper)}"
        )
        if warnings:
            head += "\n" + "\n".join(f"⚠️  {w}" for w in warnings)
        listing = "\n".join(sorted(files))
        return Response(body=head + "\n\n```\n" + listing + "\n```")

    def _render_route(self, ref_id: int, args: dict[str, Any]) -> Response:
        """The §9 place↔route round-trip via Freerouting headless. Re-places
        (escalating annealing) and re-routes until the route completes or
        ``max_passes`` is hit. Degrades to a single ``.dsn``-only pass when no
        router is installed (the gate is at this step only)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
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

    # ── route status (pcb-guided-place-route Slice 1) ──────────────────
    def _render_route_status(self, ref_id: int) -> Response:
        """The per-net route status table — a net with no ``pcb_routes`` row
        reads as ``unrouted`` (the default state before ``op='route'`` has
        ever run against it, not a missing one). A row's ``note`` (e.g. a
        dangling <2-member net's ``'realized'`` exemption — see
        ``pcb_route``'s job docstring) is appended so it never reads as an
        actually-routed net."""
        rows = self.store.pcb_route_status(ref_id)
        if not rows:
            return Response(body="no nets on this design yet")
        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        summary = ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))
        table_rows = [
            {
                "net": r["name"],
                "class": r["net_class"] or "—",
                "domain": r["domain"] or "electrical",
                "status": r["status"] + (f" ({r['note']})" if r.get("note") else ""),
            }
            for r in rows
        ]
        return Response(
            body=f"# route status — {len(rows)} net(s): {summary}\n"
            + render_agent_table(
                table_rows, schema=["net", "class", "domain", "status"]
            )
        )

    def _render_congestion(self, ref_id: int) -> Response:
        """The latest ``op='route'`` run's congestion digest — per-gap
        capacity warnings stamped onto ``refs.meta.last_route`` by the
        ``pcb_route`` job (backlog acceptance criterion: a routing failure
        must name the blocking gap, the participants, and the clearance
        arithmetic, not just report "unrouted")."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        if ref is None:
            raise NotFound(f"pcb id={ref_id} not found")
        last_route = (ref.meta or {}).get("last_route")
        if not last_route:
            return Response(
                body="no route run yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'op':'route'}) then re-check this view."
            )
        warnings = last_route.get("warnings") or []
        head = (
            f"# congestion — last route: {last_route.get('realized', 0)} realized, "
            f"{last_route.get('failed', 0)} failed, {len(warnings)} gap warning(s)"
        )
        if not warnings:
            return Response(body=head + "\n(no over-capacity gaps ✓)")
        return Response(body=head + "\n" + "\n".join(f"- {w}" for w in warnings))

    def _render_planes(self, ref_id: int) -> Response:
        """Plane assignments (``pcb_planes``) — which nets are
        plane-served on which layer, so the LLM can see the
        fanout-vs-route split before running ``op='route'``. Shows
        ``source`` so a reader can tell a human's explicit
        ``op='plane_net'`` instruction (``authored``) apart from the
        anneal's own settled decision (``derived``, written back by the
        ``pcb_route`` job, gr267526) — an authored row is never touched
        by a later route run; a derived one is replaced each run."""
        rows = self.store.pcb_planes_list(ref_id)
        if not rows:
            return Response(
                body="no plane assignments yet\n\nNext: put(kind='pcb', "
                "id='slug', args={'op':'plane_net','layer':'In1.Cu',"
                "'net':'GND'})"
            )
        table_rows = [
            {"layer": r["layer"], "net": r["net"], "source": r["source"]} for r in rows
        ]
        return Response(
            body=f"# planes — {len(rows)} assignment(s)\n"
            + render_agent_table(table_rows, schema=["layer", "net", "source"])
        )

    def _render_drc(self, ref_id: int) -> Response:
        """Geometric DRC on REALIZED copper (pcb-guided-place-route Slice
        8, :mod:`precis.pcb.drc`) — the L5 check, re-backing this view now
        that a ``pcb_route`` run leaves real copper in ``pcb_copper`` to
        check. Superseded ``eyes.drc_lite`` (graph-shape sanity only, no
        geometry); the graph-feasibility half of DRC (``ir.py``, L0-L4)
        stays inside the optimizer, not this view. Every call is itself a
        DRC "run" — findings are persisted to ``pcb_drc_findings`` under a
        fresh ``run_id`` so ``netlist_drc_clean`` and a human reviewer can
        both read the same durable record afterward.

        **Now builds board furniture (fiducials/title block/silkscreen)
        too**, via the SAME :meth:`_board_furniture` :meth:`_render_gerber`
        and the fab SVG already call — this view used to check ONLY
        component pads/copper, so a fiducial (an ordinary flashed pad on
        the real fab set) had no representation here at all: a track
        routed near an outline corner could ship a gerber with a 1mm
        copper dot sitting on top of it while ``view='drc'`` read clean,
        because the dot was never in the model this view checked. The
        furniture is COMPUTED fresh on every call, never persisted and
        read back — see :meth:`Store.pcb_drc_findings_latest`'s own
        docstring for why a persisted-then-read DRC result makes "no rows"
        and "no problems" the same value, the exact trap a silk census
        computed here (not on write) avoids."""
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        copper = self.store.pcb_copper_list(int(board["board_id"]))
        if not copper:
            return Response(
                body="no realized copper yet\n\nNext: put(kind='pcb', "
                "id='slug', args={'op':'route'}) to realize copper, then "
                "re-check this view."
            )
        stackup = board["stackup"]
        try:
            capability = capability_for(pcb_drc.process_for_stackup(stackup))
        except ValueError as exc:
            raise BadInput(f"pcb: {exc}") from exc
        layer_names = [str(layer.get("name")) for layer in stackup]
        # Pads come from the IR, which is the SAME source the router and the
        # realizer use (precis.pcb.landpattern via ir.pin_point). A second
        # pad source is how this build's recurring defect works — one rule,
        # two call sites, drifted — and connectivity is precisely the check
        # that would be fooled by pads in the wrong place.
        pads, courtyard_radius = self._drc_geometry(ref_id, layer_names)

        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        graph = self.store.pcb_graph(ref_id)
        ir = pcb_session.build_ir(graph)
        instance_sides = {
            str(i.get("refdes")): str(i.get("layer") or "top")
            for i in design["instances"]
        }
        vias = [c for c in copper if c.get("ctype") == "via"]
        outline = self._outline_from_features(ref_id)
        # `pads` below is REPLACED with `_board_furniture`'s own return, not
        # merely extended — that call already folds the component pads
        # passed in here together with the fiducial flashes it mints
        # (module docstring above: this is the fix for fiducial copper
        # being invisible to this view). Component pads never travel
        # through a second path once this line runs.
        # `furniture_warnings` (fiducial/title-block/S/N-patch drop prose)
        # is discarded here on purpose: those three are board-level
        # furniture with no per-instance `SilkPlacement` row to check
        # against (silk.py's own module docstring — fiducials/title block
        # sit outside `build_silk`'s per-instance census entirely), so
        # `run_geometric_drc` has no structured rule for them yet; the
        # gerber/fab-SVG render paths still surface this same prose to a
        # human inspecting those artifacts.
        # `clearance_mm` reused straight off the `capability` row already
        # resolved above rather than a second `capability_for` call — see
        # `_furniture_clearance_mm`'s own docstring for why this is the
        # SAME figure `plane_pours` itself used to cut this board's other
        # antipads at realize time.
        pads, silk_draws, _furniture_warnings, silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=resolve_net_rules(
                    "", layer_is_outer=True, fab_caps=capability
                ).clearance_mm,
                date=_export_date(),
            )
        )
        model = {
            "layers": layer_names,
            "copper": copper,
            "pads": pads,
            "drills": self._drc_drills(ref_id),
            "silkscreen": silk_draws,
        }
        courtyards = [
            (
                i["refdes"],
                float(i["x"]),
                float(i["y"]),
                # The part's own derived keep-out radius (see
                # :meth:`_drc_geometry`) — NOT the flat
                # ``DEFAULT_COURTYARD_RADIUS_MM`` every instance used to
                # share regardless of size. The fallback below is a safety
                # net for a refdes the IR somehow didn't carry, not the
                # normal path.
                courtyard_radius.get(i["refdes"], pcb_drc.DEFAULT_COURTYARD_RADIUS_MM),
            )
            for i in design["instances"]
            if i["x"] is not None and i["y"] is not None
        ]
        net_classes = design.get("net_classes") or {}
        net_rules: dict[str, NetRules] = {
            str(n["name"]): resolve_net_rules(
                str(n.get("net_class") or ""),
                # Clearance (the only field check_clearance reads off this
                # map) doesn't depend on layer -- an arbitrary True is fine
                # here; realize.py is the caller that resolves per-layer.
                layer_is_outer=True,
                fab_caps=capability,
                overrides=net_classes.get(n.get("net_class") or ""),
                current_a=n.get("est_current_a"),
            )
            for n in design["nets"]
        }
        findings = pcb_drc.run_geometric_drc(
            model,
            capability=capability,
            outline=outline,
            courtyards=courtyards,
            net_rules=net_rules,
            unrouted=[
                {"net": r["name"], "note": r.get("note")}
                for r in self.store.pcb_route_status(ref_id)
                if r["status"] != "realized"
            ],
            census=silk_census,
        )
        run_id = uuid.uuid4().hex
        self.store.pcb_write_drc_findings(
            int(board["board_id"]), run_id, [f.to_row() for f in findings]
        )
        n_error = sum(1 for f in findings if f.severity == "error")
        n_warn = len(findings) - n_error
        head = f"# DRC — run {run_id[:8]} — {n_error} error(s), {n_warn} warn(s)"
        if not findings:
            return Response(body=head + "\n— no findings ✓")
        rows = [
            {
                "severity": f.severity,
                "rule": f.rule,
                "where": f.where,
                "margin_mm": "" if f.margin_mm is None else f"{f.margin_mm:+.3f}",
                "detail": f.detail,
            }
            for f in findings
        ]
        return Response(
            body=head
            + "\n"
            + render_agent_table(
                rows, schema=["severity", "rule", "where", "margin_mm", "detail"]
            )
        )

    # ── SVG render (pcb-svg-render) ─────────────────────────────────────
    def _render_svg(self, ref_id: int, args: dict[str, Any]) -> Response:
        """Publication-quality vector figure (:mod:`precis.pcb.svg`).
        ``args.level='board'`` (default) — realized copper (L5): outline +
        tracks/vias/pours off ``pcb_copper``, the same model shape
        :mod:`precis.pcb.gerber` writes from. **Pads and silkscreen are
        never included here** — the store has no data source for either
        yet (no resolved, instance-transformed pad geometry anywhere in
        this codebase, and no silkscreen table at all; see
        :mod:`precis.pcb.svg`'s module docstring). ``args.level='sketch'``
        — the L3 rubber-band sketch (placed components + straight
        connections, layer-coloured wherever a persisted
        ``pcb_routes.layer_assign`` says so). ``args.layers`` restricts a
        board render to a layer-name subset; ``args.include`` further
        restricts to a subset of :data:`precis.pcb.svg.DEFAULT_INCLUDE`.
        ``args.level='fab'`` renders the board FROM ITS GERBERS with a
        layer selector (:meth:`_render_fab_svg`) — the view that shows pads
        and mask, and the only one that can be trusted about what a fab
        will image. Returns the raw SVG text — never written to disk (this
        is an inline "eye", not an exporter)."""
        ref = self.store.get_ref(kind="pcb", id=ref_id)
        slug = ref.slug if ref is not None and ref.slug else str(ref_id)
        level = str(args.get("level") or "board").strip().lower()

        if level == "sketch":
            graph = self.store.pcb_graph(ref_id)
            if not graph["instances"]:
                return Response(
                    body="no parts on this design yet — nothing to sketch\n\n"
                    "Next: put(kind='pcb', id='slug', args={'components':[...]})"
                )
            ir = pcb_session.build_ir(graph)
            pcb_session.apply_route_overrides(ir, self.store.pcb_routes_get(ref_id))
            return Response(
                body=pcb_svg.render_sketch(ir, title=f"{slug} — sketch (L3)")
            )
        if level == "fab":
            return self._render_fab_svg(ref_id, slug)
        if level != "board":
            raise BadInput(
                f"view='svg' args.level={level!r} not recognized",
                options=["board", "sketch", "fab"],
            )

        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        model: dict[str, Any] = {
            "layers": layer_names,
            "outline": self._outline_from_features(ref_id),
            "copper": self.store.pcb_copper_list(int(board["board_id"])),
            # Mounting holes have no copper around them, so without this
            # the board render simply omits every one of them — the figure
            # looks clean rather than incomplete, which is the worst way
            # for a render to be wrong.
            "drills": self._drc_drills(ref_id),
        }
        raw_layers = args.get("layers")
        raw_include = args.get("include")
        svg_text = pcb_svg.render_board(
            model,
            layers=[str(x) for x in raw_layers] if raw_layers else None,
            include={str(x) for x in raw_include} if raw_include else None,
            title=f"{slug} — board (L5)",
        )
        return Response(body=svg_text)

    def _render_fab_svg(self, ref_id: int, slug: str) -> Response:
        """``level='fab'`` — the board rendered FROM ITS GERBERS, with a
        layer selector.

        Not a prettier ``level='board'``. That one draws the model; this
        one exports the fab set and reads it back, so what you are looking
        at is what a fab's own reader will see. The difference is not
        theoretical: ``svg.render_board`` applies a per-layer dash pattern
        as a layer cue, which made continuous B.Cu copper look broken and
        cost a session proving it was not. A view that is stylistically
        different from the artefact cannot verify the artefact.

        It is also the only view that shows pads, soldermask openings and
        drill hits, because those exist in the fab set and nowhere in the
        copper table — plus generated silk (:mod:`precis.pcb.silk`), the
        SAME builder :meth:`_render_gerber` calls, so this preview matches
        what a real export would carry.
        """
        design = self.store.pcb_load(ref_id)
        board = design["board"]
        if board is None:
            return Response(
                body="no board yet\n\nNext: put(kind='pcb', id='slug', "
                "args={'components':[...],'nets':[...]}) to create the design."
            )
        layer_names = [str(layer.get("name")) for layer in board["stackup"]]
        pads = self._drc_pads(ref_id, layer_names)
        graph = self.store.pcb_graph(ref_id)
        ir = pcb_session.build_ir(graph)
        instance_sides = {
            str(i.get("refdes")): str(i.get("layer") or "top")
            for i in design["instances"]
        }
        copper = self.store.pcb_copper_list(int(board["board_id"]))
        vias = [c for c in copper if c.get("ctype") == "via"]
        outline = self._outline_from_features(ref_id) or []
        pads, silk_draws, furniture_warnings, _silk_census, copper = (
            self._board_furniture(
                ir,
                pads,
                vias=vias,
                instance_sides=instance_sides,
                copper=copper,
                outline=outline,
                layer_names=layer_names,
                slug=slug,
                clearance_mm=self._furniture_clearance_mm(board["stackup"]),
                date=_export_date(),
            )
        )
        model: dict[str, Any] = {
            "layers": layer_names,
            "outline": outline,
            "copper": copper,
            "pads": pads,
            "silkscreen": silk_draws,
        }
        # allow_synthesized, because this is a picture and not an order.
        # export_fab's refusal exists to stop a land-pattern BOUND reaching
        # a fab; looking at one is exactly how you notice it is a bound.
        files = pcb_gerber.export_fab(model, name=slug, allow_synthesized=True)
        try:
            body = gerber_view.render_fab_svg(files, title=slug)
            # **A dropped fiducial or title block must not be invisible on the
            # surface a human INSPECTS the board with.** This path used to
            # discard these: a silently-missing title block looked identical
            # to one nobody asked for, and that cost a real diagnosis. The
            # gerber path prints them; here the SVG body is the whole
            # response, so they ride along inside the document.
            #
            # **`<desc>`, not an XML comment.** The first cut used a comment
            # and every renderer rejected the whole file: these messages are
            # full of `--` ("-- dropped", "-- skipped") and an XML comment
            # may not contain a double hyphen. That turned a diagnostic into
            # a corrupted artefact, and only opening the SVG revealed it —
            # the honesty mechanism broke the thing it was reporting on.
            # `<desc>` is a legal child of `<svg>`, ignored by renderers,
            # read by screen readers, and greppable.
            if furniture_warnings:
                notes = "\n".join(
                    "  " + w.replace("&", "&amp;").replace("<", "&lt;")
                    for w in furniture_warnings
                )
                desc = f"<desc>pcb render warnings:\n{notes}\n</desc>\n"
                body = body.replace("</svg>", f"{desc}</svg>", 1)
            return Response(body=body)
        except gerber_view.UnsupportedGerber as exc:
            raise BadInput(f"pcb: cannot render this fab set — {exc}") from exc

    # ── delete ───────────────────────────────────────────────────────
    def delete(self, *, id: str | int | None = None, **_kw: Any) -> Response:
        if id is None or not str(id).strip():
            raise BadInput("delete(kind='pcb') requires id= (the design slug)")
        ref = resolve_live_slug_ref(self.store, kind="pcb", id=str(id).strip())
        counts = self.store.pcb_delete(ref.id)
        n = counts.get("pcb_instances", 0)
        return Response(body=f"retired pcb design {ref.slug} ({n} instance(s))")

    # ── search ───────────────────────────────────────────────────────
    def search(
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
        self.store.blocks.bump_salience([b.id for b, _r, _s in triples])
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
        return self.store.blocks.search_blocks(
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

    def _toc(self, design: dict[str, Any]) -> str:
        parts = []
        board = design.get("board")
        if board is not None:
            parts.append(
                f"## board: {board['name']} — {self._stackup_summary(board['stackup'])}"
            )
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
        net_classes = design.get("net_classes") or {}
        if net_classes:
            crows = [
                {"class": name, "rules": json.dumps(rules)}
                for name, rules in net_classes.items()
            ]
            parts.append(
                "## net classes\n"
                + render_agent_table(crows, schema=["class", "rules"])
            )
        n_nets = len(design["nets"])
        route_status = design.get("route_status") or {}
        if n_nets:
            if route_status:
                summary = ", ".join(f"{n} {s}" for s, n in sorted(route_status.items()))
            else:
                summary = f"{n_nets} unrouted"
            parts.append(f"## route status: {summary}")
        return "\n".join(parts)

    def _stackup_summary(self, stackup: Any) -> str:
        """``4 layers: F.Cu/In1.Cu(GND)/In2.Cu/B.Cu`` — the plane_net (if
        any) parenthesised after its layer name."""
        layers = list(stackup or [])
        names = [
            f"{layer.get('name')}({layer['plane_net']})"
            if layer.get("plane_net")
            else str(layer.get("name"))
            for layer in layers
        ]
        return f"{len(layers)} layers: {'/'.join(names)}"

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
