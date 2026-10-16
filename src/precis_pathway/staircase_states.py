"""Surface-Pourbaix slice 4 render — one rung-state verdict at a picked
``(U, pH)`` point, shared by the web picker and ``view='staircase'``.

Design of record: ``docs/backlog/spso-point-picker.md`` (gr489471, rulings
2026-10-15). :func:`point_states` is pure — no store, no engine — so the
``/pourbaix/<handle>/states`` JSON, the server-rendered first paint and
the MCP text all read the same object and can never disagree.

Per PCET rung (``sr.pcet_routes``, the mask's own convention — ruling 1),
the first state that applies wins:

1. ``off_map`` — the map's resting termination at U is not the surface
   the pathway was computed on (``staircase.mismatch``). Every rung of
   every route shares it: the whole staircase starts from the wrong
   surface. A U the map covers with no region (a gap, or outside the
   swept window) is ``unmapped`` and judges nothing — the same rule
   ``mismatch`` applies.
2. ``thermally_blocked`` — an ``n_e = 0`` rung with ``ΔG0 > ε``.
3. ``electro_uphill`` — an ``n_e ≠ 0`` rung with ``ΔG(U) > ε``; it opens
   at ``rung.u_star``.
4. ``viable``.

HER is not a rung state (ruling 2): it only moves ``in_window`` and the
window label ``outside (HER)``. Split rungs inherit their PCET parent's
state through ``split_children``. pH is taken and echoed but moves no
state — every rung and every region is pH-flat on RHE.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, field
from typing import Any

import numpy as np

from precis.quest.staircase import (
    assumed_label,
    coads_of,
    mismatch,
    resting_region,
)

from .staircase_masks import RestingMap, Route, Rung, operating_window
from .staircase_routes import StaircaseRoutes

#: Rung states in precedence order — the first is the worst.
STATES = ("off_map", "thermally_blocked", "electro_uphill", "viable")

_RANK = {s: i for i, s in enumerate(STATES)}


@dataclass(frozen=True)
class RungState:
    """One PCET rung judged at the point. ``split_children`` names the
    engine-split rungs this one merges (itself when nothing merged) — the
    staircase rows that inherit this state."""

    name: str
    kind: str
    n_e: int
    delta_g: float
    eps: float
    state: str
    reason: str
    opens_at_U: float | None
    split_children: tuple[str, ...]


@dataclass(frozen=True)
class RouteStates:
    route: str
    rungs: tuple[RungState, ...]


@dataclass(frozen=True)
class PointStates:
    """The verdict at one ``(U_RHE, pH)`` point. ``worst`` is one line —
    the highest-precedence state, ties broken by the largest ``ΔG − ε``.
    ``window`` is ``inside``, ``outside`` or ``outside (HER)``;
    ``termination`` the map's label at U (``None`` without a map)."""

    u_rhe: float
    ph: float
    routes: tuple[RouteStates, ...]
    worst: dict[str, Any] | None
    in_window: bool
    window: str
    termination: str | None
    mapped: bool
    assumed: str
    off_map_reason: str | None
    her_margin: float | None = None
    notes: tuple[str, ...] = field(default_factory=tuple)

    def as_dict(self) -> dict[str, Any]:
        """JSON-ready — the ``/states`` payload: tuples become lists and a
        non-finite float (a persisted JSON null read back as NaN) becomes
        ``None``, since strict JSON has no NaN."""

        def clean(x: Any) -> Any:
            if isinstance(x, float) and not math.isfinite(x):
                return None
            if isinstance(x, dict):
                return {k: clean(v) for k, v in x.items()}
            if isinstance(x, list | tuple):
                return [clean(v) for v in x]
            return x

        out: dict[str, Any] = clean(asdict(self))
        return out

    def state_of(self, route_index: int, split_name: str) -> RungState | None:
        """The state a split (rendered) rung inherits from its PCET
        parent, or ``None`` when the name is not on that route."""
        if route_index >= len(self.routes):
            return None
        for rs in self.routes[route_index].rungs:
            if split_name in rs.split_children:
                return rs
        return None


def pathway_coads(
    store: Any, pathway_meta: dict[str, Any], fallback_meta: dict[str, Any] | None
) -> dict[str, int]:
    """The surface a pathway was computed on (ruling 3): a ``coads`` dict
    on its own slab spec (``config.slab.coads``) when one is recorded,
    else the structure it ran on (``candidate_ref``, its ``coads_of``),
    else ``fallback_meta`` (the structure being viewed), else ``{}`` =
    clean. No stored pathway carries slab coads today, so the
    ``candidate_ref`` read is the live path."""
    slab = ((pathway_meta or {}).get("config") or {}).get("slab")
    raw = slab.get("coads") if isinstance(slab, dict) else None
    if isinstance(raw, dict):
        return {str(k): int(v) for k, v in raw.items() if isinstance(v, int | float)}
    cand = (pathway_meta or {}).get("candidate_ref")
    if isinstance(cand, int) and not isinstance(cand, bool):
        ref = store.get_ref(kind="structure", id=cand)
        if ref is not None:
            return coads_of(ref.meta or {})
    return coads_of(fallback_meta or {})


