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
  list, so hover answers "where does this go";
* a pin whose physical pad number differs from its name (``pin_numbers``
  — the footprint ``pin_map`` inverted by the handler, gr520803) draws
  the number small above the stub at the box edge, so "VSTOR" reads as
  pad 13 without inventing a second name for a passive's '1'/'2';
* every stub carries a direction mark at mid-stub (gr520809): the §1i
  etype as a KiCad-style glyph — input/output chevrons, bidi diamond,
  tristate chevron + open diamond, open_drain chevron + open circle,
  passive square, nc ×, power in/out chevron + tip bar — and a ``?``
  when the pin declares none, because "direction unknown" must be
  VISIBLE, never absent. The stub's ``<title>`` is multi-line: the net
  and its members, the pin's etype and pad, the net's declared spec
  (class, current, working voltage, impedance, edge rate, hint), and
  the pin's bounds / functions / flex-slot assignment.

**Focus mode and pinning** (``docs/backlog/pcb-workbench-layout.md``
slice 1). ``render_schematic_svg(graph, focus=<handle>)`` dims every part
outside the focus target's one-hop neighbourhood (signal nets only — a
part's power/ground nets would flood the view), keeping positions fixed;
the web tab reloads ``schematic.svg?focus=<handle>`` to enter it. A part
whose instance is placement-locked (``pcb_instances.fixed``) is drawn
with a pushpin and stamped ``data-pinned`` — ``POST /pcb/{slug}/pin``
toggles the lock, so the pin survives a reload.

**Cross-probe identity.** Every part is wrapped in a ``data-handle``
group and every pin stub and net label carries the same grammar the fab
render stamps (:mod:`precis.pcb.argue` — ``U1``, ``U1.3``, ``net:GND``),
plus ``data-pcb-part``/``-pad``/``-net``/``data-net-class`` attributes —
so the workbench's hover/click wiring (``static/pcb-crossprobe.js``)
treats a part, pin or net as one object in both panes, the way
``gerber_view``'s board side already does.

