"""What an imported board's own copper says about its spec
(pcb-epro-import slice 1c).

Reto, 2026-09-30: "We extract the points and fix our model to match, then
regenerate." Imported copper is a MEASUREMENT, never geometry precis keeps:
the source author already decided how wide each net's tracks are, how big
its vias are and how close it runs to its neighbours, and every one of those
decisions is evidence about the net's real requirements. This module puts
those measured numbers beside what precis' own rules resolve for the same
net, so a disagreement is visible BEFORE the board is re-routed from the
spec.

Pure: copper rows and pads in (the flat ``{"ctype", "layer", "net", ...}``
item shape :mod:`precis.pcb.drc` reads), dataclasses out. The gaps come
from :func:`precis.pcb.drc.clearance_pairs_indexed`, the same exact
polygon-distance engine the DRC view runs, so a gap here and a DRC finding
there cannot disagree about the geometry.

Two choices that shape the numbers:

- **Pad-to-pad gaps are excluded.** Two pads of one fine-pitch part sit
  0.2 mm apart because the PART says so; that is a footprint fact, not a
  routing decision, and it would be the minimum on almost every net. A gap
  counts only when a track or a via is on at least one side.
- **A gap is measured only up to ``probe_mm``.** A net with no foreign
  copper within the probe reports no clearance at all rather than a large
  number — "nothing nearby" is the honest reading, and the comparison only
  cares about gaps tight enough to bind.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from precis.pcb import drc
from precis.pcb.capabilities import CapabilityRow
from precis.pcb.rules import NetRules, resolve_net_rules

#: How far to look for a foreign-net neighbour. Wider than any clearance
#: rule precis resolves (the 4-layer house default is 0.15 mm), so every
#: gap that could bind is seen; narrower than a board's open space, so the
#: pair count stays proportional to copper density, not board area.
PROBE_MM = 1.0

#: Floating-point slack when comparing a measured mm value to a rule.
_EPS = 1e-6


@dataclass
class Gap:
    """The closest foreign-net copper to one net, on one layer."""

    gap_mm: float
    layer: str
    other_net: str


@dataclass
class NetCopper:
    """One net's copper as the source author drew it."""

    net: str
    layers: set[str] = field(default_factory=set)
    n_tracks: int = 0
    width_min_mm: float | None = None
    width_max_mm: float | None = None
    n_vias: int = 0
    via_dia_min_mm: float | None = None
    via_drill_min_mm: float | None = None
    closest: Gap | None = None


@dataclass
class NetComparison:
    """A net's measured copper beside the rules precis resolves for it."""

    measured: NetCopper
    rules: NetRules | None
    findings: list[str] = field(default_factory=list)


@dataclass
class CopperReport:
    nets: list[NetComparison]
    #: ``layer -> (narrowest track, tightest gap)`` across every net.
    layers: dict[str, tuple[float | None, float | None]]
    #: Why a number is missing (no pads given, a net precis does not know).
    notes: list[str] = field(default_factory=list)
    #: The author's board-wide defaults against precis' — said ONCE, so
    #: the per-net findings can be about the nets that depart from them.
    board: list[str] = field(default_factory=list)


