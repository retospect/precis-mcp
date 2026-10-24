"""The macro leg of se observation rays (structure-workspaces slice 2r.2,
docs/backlog/structure-workspaces.md §Macro leg).

The same world ray :mod:`precis_se.rays` dispatches, at macro scale: a
non-structure block's solid is its **bound cad design** when it binds one
(``bound_kind='cad'`` through :attr:`SeTree.cad_designs`) — so a drilled
hole in a cad-bound bracket reads as the hole — else its envelope.
:func:`precis.cad.probe.ray_hits` (the one macro kernel, ``probe_ray``
underneath) returns material and carved-void spans attributed to the
node that made them; this module turns them into an **event list**:

- ``enter``/``exit`` of each contiguous run of a block's material (a
  re-entry after an exit is the run ``#2`` — the undercut signal); a run
  that is all hole — the ray running down a through hole — is not
  material and has no ``enter``/``exit``;
- ``void`` for every carved feature crossed — the feature, its ``⌀`` and
  axis offset for a cylindrical cut, and its creator frame
  (``build.py:44 cut#5 · frame:<key>``) from the cad node's trace frame;
- ``gap`` between consecutive blocks;
- ``port.enter``/``port.leave`` for a port's cylinder (radius from its
  ``thread``/``diameter`` annotation, along its direction, ``depth`` or
  two diameters each way), closed-form line–cylinder intersection.

Field samples (``von-mises``, ``utilisation``, ``temperature``) need a
stored solver ``Outcome``; none is stored yet, so that field is absent
— never estimated. :func:`kind_lines` reads the event list per ray kind
(``bolt_axis`` stack, thread depth and a screw that neither falls short
of 1.5 d nor bottoms out, ``tool_access`` clear or
blocked, ``load_path`` thinnest crossing, ``manufacturability`` walls vs
``min_wall`` and undercuts).
"""

from __future__ import annotations

import itertools
import math
import re
from dataclasses import dataclass
from typing import Any

import numpy as np

#: Spans closer than this (m) along the ray are one contiguous run.
_RUN_EPS_M = 1e-9
#: Metric thread designation → nominal diameter (mm): ``M5x0.8`` → 5.
_THREAD_RE = re.compile(r"^M(\d+(?:\.\d+)?)", re.IGNORECASE)
#: ISO 4762 socket head cap screws — the length series the bolt-axis
#: screw line rounds up into (``precis.component_series``).
SCREW_SERIES = "iso-4762"
#: Engagement rule of thumb for a tapped hole in steel/aluminium: ≥ 1.5 d.
ENGAGEMENT_RATIO = 1.5


def thread_diameter_m(thread: Any) -> float | None:
    """``'M5x0.8'`` → 0.005; ``None`` when not a metric designation."""
    m = _THREAD_RE.match(str(thread or "").strip())
    return float(m.group(1)) * 1e-3 if m else None


@dataclass
class MacroGeom:
    """One non-structure block's solid in its own frame (metres)."""

    name: str
    design: Any
    placement: Any
    source: str  # 'cad <slug>' | 'envelope'
    frame_texts: dict[int, str]


def _frame_texts_by_id(doc: dict[str, Any] | None) -> dict[int, str]:
    """trace frame id → ``build.py:44 cut#5 · frame:<key>`` — the frame's
    own call site (the nearest user frame's when it has none), its qual's
    last part and id."""
    from precis_se import traceview as tv

    if not doc:
        return {}
    tr = tv.Trace(doc)
    out: dict[int, str] = {}
    for f in tr.frames:
        site = f if f.get("file") and f.get("line") else tv.nearest_user(tr, f)
        leaf = f"{str(f.get('qual') or '?').split('.')[-1]}#{f['id']}"
        where = f"{site.get('file')}:{site.get('line')} " if site is not None else ""
        out[int(f["id"])] = f"{where}{leaf} · frame:{f['key']}"
    return out


