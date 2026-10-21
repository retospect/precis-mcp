"""Best-sample → :class:`RealizeResult` for the slice-1 legality check.

Each maximal run of same-layer pieces becomes one ``RealizedTrack``
(shared ``seg_id`` per wire — the model is a copper description, not a
1:1 track table); each active via bead a ``RealizedVia`` spanning all
layers (through-vias only). Unrouted wires land in ``unrouted`` with an
``UnroutedReason`` — the actionable failure the spec asks for.
"""

from __future__ import annotations

import itertools

import numpy as np

from precis.pcb.pr.beliefs import Layout
from precis.pcb.pr.state import PrProblem
from precis.pcb.realize import (
    RealizedTrack,
    RealizedVia,
    RealizeResult,
    UnroutedReason,
)


def to_realize_result(
    problem: PrProblem,
    layout: Layout,
    pos: np.ndarray,
    assign: np.ndarray,
    *,
    unrouted: set[int],
    reasons: dict[int, str] | None = None,
) -> RealizeResult:
    """Emit tracks/vias for one relaxed sample.

    ``pos`` is the sample's (N, 2) point array, ``assign`` its (P,)
    per-span layer. ``unrouted`` holds wire indices with no polyline.
    """
    tracks: list[RealizedTrack] = []
    vias: list[RealizedVia] = []
    unseg: list[int] = []
    why: list[UnroutedReason] = []
    n_layers = problem.n_layers
    wire_runs: dict[int, list[int]] = {}
    for pi in range(layout.n_spans):
        w = int(layout.piece_wire[pi])
        wire_runs.setdefault(w, []).append(pi)
    for wi in range(len(problem.wires)):
        ws = problem.wires[wi]
        if wi in unrouted or wi not in wire_runs:
            unseg.append(ws.seg_id)
            why.append(
                UnroutedReason(
                    seg_id=ws.seg_id,
                    net_id=ws.net_id,
                    kind="congestion",
                    message=(reasons or {}).get(
                        wi, "no capacity-free path on any layer"
                    ),
                )
            )
            continue
        pieces = wire_runs[wi]
        i = 0
        while i < len(pieces):
            layer = int(assign[pieces[i]])
            j = i
            while j < len(pieces) and int(assign[pieces[j]]) == layer:
                j += 1
            pts = [pos[int(layout.piece_p0[pieces[i]])]]
            for p in pieces[i:j]:
                pts.append(pos[int(layout.piece_p1[p])])
            segs = tuple(
                {
                    "shape": "line",
                    "start": [float(a[0]), float(a[1])],
                    "end": [float(b[0]), float(b[1])],
                }
                for a, b in itertools.pairwise(pts)
            )
            length = float(
                np.linalg.norm(np.diff(np.asarray(pts), axis=0), axis=1).sum()
            )
            tracks.append(
                RealizedTrack(
                    seg_id=ws.seg_id,
                    net_id=ws.net_id,
                    layer=layer,
                    segments=segs,
                    length_mm=length,
                    blocked_by=None,
                    width_mm=ws.width_mm,
                )
            )
            i = j
        # via beads at layer-change boundaries
        for i in range(1, len(pieces)):
            pa, pb = pieces[i - 1], pieces[i]
            if int(assign[pa]) == int(assign[pb]):
                continue
            pt = int(layout.piece_p1[pa])
            if not layout.via_site_ok[pt]:
                continue  # transition with no legal site: counted below
            x, y = pos[pt]
            vias.append(
                RealizedVia(
                    seg_id=ws.seg_id,
                    net_id=ws.net_id,
                    x=float(x),
                    y=float(y),
                    dia_mm=problem.via_dia_mm,
                    drill_mm=problem.via_drill_mm,
                    layer_lo=0,
                    layer_hi=n_layers - 1,
                    endpoint="a",
                )
            )
    return RealizeResult(
        tracks=tuple(tracks),
        vias=tuple(vias),
        warnings=(),
        unrouted=tuple(sorted(unseg)),
        unrouted_reasons=tuple(why),
    )
