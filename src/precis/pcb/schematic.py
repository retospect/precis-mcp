"""Net-label schematic — the design's electrical intent as a drawing.

Renders the netlist the way service manuals do when nobody hand-placed a
schematic: every component is a box with its pins on the edges, and every
pin ends in a short stub carrying its NET LABEL. No wires are drawn
between parts — matching labels ARE the connection. That is a deliberate
dodge: auto-routing schematic wires legibly is its own research problem,
while the net-label style is the standard industry cheat (KiCad global
labels, every IC datasheet's application circuit) and stays readable at
any part count.

Pure function of :meth:`Store.pcb_graph`'s dict — no DB, no placement
needed (a design renders a schematic before its first ``op='place'``).
Consumed by the handler's ``view='schematic'`` and the web tab's
``/pcb/{slug}/schematic.svg``.

Reading aids, all deterministic:

* net labels are colour-coded by net name (stable hash → hue), so the eye
  can match the two ends of a net without tracing anything;
* ground-class nets get the three-bar ground glyph, power-class nets a
  rail bar, instead of a coloured label — mirroring how a schematic
  reader actually scans for rails;
* a pin on no net ends in a small open circle (the no-connect mark) —
  visibly intentional, not missing;
* every stub carries a ``<title>`` naming the net and its full member
  list, so hover answers "where does this go".
"""

from __future__ import annotations

import re
import zlib
from typing import Any
from xml.sax.saxutils import escape

# ── geometry constants (px) ──────────────────────────────────────────
_PIN_PITCH = 18.0  # vertical distance between pin rows
_HEADER_H = 30.0  # refdes + label lines above the first pin
_STUB_LEN = 22.0  # pin stub from box edge to the net label
_CHAR_W = 7.2  # ui-monospace advance at 12px — layout, not typography
_BOX_MIN_W = 96.0
_BOX_PAD_Y = 10.0
_ROW_GAP = 46.0  # vertical gap between shelf rows
_COL_GAP = 28.0  # horizontal gap between neighbouring part cells
_MARGIN = 24.0  # document margin
_TARGET_ROW_W = 1150.0  # shelf-wrap width


def _natural_key(pin: str) -> tuple[Any, ...]:
    """Sort '2' before '10' and 'A1' before 'A2' — datasheet pin order,
    not ASCII order."""
    return tuple(
        int(part) if part.isdigit() else part
        for part in re.split(r"(\d+)", pin)
        if part != ""
    )


def _net_hue(name: str) -> int:
    """Stable per-net hue — crc32, not ``hash()``, because the colour must
    survive interpreter restarts (two renders of one design must diff
    clean)."""
    return zlib.crc32(name.encode("utf-8")) % 360


def _is_ground(name: str, net_class: str | None) -> bool:
    return (net_class or "").lower() == "ground" or name.upper() in ("GND", "AGND")


def _is_power(name: str, net_class: str | None) -> bool:
    return (net_class or "").lower() == "power"


class _Part:
    """One component box: pins split left/right in natural order."""

    def __init__(
        self,
        refdes: str,
        label: str,
        pins: list[str],
        pin_net: dict[str, str],
    ) -> None:
        self.refdes = refdes
        self.label = label
        self.pin_net = pin_net
        ordered = sorted(pins, key=_natural_key)
        # First half left, second half right — a numeric dual-row part
        # comes out in its datasheet's counter-clockwise convention; a
        # 2-pin passive gets one pin a side.
        half = (len(ordered) + 1) // 2
        self.left = ordered[:half]
        self.right = ordered[half:]

        rows = max(len(self.left), len(self.right), 1)
        widest_pin = max((len(p) for p in pins), default=1)
        self.box_w = max(
            _BOX_MIN_W,
            # pin names inside both edges + a gap so they never collide
            2 * (widest_pin * _CHAR_W + 8.0) + 24.0,
            (len(label) * 0.55 * _CHAR_W) + 16.0,  # label is 9px, ~0.55x
        )
        self.box_h = _HEADER_H + rows * _PIN_PITCH + _BOX_PAD_Y

        def _halo(side: list[str]) -> float:
            widest = max((len(pin_net.get(p, "")) for p in side), default=0)
            return _STUB_LEN + widest * _CHAR_W + 10.0

        self.halo_l = _halo(self.left)
        self.halo_r = _halo(self.right)

    @property
    def cell_w(self) -> float:
        return self.halo_l + self.box_w + self.halo_r

    @property
    def cell_h(self) -> float:
        return self.box_h


