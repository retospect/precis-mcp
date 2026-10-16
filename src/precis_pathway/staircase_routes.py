"""Surface-Pourbaix slice 3 — the ``che_affine`` → staircase adapter.

Design of record: ``docs/backlog/surface-pourbaix-staircase-optimizer.md``
(§7 linked panels) and the 2026-10-13 sign-convention ruling: every rung
is affine in the CHE potential, ``delta_g(U) = delta_g0 + n_e * U_RHE``
with ``n_e`` proton-electron pairs **consumed** (positive for a
reduction), so a reduction rung gets easier at more negative U and
``U_L = -max(delta_g0 / n_e)`` over the route's electrochemical steps.
Every rung is pH-flat on the RHE scale.

The input is a stored pathway's ``meta.results`` — the engine's
``results.json`` (autocatpath ≥ 0.25.0, ``pipeline._che_affine`` /
``_her_report``):

* ``che_affine = {scale: "RHE", T, pH, per_route: [{route, steps:
  [{edge, delta_g0, n_e}], U_L, U_opt, span_at_UL}]}`` — ``route`` is the
  scorecard's ``"a -> b -> c"`` string, so each step's reactant/product
  names are the adjacent nodes;
* optional ``her = {U_L_her, product, leaf, dG_H_star, edges: [{step,
  edge, reactant, product, delta_g0, n_e}]}`` — the HER competitor branch
  (``network.her``), kept out of the headline ``U_L``;
* ``nodes[name].std`` carries the per-state pooled spread — a rung's
  ``sigma`` is ``hypot(std_reactant, std_product)``, and only ever set
  when a spread was actually recorded (ruling: no invented sigmas);
* ``edges[name]`` carries the per-edge kinetics the slice-7 reset ladder
  reads (``reset_contour``): ``barrier`` — a measured-NEB ``Estimate``
  (``mean`` is the rung's ``barrier_ea``) — and ``bep =
  {alpha, beta[, n_fit, rmse]}`` — the catpath BEP export fitted on the
  run's sparse NEBs (the rung's ``bep``). Missing or malformed blocks
  attach nothing; an edge with neither stays on the overdrive proxy.

:class:`StaircaseRoutes` is what B3's frontier objectives and the
``view='staircase'`` / ``/pourbaix/<handle>`` surfaces consume: typed
:class:`~precis_pathway.staircase_masks.Route`/:class:`Rung` objects plus
the footing and an honest ``missing`` list — an older engine's results
simply lack ``che_affine``, and the adapter says so rather than
fabricating rungs. Pure: numpy + dataclasses, no engine import, no DB —
``routes_for_pathway`` is the thin store read on top.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from . import reset_contour
from .staircase_masks import PREFAC_V, Route, Rung, pcet_route, u_limiting, u_she

if TYPE_CHECKING:
    from precis.store.store import Store

    from .staircase_states import PointStates

#: Name the HER competitor route gets (``kind="her"`` already labels each
#: rung; the name is what masks/renders print).
HER_ROUTE_NAME = "HER"


@dataclass(frozen=True)
class StaircaseRoutes:
    """The staircase a stored pathway result projects onto condition
    space.

    ``routes`` — one :class:`Route` per ``che_affine.per_route`` entry
    (scorecard order kept). ``her`` — the HER competitor as a Route, or
    ``None``. ``route_stats``/``her_stats`` keep the engine's own
    per-route scalars (``U_L``/``U_opt``/``span_at_UL``, and
    ``U_L_her``/``dG_H_star``) — read-only provenance; the rungs'
    ``delta_g(U)`` reproduces them anyway. ``footing`` — engine version,
    corrections record, ``network_digest``: the one-map-one-footing
    provenance the view must show. ``missing`` names what the results
    lack, so an honest "older engine" beats an invented staircase.

    ``pcet_routes`` — the same routes in the one-rung CHE convention
    (:func:`~precis_pathway.staircase_masks.pcet_route`, gr489164),
    index-aligned with ``routes``: the viability mask, the operating
    window and ``route_stats[...]["U_L"]`` read these; the reset contour
    and the rendered rungs keep the engine's split ``routes``. The
    engine's own split-convention ``U_L`` stays as ``U_L_engine``."""

    routes: list[Route] = field(default_factory=list)
    pcet_routes: list[Route] = field(default_factory=list)
    her: Route | None = None
    footing: dict[str, Any] = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    route_stats: dict[str, dict[str, Any]] = field(default_factory=dict)
    her_stats: dict[str, Any] | None = None
    scale: str | None = None
    T: float | None = None
    pH: float | None = None


