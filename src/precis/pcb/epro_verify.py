"""Re-read an exported ``.epro2`` and diff it against its model (gr495289).

``view='epro' args={'verify': true}`` answers "does the file I just wrote
read back as the board I have?" without moving the file anywhere: the
bytes :func:`precis.pcb.epro_write.zip_epro` produced go straight back
through the importer's own reader (:func:`precis.pcb.epro.read_archive` +
:func:`~precis.pcb.epro.build_design`), and the design that comes out is
compared with the model the writer was handed. An agent cannot fetch the
file from the serving host (docs/backlog/pcb-epro-mcp-roundtrip.md), so
this is the round trip it can run.

**What "want" is.** The writer's model, not a source file:
``tests/test_pcb_epro_export.py::_structural_diff`` compares an imported
``Design`` with its re-read twin, which needs the source ``.epro2``; a
native precis board has none. The writer's own exclusions are mirrored,
so a drop it already warned about (an unplaced or padless part, an orphan
pad, a zero-length segment, a pour on an undeclared layer) is not
reported a second time as a failure. Counts the writer reports in
``EproExport.stats`` are checked against what reads back.

**Frame.** The reader puts its origin at the outline's bounding-box
corner (:class:`precis.pcb.epro.Frame`), the writer keeps precis' own
frame. Re-read coordinates are shifted by the model outline's minimum
corner before any comparison — a native board whose outline does not
start at ``(0, 0)`` would otherwise fail every position check.

**Tolerances.** Positions pair within :data:`MATCH_MM` (the format's mil
quantisation plus a fresh float transform can move a point by ~0.5 um,
which straddles any fixed rounding key) and pass within :data:`TOL_MM`,
the 0.5 um the writer's round-trip tests hold.

Board fiducials are compared as ``FID<n>`` free pads (position, count).

Not compared: silk strokes and designator text, holes, the cached pour
fill, and via spans (``VIA`` has no span field; the export already warns
per via). Pours are compared by count only.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from precis.pcb import epro, padplace, pose
from precis.pcb.epro_write import arc_sweep_deg, fiducial_pads

#: Pairing window, mm.
MATCH_MM = 0.005
#: Pass threshold for a paired position, mm (0.5 um).
TOL_MM = 0.0005

#: How many offenders a failing line names before eliding the rest.
_SHOW = 5

_XY = tuple[float, float]


@dataclass
class Verdict:
    """``ok`` and ``fails`` are display lines; ``fails`` lead the render."""

    ok: list[str] = field(default_factory=list)
    fails: list[str] = field(default_factory=list)

    def check(self, passed: bool, msg: str) -> None:
        (self.ok if passed else self.fails).append(msg)

    def render(self) -> str:
        head = (
            "## verify — this file re-read through the importer's reader, "
            "diffed against the board"
        )
        body = [f"FAIL {m}" for m in self.fails] + [f"  ok {m}" for m in self.ok]
        tail = f"verify: {len(self.ok)} ok, {len(self.fails)} FAIL" + (
            " — the file does NOT carry the board as listed" if self.fails else ""
        )
        return "\n".join([head, *body, tail])


def _um(mm: float) -> str:
    return f"{mm * 1000.0:.3f} um"


def _names(items: list[Any]) -> str:
    shown = ", ".join(str(i) for i in items[:_SHOW])
    return shown + ("…" if len(items) > _SHOW else "")


def _ring(points: list[_XY]) -> list[_XY]:
    """Closing vertex dropped, rotated to start at the lowest vertex."""
    pts = list(points)
    if len(pts) > 1 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if not pts:
        return []
    k = min(range(len(pts)), key=lambda i: pts[i])
    return pts[k:] + pts[:k]


def _dist(a: _XY, b: _XY) -> float:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def verify_export(
    model: dict[str, Any], blob: bytes, *, stats: dict[str, int]
) -> Verdict:
    """Diff the ``.epro2`` ``blob`` against ``model`` (the dict handed to
    :func:`precis.pcb.epro_write.epro_files`; ``stats`` is its
    ``EproExport.stats``). Never raises on a bad file: an unreadable one is
    the first failure."""
    v = Verdict()
    try:
        project = epro.read_archive(blob)
        board = project.pcb()
        design, frame = epro.build_design(project, board)
        layers_got = list(epro.copper_layers(board).values())
        copper_got, _ = epro.measured_copper(board, frame)
    except epro.EproError as exc:
        v.check(False, f"the file does not read back through the importer: {exc}")
        return v

    outline = [(float(x), float(y)) for x, y in model.get("outline") or []]
    dx = min((x for x, _ in outline), default=0.0)
    dy = min((y for _, y in outline), default=0.0)

    def at(x: Any, y: Any) -> _XY:
        return float(x) + dx, float(y) + dy

    parts = _check_parts(v, model, design, at)
    _check_pads(v, model, design, layers_got, parts, stats, at)
    _check_nets(v, model, board, parts)
    _check_copper(v, model, copper_got, stats, at)
    _check_vias(v, model, copper_got, stats, at)
    v.check(
        len(board.bodies("POUR")) == stats.get("pours", 0),
        f"pour zones {stats.get('pours', 0)} written -> "
        f"{len(board.bodies('POUR'))} read",
    )
    _check_fiducials(v, model, board, parts, frame, stats, at)
    _check_outline(v, outline, design, at)
    _check_rules(v, model, design)
    return v


def _check_fiducials(
    v: Verdict,
    model: dict[str, Any],
    board: epro.EproDocument,
    parts: dict[str, dict[str, Any]],
    frame: epro.Frame,
    stats: dict[str, int],
    at: Callable[[Any, Any], _XY],
) -> None:
    """The auto board fiducials ride as ``FID<n>`` free pads: one per outer
    layer, at the model's position and diameter."""
    want = fiducial_pads(
        [p for p in model.get("pads") or [] if str(p.get("refdes") or "") not in parts],
        [str(n) for n in model.get("layers") or []],
    )
    got = [
        b
        for b in board.bodies("PAD")
        if str(b.get("num") or "").startswith(epro.FIDUCIAL_PAD_PREFIX)
    ]
    if not want and not got:
        return
    pool = [at(*frame.xy(float(b["centerX"]), float(b["centerY"]))) for b in got]
    missing: list[str] = []
    for num, pad, _bottom in want:
        wxy = (float(pad["x"]), float(pad["y"]))
        hit = next((k for k, xy in enumerate(pool) if _dist(xy, wxy) < MATCH_MM), None)
        if hit is None:
            missing.append(num)
            continue
        pool.pop(hit)
    written = stats.get("fiducial_pads", 0)
    v.check(
        not missing and not pool and len(want) == written,
        f"fiducial pads {len(want)} in the model, {written} written -> "
        f"{len(got)} read" + (f" (unmatched {_names(missing)})" if missing else ""),
    )