def block_geoms(tree: Any) -> list[MacroGeom]:
    """Every non-structure block with a solid to probe, in name order."""
    from precis.cad.graph import Design as CadDesign
    from precis.cad.vec import as_vec3, pose
    from precis_se.ops import effective_envelope
    from precis_se.simp_bridge import _envelope_design

    cad_res = getattr(tree, "cad_designs", None)
    traces = getattr(tree, "traces", None)
    out: list[MacroGeom] = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        owner = tree.blocks.get(node.template or "") or node
        if owner.bound_kind == "structure" and owner.bound:
            continue
        placement = pose(as_vec3(node.pose), as_vec3(node.rot))
        if owner.bound_kind == "cad" and owner.bound and cad_res is not None:
            got = cad_res(owner.bound)
            if got is not None:
                design, meta = got
                gen = (meta or {}).get("generated") or {}
                doc = traces(gen.get("trace")) if traces is not None else None
                out.append(
                    MacroGeom(
                        name,
                        design,
                        placement,
                        f"cad {owner.bound}",
                        _frame_texts_by_id(doc),
                    )
                )
                continue
        env = effective_envelope(tree, node)
        if not env:
            continue
        try:
            design, _expr = _envelope_design(env)
        except Exception:
            continue
        if not isinstance(design, CadDesign):  # pragma: no cover — defensive
            continue
        out.append(MacroGeom(name, design, placement, "envelope", {}))
    return out


def _instance(design: Any, label: str | None) -> Any:
    """The cad instance a span names (its label), or ``None``."""
    if not label:
        return None
    return next((i for i in design.instances.values() if i.label == label), None)


def _diameter_m(prim: Any) -> float | None:
    rb, rt = getattr(prim, "rb", None), getattr(prim, "rt", None)
    if rb is not None and rt is not None and abs(rb - rt) <= 1e-12 * max(rb, 1.0):
        return 2.0 * float(rb)
    return None


def _axis_offset_m(inst: Any, o_l: Any, d_l: Any, t: float) -> float | None:
    """How far off a cylindrical cut's axis the ray runs at ``t`` (block
    frame, m) — the bolt-vs-hole misfit a lateral move shows."""
    from precis.cad.vec import as_vec3

    if inst is None or _diameter_m(inst.placed.prim) is None:
        return None
    xf = inst.placed.xform
    c = np.asarray(xf.apply(as_vec3([0.0, 0.0, 0.0])), dtype=float)
    a = np.asarray(xf.apply_dir(as_vec3([0.0, 0.0, 1.0])), dtype=float)
    a = a / np.linalg.norm(a)
    p = np.asarray(o_l, dtype=float) + t * np.asarray(d_l, dtype=float) - c
    return float(np.linalg.norm(p - (p @ a) * a))


@dataclass
class MacroEvent:
    s: float  # m from the world ray origin
    block: str
    event: str  # enter | exit | void | gap | port.enter | port.leave
    subject: str
    text: str
    nums: dict[str, float]
    data: dict[str, Any]


def _fmt(v_m: float, unit: tuple[float, str]) -> str:
    f, u = unit
    return f"{v_m * f:.1f} {u}"