def _num(x: Any) -> float | None:
    """A finite float, or ``None`` (a persisted NaN/Inf reads back as
    JSON null)."""
    if isinstance(x, bool) or not isinstance(x, int | float):
        return None
    f = float(x)
    return f if math.isfinite(f) else None


def _n_e(x: Any) -> int | None:
    """``n_e`` as an int — proton-electron pairs are whole, so a
    non-integral export is malformed and the step is dropped + counted
    rather than silently truncated."""
    f = _num(x)
    if f is None or not f.is_integer():
        return None
    return int(f)


def _step_kind(product: str, n_e: float, *, terminal: bool) -> str:
    """Rung kind: ``"~"`` in the product name is catpath's clean-slab
    landing tag (``*~nh3``), so the route-terminal step into it is a
    desorption (Reto 2026-10-16: a mid-route ``*~x`` node is not); the
    rest split on whether they carry proton-electron pairs. Labels only —
    the masks are kind-blind."""
    if terminal and "~" in product:
        return "desorption"
    return "electrochemical" if n_e != 0 else "chemical"


def _sigma(results: dict[str, Any], a: Any, b: Any) -> float | None:
    """``hypot(std_a, std_b)`` from ``results.nodes[…].std`` — the
    propagated per-edge spread. ``None`` when neither endpoint recorded
    one (no invented sigmas)."""
    nodes = results.get("nodes")
    if not isinstance(nodes, dict):
        return None
    var = 0.0
    seen = False
    for n in (a, b):
        est = nodes.get(n)
        std = _num(est.get("std")) if isinstance(est, dict) else None
        if std is not None:
            var += std * std
            seen = True
    return math.sqrt(var) if seen else None


