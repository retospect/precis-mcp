"""The local PCB dev loop's driver — ``scripts/pcb-loop`` runs this in
the dev container on this worktree's dev DB (``precis_dev``, local
Postgres). No MCP server, no prod, no deploy: the same code path the
jobs run, executed in-process.

One iteration: import an ``.epro2`` into a fresh dev slug →
``placer='topo'`` place job → route-only ``place=False`` job → DRC →
SVG renders (topo placement, sketch, realized board) → ``summary.md`` +
``metrics.json``. Per-stage wall times land in both outputs; the loop's
job is to make every stage land cleanly in a few minutes.

Reuses the topo bench's helpers (``tests.fixtures.pcb.topo_bench``) so
loop numbers and bench numbers come from the same measurement code.

Run inside the container: ``python -m tests.fixtures.pcb.pcb_loop
--board <epro2> --slug <slug> --out <dir> [--seed N] [--effort N]
[--skip-route]``.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from precis.cli._common import resolve_dsn
from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.ingest.pcb_epro import import_epro
from precis.response import Response
from precis.store import Store
from precis.workers.executors._common import SUCCEEDED
from tests.fixtures.pcb import route_bench as rb
from tests.fixtures.pcb import topo_bench as tb

#: TopoStage fields mirrored into metrics["place"].
_STAGE_FIELDS = (
    "crossings_start",
    "crossings",
    "evals",
    "evals_per_s",
    "airwire_mm",
    "place_s",
)


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _fresh_slug(store: Store, want: str) -> str:
    """The dev DB persists between runs; bump a suffix rather than
    refuse or overwrite (a fresh slug is what import_epro accepts)."""
    slug, n = want, 2
    while store.get_ref(kind="pcb", id=slug) is not None:
        slug, n = f"{want}-{n}", n + 1
    return slug


def _render(out: Path, pcb: PcbHandler, slug: str, files: list[str]) -> None:
    """Four eyes on one board: this module's courtyard figure of the
    stored pose (placement.svg — the run's own topo figure is kept as
    topo-placement.svg), the sketch, the realized copper."""
    for name, view, args in (
        ("topo-placement.svg", "topo", {"part": "svg"}),
        ("sketch.svg", "svg", {"level": "sketch"}),
        ("board.svg", "svg", {"level": "board"}),
    ):
        try:
            body = str(pcb.get(id=slug, view=view, args=args).body)
        except Exception as exc:
            body = f"<!-- render failed: {exc} -->"
        if body.strip():
            (out / name).write_text(body, encoding="utf-8")
            files.append(name)


def _placement_svg(
    store: Store, pcb: PcbHandler, ref: Any, findings: Any
) -> tuple[str, list[str], float | None]:
    """Courtyard figure of the STORED pose: real courtyard outlines
    (:func:`world_courtyard_polygons`, the same shapes the DRC reserves),
    bottom-side parts in orange, overlap-finding refdes filled red, the
    board outline in black. Returns ``(svg, outside_refdes,
    outline_area_mm2)`` — outside = a part's courtyard not fully inside
    the outline (a placement violation per Reto)."""
    from shapely.geometry import Polygon

    from precis.pcb import session as pcb_session
    from precis.pcb.cost import COURTYARD_MIN_SEPARATION_MM
    from precis.pcb.ir import COURTYARD_CLEARANCE_MM, world_courtyard_polygons

    graph = store.pcb_graph(ref.id)
    features = store.pcb_features_list(ref.id)
    ir = pcb._build_ir(ref.id, graph)
    outline = pcb_session.outline_from_features(features)
    outline_poly = Polygon(outline) if outline and len(outline) >= 3 else None
    polys = world_courtyard_polygons(
        ir,
        clearance_mm=COURTYARD_CLEARANCE_MM,
        fallback_half_extent_mm=COURTYARD_MIN_SEPARATION_MM / 2.0,
    )
    overlap: set[str] = set()
    for f in findings or []:
        rule = f["rule"] if isinstance(f, dict) else f.rule
        objs = f.get("objects") if isinstance(f, dict) else f.objects
        if "courtyard" in rule or "footprint" in rule or "overlap" in rule:
            for o in objs or []:
                overlap.update(str(v) for v in o.values())

    outside: list[str] = []
    body: list[str] = []
    xs: list[float] = []
    ys: list[float] = []
    for i, poly in enumerate(polys):
        if not poly:
            continue
        refdes = str(ir.instance_refdes[i])
        pg = Polygon(poly)
        if outline_poly is not None and not pg.within(outline_poly):
            outside.append(refdes)
        xs += [p[0] for p in poly]
        ys += [p[1] for p in poly]
        color = (
            "#d62728"
            if refdes in overlap
            else ("#ff7f0e" if bool(ir.inst_bottom[i]) else "#1f77b4")
        )
        pts = " ".join(f"{x:.2f},{y:.2f}" for x, y in poly)
        body.append(
            f'<polygon points="{pts}" fill="{color}" fill-opacity="0.3" '
            f'stroke="{color}" stroke-width="0.1"/>'
        )
        body.append(
            f'<text x="{pg.centroid.x:.2f}" y="{pg.centroid.y:.2f}" '
            f'font-size="1.6" font-family="sans-serif" '
            f'text-anchor="middle">{refdes}</text>'
        )
    if outline_poly is not None and outline is not None:
        xs += [p[0] for p in outline]
        ys += [p[1] for p in outline]
        opts = " ".join(f"{x:.2f},{y:.2f}" for x, y in outline)
        body.insert(
            0,
            f'<polygon points="{opts}" fill="none" stroke="#000000" '
            'stroke-width="0.3"/>',
        )
    if not xs:
        xs, ys = [0.0], [0.0]
    x0, x1, y0, y1 = min(xs) - 2, max(xs) + 2, min(ys) - 2, max(ys) + 2
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'viewBox="{x0:.2f} {y0:.2f} {x1 - x0:.2f} {y1 - y0:.2f}">'
        + "".join(body)
        + "</svg>\n"
    )
    return svg, outside, (outline_poly.area if outline_poly else None)


def _finish(out: Path, metrics: dict[str, Any], files: list[str]) -> None:
    metrics["files"] = files
    stages = metrics["stages"]
    metrics["total_s"] = round(sum(stages.values()), 1)
    (out / "metrics.json").write_text(
        json.dumps(metrics, indent=1, default=str) + "\n", encoding="utf-8"
    )
    lines = [
        f"# pcb-loop {metrics.get('slug')} — {metrics['generated_utc']}",
        "",
        f"board: `{metrics['board']}` · seed {metrics['seed']} "
        f"· effort {metrics['effort']}",
        "",
        "## stage wall times",
        "",
        "| stage | s |",
        "|---|---|",
        *(f"| {k} | {v} |" for k, v in stages.items()),
        f"| **total** | {metrics['total_s']} |",
        "",
    ]
    vias = metrics.get("vias")
    if vias:
        lines += [
            "## vias",
            "",
            f"total {vias['total']} · density {vias['density_per_cm2']}/cm²",
            "",
            "| net | vias |",
            "|---|---|",
            *(f"| {n} | {c} |" for n, c in list(vias["per_net"].items())[:15]),
            "",
        ]
    lines += [
        "## results",
        "",
        "```json",
        json.dumps(
            {k: v for k, v in metrics.items() if k != "stages"},
            indent=1,
            default=str,
        ),
        "```",
        "",
    ]
    (out / "summary.md").write_text("\n".join(lines), encoding="utf-8")


def run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    stages: dict[str, float] = {}
    files: list[str] = []
    metrics: dict[str, Any] = {
        "board": str(args.board),
        "seed": args.seed,
        "effort": args.effort,
        "generated_utc": _utc_now(),
        "stages": stages,
    }
    store = Store.connect(resolve_dsn(None))
    pcb = PcbHandler(hub=Hub(store=store))
    slug = _fresh_slug(store, args.slug)
    metrics["slug"] = slug

    # ── import ───────────────────────────────────────────────────────
    t = time.monotonic()
    try:
        result = import_epro(
            store,
            Path(args.board).read_bytes(),
            slug=slug,
            copper="none",  # fresh place+route; authored copper is stale
            board_uuid=args.board_uuid or None,
            source_name=Path(args.board).name,
            dry_run=False,
        )
    except Exception as exc:
        stages["import"] = round(time.monotonic() - t, 1)
        metrics["import"] = {"status": "refused", "error": str(exc)}
        _render(out, pcb, slug, files)
        _finish(out, metrics, files)
        print(f"pcb-loop: import refused — {exc}", file=sys.stderr)
        return 2
    stages["import"] = round(time.monotonic() - t, 1)
    s = result.stats
    metrics["import"] = {
        "status": "ok",
        "components": s.get("components"),
        "nets": s.get("nets"),
        "connections": s.get("connections"),
        "warnings": list(result.warnings),
    }

    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None

    # ── topo place (--skip-place: nothing queued; the route leg then
    # runs the default pipeline = the anneal+vroute baseline) ──────────
    place_ok = True
    if args.skip_place:
        metrics["place"] = {"status": "skipped", "note": "--skip-place"}
    else:
        try:
            enq = pcb.put(
                id=slug,
                args={
                    "op": "place",
                    "placer": "topo",
                    "iters": args.effort,
                    "seed": args.seed,
                    "force": args.force,
                },
            )
        except BadInput as exc:
            metrics["place"] = {"status": "refused", "note": str(exc)}
            _render(out, pcb, slug, files)
            _finish(out, metrics, files)
            print(f"pcb-loop: place refused — {exc}", file=sys.stderr)
            return 2
        if "enqueued" not in enq.body:
            metrics["place"] = {
                "status": "enqueue refused",
                "note": enq.body[:400],
            }
            _render(out, pcb, slug, files)
            _finish(out, metrics, files)
            return 2
        place_job, status, place_wall = tb._drain(store, ref.id)
        stages["place"] = round(place_wall, 1)
        place_ok = status == SUCCEEDED
        if not place_ok:
            side = tb._fail_side(store, place_job, "topo place refused")
            metrics["place"] = {"status": status, "note": side.note}
            # The refusal still stamps meta.topo_run (job stamps it
            # before recording the failure) — surface its stage numbers.
            stage = tb._topo_stage(store, place_job, place_wall)
            metrics["place"].update({k: getattr(stage, k) for k in _STAGE_FIELDS})
        else:
            stage = tb._topo_stage(store, place_job, place_wall)
            metrics["place"] = {
                "status": status,
                **{k: getattr(stage, k) for k in _STAGE_FIELDS},
            }

    # ── route (vroute, poses held) — a refused place leaves the stored
    # (authored) pose; routing it still exercises the vroute leg, and the
    # row is labelled so authored-pose numbers are never read as topo.
    side = tb.PipeSide(note="--skip-route")
    route_ok = place_ok
    # --skip-place means the stored pose is the authored one and route
    # re-places: the default anneal+route pipeline, i.e. the baseline.
    want_place = bool(args.anneal_route or args.skip_place)
    if not args.skip_route:
        route_enq: Response | None
        try:
            route_enq = pcb.put(
                id=slug,
                args={
                    "op": "route",
                    # --anneal-route re-runs the joint anneal seeded from
                    # the stored pose: with --force it is the interim
                    # legalize step for a placement-illegal topo pose.
                    "place": want_place,
                    "seed": args.seed,
                    "force": args.force,
                },
            )
        except BadInput as exc:
            side = tb.PipeSide(note=f"route refused: {exc}")
            route_ok = False
            route_enq = None
        if route_enq is not None and "enqueued" in route_enq.body:
            route_job, status, route_wall = tb._drain(store, ref.id)
            stages["route"] = round(route_wall, 1)
            if status == SUCCEEDED:
                side = tb._measure(store, pcb, slug, route_wall)
            else:
                # A refused route still writes its partial copper — the
                # loop wants the counts it did get plus the failure note.
                failed = tb._fail_side(store, route_job, "route refused")
                side = dataclasses.replace(
                    tb._measure(store, pcb, slug, route_wall),
                    note=failed.note,
                )
            route_ok = status == SUCCEEDED
        elif route_enq is not None:
            side = tb.PipeSide(note=f"route enqueue refused: {route_enq.body[:400]}")
            route_ok = False

    metrics["route_mode"] = (
        "--skip-route"
        if args.skip_route
        else (
            "anneal+vroute baseline"
            if args.skip_place
            else (
                ("anneal+route from " if args.anneal_route else "pose hand-off of ")
                + ("topo pose" if place_ok else "authored pose (topo place refused)")
            )
        )
    )

    # ── DRC — _measure runs it on a routed board; run it standalone
    # when there is no route side (skip-route or a refused job) so a
    # placement-illegal topo pose still shows its errors.
    t = time.monotonic()
    if side.drc_errors is None:
        pcb._drc_run(ref.id, store.pcb_load(ref.id), record=True)
    _run, findings = store.pcb_drc_findings_latest(ref.id)
    errors, warns, _keys = rb._split_findings(findings)
    side = dataclasses.replace(side, drc_errors=sum(errors.values()))
    stages["drc"] = round(time.monotonic() - t, 1)
    # Footprint/courtyard overlap counts as a DRC violation and is
    # reported separately from the copper DRC (Reto) — on an illegal
    # topo pose it is THE number the loop is fixing.
    overlap = sum(
        v
        for r, v in errors.items()
        if "courtyard" in r or "footprint" in r or "part_overlap" in r
    )
    metrics["drc"] = {
        "errors": sum(errors.values()),
        "overlap_errors": overlap,
        "copper_errors": sum(errors.values()) - overlap,
        "warns": sum(warns.values()),
    }

    metrics["route"] = {
        "routed": side.routed,
        "nets": side.nets,
        "unrouted": (
            side.nets - side.routed
            if side.nets is not None and side.routed is not None
            else None
        ),
        "median_mm": side.median_mm,
        "vias": side.vias,
        "drc_errors": side.drc_errors,
        "note": side.note,
    }

    # ── placement figure + out-of-outline count ──────────────────────
    try:
        svg, outside, area_mm2 = _placement_svg(store, pcb, ref, findings)
        (out / "placement.svg").write_text(svg, encoding="utf-8")
        files.append("placement.svg")
    except Exception as exc:
        outside, area_mm2 = [], None
        metrics["placement_render_error"] = str(exc)
    metrics["drc"]["outside_outline"] = outside
    metrics["drc"]["outside_outline_count"] = len(outside)

    # ── vias per net + density (Reto: cluster check near R25/CN3) ────
    import collections as _c

    board = store.pcb_load(ref.id)["board"]
    via_per_net: _c.Counter[str] = _c.Counter()
    if board is not None:
        for item in store.pcb_copper_list(int(board["board_id"])):
            if item.get("ctype") == "via" and not item.get("fixed"):
                via_per_net[str(item.get("net") or "?")] += 1
    total_vias = sum(via_per_net.values())
    metrics["vias"] = {
        "total": total_vias,
        "per_net": dict(via_per_net.most_common()),
        "density_per_cm2": (
            round(total_vias / (area_mm2 / 100.0), 2) if area_mm2 else None
        ),
    }

    _render(out, pcb, slug, files)
    _finish(out, metrics, files)

    print(
        f"pcb-loop {slug}: routed {side.routed}/{side.nets} "
        f"drc {side.drc_errors} (overlap {metrics['drc']['overlap_errors']}) "
        f"wall {metrics['total_s']}s — {out}"
    )
    return 0 if (route_ok and place_ok) else (1 if place_ok else 2)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pcb_loop",
        description="local PCB dev loop: epro2 import -> topo place -> "
        "vroute -> DRC + metrics + renders (dev DB only)",
    )
    ap.add_argument("--board", required=True, help=".epro2 path (the copy)")
    ap.add_argument(
        "--board-uuid",
        default="",
        help="which PCB document in the project — required when the "
        "archive carries more than one",
    )
    ap.add_argument("--slug", required=True, help="base slug; bumps on collision")
    ap.add_argument("--out", required=True, help="output dir (worktree-relative)")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--effort", type=int, default=tb.TOPO_EFFORT)
    ap.add_argument("--skip-route", action="store_true")
    ap.add_argument(
        "--skip-place",
        action="store_true",
        help="no topo place — route runs the default pipeline: the "
        "anneal+vroute baseline for the before/after table",
    )
    ap.add_argument(
        "--anneal-route",
        action="store_true",
        help="route job re-runs the joint anneal (place=True) seeded "
        "from the stored pose instead of holding poses — the interim "
        "legalize step for a --force-committed topo pose",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="force=True on the place + route jobs' gates — commits an "
        "illegal topo pose and routes it anyway; override is recorded in "
        "each job summary",
    )
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
