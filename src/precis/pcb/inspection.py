"""Bounded text inspection over the same geometry fab/DRC consumes.

Text inspection (``wire``/``part``/``layer``/``snippet`` views) consumes
the fab model, the actual silk census, and read-only DRC findings. It does
not infer pad geometry from images. Wire topology is noded per layer,
joined through physical pads/vias, and filtered only after traversal so a
layer filter cannot reset cumulative distances. Results paginate below
4 KB with continuation calls retaining the selection.

Display rounding never changes connectivity. Pours are reported, not
assigned a fictitious travel distance. Density is occupied area per cell,
not a count of track endpoints.
"""

from __future__ import annotations

import json
import math
from collections import Counter, defaultdict
from dataclasses import dataclass
from fnmatch import fnmatchcase
from itertools import pairwise
from typing import Any

from shapely.geometry import LineString, Point, Polygon, box
from shapely.geometry.base import BaseGeometry
from shapely.ops import nearest_points, unary_union
from shapely.strtree import STRtree

from precis.errors import BadInput, NotFound
from precis.pcb import drc, gerber, pose, selectables

VIEWS = ("wire", "part", "layer", "snippet")
NEXT = "get(kind='skill', id='precis-pcb-help')"
MAX_BYTES = 3900
type Node = tuple[str, float, float]


def number(raw: Any, name: str, default: float = 0) -> float:
    try:
        value = float(default if raw is None else raw)
    except (TypeError, ValueError, OverflowError):
        raise BadInput(f"{name} must be a finite number", next=NEXT) from None
    if not math.isfinite(value) or value < 0:
        raise BadInput(f"{name} must be finite and nonnegative", next=NEXT)
    return value


def bounds(raw: Any) -> tuple[float, float, float, float]:
    try:
        if not isinstance(raw, (list, tuple)) or len(raw) != 4:
            raise ValueError
        vals = tuple(float(v) for v in raw)
        x0, y0, x1, y1 = vals
        if not all(math.isfinite(v) for v in vals) or x0 >= x1 or y0 >= y1:
            raise ValueError
    except (TypeError, ValueError, OverflowError):
        raise BadInput(
            "bbox must be [x0,y0,x1,y1] in mm, with positive area", next=NEXT
        ) from None
    return x0, y0, x1, y1


def xy(x: float, y: float) -> str:
    return f"({x:.2f},{y:.2f})"


def bbox_text(shape: BaseGeometry) -> str:
    a, b, c, d = shape.bounds
    return f"{xy(a, b)}..{xy(c, d)}"


def page(lines: list[str], slug: str, view: str, args: dict[str, Any]) -> str:
    rows: list[str] = []
    for line in lines:
        if len(line.encode("utf-8")) <= 1200:
            rows.append(line)
        else:
            rows.extend(
                ("" if i == 0 else "  continued: ") + line[i : i + 280]
                for i in range(0, len(line), 280)
            )
    lines = rows
    offset = number(args.get("offset"), "offset")
    if offset != int(offset):
        raise BadInput("offset must be an integer", next=NEXT)
    start = int(offset)
    if start > len(lines):
        raise BadInput("offset is past the inspection result", next=NEXT)
    out = [f"{slug} {view} | mm | rows {start}/{len(lines)}"]
    used = len(out[0].encode("utf-8")) + 1
    for i in range(start, len(lines)):
        line = lines[i]
        hint_args = {**args, "offset": i + 1}
        hint = (
            f"\nnext: get(kind='pcb', id={slug!r}, view={view!r}, args={hint_args!r})"
        )
        size = len(line.encode("utf-8")) + 1
        if used + size + len(hint.encode("utf-8")) > MAX_BYTES:
            continuation = {**args, "offset": i}
            out.append(
                f"next: get(kind='pcb', id={slug!r}, view={view!r}, args={continuation!r})"
            )
            break
        out.append(line)
        used += size
    return "\n".join(out)


@dataclass
class Item:
    kind: str
    layer: str
    net: str
    label: str
    shape: BaseGeometry
    row: dict[str, Any]