def _check_parts(
    v: Verdict,
    model: dict[str, Any],
    design: epro.Design,
    at: Callable[[Any, Any], _XY],
) -> dict[str, dict[str, Any]]:
    """Placed parts with at least one pad — the set the writer exports."""
    pad_refs = {str(p.get("refdes") or "") for p in model.get("pads") or []}
    want = {
        str(i["refdes"]): i
        for i in model.get("instances") or []
        if i.get("refdes")
        and i.get("x") is not None
        and i.get("y") is not None
        and str(i["refdes"]) in pad_refs
    }
    got = {str(c["refdes"]): c for c in design.components}
    missing, extra = sorted(set(want) - set(got)), sorted(set(got) - set(want))
    v.check(
        not missing and not extra,
        f"parts {len(want)} -> {len(got)}"
        + (f" (missing {_names(missing)})" if missing else "")
        + (f" (extra {_names(extra)})" if extra else ""),
    )
    worst = 0.0
    turned: list[str] = []
    lcsc: list[str] = []
    for r, inst in want.items():
        c = got.get(r)
        if c is None:
            continue
        worst = max(
            worst, _dist(at(c["x"], c["y"]), (float(inst["x"]), float(inst["y"])))
        )
        rot_w = float(inst.get("rot") or 0.0) % 360.0
        rot_g = float(c.get("rot") or 0.0) % 360.0
        turn = abs(rot_w - rot_g) % 360.0
        if min(turn, 360.0 - turn) > 1e-6 or pose.is_bottom(inst) != pose.is_bottom(c):
            turned.append(
                f"{r} {rot_w:g}/{inst.get('layer') or 'top'} -> {rot_g:g}/{c['layer']}"
            )
        if str(inst.get("part_lcsc") or "") != str(c.get("part_lcsc") or ""):
            lcsc.append(f"{r} {inst.get('part_lcsc')} -> {c.get('part_lcsc')}")
    v.check(worst < TOL_MM, f"part positions worst {_um(worst)}")
    v.check(
        not turned, "part rotation and side" + (f": {_names(turned)}" if turned else "")
    )
    v.check(not lcsc, "part LCSC numbers" + (f": {_names(lcsc)}" if lcsc else ""))
    return want


