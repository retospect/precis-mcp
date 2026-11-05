"""One clearance oracle for the topo route path (topo-route-review D-a).

A body is a core geometry swept by a radius, with a net and its own
clearance: a pad (its polygon, ``r = 0``), a placed via (its centre,
``r = dia/2``), another net's wire (its polyline, ``r = w/2``, its rule
clearance), a keepout zone or the board edge (``r = 0``, no net). A query
— a via barrel (points, ``r = dia/2``) or a stub (a segment, ``r = w/2``)
with its net's clearance — is blocked when a body of another net, or of
no net, comes closer than ``r_q + r_b + max(c_q, c_b)``. That is the
DRC's swept-primitive model, with ``FAB_QUANTUM_MM`` extra separation
reserved for export rounding (:class:`precis.pcb.drc._Prim`: circles,
capsules, polygons) with its pairwise rule (the larger clearance wins).

Why an index: ``_site_via`` judged every candidate point against every
pad on both outer layers in Python (``_pad_clear`` → ``_world_to_local``,
80 M calls; 311 of the 393 s two taz-leaf routes spent, 475 s on the
doubled leaf). An STRtree over the cores answers a whole candidate batch
in one GEOS call: a ``dwithin`` broad phase at the largest reach, then
exact distances for the survivors. Vias placed while siting runs live
beside the tree as a small array.

Slice 1 feeds the TopoDesign's pin rects in world space — the shapes the
siting walk has always used. Building the pads from the DRC's own source
(``realize.pads_for_ir``) and moving the guards, kink and shortcut checks
onto this index are D-a's next steps.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import shapely
from shapely import STRtree
from shapely.geometry.base import BaseGeometry

from precis.pcb.clearance import FAB_QUANTUM_MM

#: Net of a body that blocks every query (pads, zones, the board edge,
#: placed vias).
ANY_NET = -1
#: Query net that owns no body: everything blocks it.
NO_OWN_NET = -2


@dataclass(frozen=True, slots=True)
class Body:
    """One obstacle: ``geom`` swept by ``r``; ``net`` (:data:`ANY_NET`
    blocks every query) and the body's own ``clearance``."""

    geom: BaseGeometry
    r: float = 0.0
    net: int = ANY_NET
    clearance: float = 0.0


class CopperIndex:
    """The bodies of one routed pose, queried in batches."""

    def __init__(self, bodies: Sequence[Body]) -> None:
        self._geoms = np.asarray([b.geom for b in bodies], dtype=object)
        self._r = np.asarray([b.r for b in bodies], dtype=np.float64)
        self._net = np.asarray([b.net for b in bodies], dtype=np.int64)
        self._c = np.asarray([b.clearance for b in bodies], dtype=np.float64)
        self._tree = STRtree(self._geoms) if len(bodies) else None
        self._reach = float(np.max(self._r + self._c)) if len(bodies) else 0.0
        # bumped by add_disc: callers caching verdicts keyed on the
        # index's identity must key on its state instead
        self._version = 0
        self._disc_xy = np.zeros((0, 2), dtype=np.float64)
        self._disc_r = np.zeros(0, dtype=np.float64)
        self._disc_net = np.zeros(0, dtype=np.int64)
        self._disc_c = np.zeros(0, dtype=np.float64)

    def add_disc(
        self,
        x: float,
        y: float,
        r: float,
        *,
        net: int = ANY_NET,
        clearance: float = 0.0,
    ) -> None:
        """A disc body known only once siting runs — a via just placed."""
        self._version += 1
        self._disc_xy = np.vstack([self._disc_xy, [[float(x), float(y)]]])
        self._disc_r = np.append(self._disc_r, float(r))
        self._disc_net = np.append(self._disc_net, int(net))
        self._disc_c = np.append(self._disc_c, float(clearance))

    def _tree_hits(
        self,
        q: np.ndarray,
        r: float,
        clearance: float,
        net: int,
        own_clearance: float | None = None,
    ) -> np.ndarray:
        out = np.zeros(len(q), dtype=bool)
        if self._tree is None or not len(q):
            return out
        qi, ti = self._tree.query(
            q,
            predicate="dwithin",
            distance=r + clearance + self._reach + FAB_QUANTUM_MM,
        )
        if not len(qi):
            return out
        if own_clearance is None:
            live = (self._net[ti] == ANY_NET) | (self._net[ti] != net)
            qi, ti = qi[live], ti[live]
        lim = r + self._r[ti] + np.maximum(clearance, self._c[ti])
        if own_clearance is not None:
            own = self._net[ti] == net
            lim[own] = (
                r + self._r[ti][own] + np.maximum(own_clearance, self._c[ti][own])
            )
        lim += FAB_QUANTUM_MM
        d = shapely.distance(q[qi], self._geoms[ti])
        out[qi[d < lim]] = True
        return out

    def _live_discs(self, net: int) -> np.ndarray:
        return (self._disc_net == ANY_NET) | (self._disc_net != net)

    def blocked_points(
        self,
        xy: np.ndarray,
        r: float,
        clearance: float,
        *,
        net: int = NO_OWN_NET,
        own_clearance: float | None = None,
    ) -> np.ndarray:
        """Per point of ``xy`` ``(N, 2)``, swept by ``r``: whether a body
        of another net comes closer than ``r + r_b + max(clearance,
        c_b)``. ``net`` is the query's own net — its own wires never
        block it, unless ``own_clearance`` is set: then a same-net body
        blocks within ``r + r_b + max(own_clearance, c_b)`` — the
        planter's own-pad copper-overlap bar (G2g)."""
        xy = np.asarray(xy, dtype=np.float64).reshape(-1, 2)
        out = self._tree_hits(shapely.points(xy), r, clearance, net, own_clearance)
        live = self._live_discs(net)
        if len(xy) and live.any():
            dxy = self._disc_xy[live]
            d = np.hypot(
                xy[:, None, 0] - dxy[None, :, 0], xy[:, None, 1] - dxy[None, :, 1]
            )
            lim = r + self._disc_r[live] + np.maximum(clearance, self._disc_c[live])
            lim += FAB_QUANTUM_MM
            out |= (d < lim[None, :]).any(axis=1)
        return out

    def blocked(
        self,
        geom: BaseGeometry,
        r: float,
        clearance: float,
        *,
        net: int = NO_OWN_NET,
    ) -> bool:
        """:meth:`blocked_points` for one geometry — a stub or a leg."""
        q = np.asarray([geom], dtype=object)
        if self._tree_hits(q, r, clearance, net)[0]:
            return True
        live = self._live_discs(net)
        if not live.any():
            return False
        d = shapely.distance(geom, shapely.points(self._disc_xy[live]))
        lim = r + self._disc_r[live] + np.maximum(clearance, self._disc_c[live])
        lim += FAB_QUANTUM_MM
        return bool((d < lim).any())


__all__ = ["ANY_NET", "NO_OWN_NET", "Body", "CopperIndex"]