def _split_children(split: Route, pcet: Route) -> list[tuple[str, ...]]:
    """For each PCET rung, the split rungs it came from. ``pcet_route``
    keeps order and merges only adjacent pairs, so a walk suffices."""
    out: list[tuple[str, ...]] = []
    rungs = split.rungs
    i = 0
    for pr in pcet.rungs:
        if i < len(rungs) and rungs[i].name == pr.name:
            out.append((rungs[i].name,))
            i += 1
        elif i + 1 < len(rungs):
            out.append((rungs[i].name, rungs[i + 1].name))
            i += 2
        else:
            out.append((pr.name,))
    return out


def _judge(
    rung: Rung, u: float, children: tuple[str, ...], off_map: str | None
) -> RungState:
    dg = float(rung.delta_g(u))
    eps = rung.eps
    opens = rung.u_star
    if off_map is not None:
        state, reason = "off_map", off_map
    elif rung.n_e == 0 and dg > eps:
        state, reason = "thermally_blocked", f"{dg:.2f} eV > {eps:.2f} eV"
    elif rung.n_e != 0 and dg > eps:
        side = "below" if rung.n_e > 0 else "above"
        state = "electro_uphill"
        reason = (
            f"ΔG {dg:+.2f} eV > {eps:.2f} eV; opens {side} {opens:+.2f} V"
            if opens is not None
            else f"ΔG {dg:+.2f} eV > {eps:.2f} eV"
        )
    else:
        state, reason = "viable", f"ΔG {dg:+.2f} eV ≤ {eps:.2f} eV"
    return RungState(
        name=rung.name,
        kind=rung.kind,
        n_e=rung.n_e,
        delta_g=dg,
        eps=eps,
        state=state,
        reason=reason,
        opens_at_U=opens,
        split_children=children,
    )


def point_states(
    sr: StaircaseRoutes,
    map_entry: dict[str, Any] | None,
    *,
    u_rhe: float,
    ph: float,
    coads: dict[str, int],
) -> PointStates:
    """Judge every PCET rung of ``sr`` at ``(u_rhe, ph)``.

    ``map_entry`` is one surface-Pourbaix map (``pourbaix_map.build_map``
    shape: ``regions[*].U_RHE_range/label/termination``) or ``None``;
    ``coads`` the surface the pathway was computed on (the caller resolves
    ruling 3: the pathway's slab spec, else the structure's ``coads_of``,
    else ``{}`` = clean)."""
    u = float(u_rhe)
    p = float(ph)
    notes: list[str] = []
    region = resting_region(map_entry, u) if map_entry else None
    label = None if region is None else str(region.get("label") or "?")
    mapped = region is not None
    assumed = assumed_label(coads)
    off_map: str | None = None
    if map_entry is None:
        notes.append("no surface-Pourbaix map — starting surface not checked")
    elif region is None:
        notes.append(f"the map has no region at {u:+.2f} V — starting surface unknown")
    elif mismatch(region, coads, u=u, label=label) is not None:
        off_map = f"map: {label} here; pathway computed on {assumed}"

    routes: list[RouteStates] = []
    candidates: list[tuple[int, float, int, RungState]] = []
    for i, split in enumerate(sr.routes):
        pcet = sr.pcet_routes[i] if i < len(sr.pcet_routes) else split
        kids = _split_children(split, pcet)
        judged = tuple(
            _judge(rg, u, kids[k], off_map) for k, rg in enumerate(pcet.rungs)
        )
        routes.append(RouteStates(route=split.name, rungs=judged))
        for rs in judged:
            candidates.append((_RANK[rs.state], -(rs.delta_g - rs.eps), i, rs))

    worst: dict[str, Any] | None = None
    if candidates:
        _, _, idx, top = min(candidates, key=lambda c: (c[0], c[1]))
        worst = {
            "route": routes[idx].route,
            "route_index": idx,
            "rung": top.name,
            "state": top.state,
            "reason": top.reason,
            "line": f"{top.state}: {top.name} — {top.reason}",
        }

    # Window membership, by the slice-4 function itself on a 1×1 grid:
    # the resting cell is wanted unless the point is off-map.
    resting = RestingMap(
        np.array([u]), np.array([p]), np.array([[off_map is None]], dtype=bool)
    )
    in_window = False
    window = "outside"
    her_margin: float | None = None
    head = (
        sr.pcet_routes[0] if sr.pcet_routes else (sr.routes[0] if sr.routes else None)
    )
    if head is not None and head.rungs:
        win = operating_window(resting, head, her=sr.her)
        in_window = bool(win.mask[0, 0])
        if sr.her is not None and sr.her.rungs:
            her_margin = float(sr.her.limiting_g(u)) - float(head.limiting_g(u))
            if not in_window and her_margin < 0:
                rungs_ok = all(rs.state == "viable" for rs in routes[0].rungs)
                if rungs_ok:
                    window = "outside (HER)"
        if in_window:
            window = "inside"
    return PointStates(
        u_rhe=u,
        ph=p,
        routes=tuple(routes),
        worst=worst,
        in_window=in_window,
        window=window,
        termination=label,
        mapped=mapped,
        assumed=assumed,
        off_map_reason=off_map,
        her_margin=her_margin,
        notes=tuple(notes),
    )


__all__ = [
    "STATES",
    "PointStates",
    "RouteStates",
    "RungState",
    "pathway_coads",
    "point_states",
]
