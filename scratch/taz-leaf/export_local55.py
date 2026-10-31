"""Phase 1 of the copper upload: export taz-leaf-local-55's committed
placement + realized routes/copper from the dev DB into JSON for the
host-side prod push (prod_put_copper.py).

The dev postgres publishes no host port, so the prod write cannot read it
directly; this script runs INSIDE the dev container:

    scripts/dev uv run --no-sync python scratch/taz-leaf/export_local55.py

Exports to scratch/taz-leaf/out/local-55_export/:
  poses.json    {refdes: {x, y, rot, layer}} — the committed (anneal+shove)
                instance poses, read from pcb_instances
  label_spots.json  {refdes: spot} — the placer's label pins (instance meta)
  routes.json   pcb_routes_get() verbatim — the canonical sketch per net
  copper.json   raw derived pcb_copper rows (ctype/layer/net/route?/geom)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn
from precis.store import Store

HERE = Path(__file__).parent
SLUG = sys.argv[1] if len(sys.argv) > 1 else "taz-leaf-local-55"
OUT = HERE / "out" / (sys.argv[2] if len(sys.argv) > 2 else "local-55_export")


def main() -> int:
    store = Store.connect(resolve_dsn(None))
    ref = store.get_ref(kind="pcb", id=SLUG)
    if ref is None:
        print(f"no pcb ref {SLUG!r}")
        return 1
    ref_id = int(ref.id)
    OUT.mkdir(parents=True, exist_ok=True)

    board = store.pcb_load(ref_id)
    poses: dict[str, dict[str, float | int]] = {}
    spots: dict[str, str] = {}
    for inst in board["instances"]:
        rd = str(inst["refdes"])
        poses[rd] = {
            "x": float(inst["x"]),
            "y": float(inst["y"]),
            "rot": float(inst["rot"] or 0.0),
            "layer": inst["layer"],
        }
        spot = inst.get("label_spot")
        if spot:
            spots[rd] = str(spot)
    (OUT / "poses.json").write_text(json.dumps(poses, indent=1), encoding="utf-8")
    (OUT / "label_spots.json").write_text(json.dumps(spots, indent=1), encoding="utf-8")
    print(f"poses: {len(poses)} parts, label spots: {len(spots)}")

    routes = store.pcb_routes_get(ref_id)
    (OUT / "routes.json").write_text(json.dumps(routes, indent=1), encoding="utf-8")
    n_realized = sum(1 for r in routes.values() if r["status"] == "realized")
    print(f"routes: {len(routes)} nets, {n_realized} realized")

    with store.pool.connection() as conn:
        brow = conn.execute(
            "SELECT board_id FROM pcb_boards WHERE ref_id = %s AND name = 'main' "
            "AND retired_at IS NULL",
            (ref_id,),
        ).fetchone()
        if brow is None:
            print("no board row")
            return 1
        board_id = int(brow[0])
        rows = conn.execute(
            "SELECT c.ctype, c.layer, n.name, c.route_id IS NOT NULL, c.geom "
            "FROM pcb_copper c JOIN pcb_nets n ON n.net_id = c.net_id "
            "WHERE c.board_id = %s AND n.retired_at IS NULL ORDER BY c.copper_id",
            (board_id,),
        ).fetchall()
    copper = [
        {
            "ctype": ctype,
            "layer": layer,
            "net": name,
            "has_route": bool(has_route),
            "geom": geom,
        }
        for ctype, layer, name, has_route, geom in rows
    ]
    (OUT / "copper.json").write_text(json.dumps(copper, indent=1), encoding="utf-8")
    n_via = sum(1 for c in copper if c["ctype"] == "via")
    print(f"copper: {len(copper)} rows ({n_via} vias) on board_id={board_id}")
    print(f"export -> {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
