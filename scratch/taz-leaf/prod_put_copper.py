"""Phase 2 of the copper upload: put the taz-leaf-local-55 design +
committed placement + realized routes/copper to PROD as slug taz-leaf-i
(the web view at melchior.../pcb/taz-leaf-i).

Reads the phase-1 export (scratch/taz-leaf/out/local-55_export/, produced
inside the dev container by export_local55.py) and the design
(out/batch.min.json); writes to prod via the same pattern as
prod_put_latest.py (taz-leaf-h), plus the topo route stage's products:
pcb_routes (canonical sketch per net) and pcb_copper (derived tracks/vias)
— the board's first prod upload WITH copper (11/38 nets realized on
local-55).

Authority: memory me540899 (Reto 2026-10-30, in-pane: taz-leaf prod
design-flow writes are a standing approved write) + Reto's ask.

Run (host-side, mirrors prod-precis credential handling — the DSN never
hits argv or output):

    export PRECIS_DATABASE_URL="$(sed 's#@host.docker.internal:#@127.0.0.1:#' "$HOME/.secrets/pw/PRECIS_DATABASE_URL")"
    uv run --no-sync python scratch/taz-leaf/prod_put_copper.py            # dry run
    PRECIS_PROD_WRITE=1 uv run --no-sync python scratch/taz-leaf/prod_put_copper.py

Refuses to run if a pcb design 'taz-leaf-i' already exists.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from precis.cli._common import resolve_dsn
from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store

HERE = Path(__file__).parent
SLUG = sys.argv[1] if len(sys.argv) > 1 else "taz-leaf-i"
EXPORT = HERE / "out" / (sys.argv[2] if len(sys.argv) > 2 else "local-55_export")
WEB = "https://melchior.tailded4cf.ts.net/pcb/"


def main() -> int:
    write = os.environ.get("PRECIS_PROD_WRITE") == "1"
    poses = json.loads((EXPORT / "poses.json").read_text(encoding="utf-8"))
    spots = json.loads((EXPORT / "label_spots.json").read_text(encoding="utf-8"))
    routes = json.loads((EXPORT / "routes.json").read_text(encoding="utf-8"))
    copper = json.loads((EXPORT / "copper.json").read_text(encoding="utf-8"))
    print(
        f"export: {len(poses)} poses, {len(spots)} label spots, "
        f"{len(routes)} routes, {len(copper)} copper rows"
    )

    store = Store.connect(resolve_dsn(None))
    if store.get_ref(kind="pcb", id=SLUG) is not None:
        print(f"refusing to overwrite: kind='pcb' id='{SLUG}' already exists", file=sys.stderr)
        return 2

    batch = json.loads((HERE / "out" / "batch.min.json").read_text(encoding="utf-8"))
    overridden = 0
    for c in batch["components"]:
        p = poses.get(c["refdes"])
        if p is None:
            print(f"note: no committed pose for {c['refdes']} — keeping authored", file=sys.stderr)
            continue
        c["x"], c["y"], c["rot"], c["layer"] = p["x"], p["y"], p["rot"], p["layer"]
        overridden += 1
    for c in batch["components"]:
        if c["refdes"] in {"SNS1", "SNS2"}:
            c["polarized"] = False
    print(f"committed poses applied to {overridden}/{len(batch['components'])} components")

    if not write:
        print("\nDRY RUN — no prod writes. Set PRECIS_PROD_WRITE=1 to apply.")
        return 0

    pcb = PcbHandler(hub=Hub(store=store))
    put = pcb.put(id=SLUG, args=batch)
    print(put.body)
    moves = [
        {"refdes": rd, "x": p["x"], "y": p["y"], "rot": p["rot"] or 0.0}
        for rd, p in poses.items()
    ]
    mv = pcb.put(id=SLUG, args={"op": "move", "moves": moves})
    print(mv.body)
    for rd, spot in sorted(spots.items()):
        r = pcb.put(id=SLUG, args={"op": "move", "refdes": rd, "label_spot": spot})
        first = (r.body or "").splitlines()
        print(f"pin {rd}={spot}: {first[0] if first else r.body}")

    ref = store.get_ref(kind="pcb", id=SLUG)
    assert ref is not None
    ref_id = int(ref.id)
    board_id = store.pcb_ensure_board(ref_id)
    n_routes = store.pcb_routes_write(ref_id, board_id, routes)
    print(f"pcb_routes written: {n_routes}")

    net_ids: dict[str, int] = {}
    with store.pool.connection() as conn:
        for name in {c["net"] for c in copper}:
            row = conn.execute(
                "SELECT net_id FROM pcb_nets WHERE ref_id = %s AND name = %s "
                "AND retired_at IS NULL",
                (ref_id, name),
            ).fetchone()
            if row is None:
                print(f"note: no prod net {name!r} — copper rows skipped", file=sys.stderr)
                continue
            net_ids[name] = int(row[0])
        route_ids: dict[str, int] = {}
        for name in {c["net"] for c in copper if c["has_route"]}:
            row = conn.execute(
                "SELECT route_id FROM pcb_routes WHERE board_id = %s AND net_id = %s",
                (board_id, net_ids[name]),
            ).fetchone()
            if row is not None:
                route_ids[name] = int(row[0])
    rows = [
        {
            "ctype": c["ctype"],
            "layer": c["layer"],
            "net_id": net_ids[c["net"]],
            "route_id": route_ids.get(c["net"]),
            "geom": c["geom"],
        }
        for c in copper
        if c["net"] in net_ids
    ]
    n_copper = store.pcb_copper_replace(board_id, rows)
    print(f"pcb_copper written: {n_copper} rows")
    print(f"view: {WEB}{SLUG}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