def _edges_by_name(results: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """``results.edges`` keyed by step name — the join key
    ``che_affine`` steps and ``her.edges`` both carry as ``edge``."""
    edges = results.get("edges")
    out: dict[str, dict[str, Any]] = {}
    if isinstance(edges, list):
        for e in edges:
            if isinstance(e, dict) and isinstance(e.get("name"), str):
                out[e["name"]] = e
    return out


def _kinetics(edge_record: dict[str, Any] | None) -> dict[str, Any]:
    """``{bep, barrier_ea}`` kwargs for a :class:`Rung` from one
    ``results.edges`` record — the slice-7 kinetics inputs. Only present
    when recorded and finite; nothing is fabricated for a missing block."""
    if not isinstance(edge_record, dict):
        return {}
    out: dict[str, Any] = {}
    bep = edge_record.get("bep")
    if isinstance(bep, dict):
        alpha, beta = _num(bep.get("alpha")), _num(bep.get("beta"))
        if alpha is not None and beta is not None:
            out["bep"] = (alpha, beta)
    barrier = edge_record.get("barrier")
    if isinstance(barrier, dict):
        mean = _num(barrier.get("mean"))
        if mean is not None:
            out["barrier_ea"] = mean
    return out


def _pcet_with_sigma(results: dict[str, Any], route: Route) -> Route:
    """:func:`pcet_route` with each merged rung's ``sigma`` re-read from
    its true endpoints (``hypot(std_X, std_XH)``) instead of the bound
    the pure merge carries."""
    pcet = pcet_route(route)
    if pcet is route:
        return route
    split = {rg.name for rg in route.rungs}
    return Route(
        name=pcet.name,
        rungs=tuple(
            rg
            if rg.name in split
            else replace(rg, sigma=_sigma(results, *rg.name.split("->", 1)))
            for rg in pcet.rungs
        ),
    )


def routes_from_results(
    results: dict[str, Any] | None, *, engine_version: str | None = None
) -> StaircaseRoutes:
    """``meta.results`` → typed staircase. Never invents values: whatever
    the engine did not record lands in ``missing``, and the footing is
    what the results carry, not what the caller wishes."""
    r = results or {}
    missing: list[str] = []
    footing = {
        "engine_version": engine_version or r.get("engine_version"),
        "corrections": r.get("corrections"),
        "network_digest": r.get("network_digest"),
    }
    if "corrections" not in r:
        missing.append("corrections record absent — footing provenance-incomplete")
    if not r.get("network_digest"):
        missing.append("network_digest absent — footing provenance-incomplete")

    routes: list[Route] = []
    pcet_routes: list[Route] = []
    route_stats: dict[str, dict[str, Any]] = {}
    scale: str | None = None
    T: float | None = None
    ph: float | None = None
    edges_by_name = _edges_by_name(r)
    che = r.get("che_affine")
    if not isinstance(che, dict):
        missing.append("che_affine absent (engine < 0.25.0)")
    else:
        scale = str(che.get("scale")) if che.get("scale") else None
        T = _num(che.get("T"))
        ph = _num(che.get("pH"))
        per_route = che.get("per_route")
        if not per_route:
            missing.append("che_affine.per_route empty — no scored routes")
        dropped = 0
        for i, pr in enumerate(per_route or []):
            if not isinstance(pr, dict):
                continue
            nodes = str(pr.get("route") or "").split(" -> ")
            rungs: list[Rung] = []
            for j, s in enumerate(pr.get("steps") or []):
                if not isinstance(s, dict):
                    continue
                dg0, ne = _num(s.get("delta_g0")), _n_e(s.get("n_e"))
                if dg0 is None or ne is None:
                    dropped += 1
                    continue
                a = nodes[j] if j < len(nodes) else ""
                b = nodes[j + 1] if j + 1 < len(nodes) else ""
                edge_name = str(s.get("edge") or f"{a}->{b}")
                rungs.append(
                    Rung(
                        name=edge_name,
                        delta_g0=dg0,
                        n_e=ne,
                        kind=_step_kind(b, ne, terminal=j + 2 == len(nodes)),
                        sigma=_sigma(r, a, b),
                        **_kinetics(edges_by_name.get(edge_name)),
                    )
                )
            name = str(pr.get("route") or f"route {i + 1}")
            route = Route(name=name, rungs=tuple(rungs))
            pcet = _pcet_with_sigma(r, route)
            routes.append(route)
            pcet_routes.append(pcet)
            route_stats[name] = {
                "U_L": u_limiting(pcet),
                "U_L_engine": _num(pr.get("U_L")),
                "U_opt": _num(pr.get("U_opt")),
                "span_at_UL": _num(pr.get("span_at_UL")),
            }
        if dropped:
            missing.append(f"{dropped} step(s) dropped — delta_g0/n_e missing")

    her_route: Route | None = None
    her_stats: dict[str, Any] | None = None
    her = r.get("her")
    if isinstance(her, dict):
        rungs = []
        for e in her.get("edges") or []:
            if not isinstance(e, dict):
                continue
            dg0, ne = _num(e.get("delta_g0")), _n_e(e.get("n_e"))
            if dg0 is None or ne is None:
                continue
            edge_name = str(e.get("edge") or e.get("step") or "?")
            rungs.append(
                Rung(
                    name=edge_name,
                    delta_g0=dg0,
                    n_e=ne,
                    kind="her",
                    sigma=_sigma(r, e.get("reactant"), e.get("product")),
                    **_kinetics(edges_by_name.get(edge_name)),
                )
            )
        if rungs:
            her_route = Route(name=HER_ROUTE_NAME, rungs=tuple(rungs))
            her_stats = {
                "U_L_her": _num(her.get("U_L_her")),
                "dG_H_star": _num(her.get("dG_H_star")),
                "product": her.get("product"),
                "leaf": her.get("leaf"),
            }
    else:
        side = (r.get("selectivity") or {}).get("side_products") or []
        if any(isinstance(s, dict) and s.get("product") == "H2" for s in side):
            missing.append(
                "her block absent though H2 is a side product "
                "(network.her on; engine recorded no HER edges)"
            )
    return StaircaseRoutes(
        routes=routes,
        pcet_routes=pcet_routes,
        her=her_route,
        footing=footing,
        missing=missing,
        route_stats=route_stats,
        her_stats=her_stats,
        scale=scale,
        T=T,
        pH=ph,
    )


def routes_for_pathway(store: Store, pathway_ref: Any) -> StaircaseRoutes:
    """The stored staircase for a pathway ref (``Ref`` or id/slug).
    Reads ``meta.results`` + ``meta.autocatpath_version``; a missing or
    uncomputed pathway comes back as an empty object with the reason in
    ``missing`` — never raises."""
    ref = (
        store.get_ref(kind="pathway", id=pathway_ref)
        if isinstance(pathway_ref, int | str)
        else pathway_ref
    )
    if ref is None:
        return StaircaseRoutes(missing=[f"no pathway {pathway_ref!r}"])
    meta = ref.meta or {}
    results = meta.get("results")
    if not isinstance(results, dict) or not results:
        return StaircaseRoutes(
            footing={
                "engine_version": meta.get("autocatpath_version"),
                "corrections": None,
                "network_digest": None,
            },
            missing=[
                f"pathway not computed (status {meta.get('status', '?')}) — "
                "no stored results"
            ],
        )
    return routes_from_results(results, engine_version=meta.get("autocatpath_version"))


# ── text render (the view='staircase' / web-panel contract) ─────────────


def _fmt_v(x: float | None) -> str:
    return "n/a" if x is None else f"{x:+.3f} V"


def render_staircase(
    sr: StaircaseRoutes,
    results: dict[str, Any] | None = None,
    *,
    u_rhe: float,
    title: str | None = None,
    ph: float | None = None,
    structures: list[dict[str, Any]] | None = None,
    states: PointStates | None = None,
) -> str:
    """The staircase at one potential, agent-readable text.

    ``structures`` — optional per-structure resting context (the §7.2
    map → staircase link): each ``{handle, resting, assumed, mismatch}``
    row is the map's resting termination at ``u_rhe`` plus the coads
    verdict, computed by the caller (the plugin-side helper or
    :mod:`precis.quest.staircase`).

    ``states`` — the picked point's verdict
    (:func:`~precis_pathway.staircase_states.point_states`, gr489471):
    each rung line gains its state (a split rung shows its PCET
    parent's), and a point block names the worst rung and the window."""
    u = float(u_rhe)
    ph_v = ph if ph is not None else sr.pH
    lines = [f"# reaction staircase — {title or 'pathway'}", ""]
    she = (
        f"  (= {u_she(u, ph_v):+.3f} V vs SHE at pH {ph_v:g}, "
        f"{PREFAC_V:.3f} V/pH — display only, every rung is pH-flat on RHE)"
        if ph_v is not None
        else ""
    )
    lines.append(
        f"U_RHE {u:+.3f} V{she}; ΔG(U) = ΔG0 + n_e·U, n_e = proton-electron "
        "pairs consumed — a reduction rung eases at more negative U."
    )
    f = sr.footing or {}
    corr = f.get("corrections")
    corr_txt = "unrecorded" if "corrections" not in f or corr is None else repr(corr)
    nd = str(f.get("network_digest") or "")
    lines.append(
        f"footing: engine {f.get('engine_version') or '?'} · corrections "
        f"{corr_txt} · network_digest {nd[:12] + '…' if nd else '?'}"
        + (f" · T {sr.T:g} K" if sr.T is not None else "")
        + (f" · scale {sr.scale}" if sr.scale else "")
    )

    for s in structures or []:
        resting = s.get("resting")
        if resting:
            lines.append(
                f"staircase starts from {resting} (resting at {u:+.2f} V "
                f"on {s.get('handle', '?')})"
            )
        elif s.get("handle"):
            lines.append(f"{s['handle']}: no resting map — starting surface unknown")
        if s.get("mismatch"):
            lines.append(s["mismatch"])

    results = results or {}
    dag_ul = _num(results.get("U_L"))
    if dag_ul is not None:
        lines.append(
            f"DAG-wide U_L {_fmt_v(dag_ul)} (every electrochemical step "
            "reachable from root, HER branch excluded)"
        )
    for i, route in enumerate(sr.routes):
        stats = sr.route_stats.get(route.name) or {}
        pcet = sr.pcet_routes[i] if i < len(sr.pcet_routes) else route
        # A U-flat (n_e 0) rung over its eps blocks the route at every U —
        # a bare U_L would read as good, so the header names the blocker
        # instead (gr491369).
        blocker = max(
            (rg for rg in pcet.rungs if rg.n_e == 0 and rg.delta_g0 > rg.eps),
            key=lambda rg: rg.delta_g0 - rg.eps,
            default=None,
        )
        parts: list[str] = []
        if blocker is not None:
            parts.append(
                f"U_L n/a — blocked by {blocker.name} "
                f"(ΔG0 {blocker.delta_g0:+.3f} eV, U-flat)"
            )
        elif stats:
            parts.append(
                f"U_L {_fmt_v(stats.get('U_L'))} (one PCET rung per "
                f"H+ + e-; engine split {_fmt_v(stats.get('U_L_engine'))})"
            )
        if stats:
            parts.append(f"U_opt {_fmt_v(stats.get('U_opt'))}")
            parts.append(
                f"span_at_UL {stats['span_at_UL']:.3f} eV"
                if stats.get("span_at_UL") is not None
                else "span_at_UL n/a"
            )
        lines.append("")
        lines.append(
            f"route {i + 1} of {len(sr.routes)}: {route.name}"
            + ("  — " + ", ".join(parts) if parts else "")
        )
        if not route.rungs:
            lines.append("  (no rungs)")
            continue
        best: tuple[Rung, float] | None = None
        for rung in route.rungs:
            dg = float(rung.delta_g(u))
            if best is None or dg > best[1]:
                best = (rung, dg)
            sig = f" ±{rung.sigma:.3f}" if rung.sigma is not None else ""
            st = states.state_of(i, rung.name) if states is not None else None
            lines.append(
                f"  {rung.name:<28} {rung.kind:<15} n_e {rung.n_e:>2}  "
                f"ΔG0 {rung.delta_g0:+.3f}  ΔG(U) {dg:+.3f} eV{sig}"
                + (f"  [{st.state}]" if st is not None else "")
            )
        assert best is not None
        lines.append(
            f"  limiting rung at U = {u:+.2f} V: {best[0].name} (ΔG {best[1]:+.3f} eV)"
        )
        if pcet is not route:
            top = max(pcet.rungs, key=lambda rg: float(rg.delta_g(u)))
            lines.append(
                f"  limiting PCET rung (H* adsorption + hydrogenation as one "
                f"step): {top.name} (ΔG {float(top.delta_g(u)):+.3f} eV)"
            )
        edges = reset_contour.reset_edges(route)
        worst = min(edges, key=lambda e: float(e.slack(u)))
        slack = float(worst.slack(u))
        est = "/".join(sorted({e.estimator for e in edges}))
        if slack >= 0:
            verdict = (
                f"guaranteed — every edge downhill by its margin "
                f"(limiting {worst.rung.name}, slack {slack:+.3f} eV)"
            )
        elif all(float(rung.delta_g(u)) <= rung.eps for rung in pcet.rungs):
            verdict = (
                f"hysteretic — {worst.rung.name} is {-slack:.3f} eV "
                "short of its reset margin"
            )
        else:
            blocking = max(pcet.rungs, key=lambda rg: float(rg.delta_g(u)) - rg.eps)
            verdict = f"outside the viable window ({blocking.name} uphill)"
        lines.append(
            f"  reset (slice 7): {verdict} "
            f"[estimators: {est}, η {reset_contour.DEFAULT_ETA_EV:g} eV]"
        )
    if sr.her is not None:
        lines.append("")
        her_g = float(sr.her.limiting_g(u))
        stats = sr.her_stats or {}
        lines.append(
            f"HER competitor ({stats.get('product') or 'H2'}): "
            f"U_L_her {_fmt_v(stats.get('U_L_her'))}"
            + (
                f", dG_H* {stats['dG_H_star']:+.3f} eV"
                if stats.get("dG_H_star") is not None
                else ""
            )
            + f"; limiting HER rung at U ΔG {her_g:+.3f} eV"
        )
        for rung in sr.her.rungs:
            dg = float(rung.delta_g(u))
            lines.append(
                f"  {rung.name:<28} her            n_e {rung.n_e:>2}  "
                f"ΔG0 {rung.delta_g0:+.3f}  ΔG(U) {dg:+.3f} eV"
            )
        for i, route in enumerate(sr.routes):
            if not route.rungs:
                continue
            pcet = sr.pcet_routes[i] if i < len(sr.pcet_routes) else route
            margin = her_g - float(pcet.limiting_g(u))
            lines.append(
                f"  route {i + 1} HER margin at U: {margin:+.3f} eV "
                "(limiting_HER − limiting_route; positive = the route "
                "out-competes HER)"
            )
    if states is not None:
        lines.append("")
        lines.append(
            f"point U_RHE {states.u_rhe:+.3f} V, pH {states.ph:g}: window "
            f"{states.window}; resting {states.termination or 'unmapped'}, "
            f"pathway computed on {states.assumed}"
        )
        if states.worst is not None:
            lines.append(
                f"  worst rung (route {states.worst['route_index'] + 1}): "
                + states.worst["line"]
            )
        for note in states.notes:
            lines.append(f"  {note}")
    if sr.missing:
        lines.append("")
        lines.append("missing: " + "; ".join(str(m) for m in sr.missing))
    return "\n".join(lines)


__all__ = [
    "HER_ROUTE_NAME",
    "StaircaseRoutes",
    "render_staircase",
    "routes_for_pathway",
    "routes_from_results",
]