def block_events(
    g: MacroGeom,
    o_w: np.ndarray,
    d_w: np.ndarray,
    length: float,
    unit: tuple[float, str],
) -> list[MacroEvent]:
    """Material runs, carved voids and their features for one block. A run
    that is ALL void — a through hole the ray runs down — is not material:
    it emits its ``void`` events only, never ``enter``/``exit``."""
    from precis.cad.probe import ray_hits
    from precis.cad.vec import as_vec3

    inv = g.placement.inverse()
    o_l = inv.apply(as_vec3(o_w))
    d_l = inv.apply_dir(as_vec3(d_w))
    try:
        spans = ray_hits(g.design, o_l, d_l, length=length, block_id=g.name)
    except (ValueError, ArithmeticError):  # pragma: no cover — defensive
        return []
    runs: list[list[Any]] = []
    for sp in spans:
        if runs and sp.entry - runs[-1][-1].exit <= _RUN_EPS_M:
            runs[-1].append(sp)
        else:
            runs.append([sp])
    out: list[MacroEvent] = []
    n_material = 0
    for run in runs:
        material = any(sp.state == "solid" for sp in run)
        subject = g.name
        if material:
            n_material += 1
            subject = g.name if n_material == 1 else f"{g.name}#{n_material}"
            first_solid = next(sp for sp in run if sp.state == "solid")
            inst = _instance(g.design, first_solid.feature)
            fid = inst.frame if inst is not None else None
            where = g.frame_texts.get(fid) if fid is not None else None
            feat = first_solid.feature
            feat_txt = (
                f" · {feat}" + (f" ({where})" if where else "")
                if feat and feat not in (g.name, "envelope")
                else ""
            )
            out.append(
                MacroEvent(
                    run[0].entry,
                    g.name,
                    "enter",
                    subject,
                    f"enter {subject}{feat_txt}",
                    {},
                    {"run": n_material},
                )
            )
        for sp in run:
            if sp.state != "void":
                continue
            inst = _instance(g.design, sp.feature)
            dia = _diameter_m(inst.placed.prim) if inst is not None else None
            off = _axis_offset_m(inst, o_l, d_l, sp.entry)
            fid = inst.frame if inst is not None else None
            where = g.frame_texts.get(fid) if fid is not None else None
            text = f"void {g.name}.{sp.feature}"
            if dia is not None:
                text += f" ⌀{_fmt(dia, unit)}"
            text += f" · {_fmt(sp.length, unit)}"
            if off is not None and off >= 1e-5:  # 0.01 mm: shown when it matters
                text += f" · axis offset {_fmt(off, unit)}"
            if where:
                text += f" · {where}"
            nums = {"len": sp.length}
            if dia is not None:
                nums["dia"] = dia
            if off is not None:
                nums["off"] = off
            out.append(
                MacroEvent(
                    sp.entry,
                    g.name,
                    "void",
                    f"{g.name}.{sp.feature}",
                    text,
                    nums,
                    {"feature": sp.feature, "exit": sp.exit, "in_material": material},
                )
            )
        if material:
            walls = [sp.length for sp in run if sp.state == "solid"]
            t_in, t_out = run[0].entry, run[-1].exit
            out.append(
                MacroEvent(
                    t_out,
                    g.name,
                    "exit",
                    subject,
                    f"exit {subject} · {_fmt(t_out - t_in, unit)}",
                    {"len": t_out - t_in, "solid": sum(walls)},
                    {"run": n_material, "walls": walls},
                )
            )
    return out


def _line_cylinder(
    o: np.ndarray, d: np.ndarray, c: np.ndarray, a: np.ndarray, r: float, half: float
) -> tuple[float, float] | None:
    """``(s_in, s_out)`` of a line ``o + s·d`` (unit ``d``) in the finite
    cylinder (centre ``c``, unit axis ``a``, radius ``r``, ``|h| ≤ half``),
    closed form; ``None`` when it misses."""
    rel = o - c
    w0 = rel - (rel @ a) * a
    wd = d - (d @ a) * a
    A = float(wd @ wd)
    lo, hi = -math.inf, math.inf
    if A < 1e-24:  # parallel to the axis: radially in for all s, or never
        if float(w0 @ w0) > r * r:
            return None
    else:
        B = 2.0 * float(w0 @ wd)
        C = float(w0 @ w0) - r * r
        disc = B * B - 4 * A * C
        if disc < 0:
            return None
        sq = math.sqrt(disc)
        lo, hi = (-B - sq) / (2 * A), (-B + sq) / (2 * A)
    h0, hd = float(rel @ a), float(d @ a)
    if abs(hd) < 1e-15:
        if abs(h0) > half:
            return None
    else:
        t1, t2 = (-half - h0) / hd, (half - h0) / hd
        lo, hi = max(lo, min(t1, t2)), min(hi, max(t1, t2))
    return (lo, hi) if lo < hi else None


