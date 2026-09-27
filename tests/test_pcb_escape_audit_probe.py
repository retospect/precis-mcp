"""DIAGNOSTIC PROBE (throwaway) — what does 35/54 actually mean?

Two questions ``escapes_realized`` cannot answer on its own:

1. **Is the copper really there?** ``pcb_routes.status`` writes
   ``'realized'`` with an explanatory ``note`` for a dangling (<2 member)
   net too, so a bare status count can include nets with nothing routed.
   And a net can hold copper in TWO islands — electrode stub and driver
   stub, never joined — which is ``realized`` by status and open by
   ohmmeter. Measured here with :func:`precis.pcb.connectivity.net_islands`
   over the same (fixed + routed) copper the fab would see.
2. **Did the pin swap settle into an ORDERED assignment?** The driver's
   64 channels are one pin-swap group, so the router is free to choose
   which channel drives which electrode. If it chose well, the
   electrode -> channel airwires should barely cross each other; a high
   crossing count means the assignment is arbitrary and every escape is
   fighting its neighbours for the same B.Cu.

Always passes; it is a measurement, not a contract. Results land in
``.pcb-escape-audit.jsonl`` at the worktree root (NOT /tmp — scripts/test
runs in a container and only the worktree is mounted).
"""

from __future__ import annotations

import collections
import itertools
import json
import pathlib
from typing import Any

import pytest

from precis.pcb import connectivity as pcb_connectivity
from precis.pcb.ir import pin_point
from tests.test_pcb_ewod_dogfood import (
    _drain_one_job,
    _seed,
    pcb,  # noqa: F401  (fixture)
)

OUT = pathlib.Path(__file__).resolve().parents[1] / ".pcb-escape-audit.jsonl"


def _segments_cross(
    a0: tuple[float, float],
    a1: tuple[float, float],
    b0: tuple[float, float],
    b1: tuple[float, float],
) -> bool:
    """Proper crossing only — shared endpoints (two escapes leaving the
    same plaza, or two channels on one driver edge) are not crossings, and
    counting them would drown the signal we want."""

    def orient(
        p: tuple[float, float], q: tuple[float, float], r: tuple[float, float]
    ) -> float:
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])

    if a0 in (b0, b1) or a1 in (b0, b1):
        return False
    d1, d2 = orient(a0, a1, b0), orient(a0, a1, b1)
    d3, d4 = orient(b0, b1, a0), orient(b0, b1, a1)
    return (d1 > 0) != (d2 > 0) and (d3 > 0) != (d4 > 0)


@pytest.mark.slow
def test_probe_escape_audit(pcb, store) -> None:  # noqa: F811
    slug = _seed(pcb)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    pcb.put(id=slug, args={"op": "route", "seed": 1})
    _drain_one_job(store, ref.id)

    rows = store.pcb_route_status(ref.id)
    escapes = {
        str(r["name"]): (str(r["status"]), str(r.get("note") or ""))
        for r in rows
        if str(r["name"]).startswith("ARR1_R")
    }
    realized = {n for n, (s, _) in escapes.items() if s == "realized"}
    realized_with_note = {
        n: note for n, (s, note) in escapes.items() if s == "realized" and note
    }

    design = store.pcb_load(ref.id)
    layer_names = [str(layer["name"]) for layer in design["board"]["stackup"]]
    board_id = int(design["board"]["board_id"])
    copper = store.pcb_copper_list(board_id)

    # The same model shape DRC builds: every copper row the fab would get
    # (fixed fabric AND router output), plus the full pad set.
    model: dict[str, Any] = {
        "layers": layer_names,
        "copper": [dict(c) for c in copper],
        "pads": pcb._drc_pads(ref.id, layer_names),
    }
    islands = {
        str(isl.net): int(isl.components)
        for isl in pcb_connectivity.net_islands(model)
        if str(isl.net).startswith("ARR1_R")
    }

    # Pin-swap outcome: is the settled electrode -> channel assignment
    # geometrically ordered, or arbitrary?
    graph = store.pcb_graph(ref.id)
    ir = pcb._build_ir(ref.id, graph)
    ends: dict[str, dict[str, tuple[float, float]]] = collections.defaultdict(dict)
    for pid in range(ir.n_pins):
        net_id = int(ir.pin_net[pid])
        if net_id < 0:
            continue
        net = str(ir.net_name[net_id])
        if not net.startswith("ARR1_R"):
            continue
        point = pin_point(ir, pid)
        if point is None:
            continue
        refdes = str(ir.instance_refdes[int(ir.pin_instance[pid])])
        side = "driver" if "SINK" in refdes else "electrode"
        ends[net][side] = (round(float(point[0]), 4), round(float(point[1]), 4))

    airwires = {
        net: (e["electrode"], e["driver"])
        for net, e in ends.items()
        if "electrode" in e and "driver" in e
    }
    crossings = sum(
        1
        for (n1, (a0, a1)), (n2, (b0, b1)) in itertools.combinations(
            sorted(airwires.items()), 2
        )
        if _segments_cross(a0, a1, b0, b1)
    )

    row = {
        "escapes_total": len(escapes),
        "escapes_realized": len(realized),
        "realized_but_noted": realized_with_note,
        "realized_yet_split": {n: c for n, c in islands.items() if n in realized},
        "unrealized_with_copper_islands": {
            n: c for n, c in islands.items() if n not in realized
        },
        "swaps_settled": len(store.pcb_pin_swaps_list(ref.id)),
        "airwire_pairs": len(airwires),
        "airwire_crossings": crossings,
        "routed_track": sum(
            1 for c in copper if not c.get("fixed") and c.get("ctype") == "track"
        ),
        "routed_via": sum(
            1 for c in copper if not c.get("fixed") and c.get("ctype") == "via"
        ),
        "fixed_track": sum(
            1 for c in copper if c.get("fixed") and c.get("ctype") == "track"
        ),
        "fixed_via": sum(
            1 for c in copper if c.get("fixed") and c.get("ctype") == "via"
        ),
    }
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
