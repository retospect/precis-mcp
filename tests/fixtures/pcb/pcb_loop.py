"""The local PCB dev loop's driver — ``scripts/pcb-loop`` runs this in
the dev container on this worktree's dev DB (``precis_dev``, local
Postgres). No MCP server, no prod, no deploy: the same code path the
jobs run, executed in-process.

One iteration: import an ``.epro2`` into a fresh dev slug →
``placer='topo'`` place job → DRC → SVG renders (topo placement,
sketch, board) → ``summary.md`` + ``metrics.json``. Per-stage wall
times land in both outputs; the loop's job is to make every stage land
cleanly in a few minutes.

Route is topo slice 2 output, landing with builder topo-2b; vroute is
gone — topo is the only router (Reto 2026-10-25). Until the topo route
exists the loop reports the place stage's own numbers (crossings,
evals, airwire) plus the realized via count, and never enqueues an
``op='route'`` job. The route stage below is the seam topo-2b's output
plugs into.

Reuses the topo bench's helpers (``tests.fixtures.pcb.topo_bench``) so
loop numbers and bench numbers come from the same measurement code.

Run inside the container: ``python -m tests.fixtures.pcb.pcb_loop
--board <epro2> --slug <slug> --out <dir> [--seed N] [--effort N]
[--skip-place]``.
"""

from __future__ import annotations

import argparse
import collections
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
from precis.pcb.capabilities import capability_for
from precis.pcb.drc import process_for_stackup
from precis.pcb.rules import resolve_net_rules
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
    "shove_mm",
    "shove_rounds",
    "legalize_mm",
    "legalize_left",
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
    for i, poly in enumerate(polys):
        if not poly:
            continue
        refdes = str(ir.instance_refdes[i])
        pg = Polygon(poly)
        if outline_poly is not None and not pg.within(outline_poly):
            outside.append(refdes)
    from precis.pcb.topoplace.render import courtyard_figure

    svg = courtyard_figure(
        [poly or None for poly in polys],
        refdes=[str(r) for r in ir.instance_refdes],
        bottom=[bool(b) for b in ir.inst_bottom],
        overlap=overlap,
        outline=outline if outline_poly is not None else None,
    )
    return svg, outside, (outline_poly.area if outline_poly else None)


#: Power-copper floors for the widen stage (handoff numbers): tracks
#: never narrower than this, vias never smaller — a net's resolved or
#: declared width only ever RAISES the target.
_WIDEN_TRACK_FLOOR_MM = 0.5
_WIDEN_VIA_FLOOR_MM = 0.8