def port_events(
    tree: Any,
    o_w: np.ndarray,
    d_w: np.ndarray,
    length: float,
    unit: tuple[float, str],
) -> list[MacroEvent]:
    """Enter/leave each port's cylinder, closed form: radius from the
    ``thread``/``diameter`` (m) annotation, along the port's direction,
    ``depth`` (m) — else two diameters — each way from the port's pose.
    Ports without pose, direction or a size are not regions."""
    from precis.cad.vec import as_vec3, pose
    from precis_se.ops import effective_ports

    out: list[MacroEvent] = []
    for name in sorted(tree.blocks):
        node = tree.blocks[name]
        placement = pose(as_vec3(node.pose), as_vec3(node.rot))
        for pname, port in sorted(effective_ports(tree, node).items()):
            ann = dict(port.annotations or {})
            dia = thread_diameter_m(ann.get("thread"))
            if dia is None and ann.get("diameter") is not None:
                dia = float(ann["diameter"])
            if port.pose is None or port.direction is None or not dia:
                continue
            c = np.asarray(placement.apply(as_vec3(port.pose)), dtype=float)
            a = np.asarray(placement.apply_dir(as_vec3(port.direction)), dtype=float)
            a = a / np.linalg.norm(a)
            half = float(ann.get("depth") or 2.0 * dia)
            hit = _line_cylinder(o_w, d_w, c, a, dia / 2.0, half)
            if hit is None:
                continue
            s_in, s_out = max(hit[0], 0.0), min(hit[1], length)
            if s_out <= s_in:
                continue
            p_in = o_w + s_in * d_w - c
            off = float(np.linalg.norm(p_in - (p_in @ a) * a))
            roles = ",".join(port.roles or []) or "—"
            label = f"{name}.{pname}"
            desc = label + (f" {ann['thread']}" if ann.get("thread") else "")
            data = {"port": pname, "thread": ann.get("thread"), "dia": dia}
            out.append(
                MacroEvent(
                    s_in,
                    name,
                    "port.enter",
                    label,
                    f"port.enter {desc} (role {roles}) · axis offset {_fmt(off, unit)}",
                    {"off": off},
                    data,
                )
            )
            if s_out < length:
                out.append(
                    MacroEvent(
                        s_out,
                        name,
                        "port.leave",
                        label,
                        f"port.leave {label}",
                        {},
                        data,
                    )
                )
    return out


def _extents(events: list[MacroEvent]) -> list[tuple[float, float, str, str]]:
    """``(start, end, block, subject)`` of every stretch of a block the ray
    is in — material runs (enter→exit) and holes outside material."""
    out = []
    open_: dict[str, MacroEvent] = {}
    for e in sorted(events, key=lambda e: e.s):
        if e.event == "enter":
            open_[e.subject] = e
        elif e.event == "exit" and e.subject in open_:
            out.append((open_.pop(e.subject).s, e.s, e.block, e.subject))
        elif e.event == "void" and not e.data.get("in_material"):
            out.append((e.s, float(e.data["exit"]), e.block, e.subject))
    return sorted(out)


def gap_events(events: list[MacroEvent], unit: tuple[float, str]) -> list[MacroEvent]:
    """``gap`` between consecutive blocks along the ray — measured between
    the stretches the ray spends in each (material or hole)."""
    ext = _extents(events)
    out: list[MacroEvent] = []
    for (_s0, e0, b0, sub0), (s1, _e1, b1, sub1) in itertools.pairwise(ext):
        if b0 != b1 and s1 - e0 > _RUN_EPS_M:
            out.append(
                MacroEvent(
                    e0,
                    b0,
                    "gap",
                    f"{sub0}→{sub1}",
                    f"gap {_fmt(s1 - e0, unit)}",
                    {"len": s1 - e0},
                    {"to": b1},
                )
            )
    return out