def _mode(values: list[Any]) -> Any:
    """The most common value; ties go to the smaller, so the answer does
    not depend on dict order."""
    counts: dict[Any, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return min(counts, key=lambda v: (-counts[v], v)) if counts else None


def _lo(cur: float | None, v: float) -> float:
    return v if cur is None else min(cur, v)


def _hi(cur: float | None, v: float) -> float:
    return v if cur is None else max(cur, v)


def measure(
    copper: list[dict[str, Any]],
    pads: list[dict[str, Any]],
    layers: list[str],
    *,
    probe_mm: float = PROBE_MM,
) -> tuple[dict[str, NetCopper], dict[str, tuple[float | None, float | None]]]:
    """Per-net and per-layer minima of ``copper`` (flat track/via items).

    ``pads`` are the placed pads (:func:`precis.pcb.padplace.board_pads`'
    shape); they take part in gaps as the far side of a track or via, never
    as a pad-to-pad pair (module docstring). Pass ``[]`` when there are none
    to hand and the gaps will cover track/via copper only.
    """
    nets: dict[str, NetCopper] = {}
    layer_width: dict[str, float] = {}
    layer_gap: dict[str, float] = {}

    def net_of(name: str) -> NetCopper:
        return nets.setdefault(name, NetCopper(net=name))

    for item in copper:
        nc = net_of(str(item.get("net") or ""))
        if item.get("ctype") == "track":
            w = float(item["width_mm"])
            layer = str(item.get("layer") or "")
            nc.layers.add(layer)
            nc.n_tracks += 1
            nc.width_min_mm = _lo(nc.width_min_mm, w)
            nc.width_max_mm = _hi(nc.width_max_mm, w)
            layer_width[layer] = _lo(layer_width.get(layer), w)
        elif item.get("ctype") == "via":
            nc.n_vias += 1
            nc.via_dia_min_mm = _lo(nc.via_dia_min_mm, float(item["dia_mm"]))
            nc.via_drill_min_mm = _lo(nc.via_drill_min_mm, float(item["drill_mm"]))

    model = {"copper": copper, "pads": pads, "layers": layers}
    # The index space `clearance_pairs_indexed` documents: copper first,
    # then the pads tagged `ctype="pad"`.
    items = [*copper, *({**p, "ctype": "pad"} for p in pads)]
    for i, j, gap, layer in drc.clearance_pairs_indexed(model, required_mm=probe_mm):
        a, b = items[i], items[j]
        if a.get("ctype") == "pad" and b.get("ctype") == "pad":
            continue
        layer_gap[layer] = _lo(layer_gap.get(layer), gap)
        for this, other in ((a, b), (b, a)):
            if this.get("ctype") == "pad":
                # The gap belongs to the track/via side; a pad's net may
                # have no copper of its own at all.
                continue
            nc = net_of(str(this.get("net") or ""))
            if nc.closest is None or gap < nc.closest.gap_mm:
                nc.closest = Gap(gap, layer, str(other.get("net") or ""))

    per_layer = {
        name: (layer_width.get(name), layer_gap.get(name))
        for name in layers
        if name in layer_width or name in layer_gap
    }
    return nets, per_layer


def compare(
    measured: dict[str, NetCopper],
    rules: dict[str, NetRules],
    capability: CapabilityRow,
    *,
    board_width_mm: float | None = None,
    board_via_mm: float | None = None,
) -> list[NetComparison]:
    """Each measured net against its resolved rules and the fab minimum.

    A finding is written for every disagreement that would change what a
    re-route draws. BELOW THE FAB MINIMUM is the case the spec calls out by
    name ("must be called out, not averaged away"): the source board itself
    is not manufacturable there at this process, whatever precis decides.

    ``board_width_mm``/``board_via_mm`` are the author's own defaults (the
    most common track width and via diameter). A net is flagged as wider
    only past BOTH the rule and that default, and as having a small via
    only below both: on the first real board every net used the author's
    0.254 mm default against precis' 0.150 mm, so comparing to the rule
    alone flagged all 89 nets and hid the genuinely wide ones among them.
    The default-vs-rule difference is reported once, board-level.
    """
    floor = capability.jlc_min
    out: list[NetComparison] = []
    for name in sorted(measured):
        m = measured[name]
        r = rules.get(name)
        cmp = NetComparison(measured=m, rules=r)
        f = cmp.findings

        w_floor = floor.get("trace_width_mm")
        if m.width_min_mm is not None and w_floor is not None:
            if m.width_min_mm < w_floor - _EPS:
                f.append(
                    f"BELOW FAB MINIMUM: source track {m.width_min_mm:.3f} mm < "
                    f"{w_floor:.3f} mm ({capability.process})"
                )
        g_floor = floor.get("trace_spacing_mm")
        if m.closest is not None and g_floor is not None:
            if m.closest.gap_mm < g_floor - _EPS:
                f.append(
                    f"BELOW FAB MINIMUM: source gap {m.closest.gap_mm:.3f} mm to "
                    f"{m.closest.other_net or '(no net)'} on {m.closest.layer} < "
                    f"{g_floor:.3f} mm ({capability.process})"
                )
        for value, key, label in (
            (m.via_dia_min_mm, "via_diameter_mm", "via diameter"),
            (m.via_drill_min_mm, "drill_mm", "via drill"),
        ):
            lim = floor.get(key)
            if value is not None and lim is not None and value < lim - _EPS:
                f.append(
                    f"BELOW FAB MINIMUM: source {label} {value:.3f} mm < "
                    f"{lim:.3f} mm ({capability.process})"
                )

        if r is None:
            f.append("precis has no such net, so there is no rule to compare")
            out.append(cmp)
            continue
        wide_past = max(r.track_width_mm, board_width_mm or 0.0)
        if m.width_max_mm is not None and m.width_max_mm > wide_past + _EPS:
            f.append(
                f"source widens to {m.width_max_mm:.3f} mm where precis would "
                f"draw {r.track_width_mm:.3f} mm — a current or class the spec "
                f"does not carry yet?"
            )
        if m.width_min_mm is not None and m.width_min_mm < r.track_width_mm - _EPS:
            f.append(
                f"source narrows to {m.width_min_mm:.3f} mm where precis "
                f"requires {r.track_width_mm:.3f} mm — a re-route draws it wider "
                f"and may not fit where the source did"
            )
        if m.closest is not None and m.closest.gap_mm < r.clearance_mm - _EPS:
            f.append(
                f"source runs {m.closest.gap_mm:.3f} mm from "
                f"{m.closest.other_net or '(no net)'} on {m.closest.layer} where "
                f"precis requires {r.clearance_mm:.3f} mm"
            )
        if (
            m.via_dia_min_mm is not None
            and r.via_dia_mm is not None
            and m.via_dia_min_mm
            < min(r.via_dia_mm, board_via_mm or r.via_dia_mm) - _EPS
        ):
            f.append(
                f"source via {m.via_dia_min_mm:.3f} mm is smaller than precis' "
                f"{r.via_dia_mm:.3f} mm"
            )
        out.append(cmp)
    return out


def report(
    copper: list[dict[str, Any]],
    pads: list[dict[str, Any]],
    layers: list[str],
    rules: dict[str, NetRules],
    capability: CapabilityRow,
    *,
    probe_mm: float = PROBE_MM,
) -> CopperReport:
    """:func:`measure` then :func:`compare`, with the reasons a number is
    missing written down rather than left as a blank."""
    measured, per_layer = measure(copper, pads, layers, probe_mm=probe_mm)
    notes: list[str] = []
    if not pads:
        notes.append(
            "no pads given: gaps cover track and via copper only, so a track "
            "running close to a foreign pad is not measured"
        )
    width = _mode(
        [round(float(c["width_mm"]), 3) for c in copper if c.get("ctype") == "track"]
    )
    via = _mode(
        [
            (round(float(c["dia_mm"]), 3), round(float(c["drill_mm"]), 3))
            for c in copper
            if c.get("ctype") == "via"
        ]
    )
    # The rule for an unannotated net — what precis draws by default.
    default = resolve_net_rules("", layer_is_outer=True, fab_caps=capability)
    board: list[str] = []
    if width is not None:
        board.append(
            f"author's default track {width:.3f} mm; precis draws "
            f"{default.track_width_mm:.3f} mm for an unannotated net"
        )
    if via is not None:
        board.append(
            f"author's via {via[0]:.3f}/{via[1]:.3f} mm; precis' "
            f"{_mm(default.via_dia_mm)}/{_mm(default.via_drill_mm)} mm"
        )
    return CopperReport(
        nets=compare(
            measured,
            rules,
            capability,
            board_width_mm=width,
            board_via_mm=None if via is None else via[0],
        ),
        layers=per_layer,
        notes=notes,
        board=board,
    )


def _mm(v: float | None) -> str:
    return "—" if v is None else f"{v:.3f}"


def render(rep: CopperReport) -> list[str]:
    """Plain lines for a terminal: per-layer minima, then one line per net,
    then every finding. Nets with findings are listed first so the cases
    that need a spec change are not buried under the ones that agree."""
    lines = ["copper (source board, measured):"]
    lines.extend(f"  {b}" for b in rep.board)
    for layer, (w, g) in rep.layers.items():
        lines.append(
            f"  {layer:<7} narrowest track {_mm(w)} mm, tightest gap {_mm(g)} mm"
        )
    flagged = [c for c in rep.nets if c.findings]
    lines.append(
        f"  {len(rep.nets)} net(s) carry copper; {len(flagged)} disagree with "
        f"precis' rules"
    )
    for c in sorted(rep.nets, key=lambda c: (not c.findings, c.measured.net)):
        m, r = c.measured, c.rules
        lines.append(
            f"  {m.net}: track {_mm(m.width_min_mm)}–{_mm(m.width_max_mm)} mm "
            f"(rule {_mm(r.track_width_mm if r else None)}), via "
            f"{_mm(m.via_dia_min_mm)}/{_mm(m.via_drill_min_mm)} mm "
            f"(rule {_mm(r.via_dia_mm if r else None)}/"
            f"{_mm(r.via_drill_mm if r else None)}), gap "
            f"{_mm(m.closest.gap_mm if m.closest else None)} mm "
            f"(rule {_mm(r.clearance_mm if r else None)})"
        )
        for finding in c.findings:
            lines.append(f"    ! {finding}")
    for note in rep.notes:
        lines.append(f"  note: {note}")
    return lines