def _dedupe(pads: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One entry per (refdes, pin, position): a through-hole pad repeats
    once per copper layer on both sides (the writer collapses it too)."""
    seen: set[tuple[str, str, float, float]] = set()
    out: list[dict[str, Any]] = []
    for p in pads:
        key = (
            str(p.get("refdes") or ""),
            str(p.get("pin") or ""),
            round(float(p["x"]), 4),
            round(float(p["y"]), 4),
        )
        if key not in seen:
            seen.add(key)
            out.append(p)
    return out


def _sized(pad: dict[str, Any]) -> dict[str, Any]:
    """The reader gives a polygon pad its vertex ring and no ``w``/``h``;
    the model's polygon pads carry the ring's bounding box as their size,
    so give the re-read one the same."""
    poly = pad.get("poly")
    if "w" in pad or not poly:
        return pad
    xs, ys = [float(p[0]) for p in poly], [float(p[1]) for p in poly]
    return {**pad, "w": max(xs) - min(xs), "h": max(ys) - min(ys)}


def _got_pads(design: epro.Design, layers: list[str]) -> list[dict[str, Any]]:
    """Every re-read component's footprint pads, placed in the reader's
    frame through the same :func:`padplace.place_footprint_pads` the
    import uses, with the net each pad's connection row names."""
    fps = {f["name"]: f for f in design.footprints}
    net_of = {(c["refdes"], c["pin"]): c["net"] for c in design.connections}
    out: list[dict[str, Any]] = []
    for comp in design.components:
        fp = fps.get(comp.get("footprint"))
        if fp is None:
            continue
        pad_net = {
            str(p["pad"]): net_of[(comp["refdes"], p["name"])]
            for p in comp.get("pins", [])
            if (comp["refdes"], p["name"]) in net_of
        }
        raw = [{**_sized(p), "number": p["pin"]} for p in fp["pads"]]
        placed, _ = padplace.place_footprint_pads(
            raw, comp, layers=layers, pin_to_net=pad_net
        )
        out += placed
    return _dedupe(out)


def _check_pads(
    v: Verdict,
    model: dict[str, Any],
    design: epro.Design,
    layers: list[str],
    parts: dict[str, dict[str, Any]],
    stats: dict[str, int],
    at: Callable[[Any, Any], _XY],
) -> None:
    """Pads pair by part and nearest position: the re-read pin name is the
    pad number (no DEVICE records travel), so names cannot pair them."""
    want = _dedupe(
        [p for p in model.get("pads") or [] if str(p.get("refdes") or "") in parts]
    )
    pool: dict[str, list[dict[str, Any]]] = {}
    for g in _got_pads(design, layers):
        pool.setdefault(str(g["refdes"]), []).append(g)
    got_n = sum(len(b) for b in pool.values())
    missing: list[str] = []
    worst = 0.0
    nets: list[str] = []
    shapes: list[str] = []
    for w in want:
        r, wxy = str(w["refdes"]), (float(w["x"]), float(w["y"]))
        bucket = pool.get(r, [])
        best, best_d = None, MATCH_MM
        for k, g in enumerate(bucket):
            d = _dist(at(g["x"], g["y"]), wxy)
            if d < best_d:
                best, best_d = k, d
        if best is None:
            missing.append(f"{r}.{w.get('pin')}")
            continue
        g = bucket.pop(best)
        worst = max(worst, best_d)
        tag = f"{r}.{w.get('pin')}"
        if str(w.get("net") or "") != str(g.get("net") or ""):
            nets.append(f"{tag} {w.get('net') or '-'} -> {g.get('net') or '-'}")
        size_w = sorted((float(w["w"]), float(w.get("h", w["w"]))))
        size_g = sorted((float(g["w"]), float(g.get("h", g["w"]))))
        drill_w, drill_g = float(w.get("drill") or 0.0), float(g.get("drill") or 0.0)
        if (
            w.get("shape") != g.get("shape")
            or max(abs(a - b) for a, b in zip(size_w, size_g, strict=True)) >= TOL_MM
            or abs(drill_w - drill_g) >= TOL_MM
        ):
            shapes.append(tag)
    extra = [f"{g['refdes']}.{g.get('pin')}" for b in pool.values() for g in b]
    written = stats.get("pads", len(want))
    v.check(
        not missing and not extra and written == len(want),
        f"pads {len(want)} in the model, {written} written -> {got_n} read"
        + (f" (unmatched {_names(missing)})" if missing else "")
        + (f" (extra {_names(extra)})" if extra else ""),
    )
    v.check(worst < TOL_MM, f"pad positions worst {_um(worst)}")
    v.check(
        not nets,
        "pad nets" + (f": {len(nets)} differ — {_names(nets)}" if nets else ""),
    )
    v.check(
        not shapes,
        "pad shape, size and drill"
        + (f": {len(shapes)} differ — {_names(shapes)}" if shapes else ""),
    )


def _check_nets(
    v: Verdict,
    model: dict[str, Any],
    board: epro.EproDocument,
    parts: dict[str, dict[str, Any]],
) -> None:
    """Every net the board names is declared in the file. Pro has no NET
    record; a net lives in ``PAD_NET`` rows and copper ``netName``s, and a
    memberless one rides a stale ``PAD_NET`` (the writer's docstring)."""
    layers = {str(n) for n in model.get("layers") or []}
    want = {str(n) for n in model.get("nets") or [] if n}
    want |= {
        str(p["net"])
        for p in model.get("pads") or []
        if p.get("net") and str(p.get("refdes") or "") in parts
    }
    for item in model.get("copper") or []:
        ctype = item.get("ctype")
        on_board = ctype == "via" or str(item.get("layer") or "") in layers
        if item.get("net") and ctype in ("track", "via", "pour") and on_board:
            want.add(str(item["net"]))
    declared = {str(b["padNet"]) for b in board.bodies("PAD_NET") if b.get("padNet")}
    declared |= {
        str(b["netName"])
        for t in ("LINE", "ARC", "VIA", "POUR")
        for b in board.bodies(t)
        if b.get("netName")
    }
    missing = sorted(want - declared)
    v.check(
        not missing,
        f"nets {len(want)} named -> {len(declared)} declared in the file"
        + (f" (missing {_names(missing)})" if missing else ""),
    )


@dataclass(frozen=True)
class _Seg:
    layer: str
    net: str
    width: float
    a: _XY
    b: _XY
    center: _XY | None
    cw: bool


def _segments(items: list[dict[str, Any]], at: Callable[[Any, Any], _XY]) -> list[_Seg]:
    out: list[_Seg] = []
    for t in items:
        if t.get("ctype") != "track":
            continue
        width = float(t.get("width_mm") or 0.0)
        for seg in t.get("segments") or []:
            a, b = at(*seg["start"]), at(*seg["end"])
            arc = seg.get("shape") == "arc"
            out.append(
                _Seg(
                    layer=str(t.get("layer") or ""),
                    net=str(t.get("net") or ""),
                    width=width,
                    a=a,
                    b=b,
                    center=at(*seg["center"]) if arc else None,
                    cw=bool(seg.get("cw", True)) if arc else False,
                )
            )
    return out


def _seg_error(w: _Seg, g: _Seg) -> float | None:
    """Worst coordinate error between two segments, or ``None`` when they
    are not the same segment (either direction; reversing an arc flips
    its handedness, as the reader's chaining may do)."""
    if (w.center is None) != (g.center is None) or abs(w.width - g.width) >= TOL_MM:
        return None
    best: float | None = None
    for flipped, (ga, gb) in ((False, (g.a, g.b)), (True, (g.b, g.a))):
        if w.center is not None and g.center is not None and w.cw != (g.cw != flipped):
            continue
        err = max(_dist(w.a, ga), _dist(w.b, gb))
        if w.center is not None and g.center is not None:
            err = max(err, _dist(w.center, g.center))
        if err < MATCH_MM and (best is None or err < best):
            best = err
    return best


def _check_copper(
    v: Verdict,
    model: dict[str, Any],
    copper_got: list[dict[str, Any]],
    stats: dict[str, int],
    at: Callable[[Any, Any], _XY],
) -> None:
    """Track segments: every one read back pairs with a model segment, and
    as many read back as the writer says it wrote."""
    layers = {str(n) for n in model.get("layers") or []}

    def writable(s: _Seg) -> bool:
        if s.layer not in layers or s.width <= 0.0 or s.a == s.b:
            return False
        return s.center is None or arc_sweep_deg(s.a, s.b, s.center, cw=s.cw) != 0.0

    want = [
        s
        for s in _segments(model.get("copper") or [], lambda x, y: (float(x), float(y)))
        if writable(s)
    ]
    got = _segments(copper_got, at)
    written = stats.get("lines", 0) + stats.get("arcs", 0)
    pool: dict[tuple[str, str], list[_Seg]] = {}
    for s in want:
        pool.setdefault((s.layer, s.net), []).append(s)
    unmatched: list[str] = []
    worst = 0.0
    for g in got:
        bucket = pool.get((g.layer, g.net), [])
        best, best_err = None, None
        for k, w in enumerate(bucket):
            err = _seg_error(w, g)
            if err is not None and (best_err is None or err < best_err):
                best, best_err = k, err
        if best is None or best_err is None:
            unmatched.append(f"{g.layer} {g.net or '-'} ({g.a[0]:.3f}, {g.a[1]:.3f})")
            continue
        bucket.pop(best)
        worst = max(worst, best_err)
    v.check(
        not unmatched and len(got) == written == len(want),
        f"copper segments {len(want)} in the model, {written} written -> "
        f"{len(got)} read"
        + (f" (not in the model: {_names(unmatched)})" if unmatched else ""),
    )
    v.check(worst < TOL_MM, f"copper positions worst {_um(worst)}")


def _check_vias(
    v: Verdict,
    model: dict[str, Any],
    copper_got: list[dict[str, Any]],
    stats: dict[str, int],
    at: Callable[[Any, Any], _XY],
) -> None:
    want = [
        i
        for i in model.get("copper") or []
        if i.get("ctype") == "via" and i.get("x") is not None and i.get("y") is not None
    ]
    got = [i for i in copper_got if i.get("ctype") == "via"]
    pool: dict[str, list[dict[str, Any]]] = {}
    for w in want:
        pool.setdefault(str(w.get("net") or ""), []).append(w)
    unmatched: list[str] = []
    sizes: list[str] = []
    worst = 0.0
    for g in got:
        gxy = at(g["x"], g["y"])
        bucket = pool.get(str(g.get("net") or ""), [])
        best, best_d = None, MATCH_MM
        for k, w in enumerate(bucket):
            d = _dist((float(w["x"]), float(w["y"])), gxy)
            if d < best_d:
                best, best_d = k, d
        if best is None:
            unmatched.append(f"{g.get('net') or '-'} ({gxy[0]:.3f}, {gxy[1]:.3f})")
            continue
        w = bucket.pop(best)
        worst = max(worst, best_d)
        for key in ("dia_mm", "drill_mm"):
            if abs(float(w.get(key) or 0.0) - float(g.get(key) or 0.0)) >= TOL_MM:
                sizes.append(
                    f"{g.get('net') or '-'} {key} {w.get(key)} -> {g.get(key)}"
                )
    v.check(
        not unmatched and len(got) == stats.get("vias", 0) == len(want),
        f"vias {len(want)} in the model, {stats.get('vias', 0)} written -> "
        f"{len(got)} read"
        + (f" (not in the model: {_names(unmatched)})" if unmatched else ""),
    )
    v.check(
        worst < TOL_MM and not sizes,
        f"via positions worst {_um(worst)}"
        + (f"; sizes differ: {_names(sizes)}" if sizes else ""),
    )


def _check_outline(
    v: Verdict,
    outline: list[_XY],
    design: epro.Design,
    at: Callable[[Any, Any], _XY],
) -> None:
    feat = next((f for f in design.features if f.get("ftype") == "outline"), None)
    if feat is None:
        v.check(False, "outline: none read back")
        return
    want = _ring(outline)
    got = _ring([at(x, y) for x, y in feat["geom"]["path"]])
    if len(want) != len(got):
        v.check(False, f"outline vertices {len(want)} -> {len(got)}")
        return
    worst = max((_dist(a, b) for a, b in zip(want, got, strict=True)), default=0.0)
    v.check(worst < TOL_MM, f"outline {len(want)} vertices, worst {_um(worst)}")


def _check_rules(v: Verdict, model: dict[str, Any], design: epro.Design) -> None:
    """The imported rule table rides back byte-identical — only for a board
    that carries one (``meta.epro_rules``)."""
    rules = model.get("epro_rules") or []
    if not rules:
        return
    want = {str(r["id"]): r.get("body") for r in rules}
    got = {str(r["id"]): r.get("body") for r in design.epro_rules}
    v.check(want == got, f"rule records {len(want)} -> {len(got)}, bodies verbatim")


__all__ = ["MATCH_MM", "TOL_MM", "Verdict", "verify_export"]