def _label_glyph(
    x: float, y: float, name: str, net_class: str | None, *, rightward: bool
) -> str:
    """The net-label end of a stub: ground glyph, power rail, or a
    coloured text label. ``x`` is the stub tip; text grows away from the
    box."""
    if _is_ground(name, net_class):
        # three shrinking bars, drawn below the stub tip
        return (
            f'<g stroke="#111" stroke-width="1.6" fill="none">'
            f'<path d="M {x:.1f} {y:.1f} v 5"/>'
            f'<path d="M {x - 7:.1f} {y + 5:.1f} h 14"/>'
            f'<path d="M {x - 4.5:.1f} {y + 8.5:.1f} h 9"/>'
            f'<path d="M {x - 2:.1f} {y + 12:.1f} h 4"/></g>'
        )
    anchor = "start" if rightward else "end"
    tx = x + 4.0 if rightward else x - 4.0
    if _is_power(name, net_class):
        bar = f'<path d="M {x:.1f} {y - 5:.1f} v 10" stroke="#c62828" stroke-width="2.4"/>'
        return (
            f'{bar}<text x="{tx:.1f}" y="{y + 4:.1f}" text-anchor="{anchor}" '
            f'class="netlabel" fill="#c62828">{escape(name)}</text>'
        )
    colour = f"hsl({_net_hue(name)},55%,38%)"
    return (
        f'<text x="{tx:.1f}" y="{y + 4:.1f}" text-anchor="{anchor}" '
        f'class="netlabel" fill="{colour}">{escape(name)}</text>'
    )