def _widen_power_copper(store: Store, pcb: PcbHandler, ref: Any) -> dict[str, Any]:
    """Widen the route's GND/power copper before final DRC (loop
    quality ask): each power net's tracks grow to
    ``max(0.5, declared width_mm, rules-resolved width_mm)`` and its
    vias to ``max(0.8, rules-resolved via_dia_mm)``, then the DERIVED
    ``pcb_copper`` rows are rewritten wholesale via
    ``SELECT pcb_copper`` + ``pcb_copper_replace`` — never
    ``pcb_copper_list`` back in, which would persist its unioned
    authored fixed copper into the derived table. DRC brackets the
    rewrite so the pre/post numbers land in the report."""
    design = store.pcb_load(ref.id)
    board = design["board"]
    out: dict[str, Any] = {"changed": 0}
    if board is None:
        out["note"] = "no board"
        return out
    board_id = int(board["board_id"])
    nets = design.get("nets") or []
    power = tb.power_net_names(nets)
    out["power_nets"] = sorted(power)
    if not power:
        return out
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT c.ctype, c.layer, c.net_id, c.route_id, c.geom, n.name "
            "FROM pcb_copper c JOIN pcb_nets n ON n.net_id = c.net_id "
            "WHERE c.board_id = %s AND n.retired_at IS NULL",
            (board_id,),
        ).fetchall()
    if not rows:
        return out
    caps = capability_for(process_for_stackup(board.get("stackup") or []))
    net_classes = design.get("net_classes") or {}
    stack_layers = [str(ly["name"]) for ly in board.get("stackup") or []]
    outer = {stack_layers[0], stack_layers[-1]} if stack_layers else set()

    def targets(net: dict[str, Any]) -> tuple[float, float, float]:
        cls = str(net.get("net_class") or "")
        overrides = net_classes.get(cls)
        current = net.get("est_current_a")
        outer_r = resolve_net_rules(
            cls,
            layer_is_outer=True,
            fab_caps=caps,
            overrides=overrides,
            current_a=current,
        )
        inner_w = resolve_net_rules(
            cls,
            layer_is_outer=False,
            fab_caps=caps,
            overrides=overrides,
            current_a=current,
        ).track_width_mm
        declared = float(net.get("width_mm") or 0.0)
        floor = max(_WIDEN_TRACK_FLOOR_MM, declared)
        return (
            max(floor, outer_r.track_width_mm),
            max(floor, inner_w),
            max(_WIDEN_VIA_FLOOR_MM, outer_r.via_dia_mm or 0.0),
        )

    want = {str(n["name"]): targets(n) for n in nets if str(n.get("name")) in power}
    per_net: dict[str, dict[str, Any]] = collections.defaultdict(
        lambda: {"tracks": 0, "vias": 0, "mm": 0.0}
    )
    new_rows: list[dict[str, Any]] = []
    for ctype, layer, net_id, route_id, geom, net_name in rows:
        g = dict(geom or {})
        name = str(net_name)
        w_outer, w_inner, w_via = want.get(name, (0.0, 0.0, 0.0))
        if ctype == "track" and name in power:
            want_w = w_outer if str(layer) in outer else w_inner
            per_net[name]["mm"] += float(rb._track_length(g.get("segments") or ()))
            if float(g.get("width_mm") or 0.0) < want_w:
                g["width_mm"] = want_w
                per_net[name]["tracks"] += 1
        elif ctype == "via" and name in power:
            if float(g.get("dia_mm") or 0.0) < w_via:
                g["dia_mm"] = w_via
                per_net[name]["vias"] += 1
        new_rows.append(
            {
                "ctype": ctype,
                "layer": layer,
                "net_id": net_id,
                "route_id": route_id,
                "geom": g,
            }
        )
    changed = sum(v["tracks"] + v["vias"] for v in per_net.values())
    out.update(
        {"changed": changed, "per_net": {k: dict(v) for k, v in per_net.items()}}
    )
    if changed:
        # DRC brackets the wholesale rewrite: pre numbers read the
        # route's copper, post numbers read the widened one.
        out["drc_pre"] = _drc_errors(store, pcb, ref)
        store.pcb_copper_replace(board_id, new_rows)
        out["drc_post"] = _drc_errors(store, pcb, ref)
    return out