**Sheets** (``docs/backlog/pcb-workbench-layout.md`` slice 2). The
schematic splits by the board's authored placement groups — the
``"group"`` key :meth:`Store.pcb_graph` hoists from instance meta.
``sheet=<name>`` renders only that group's parts; ``sheet=None`` is the
overview tab holding everything (ungrouped parts live there only). A
signal net with members on other sheets ends each stub in an off-sheet
connector — a flag glyph naming the destination sheets (``→ MCU``),
plus ``overview`` when the net also has ungrouped members, the only
place they render. Power/ground nets keep their global rail/ground
glyphs and never get connectors — the same "power is global, never
flood" convention focus mode and the click cross-probe already follow
(KiCad's own rule: power symbols connect across sheets without a
connector).
"""

from __future__ import annotations

import re
import zlib
from typing import Any
from xml.sax.saxutils import escape

from precis.pcb import PIN_ETYPES, handles
from precis.utils.text import esc

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
    not ASCII order. Each segment is tagged (0=number, 1=text) because a
    part can mix numeric and named pins — an imported EasyEDA symbol's
    'A1' next to '1' must never compare ``int < str`` (gr496856)."""
    return tuple(
        (0, int(part)) if part.isdigit() else (1, part)
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


def sheet_map(graph: dict[str, Any]) -> dict[str, str]:
    """refdes → its authored placement group (the ``"group"`` key
    :meth:`Store.pcb_graph` hoists from instance meta) — the sheet the
    workbench's cross-probe switches to for the part."""
    return {
        str(i["refdes"]): str(i["group"])
        for i in graph.get("instances", [])
        if i.get("group") and i.get("refdes") is not None
    }


def sheet_names(graph: dict[str, Any]) -> list[str]:
    """The design's placement-group sheet names, sorted — the tab list
    the workbench renders. Ungrouped parts are on no named sheet; they
    live on the overview only."""
    return sorted(set(sheet_map(graph).values()))


def _focus_set(
    focus: str | None,
    all_refdes: set[str],
    net_names: set[str],
    parts_on_net: dict[str, set[str]],
    nets_of_part: dict[str, set[str]],
    net_class_of: dict[str, str | None],
) -> tuple[set[str] | None, str | None, set[str] | None]:
    """The one-hop neighbourhood to keep bright in focus mode, else
    ``(None, None, None)`` for no focus (nothing dimmed). ``focus`` is a
    cross-probe handle: a refdes (``U1``), a pin (``U1.3`` — focuses its
    part), or a net (``net:GND``). A part focus expands through its SIGNAL
    nets only — power/ground nets are shared board-wide, so expanding
    through them would flood the view with everything; excluding them is
    the same rule the click cross-probe follows (static/pcb-crossprobe.js).
    A focus target the design doesn't hold is a no-op (returns no focus),
    never a view dimmed down to one missing part.

    The third value, ``focus_nets``, is what ``only=True`` keeps pins on:
    a net focus keeps ``{name}``; a part focus keeps the part's signal
    nets (the same expansion the bright set follows); no focus → None."""
    f = (focus or "").strip()
    if not f:
        return None, None, None
    if f.startswith("net:"):
        name = f[4:]
        if name not in net_names:
            return None, None, None
        return set(parts_on_net.get(name, set())), None, {name}
    refdes = f.split(".", 1)[0] if "." in f else f
    if refdes not in all_refdes:
        return None, None, None
    bright = {refdes}
    signal = set()
    for net in nets_of_part.get(refdes, set()):
        if _is_ground(net, net_class_of.get(net)) or _is_power(
            net, net_class_of.get(net)
        ):
            continue
        signal.add(net)
        bright |= parts_on_net.get(net, set())
    return bright, refdes, signal


class _Part:
    """One component box: pins split left/right in natural order."""

    def __init__(
        self,
        refdes: str,
        label: str,
        pins: list[str],
        pin_net: dict[str, str],
        *,
        fixed: bool = False,
        pin_off: dict[str, list[str]] | None = None,
        pin_extra: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self.refdes = refdes
        self.label = label
        self.pin_net = pin_net
        #: pin → the sheet names its net continues on (off-sheet
        #: connector), absent for same-sheet/power/ground/no-connect.
        self.pin_off = pin_off or {}
        #: pin → declared extras for the hover title (gr520809):
        #: ``functions`` (pin_functions), ``max_current_a`` and ``slot``
        #: (the flex slot the pin was assigned), absent when undeclared.
        self.pin_extra = pin_extra or {}
        #: the instance carries a placement lock (``pcb_instances.fixed`` is
        #: 'xy'/'both') — "I want this part to stay here". Drawn as a pushpin
        #: and stamped ``data-pinned`` so the pin survives a reload.
        self.fixed = fixed
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
            def label_w(pin: str) -> int:
                net = pin_net.get(pin, "")
                off = self.pin_off.get(pin)
                # the drawn text: "net" or "net → dest, dest" for a
                # cross-sheet connector
                return len(net) + (len(" → " + ", ".join(off)) if off else 0)

            widest = max((label_w(p) for p in side), default=0)
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


def _pin_dir_glyph(
    etype: str | None, mx: float, py: float, tip: float, out: float
) -> str:
    """The mid-stub direction mark (gr520809) — drawn on EVERY pin, an
    explicit ``?`` when the etype is undeclared: direction unknown must
    be visible, not absent. ``mx`` is mid-stub, ``out`` ±1 the stub's
    direction off the box edge, ``tip`` the stub's far end (the power
    bar rides it)."""

    def chevron(d: float, x: float) -> str:
        # vertex leads in direction d (±1), arms trail
        return (
            f"M {x - 2.6 * d:.1f} {py - 3.0:.1f} L {x + 3.4 * d:.1f} {py:.1f} "
            f"L {x - 2.6 * d:.1f} {py + 3.0:.1f}"
        )

    def path(d: str) -> str:
        return f'<path d="{d}" class="pindir"/>'

    if etype == "input":
        return path(chevron(-out, mx))
    if etype == "output":
        return path(chevron(out, mx))
    if etype == "bidi":
        return path(
            f"M {mx - 3.4:.1f} {py:.1f} L {mx:.1f} {py - 3.4:.1f} "
            f"L {mx + 3.4:.1f} {py:.1f} L {mx:.1f} {py + 3.4:.1f} Z"
        )
    if etype == "passive":
        return (
            f'<rect x="{mx - 3.0:.1f}" y="{py - 3.0:.1f}" width="6" '
            f'height="6" class="pindir"/>'
        )
    if etype == "nc":
        return path(
            f"M {mx - 3.0:.1f} {py - 3.0:.1f} L {mx + 3.0:.1f} {py + 3.0:.1f} "
            f"M {mx + 3.0:.1f} {py - 3.0:.1f} L {mx - 3.0:.1f} {py + 3.0:.1f}"
        )
    bar = path(f"M {tip:.1f} {py - 4.0:.1f} V {py + 4.0:.1f}")
    if etype == "power_in":
        return path(chevron(-out, mx)) + bar
    if etype == "power_out":
        return path(chevron(out, mx)) + bar
    if etype == "tristate":
        dx = mx + 4.6 * out
        return path(chevron(out, mx - 2.0 * out)) + path(
            f"M {dx - 2.4 * out:.1f} {py:.1f} L {dx:.1f} {py - 2.4:.1f} "
            f"L {dx + 2.4 * out:.1f} {py:.1f} L {dx:.1f} {py + 2.4:.1f} Z"
        )
    if etype == "open_drain":
        return path(chevron(out, mx - 2.0 * out)) + (
            f'<circle cx="{mx + 4.6 * out:.1f}" cy="{py:.1f}" r="2" class="pindir"/>'
        )
    # undeclared (or outside the closed vocabulary): '?' — "direction
    # unknown" is required-visible, never a missing glyph
    return (
        f'<text x="{mx:.1f}" y="{py + 3.0:.1f}" text-anchor="middle" '
        f'class="pindirt">?</text>'
    )


#: §1j/Slice-5 per-pin bounds the hover title lists when declared
#: (``pcb_pins.meta`` — the same keys the ERC rules read).
_PIN_BOUND_KEYS = (
    "max_voltage_v",
    "vih",
    "vil",
    "max_current_a",
    "est_current_a",
    "current_role",
    "rail",
    "min_dielectric",
)


def _pin_title(
    refdes: str,
    pin: str,
    net: str,
    etype: str | None,
    pad: str | None,
    members: list[str] | None,
    net_rec: dict[str, Any] | None,
    meta: dict[str, Any] | None,
    extra: dict[str, Any] | None,
) -> list[str]:
    """The stub's multi-line hover (gr520809): the net/members line
    (verbatim — tests assert it), then the pin's etype or "direction
    unknown", the net's declared spec fields, and the pin's bounds,
    functions and flex slot — everything declared, nothing guessed."""
    lines = [
        f"net {net} — " + ", ".join(members or [])
        if net
        else f"{refdes}.{pin} — no connect"
    ]
    lines.append(
        f"{refdes}.{pin}"
        + (f" (pad {pad})" if pad is not None else "")
        + f" — {etype or 'direction unknown'}"
    )
    if net and net_rec:
        bits = []
        if net_rec.get("net_class"):
            bits.append(f"class {net_rec['net_class']}")
        if net_rec.get("est_current_a") is not None:
            bits.append(f"est {net_rec['est_current_a']}A")
        if net_rec.get("working_voltage_v") is not None:
            bits.append(f"working {net_rec['working_voltage_v']}V")
        if net_rec.get("impedance_ohm") is not None:
            bits.append(f"{net_rec['impedance_ohm']}Ω")
        if net_rec.get("edge_rate_v_per_ns") is not None:
            bits.append(f"edge {net_rec['edge_rate_v_per_ns']}V/ns")
        if net_rec.get("function_hint") is not None:
            bits.append(str(net_rec["function_hint"]))
        if bits:
            lines.append(f"net {net} — " + " · ".join(bits))
    meta = meta or {}
    bounds = [f"{k}={meta[k]}" for k in _PIN_BOUND_KEYS if meta.get(k) is not None]
    if bounds:
        lines.append("pin — " + ", ".join(bounds))
    extra = extra or {}
    fns = extra.get("functions") or []
    if fns:
        seg = "functions " + ", ".join(fns)
        if extra.get("max_current_a") is not None:
            seg += f" (max {extra['max_current_a']}A)"
        lines.append("pin — " + seg)
    if extra.get("slot"):
        lines.append(f"pin — flex slot {extra['slot']}")
    return lines


def _offsheet_glyph(
    x: float, y: float, name: str, dests: list[str], *, rightward: bool
) -> str:
    """The off-sheet connector (slice 2): a flag at the stub tip naming
    the net and the sheet(s) it continues on — ``SIG → MCU`` — in the
    net's colour. ``x`` is the stub tip; the flag grows away from the
    box, pointing outward the way a KiCad sheet pin does.

    Each destination is its own ``<tspan data-sheet="…">`` so the
    workbench's click wiring can switch straight to the named sheet
    (gr520804 — previously one flat ``NET → a, b`` run that neither
    explained itself nor went anywhere when clicked); ``overview``
    carries ``data-sheet=""``, the key pcb-sheets.js uses for the
    catch-all tab. The flag's own ``<title>` says what a flag means at
    all."""
    text = f"{name} → {', '.join(dests)}"
    w = len(text) * _CHAR_W + 14.0
    d = 1.0 if rightward else -1.0
    pts = " ".join(
        f"{x + d * px:.1f},{y + py:.1f}"
        for px, py in ((0, -8), (w - 8, -8), (w, 0), (w - 8, 8), (0, 8))
    )
    colour = f"hsl({_net_hue(name)},55%,38%)"
    anchor = "start" if rightward else "end"
    tx = x + d * 6.0
    runs = ", ".join(
        f'<tspan data-sheet="{esc("" if dest == "overview" else dest, quote=True)}"'
        f' class="offsheet-dest">'
        f"<title>net {escape(name)} continues on sheet {escape(dest)}"
        " — click to open it</title>"
        f"{escape(dest)}</tspan>"
        for dest in dests
    )
    return (
        f'<polygon points="{pts}" class="offsheet" fill="#fdfdf6" '
        f'stroke="{colour}" stroke-width="1.2">'
        f"<title>off-sheet net {escape(name)} — continues on sheet(s): "
        f"{escape(', '.join(dests))}; click a destination to open that "
        "sheet</title></polygon>"
        f'<text x="{tx:.1f}" y="{y + 3.5:.1f}" text-anchor="{anchor}" '
        f'class="netlabel" fill="{colour}">{escape(name + " → ")}{runs}</text>'
    )


def render_schematic_svg(
    graph: dict[str, Any],
    *,
    title: str = "schematic",
    focus: str | None = None,
    sheet: str | None = None,
    pin_numbers: dict[str, dict[str, str]] | None = None,
    only: bool = False,
) -> str:
    """The design as a net-label schematic SVG (see module doc).

    ``focus`` enters focus mode (the spec's "fisheye"): the given part or
    net and its one-hop neighbours stay bright while every other part is
    dimmed in place — nothing moves. ``focus`` is a cross-probe handle
    (``U1``, ``U1.3``, ``net:GND``); an unknown target dims nothing.

    ``sheet`` names a placement group (``sheet_names``): only its parts
    render, and signal nets continuing on other sheets end their stubs
    in off-sheet connectors. ``None`` is the overview — every part, no
    connectors; an unknown sheet name renders the overview too, the same
    graceful no-op an unknown focus target gets.

    ``pin_numbers`` (gr520803) maps refdes → pin name → the physical pad
    number the footprint binds it to; a pin whose number differs from
    its name gets a small ``pinno`` label at the box edge.

    ``only`` (gr520807) turns focus dimming into removal: with a live
    ``focus`` the dimmed parts and every pin off the focus's nets are
    dropped outright — the compact relayout a grey-out cannot give. No
    focus (or an unknown target) makes ``only`` a no-op.
    """
    net_class_of: dict[str, str | None] = {}
    net_members: dict[str, list[str]] = {}
    net_rec: dict[str, dict[str, Any]] = {}
    pin_net: dict[tuple[str, str], str] = {}
    parts_on_net: dict[str, set[str]] = {}
    nets_of_part: dict[str, set[str]] = {}
    pin_etype: dict[tuple[str, str], str | None] = {}
    pin_meta: dict[tuple[str, str], dict[str, Any]] = {}
    pin_pad: dict[tuple[str, str], str] = {}
    for net in graph.get("nets", []):
        name = str(net.get("name", "?"))
        net_class_of[name] = net.get("net_class")
        net_rec[name] = net
        members = [f"{m['refdes']}.{m['pin']}" for m in net.get("members", [])]
        net_members[name] = members
        for m in net.get("members", []):
            refdes = str(m["refdes"])
            pin = str(m["pin"])
            pin_net[(refdes, pin)] = name
            # the §1i etype is the PIN_ETYPES word among the pin's tags
            # (same resolution drc._pin_etype uses); tags/meta/pad feed
            # the stub's direction glyph and hover lines (gr520809)
            pin_etype[(refdes, pin)] = next(
                (t for t in PIN_ETYPES if t in (m.get("tags") or [])), None
            )
            if m.get("meta"):
                pin_meta[(refdes, pin)] = dict(m["meta"])
            if m.get("pad") is not None:
                pin_pad[(refdes, pin)] = str(m["pad"])
            parts_on_net.setdefault(name, set()).add(refdes)
            nets_of_part.setdefault(refdes, set()).add(name)

    # Sheet membership (slice 2): authored placement groups. A ``sheet``
    # render draws that group's parts; net → the sheets its members live
    # on (named sheets only; ``_UNGROUPED`` marks members on no sheet).
    sheet_of = sheet_map(graph)
    if sheet is not None and sheet not in sheet_names(graph):
        sheet = None
    _UNGROUPED = "overview"  # connector text for the catch-all tab
    net_dests: dict[str, list[str]] = {}
    if sheet is not None:
        for name, members in net_members.items():
            if _is_ground(name, net_class_of.get(name)) or _is_power(
                name, net_class_of.get(name)
            ):
                continue  # rails are global — glyph, never a connector
            hold = {sheet_of.get(r) for r in parts_on_net.get(name, set())}
            others = sorted(h for h in hold if h is not None and h != sheet)
            if None in hold:
                # a member on no named sheet continues on the overview
                others.append(_UNGROUPED)
            if others:
                net_dests[name] = others

    # Every pin the design knows about: net members + explicit no-connects.
    pins_of: dict[str, set[str]] = {}
    for (refdes, pin), _ in pin_net.items():
        pins_of.setdefault(refdes, set()).add(pin)
    for u in graph.get("unconnected", []):
        refdes_u, pin_u = str(u["refdes"]), str(u["pin"])
        pins_of.setdefault(refdes_u, set()).add(pin_u)
        pin_etype[(refdes_u, pin_u)] = next(
            (t for t in PIN_ETYPES if t in (u.get("tags") or [])), None
        )
        if u.get("meta"):
            pin_meta[(refdes_u, pin_u)] = dict(u["meta"])
        if u.get("pad") is not None:
            pin_pad[(refdes_u, pin_u)] = str(u["pad"])

    bright, focus_ref, focus_nets = _focus_set(
        focus,
        {
            str(i["refdes"])
            for i in graph.get("instances", [])
            if sheet is None or sheet_of.get(str(i["refdes"])) == sheet
        },
        set(net_members),
        parts_on_net,
        nets_of_part,
        net_class_of,
    )
    keep = bright if only else None  # 'only connected' (gr520807): drop, not dim

    parts: list[_Part] = []
    for inst in sorted(graph.get("instances", []), key=lambda i: str(i["refdes"])):
        refdes = str(inst["refdes"])
        if sheet is not None and sheet_of.get(refdes) != sheet:
            continue
        if keep is not None and refdes not in keep:
            continue
        pins = sorted(pins_of.get(refdes, set()), key=_natural_key)
        if keep is not None and focus_nets is not None and refdes != focus_ref:
            # a kept neighbour shows only the pins tying it to the focus
            # — filter BEFORE _Part builds its geometry off the list
            pins = [p for p in pins if pin_net.get((refdes, p)) in focus_nets]
        # the 1b declarations' hover extras (gr520809): pin_functions
        # (a record {functions, max_current_a} or a bare list — the same
        # normalize topoplace's _funcs_of does) and the flex slot a pin
        # was assigned to
        decl = inst.get("pin_decls") or {}
        funcs_of = decl.get("pin_functions") or {}
        slot_of = {
            str(p): str(s["name"])
            for s in decl.get("slots") or []
            for p in s.get("pins") or []
            if s.get("name") is not None
        }
        pin_extra: dict[str, dict[str, Any]] = {}
        for p in pins:
            rec = funcs_of.get(p)
            ex = {
                "functions": [
                    str(f)
                    for f in (
                        (rec.get("functions") if isinstance(rec, dict) else rec) or []
                    )
                ],
                "max_current_a": (
                    rec.get("max_current_a") if isinstance(rec, dict) else None
                ),
                "slot": slot_of.get(p),
            }
            if (
                ex["functions"]
                or ex["max_current_a"] is not None
                or ex["slot"] is not None
            ):
                pin_extra[p] = ex
        parts.append(
            _Part(
                refdes,
                str(inst.get("label") or ""),
                pins,
                {p: pin_net.get((refdes, p), "") for p in pins},
                fixed=bool(inst.get("fixed")),
                pin_off={
                    p: net_dests[pin_net[(refdes, p)]]
                    for p in pins
                    if (refdes, p) in pin_net and pin_net[(refdes, p)] in net_dests
                },
                pin_extra=pin_extra,
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
            # The part group carries the refdes handle; pin groups inside
            # it carry their own, so closest('[data-handle]') from a stub
            # resolves the pin and from the box the part — the same rule
            # the board SVG's stamped handles follow.
            cls = "sch-part"
            if bright is not None and part.refdes not in bright:
                cls += " dimmed"  # outside focus's one-hop neighbourhood
            elif focus_ref == part.refdes:
                cls += " focused"  # the part focus is centred on
            attrs = (
                f'class="{cls}" data-handle="{esc(part.refdes, quote=True)}"'
                f' data-pcb-part="{esc(part.refdes, quote=True)}"'
            )
            if part.fixed:
                attrs += ' data-pinned="1"'
            body.append(f"<g {attrs}>")
            body.append(
                f'<rect x="{bx:.1f}" y="{by:.1f}" width="{part.box_w:.1f}" '
                f'height="{part.box_h:.1f}" class="part"/>'
            )
            body.append(
                f'<text x="{bx + part.box_w / 2:.1f}" y="{by + 14:.1f}" '
                f'text-anchor="middle" class="refdes">{escape(part.refdes)}</text>'
            )
            if part.fixed:
                # a small pushpin in the box's top-right corner
                body.append(
                    f'<g class="pin-mark" transform="translate('
                    f'{bx + part.box_w - 8.0:.1f},{by + 8.0:.1f})">'
                    f'<circle r="3.2"/><path d="M 0 3 v 6"/></g>'
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
                    out = -1.0 if side == "L" else 1.0
                    pad = (
                        pin_numbers.get(part.refdes, {}).get(pin)
                        if pin_numbers
                        else None
                    ) or pin_pad.get((part.refdes, pin))
                    etype = pin_etype.get((part.refdes, pin))
                    hover = "\n".join(
                        _pin_title(
                            part.refdes,
                            pin,
                            net,
                            etype,
                            pad,
                            net_members.get(net),
                            net_rec.get(net),
                            pin_meta.get((part.refdes, pin)),
                            part.pin_extra.get(pin),
                        )
                    )
                    pin_attrs = (
                        f'data-handle="{esc(handles.pad_handle(part.refdes, pin), quote=True)}"'
                        f' data-pcb-part="{esc(part.refdes, quote=True)}"'
                        f' data-pcb-pad="{esc(pin, quote=True)}"'
                    )
                    if net:
                        pin_attrs += (
                            f' data-pcb-net="{esc(net, quote=True)}"'
                            f' data-net-class="{esc(str(net_class_of.get(net) or ""), quote=True)}"'
                        )
                    body.append(
                        f"<g {pin_attrs}><title>{escape(hover)}</title>"
                        f'<path d="M {edge:.1f} {py:.1f} H {tip:.1f}" class="stub"/>'
                        + _pin_dir_glyph(etype, edge + out * 10.0, py, tip, out)
                    )
                    if net:
                        dests = part.pin_off.get(pin)
                        glyph = (
                            _offsheet_glyph(
                                tip, py, net, dests, rightward=(side == "R")
                            )
                            if dests
                            else _label_glyph(
                                tip,
                                py,
                                net,
                                net_class_of.get(net),
                                rightward=(side == "R"),
                            )
                        )
                        body.append(
                            f'<g data-handle="{esc(handles.net_handle(net), quote=True)}"'
                            f' data-pcb-net="{esc(net, quote=True)}"'
                            f' data-net-class="{esc(str(net_class_of.get(net) or ""), quote=True)}">'
                            + glyph
                            + "</g>"
                        )
                    else:
                        # the no-connect mark: a small open circle
                        body.append(
                            f'<circle cx="{tip:.1f}" cy="{py:.1f}" r="2.6" class="nc"/>'
                        )
                    num = (
                        pin_numbers.get(part.refdes, {}).get(pin)
                        if pin_numbers
                        else None
                    )
                    if num is not None and str(num) != pin:
                        # the physical pad number rides just outside the
                        # box edge above the stub (gr520803); skipped when
                        # the pin name IS the number — a two-pin
                        # passive's '1'/'2' already says it.
                        pnx = bx - 2.0 if side == "L" else bx + part.box_w + 2.0
                        pn_anchor = "end" if side == "L" else "start"
                        body.append(
                            f'<text x="{pnx:.1f}" y="{py - 3.5:.1f}" '
                            f'text-anchor="{pn_anchor}" class="pinno">'
                            f"{escape(str(num))}</text>"
                        )
                    body.append(
                        f'<text x="{pin_x:.1f}" y="{py + 3.5:.1f}" '
                        f'text-anchor="{pin_anchor}" class="pin">{escape(pin)}</text>'
                        "</g>"
                    )
            body.append("</g>")
            x += part.cell_w + _COL_GAP
        doc_w = max(doc_w, x - _COL_GAP + _MARGIN)
        y += row_h + _ROW_GAP
    doc_h = y - _ROW_GAP + _MARGIN

    n_parts = len(parts)
    n_nets = len(net_members)
    heading = escape(f"{title} — {n_parts} part(s), {n_nets} net(s), net-label style")
    if sheet is not None:
        heading += escape(f" · sheet {sheet}")
    if bright is not None:
        heading += escape(f" · focus {focus_ref or (focus or '').strip()}")
        if keep is not None:
            heading += escape(" · connected only")
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
  .pinno {{ font-size: 8px; fill: #888; }}
  .netlabel {{ font-size: 11px; font-weight: 600; }}
  .part {{ fill: #fdfdf6; stroke: #222; stroke-width: 1.4; rx: 3; }}
  .stub {{ stroke: #444; stroke-width: 1.3; fill: none; }}
  .nc {{ fill: none; stroke: #999; stroke-width: 1.3; }}
  /* the §1i direction marks at mid-stub (gr520809) — '?' is text, so it
     gets its own fill rule (the paths are all stroke, no fill) */
  .pindir {{ stroke: #666; stroke-width: 1.1; fill: none; }}
  .pindirt {{ font-size: 9px; fill: #666; }}
  /* off-sheet connector destinations are clickable (pcb-crossprobe.js
     reads data-sheet) — underline marks them as links. */
  .offsheet-dest {{ text-decoration: underline; cursor: pointer; }}
  /* focus mode: the one-hop neighbourhood stays bright, the rest dims in
     place (static/pcb-focus.js reloads the schematic with a focus query). */
  .sch-part.dimmed {{ opacity: 0.16; }}
  .sch-part.focused > rect.part {{ stroke: #1558b0; stroke-width: 2.6; }}
  /* the placement-lock pushpin */
  .pin-mark circle {{ fill: #b26a00; stroke: none; }}
  .pin-mark path {{ stroke: #b26a00; stroke-width: 1.5; fill: none; }}
</style>
<rect x="0" y="0" width="{doc_w:.0f}" height="{doc_h:.0f}" fill="#ffffff"/>
<text x="{_MARGIN:.0f}" y="{_MARGIN - 6:.0f}" class="plabel">{heading}</text>
{chr(10).join(body)}
</svg>
"""


__all__ = ["render_schematic_svg", "sheet_map", "sheet_names"]