def screw_lengths_mm(thread_mm: float) -> tuple[float, ...]:
    """The ISO 4762 standard lengths (mm) for ``M<thread_mm>``; ``()`` when
    the size is not in the series."""
    from precis import component_series as cs

    series = cs.find_series(SCREW_SERIES)
    size = series.size(f"M{thread_mm:g}") if series is not None else None
    return tuple(float(L) for L in size.lengths) if size is not None else ()


def pick_screw_mm(
    lengths: tuple[float, ...], stack_mm: float, depth_mm: float, need_mm: float
) -> tuple[float | None, str]:
    """The shortest standard length ``L`` with ``stack + need ≤ L ≤ stack +
    depth`` — enough thread in the hole without bottoming out — and
    ``(None, why)`` when none fits, naming the nearest short and long."""
    lo, hi = stack_mm + need_mm, stack_mm + depth_mm
    fits = [L for L in lengths if lo - 1e-9 <= L <= hi + 1e-9]
    if fits:
        return fits[0], ""
    short = [L for L in lengths if lo - 1e-9 > L]
    long_ = [L for L in lengths if hi + 1e-9 < L]
    parts = []
    if short:
        parts.append(f"{short[-1]:g} short by {lo - short[-1]:.1f} mm")
    if long_:
        parts.append(f"{long_[0]:g} bottoms out by {long_[0] - hi:.1f} mm")
    return None, ", ".join(parts) or "no lengths in the series"


def kind_lines(
    tree: Any, spec: Any, events: list[MacroEvent], unit: tuple[float, str]
) -> list[str]:
    """The per-kind report lines under the event list (§Macro leg table)."""
    kind = spec.kind
    if kind == "bolt_axis":
        return _bolt_lines(events, unit)
    if kind == "tool_access":
        blocking = [e for e in events if e.event == "enter"]
        if not blocking:
            return [
                f"  tool access: clear ✓ ({_bundle_text(spec, unit)}"
                f"{_fmt(spec.length, unit)})"
            ]
        first = blocking[0]
        return [f"  tool access: blocked by {first.subject} at {_fmt(first.s, unit)} ✗"]
    if kind == "load_path":
        solids = [e for e in events if e.event == "exit"]
        if not solids:
            return ["  load path: crosses no material"]
        thin = min(solids, key=lambda e: e.nums["solid"])
        return [
            f"  load path: thinnest crossing {_fmt(thin.nums['solid'], unit)} "
            f"in {thin.subject} · stress/utilisation along the ray — no stored "
            "field outcome"
        ]
    if kind == "manufacturability":
        return _manufacture_lines(tree, events, unit)
    return []


def _bundle_text(spec: Any, unit: tuple[float, str]) -> str:
    b = spec.bundle or {}
    if not b:
        return ""
    return (
        f"{int(b.get('count') or 0)} rays r {_fmt(float(b.get('radius') or 0), unit)}, "
    )