class Inspection:
    def __init__(
        self,
        model: dict[str, Any],
        graph: dict[str, Any],
        courtyards: dict[str, BaseGeometry],
        findings: list[drc.DrcFinding],
    ) -> None:
        self.model = model
        self.graph = graph
        self.courtyards = courtyards
        self.findings = findings
        self.layers = list(model["layers"])
        self.nets = {str(n["name"]): n for n in graph["nets"]}
        self.pin_aliases = {
            f"{m['refdes']}.{m['pad']}": f"{m['refdes']}.{m['pin']}"
            for n in graph["nets"]
            for m in n.get("members", [])
            if m.get("pad") is not None
        }
        self.instances = {str(i["refdes"]): i for i in graph["instances"]}
        self.items: list[Item] = []
        for kind, rows in (
            ("pad", model.get("pads", [])),
            ("copper", model.get("copper", [])),
        ):
            for row in rows:
                ctype = kind if kind == "pad" else str(row.get("ctype"))
                shape = drc._copper_item_polygon({**row, "ctype": ctype})
                if shape is None or shape.is_empty:
                    continue
                layers = (
                    gerber._via_layers(row, self.layers)
                    if ctype == "via"
                    else [str(row.get("layer") or "")]
                )
                label = (
                    f"{row.get('refdes', '?')}.{row.get('pin', '?')}"
                    if ctype == "pad"
                    else ctype
                )
                for layer in layers:
                    self.items.append(
                        Item(ctype, layer, str(row.get("net") or ""), label, shape, row)
                    )
        silk_items: dict[tuple[str, str, str], list[BaseGeometry]] = defaultdict(list)
        silk_rows: dict[tuple[str, str, str], dict[str, Any]] = {}
        for side, draws in (model.get("silkscreen") or {}).items():
            layer = "B.Silkscreen" if side == "bottom" else "F.Silkscreen"
            for row in draws:
                if row.get("shape") == "region":
                    shape = Polygon(row["polygon"])
                else:
                    points = selectables._draw_points(row)
                    if len(points) < 2:
                        continue
                    shape = LineString(points).buffer(
                        float(row.get("width_mm") or 0.15) / 2
                    )
                key = layer, str(row.get("role") or ""), str(row.get("refdes") or "")
                silk_items[key].append(shape)
                silk_rows[key] = row
        for key, shapes in sorted(silk_items.items()):
            layer, role, refdes = key
            self.items.append(
                Item(
                    "text" if role == "refdes" else "silk",
                    layer,
                    "",
                    refdes or role,
                    unary_union(shapes),
                    silk_rows[key],
                )
            )

    def accepts(self, net: str, args: dict[str, Any]) -> bool:
        return ("net" not in args or fnmatchcase(net, str(args["net"]))) and (
            "net_class" not in args
            or fnmatchcase(
                str(self.nets.get(net, {}).get("net_class") or ""),
                str(args["net_class"]),
            )
        )

    def region(
        self, args: dict[str, Any], *, required: bool = False
    ) -> BaseGeometry | None:
        if "bbox" in args and "refdes" in args:
            raise BadInput("choose bbox or refdes, not both", next=NEXT)
        if "margin" in args and "refdes" not in args:
            raise BadInput("margin requires refdes", next=NEXT)
        if "bbox" in args:
            return box(*bounds(args["bbox"]))
        if "refdes" in args:
            raw = args["refdes"]
            names = [raw] if isinstance(raw, str) else raw
            if (
                not isinstance(names, list)
                or not names
                or not all(isinstance(n, str) for n in names)
            ):
                raise BadInput(
                    "refdes must be a name or nonempty list of names", next=NEXT
                )
            missing = set(names) - self.courtyards.keys()
            if missing:
                raise NotFound(f"no placed courtyard for {sorted(missing)}", next=NEXT)
            shape = unary_union([self.courtyards[n] for n in names])
            margin = number(args.get("margin"), "margin", 1)
            x0, y0, x1, y1 = shape.bounds
            return box(x0 - margin, y0 - margin, x1 + margin, y1 + margin)
        if required:
            raise BadInput(
                "snippet needs bbox or refdes plus optional margin", next=NEXT
            )
        return None

    def selected(self, args: dict[str, Any], region: BaseGeometry | None) -> list[Item]:
        return [
            i
            for i in self.items
            if ("layer" not in args or i.layer == args["layer"])
            and self.accepts(i.net, args)
            and (region is None or i.shape.intersects(region))
        ]

    def finding_lines(
        self, names: set[str], region: BaseGeometry | None = None
    ) -> list[str]:
        out = []
        for finding in self.findings:
            owners: set[str] = set()
            shapes: list[BaseGeometry] = []
            for obj in finding.objects:
                owners.update(
                    str(obj[k]).removeprefix("part:")
                    for k in ("refdes", "a_refdes", "b_refdes", "a", "b")
                    if obj.get(k) is not None
                )
                for prefix in ("", "a_", "b_", "via_"):
                    x, y = obj.get(prefix + "x"), obj.get(prefix + "y")
                    if x is not None and y is not None:
                        shapes.append(Point(float(x), float(y)))
                shape = drc._copper_item_polygon(obj)
                if shape is not None:
                    shapes.append(shape)
            owner_hit = bool(owners & names)
            spatial_hit = region is not None and any(
                s.intersects(region) for s in shapes
            )
            if owner_hit or spatial_hit:
                out.append(
                    f"finding {finding.severity} {finding.rule}: {finding.where} {finding.detail}"
                )
        return out

    def part(self, args: dict[str, Any]) -> list[str]:
        pattern = str(args.get("refdes") or "*")
        matches = [
            i for rd, i in sorted(self.instances.items()) if fnmatchcase(rd, pattern)
        ]
        if not matches:
            raise NotFound(f"no part matching {pattern!r}", next=NEXT)
        lines = []
        for inst in matches:
            rd = str(inst["refdes"])
            side = "bottom" if pose.is_bottom(inst) else "top"
            pads = [
                i for i in self.items if i.kind == "pad" and i.row.get("refdes") == rd
            ]
            touching = {
                name
                for name, net in self.nets.items()
                if any(m["refdes"] == rd for m in net.get("members", []))
            } | {i.net for i in pads if i.net}
            if args.get("side", side) != side or (
                "net" in args and not any(self.accepts(name, args) for name in touching)
            ):
                continue
            placed = inst.get("x") is not None and inst.get("y") is not None
            loc = xy(float(inst["x"]), float(inst["y"])) if placed else "unplaced"
            lines.append(
                f"part {rd}: {loc} {side} rot={float(inst.get('rot') or 0):.2f}"
            )
            lines.append("  nets: " + ", ".join(sorted(touching)))
            if rd in self.courtyards:
                shape = self.courtyards[rd]
                lines.append(f"  courtyard bbox {bbox_text(shape)}")
                neighbors = sorted(
                    (shape.distance(s), other)
                    for other, s in self.courtyards.items()
                    if other != rd
                    and pose.is_bottom(self.instances[other]) == pose.is_bottom(inst)
                )
                lines.extend(
                    f"  neighbor {other} courtyard gap={gap:.2f}"
                    for gap, other in neighbors[:5]
                )
            else:
                lines.append("  courtyard unavailable")
            for pad in pads:
                lines.append(
                    f"  pad {pad.label} {pad.layer} {xy(pad.row['x'], pad.row['y'])} net={pad.net or '-'}"
                    + (" synthesized" if pad.row.get("synthesized") else "")
                )
            for silk in self.model.get("silk_census", []):
                if silk["refdes"] != rd or silk["kind"] != "refdes":
                    continue
                text = (
                    f"  label {silk['outcome']} spot={inst.get('label_spot') or 'auto'}"
                )
                if silk["x_mm"] is not None and placed:
                    x, y = silk["x_mm"], silk["y_mm"]
                    text += f" {xy(x, y)} offset={xy(x - float(inst['x']), y - float(inst['y']))}"
                if silk.get("reason"):
                    text += f" {silk['reason']}"
                lines.append(text)
            lines.extend(self.finding_lines({rd}))
        return lines or ["no parts match filters"]

    def layer(self, args: dict[str, Any]) -> list[str]:
        if "layer" not in args:
            raise BadInput("layer sheet needs layer", options=self.layers, next=NEXT)
        region = self.region(args)
        items = self.selected(args, region)
        lines = [
            f"layer {args['layer']} counts: {dict(sorted(Counter(i.kind for i in items).items()))}"
        ]
        lines.append(
            "net histogram (objects): "
            + json.dumps(dict(sorted(Counter(i.net or "-" for i in items).items())))
        )
        if not items:
            return [*lines, "empty layer"]
        shape = unary_union([i.shape for i in items])
        lines.append(f"extents {bbox_text(shape)}")
        area = region if region is not None else shape.envelope
        x0, y0, x1, y1 = area.bounds
        dx, dy = (x1 - x0) / 12, (y1 - y0) / 6
        if dx <= 0 or dy <= 0:
            return [*lines, "density unavailable: zero-area extents"]
        lines.append(
            f"density occupied area 12x6, top=+Y; cell={xy(dx, dy)}; . <10%, : <30%, * <60%, # >=60%"
        )
        for y in reversed(range(6)):
            row = ""
            for x in range(12):
                cell = box(
                    x0 + x * dx, y0 + y * dy, x0 + (x + 1) * dx, y0 + (y + 1) * dy
                )
                fraction = shape.intersection(cell).area / cell.area
                row += (
                    "."
                    if fraction < 0.1
                    else ":"
                    if fraction < 0.3
                    else "*"
                    if fraction < 0.6
                    else "#"
                )
            lines.append(row)
        return lines

    def snippet(self, args: dict[str, Any]) -> list[str]:
        region = self.region(args, required=True)
        assert region is not None
        items = self.selected(args, region)
        names = {
            rd for rd, shape in self.courtyards.items() if shape.intersects(region)
        }
        lines = [f"snippet bbox {bbox_text(region)}"]
        lines.extend(
            f"part {rd} courtyard {bbox_text(self.courtyards[rd])}"
            for rd in sorted(names)
        )
        for item in items:
            lines.append(
                f"{item.kind} {item.label} {item.layer} net={item.net or '-'} bbox={bbox_text(item.shape)}"
            )
            if item.kind == "track":
                for seg in item.row.get("segments") or []:
                    if LineString(drc._flatten_segments([seg])).intersects(region):
                        lines.append(
                            f"  {seg.get('shape', 'line')} {xy(*seg['start'])}->{xy(*seg['end'])}"
                        )
        for silk in self.model.get("silk_census", []):
            if silk["refdes"] in names:
                lines.append(
                    f"silk census {silk['refdes']} {silk['kind']} {silk['side']}: {silk['outcome']}"
                    + (
                        f" {xy(silk['x_mm'], silk['y_mm'])}"
                        if silk["x_mm"] is not None
                        else ""
                    )
                )
        lines.extend(self.finding_lines(names, region))
        return lines

    def wire(self, args: dict[str, Any]) -> list[str]:
        pin = args.get("pin")
        if pin is not None:
            pin = self.pin_aliases.get(str(pin), pin)
        if "net" not in args and pin is None:
            raise BadInput("wire needs net or pin=REFDES.PAD", next=NEXT)
        pads = [i for i in self.items if i.kind == "pad"]
        if pin is not None:
            matching = {p.net for p in pads if p.label == pin}
            if not matching:
                raise NotFound(f"no placed pad {pin!r}", next=NEXT)
            if "net" in args and not any(
                fnmatchcase(n, str(args["net"])) for n in matching
            ):
                raise BadInput("pin and net select different nets", next=NEXT)
        else:
            matching = set(self.nets)
        nets = [n for n in sorted(matching) if self.accepts(n, args)]
        if not nets:
            raise NotFound("no net matches wire filters", next=NEXT)
        min_length = number(args.get("min_length"), "min_length")
        lines = []
        for net in nets:
            lines.extend(self._wire_net(net, pin, args.get("layer"), min_length))
        return lines

    def _wire_net(
        self, net: str, pin: Any, layer_filter: Any, min_length: float
    ) -> list[str]:
        # Full graph first; layer/length are display filters, not topology edits.
        adj: dict[Node, list[tuple[Node, float, str]]] = defaultdict(list)
        roots: dict[str, set[Node]] = defaultdict(set)
        sites = [i for i in self.items if i.net == net and i.kind in ("pad", "via")]

        def key(layer: str, x: float, y: float) -> Node:
            return layer, round(float(x), 6), round(float(y), 6)

        def edge(a: Node, b: Node, length: float, label: str) -> None:
            if a != b:
                adj[a].append((b, length, label))
                adj[b].append((a, length, label))

        for site in sites:
            n = key(site.layer, site.row["x"], site.row["y"])
            if site.kind == "pad":
                roots[site.label].add(n)
            adj[n]
        by_site: dict[tuple[str, float, float], list[Item]] = defaultdict(list)
        for site in sites:
            by_site[
                (
                    site.label if site.kind == "pad" else "via",
                    site.row["x"],
                    site.row["y"],
                )
            ].append(site)
        for group in by_site.values():
            for a, b in pairwise(group):
                na = key(a.layer, a.row["x"], a.row["y"])
                nb = key(b.layer, b.row["x"], b.row["y"])
                label = (
                    f"via {xy(na[1], na[2])} layers={','.join(s.layer for s in group)}"
                    if a.kind == "via"
                    else f"pad {a.label} plated-through"
                )
                edge(na, nb, 0, label)
        tracks = [i for i in self.items if i.kind == "track" and i.net == net]
        for layer in self.layers:
            layer_tracks = [i for i in tracks if i.layer == layer]
            centerlines = [
                LineString(drc._flatten_segments(i.row.get("segments") or []))
                for i in layer_tracks
            ]
            if not centerlines:
                continue
            # Thick copper can touch without intersecting centre lines.
            # Such contacts join paths with no invented centre-line length.
            tree = STRtree([i.shape for i in layer_tracks])
            joins: list[tuple[Any, Any]] = []
            for i, item in enumerate(layer_tracks):
                for other in tree.query(item.shape, predicate="intersects"):
                    j = int(other)
                    if j <= i or centerlines[i].intersects(centerlines[j]):
                        continue
                    joins.append(nearest_points(centerlines[i], centerlines[j]))
            merged = unary_union(centerlines)
            parts = [merged] if merged.geom_type == "LineString" else list(merged.geoms)
            local_sites = [s for s in sites if s.layer == layer]
            for path in parts:
                distances = {0.0, path.length}
                contacts = []
                midpoint = path.interpolate(0.5, normalized=True)
                half_width = max(
                    (
                        float(item.row.get("width_mm") or 0) / 2
                        for line, item in zip(centerlines, layer_tracks, strict=True)
                        if line.distance(midpoint) < 1e-7
                    ),
                    default=0,
                )
                stroke = path.buffer(half_width) if half_width > 0 else path
                for a, b in joins:
                    for point in (a, b):
                        if path.distance(point) < 1e-7:
                            distances.add(path.project(point))
                for site in local_sites:
                    if not site.shape.intersects(stroke):
                        continue
                    center = Point(site.row["x"], site.row["y"])
                    distance = path.project(center)
                    distances.add(distance)
                    contacts.append((site, distance))
                # Keep bends visible while noding T junctions and pad contacts.
                for coord in list(path.coords)[1:-1]:
                    distances.add(path.project(Point(coord)))
                ordered = sorted(distances)
                for a, b in pairwise(ordered):
                    pa, pb = path.interpolate(a), path.interpolate(b)
                    edge(
                        key(layer, pa.x, pa.y), key(layer, pb.x, pb.y), b - a, "segment"
                    )
                for site, distance in contacts:
                    point = path.interpolate(distance)
                    edge(
                        key(layer, point.x, point.y),
                        key(layer, site.row["x"], site.row["y"]),
                        0,
                        f"{site.kind} {site.label}",
                    )
            for a, b in joins:
                edge(key(layer, a.x, a.y), key(layer, b.x, b.y), 0, "copper contact")
        lines = [f"net {net} class={self.nets.get(net, {}).get('net_class') or '-'}"]
        pad_nodes: dict[Node, set[str]] = defaultdict(set)
        for label, nodes in roots.items():
            for n in nodes:
                pad_nodes[n].add(label)
        if pin is None:
            remaining = set(adj)
            while remaining:
                first = min(remaining)
                component: set[Node] = set()
                pending = [first]
                while pending:
                    current = pending.pop()
                    if current in component:
                        continue
                    component.add(current)
                    pending.extend(n for n, _length, _label in adj[current])
                remaining -= component
                if not any(n in pad_nodes for n in component):
                    roots[f"copper {first[0]} {xy(first[1], first[2])} (no pin)"].add(
                        first
                    )
        visited_total: set[Node] = set()
        for label, nodes in sorted(roots.items()):
            if pin is not None and label != pin:
                continue
            lines.append(label if label.startswith("copper ") else f"pin {label}")
            seen: set[Node] = set()
            stack: list[tuple[Node, float, int, Node | None, float, str]] = [
                (n, 0.0, 0, None, 0.0, "") for n in sorted(nodes, reverse=True)
            ]
            while stack:
                current, distance, depth, parent, length, kind = stack.pop()
                if current in seen:
                    if (
                        parent is not None
                        and kind == "segment"
                        and length >= min_length
                        and (layer_filter is None or current[0] == layer_filter)
                    ):
                        lines.append(
                            f"  loop segment {current[0]} len={length:.2f} cum={distance:.2f} "
                            f"{xy(parent[1], parent[2])}->{xy(current[1], current[2])}"
                        )
                    continue
                seen.add(current)
                visited_total.add(current)
                layer, x, y = current
                if (
                    parent is not None
                    and (layer_filter is None or layer == layer_filter)
                    and (kind != "segment" or length >= min_length)
                ):
                    prefix = "  " * min(depth, 6)
                    lines.append(
                        f"{prefix}{kind} {layer} len={length:.2f} cum={distance:.2f} "
                        f"{xy(parent[1], parent[2])}->{xy(x, y)}"
                    )
                others = pad_nodes[current] - {label}
                if others:
                    lines.append(
                        f"  endpoint {','.join(sorted(others))} cum={distance:.2f} {layer} {xy(x, y)}"
                    )
                neighbors = sorted(adj[current], key=lambda e: (e[0], e[2]))
                children = [e for e in neighbors if e[0] not in seen]
                if len(children) > 1:
                    lines.append(
                        f"  branch {layer} {xy(x, y)} cum={distance:.2f} arms={len(children)}"
                    )
                stack.extend(
                    (n, distance + l, depth + 1, current, l, k)
                    for n, l, k in reversed(children)
                )
            if len(seen) == 1 and not adj[next(iter(seen))]:
                lines.append("  endpoint: no stored track touches this pin")
        unreached = set(adj) - visited_total
        if unreached:
            lines.append(
                f"disconnected copper: {len(unreached)} graph sites not reached from selected pins"
            )
        pours = sum(1 for i in self.items if i.net == net and i.kind == "pour")
        if pours:
            lines.append(f"pours: {pours}; no centre-line travel distance assigned")
        if not roots:
            lines.append("no placed pin geometry; stored copper only")
        return lines