def render_schematic_svg(graph: dict[str, Any], *, title: str = "schematic") -> str:
    """The whole design as one net-label schematic SVG (see module doc)."""
    net_class_of: dict[str, str | None] = {}
    net_members: dict[str, list[str]] = {}
    pin_net: dict[tuple[str, str], str] = {}
    for net in graph.get("nets", []):
        name = str(net.get("name", "?"))
        net_class_of[name] = net.get("net_class")
        members = [f"{m['refdes']}.{m['pin']}" for m in net.get("members", [])]
        net_members[name] = members
        for m in net.get("members", []):
            pin_net[(str(m["refdes"]), str(m["pin"]))] = name

    # Every pin the design knows about: net members + explicit no-connects.
    pins_of: dict[str, set[str]] = {}
    for (refdes, pin), _ in pin_net.items():
        pins_of.setdefault(refdes, set()).add(pin)
    for u in graph.get("unconnected", []):
        pins_of.setdefault(str(u["refdes"]), set()).add(str(u["pin"]))

    parts: list[_Part] = []
    for inst in sorted(graph.get("instances", []), key=lambda i: str(i["refdes"])):
        refdes = str(inst["refdes"])
        pins = sorted(pins_of.get(refdes, set()), key=_natural_key)
        parts.append(
            _Part(
                refdes,
                str(inst.get("label") or ""),
                pins,
                {p: pin_net.get((refdes, p), "") for p in pins},
            )
        )

    # ── shelf packing: fill rows left-to-right, wrap at the target width
    rows: list[list[_Part]] = [[]]
    x_cursor = 0.0
    for part in parts:
        if rows[-1] and x_cursor + part.cell_w > _TARGET_ROW_W:
            rows.append([])
            x_cursor = 0.0
        rows[-1].append(part)
        x_cursor += part.cell_w + _COL_GAP

    body: list[str] = []
    y = _MARGIN + 18.0  # room for the heading line
    doc_w = 0.0
    for row in rows:
        row_h = max((p.cell_h for p in row), default=0.0)
        x = _MARGIN
        for part in row:
            bx = x + part.halo_l  # box left edge
            by = y + (row_h - part.box_h) / 2.0
            body.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{part.box_w:.1f}" '
                f'height="{part.box_h:.1f}" class="part"/>'
            )
            body.append(
                f'<text x="{bx + part.box_w / 2:.1f}" y="{by + 14:.1f}" '
                f'text-anchor="middle" class="refdes">{escape(part.refdes)}</text>'
            )
            if part.label:
                body.append(
                    f'<text x="{bx + part.box_w / 2:.1f}" y="{by + 25:.1f}" '
                    f'text-anchor="middle" class="plabel">{escape(part.label)}</text>'
                )
            for side, pins in (("L", part.left), ("R", part.right)):
                for i, pin in enumerate(pins):
                    py = by + _HEADER_H + (i + 0.5) * _PIN_PITCH
                    net = part.pin_net.get(pin, "")
                    if side == "L":
                        edge, tip = bx, bx - _STUB_LEN
                        pin_x, pin_anchor = bx + 5.0, "start"
                    else:
                        edge, tip = bx + part.box_w, bx + part.box_w + _STUB_LEN
                        pin_x, pin_anchor = bx + part.box_w - 5.0, "end"
                    hover = (
                        f"net {net} — " + ", ".join(net_members.get(net, []))
                        if net
                        else f"{part.refdes}.{pin} — no connect"
                    )
                    body.append(
                        f"<g><title>{escape(hover)}</title>"
                        f'<path d="M {edge:.1f} {py:.1f} H {tip:.1f}" class="stub"/>'
                    )
                    if net:
                        body.append(
                            _label_glyph(
                                tip,
                                py,
                                net,
                                net_class_of.get(net),
                                rightward=(side == "R"),
                            )
                        )
                    else:
                        # the no-connect mark: a small open circle
                        body.append(
                            f'<circle cx="{tip:.1f}" cy="{py:.1f}" r="2.6" class="nc"/>'
                        )
                    body.append(
                        f'<text x="{pin_x:.1f}" y="{py + 3.5:.1f}" '
                        f'text-anchor="{pin_anchor}" class="pin">{escape(pin)}</text>'
                        "</g>"
                    )
            x += part.cell_w + _COL_GAP
        doc_w = max(doc_w, x - _COL_GAP + _MARGIN)
        y += row_h + _ROW_GAP
    doc_h = y - _ROW_GAP + _MARGIN

    n_parts = len(parts)
    n_nets = len(net_members)
    heading = escape(f"{title} — {n_parts} part(s), {n_nets} net(s), net-label style")
    # Attribute position: escape() alone leaves `"` intact, which would let a
    # slug break out of aria-label.
    aria = escape(title, {'"': "&quot;"})
    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {doc_w:.0f} {doc_h:.0f}"
 width="{doc_w:.0f}" height="{doc_h:.0f}" role="img" aria-label="{aria}">
<title>{heading}</title>
<style>
  text {{ font: 12px ui-monospace, monospace; }}
  .refdes {{ font-weight: 700; }}
  .plabel {{ font-size: 9px; fill: #666; }}
  .pin {{ font-size: 10px; fill: #333; }}
  .netlabel {{ font-size: 11px; font-weight: 600; }}
  .part {{ fill: #fdfdf6; stroke: #222; stroke-width: 1.4; rx: 3; }}
  .stub {{ stroke: #444; stroke-width: 1.3; fill: none; }}
  .nc {{ fill: none; stroke: #999; stroke-width: 1.3; }}
</style>
<rect x="0" y="0" width="{doc_w:.0f}" height="{doc_h:.0f}" fill="#ffffff"/>
<text x="{_MARGIN:.0f}" y="{_MARGIN - 6:.0f}" class="plabel">{heading}</text>
{chr(10).join(body)}
</svg>
"""


__all__ = ["render_schematic_svg"]