def _bolt_lines(events: list[MacroEvent], unit: tuple[float, str]) -> list[str]:
    """Stack = from the first block on the axis to the threaded port's
    block; thread depth = that block's hole along the ray (≥ 1.5 d ✓/✗);
    screw = the shortest ISO 4762 length with stack + 1.5 d ≤ L ≤ stack +
    depth, engagement = L − stack; none fitting is said, never rounded."""
    threaded = next(
        (e for e in events if e.event == "port.enter" and e.data.get("thread")), None
    )
    if threaded is None:
        return ["  bolt: no threaded port on the axis — stack and engagement unknown"]
    d = float(threaded.data["dia"])
    tblock = threaded.block
    ext = _extents(events)
    if not ext:
        return ["  bolt: the axis crosses no block"]
    start = ext[0][0]
    t_ext = next((x for x in ext if x[2] == tblock), None)
    if t_ext is None:
        return [f"  bolt: the axis never reaches {tblock} — the hole is missed ✗"]
    hole = next(
        (
            e
            for e in events
            if e.event == "void" and e.block == tblock and e.s >= t_ext[0] - _RUN_EPS_M
        ),
        None,
    )
    depth = float(hole.nums["len"]) if hole is not None else 0.0
    stack = t_ext[0] - start
    need = ENGAGEMENT_RATIO * d
    ok = "✓" if depth >= need - 1e-9 else "✗"
    # the clearance holes are the stack's: blocks before the threaded one
    clear = [
        e
        for e in events
        if e.event == "void"
        and e.block != tblock
        and "dia" in e.nums
        and e.s < t_ext[0] - _RUN_EPS_M
    ]
    f, u = unit
    tail = ""
    if clear:
        rc = (min(e.nums["dia"] for e in clear) - d) / 2
        tail += f" · radial clearance {rc * f:.2f} {u}"
    off = float(hole.nums.get("off", 0.0)) if hole is not None else 0.0
    if hole is not None and off > 1e-4:  # 0.1 mm off the threaded hole's axis
        tail += f" · axis offset {_fmt(off, unit)} from {hole.subject} ✗"
    lines = [
        f"  bolt: M{d * 1e3:g} · stack {_fmt(stack, unit)} · thread depth "
        f"{_fmt(depth, unit)} (≥ {_fmt(need, unit)} = {ENGAGEMENT_RATIO:g} d {ok})"
        + tail
    ]
    lengths = screw_lengths_mm(d * 1e3)
    if lengths and depth > 0:
        L, why = pick_screw_mm(lengths, stack * 1e3, depth * 1e3, need * 1e3)
        if L is not None:
            eng = L - stack * 1e3
            lines.append(
                f"  screw: M{d * 1e3:g} × {L:g} · engagement {eng:.1f} mm "
                f"(stack {stack * 1e3:.1f} + {ENGAGEMENT_RATIO:g} d ≤ L ≤ stack + "
                f"depth {depth * 1e3:.1f})"
            )
        else:
            lines.append(f"  screw: no standard M{d * 1e3:g} length fits — {why} ✗")
    return lines


def _manufacture_lines(
    tree: Any, events: list[MacroEvent], unit: tuple[float, str]
) -> list[str]:
    """Each solid wall the axis crosses (holes between them are not walls)
    vs the block mode's ``min_wall``; an undercut is a block re-entered."""
    from precis_se.printing import _rules_for

    lines = []
    for e in (e for e in events if e.event == "exit"):
        node = tree.blocks.get(e.block)
        min_wall = None
        if node is not None and node.mode:
            try:
                min_wall = _rules_for(tree, node).get("min_wall")
            except (KeyError, ValueError, TypeError):  # an unknown mode family
                min_wall = None
        for k, wall in enumerate(e.data.get("walls") or [], start=1):
            verdict = ""
            if min_wall:
                verdict = f" (min_wall {_fmt(float(min_wall), unit)} " + (
                    "✓)" if wall >= float(min_wall) - 1e-12 else "✗)"
                )
            name = e.subject if len(e.data["walls"]) == 1 else f"{e.subject}/{k}"
            lines.append(f"  wall {name}: {_fmt(wall, unit)}{verdict}")
    re_entered = sorted(
        {e.block for e in events if e.event == "enter" and "#" in e.subject}
    )
    lines.append(
        "  undercut: "
        + (
            ", ".join(f"{b} re-entered along the tool axis" for b in re_entered)
            if re_entered
            else "none along this axis"
        )
    )
    return lines


def bundle_offsets(
    direction: np.ndarray, radius: float, count: int
) -> list[np.ndarray]:
    """``count`` offsets on a circle of ``radius`` perpendicular to the
    ray — one probe, the union of their hits (a tool's tube)."""
    from precis.cad.probe import _basis_perp
    from precis.cad.vec import as_vec3

    u, v = _basis_perp(as_vec3(direction))
    u_a, v_a = np.asarray(u, dtype=float), np.asarray(v, dtype=float)
    return [
        radius
        * (
            math.cos(2 * math.pi * k / count) * u_a
            + math.sin(2 * math.pi * k / count) * v_a
        )
        for k in range(count)
    ]