def render(inspection: Inspection, slug: str, view: str, args: dict[str, Any]) -> str:
    allowed = {
        "wire": {"net", "pin", "layer", "min_length", "net_class", "offset"},
        "part": {"refdes", "side", "net", "offset"},
        "layer": {"layer", "net", "net_class", "bbox", "refdes", "margin", "offset"},
        "snippet": {"bbox", "refdes", "margin", "layer", "net", "net_class", "offset"},
    }
    unknown = set(args) - allowed[view]
    if unknown:
        raise BadInput(
            f"{view} does not accept {sorted(unknown)}",
            options=sorted(allowed[view]),
            next=NEXT,
        )
    if len(repr(args).encode("utf-8")) > 1000:
        raise BadInput(
            "inspection filters exceed 1000 bytes; narrow the selection", next=NEXT
        )
    layers = [*inspection.layers, "F.Silkscreen", "B.Silkscreen"]
    if "layer" in args and args["layer"] not in layers:
        raise BadInput(f"unknown layer {args['layer']!r}", options=layers, next=NEXT)
    if "side" in args and args["side"] not in ("top", "bottom"):
        raise BadInput(
            "side must be top or bottom", options=["top", "bottom"], next=NEXT
        )
    lines = getattr(inspection, view)(args)
    return page(lines, slug, view, args)