def _drc_errors(store: Store, pcb: PcbHandler, ref: Any) -> int:
    """One recorded DRC run's error count — the widen stage's pre/post
    bracket."""
    pcb._drc_run(ref.id, store.pcb_load(ref.id), record=True)
    _run, findings = store.pcb_drc_findings_latest(ref.id)
    errors, _warns, _keys = rb._split_findings(findings)
    return sum(errors.values())


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
    widen = metrics.get("widen") or {}
    if widen.get("power_nets"):
        lines += [
            "## power nets (widen)",
            "",
            f"changed {widen.get('changed', 0)} copper rows "
            f"(drc {widen.get('drc_pre', '—')} → {widen.get('drc_post', '—')})",
            "",
            "| net | tracks | vias | track mm |",
            "|---|---|---|---|",
            *(
                f"| {n} | {v.get('tracks', 0)} | {v.get('vias', 0)} "
                f"| {v.get('mm', 0.0):.1f} |"
                for n, v in sorted((widen.get("per_net") or {}).items())
            ),
            "",
        ]
    unrouted = (metrics.get("route") or {}).get("unrouted_nets") or []
    if unrouted:
        lines += [
            "## unrouted nets",
            "",
            "| net | status | reason |",
            "|---|---|---|",
            *(
                f"| {u['name']} | {u['status']} "
                f"| {(u.get('note') or '').replace('|', '/')} |"
                for u in unrouted
            ),
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

    # ── topo place (--skip-place: nothing queued; DRC and the renders
    # then read the stored authored pose) ─────────────────────────────
    place_ok = True
    stage = tb.TopoStage()
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
        stage = tb._topo_stage(store, place_job, place_wall)
        if not place_ok:
            # The refusal still stamps meta.topo_run (job stamps it
            # before recording the failure) — surface its stage numbers.
            note = tb._fail_side(store, place_job, "topo place refused").note
            metrics["place"] = {"status": status, "note": note}
            metrics["place"].update({k: getattr(stage, k) for k in _STAGE_FIELDS})
        else:
            metrics["place"] = {
                "status": status,
                **{k: getattr(stage, k) for k in _STAGE_FIELDS},
            }

    # ── route — topo slice 2 output once topo-2b lands it; this is the
    # seam. vroute is gone (topo is the only router), so no job is
    # enqueued: the loop reports the place stage's crossing numbers and
    # the realized via count (computed below) until the topo route
    # writes copper.
    route_ran = False
    metrics["route"] = {
        "status": "pending topo slice 2",
        "note": "topo route output lands with topo-2b; vroute removed",
        "crossings_start": stage.crossings_start,
        "crossings": stage.crossings,
        "via_bracket": stage.via_bracket,
    }

    # ── widen — GND/power copper grown after route, before final DRC
    # (the loop's power-rail ask). No route stage means no derived
    # copper, so today it reports 0 — the seam and its DRC bracket are
    # in place for topo-2b's output.
    metrics["widen"] = _widen_power_copper(store, pcb, ref)

    # ── DRC — no route stage runs it; run it standalone so a
    # placement-illegal topo pose still shows its errors. A widen pass
    # that rewrote copper already recorded the post-widen run — the
    # latest findings ARE the final ones.
    t = time.monotonic()
    if "drc_post" not in metrics["widen"]:
        pcb._drc_run(ref.id, store.pcb_load(ref.id), record=True)
    _run, findings = store.pcb_drc_findings_latest(ref.id)
    errors, warns, _keys = rb._split_findings(findings)
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

    # Per-net route statuses with their reasons — the same rows
    # view='route-status' shows (status + the legible note the route job
    # writes), restricted to the >=2-member nets the routed count
    # covers. Meaningful only once the topo route stage runs; with no
    # router every net reports unrouted, which is noise not signal.
    unrouted_nets: list[dict[str, Any]] = []
    if route_ran:
        fanout = {
            str(n["name"]): len(n["members"]) for n in store.pcb_graph(ref.id)["nets"]
        }
        unrouted_nets = [
            {"name": r["name"], "status": r["status"], "note": r.get("note")}
            for r in store.pcb_route_status(ref.id)
            if fanout.get(str(r["name"]), 0) >= 2 and r["status"] != "realized"
        ]
    metrics["route"]["unrouted_nets"] = unrouted_nets

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
    board = store.pcb_load(ref.id)["board"]
    via_per_net: collections.Counter[str] = collections.Counter()
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
        f"pcb-loop {slug}: crossings {stage.crossings_start}→{stage.crossings} "
        f"drc {metrics['drc']['errors']} "
        f"(overlap {metrics['drc']['overlap_errors']}) "
        f"wall {metrics['total_s']}s — {out}"
    )
    return 0 if place_ok else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="pcb_loop",
        description="local PCB dev loop: epro2 import -> topo place -> "
        "DRC + metrics + renders (dev DB only; route is topo slice 2, "
        "pending topo-2b)",
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
    ap.add_argument(
        "--skip-place",
        action="store_true",
        help="no topo place — DRC and the renders read the stored authored pose",
    )
    ap.add_argument(
        "--force",
        action="store_true",
        help="force=True on the place job's gates — commits an illegal "
        "topo pose anyway; override is recorded in the job summary",
    )
    args = ap.parse_args(argv)
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
