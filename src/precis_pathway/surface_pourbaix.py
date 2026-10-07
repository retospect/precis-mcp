"""Surface-Pourbaix slice 1 — the inner CHE sweep over coverage anchors, with
its two error bands kept apart.

Design of record: ``docs/backlog/surface-pourbaix-staircase-optimizer.md``
(the index of Reto's transfer prompt). The outer loop pays DFT/MLIP once per
slab for *anchors* — the clean surface plus each adsorbate at a few
coverages. Everything here is the inner loop: a potential sweep that costs
nothing once the anchors exist, because under the computational hydrogen
electrode (CHE) every anchor's surface free energy is **affine in U**.

**Anchors.** catpath's ``autocatpath coverage`` scan (``coverage.py``) gives,
per adsorbate ``A`` and integer count ``n`` on the supercell::

    gamma_A(n; 0) = [E(slab + n A) + n c_A - E(slab) - n mu_A] / area

at the stated conditions and U = 0 V vs RHE, with ``mu_A`` priced from the
elemental reservoirs the engine already uses (``mu_H = 1/2 G(H2)``,
``mu_O = G(H2O) - 2 mu_H``, ``mu_C = G(CH4) - 4 mu_H``, ``mu_N = 1/2 G(N2)``).

**CHE.** Each H the reservoir supplies is a proton-electron pair, so on the
RHE scale ``mu_H(U) = mu_H(0) - eU`` and the reservoirs it prices follow:
``mu_O(U) = mu_O(0) + 2eU``, ``mu_C(U) = mu_C(0) + 4eU``, ``mu_N`` flat.
An adsorbate therefore releases ``nu_A = 2 n_O + 4 n_C - n_H`` electrons
when formed from the reservoirs, and::

    gamma_A(n; U) = gamma_A(n; 0) - n * nu_A * U / area

pH enters only through the scale: ``U_RHE = U_SHE + PREFAC * pH``, so a map
drawn against ``U_RHE`` is flat in pH and the SHE view tilts every line by
0.059 V per unit (:func:`u_she`). The resting termination at U is the lower
envelope over every anchor with the clean surface at ``gamma = 0``; the
places where the winner changes are the **boundaries**, each the crossing
of two affine lines, so they are closed-form (:func:`boundaries`).

**Two bands, never blended** (transfer prompt §5):

* *propagated thermodynamic* (uncertainty source 2) — each anchor's own
  error bar pushed linearly through the CHE arithmetic into the boundary
  position: ``sigma_U* = sqrt(sigma_i^2 + sigma_j^2) / |s_i - s_j|`` for the
  two lines meeting there. The per-anchor bar is the run's own stated
  numerical tolerance (``search.energy_thresh``, applied to both the covered
  and the clean slab) plus any stated uncertainty on the reservoir pricing.
  Its remedy is *add an anchor near this boundary*, so the sweep ranks the
  boundaries by this width (``needs_anchor``).
* *model-form* (uncertainty source 3) — the spread of the same boundary
  across the MLIP models that computed it. It does not shrink with more
  sampling; its remedy is a better potential or an experiment. Reported
  beside the first, never added to it.

No ``autocatpath`` import: this module is pure arithmetic over the JSON the
scan already wrote, so it is testable on a host without the engine.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

#: Boltzmann constant (eV/K) and the CHE reference temperature — the same two
#: numbers ``autocatpath.electrochem`` defines (not imported, see the module
#: docstring).
K_B_EV = 8.617333262e-5
T_STANDARD = 298.15
#: Nernst prefactor, V per pH unit: ``k_B T ln 10`` at 298.15 K (0.0592).
PREFAC_V = K_B_EV * T_STANDARD * math.log(10.0)

#: Electrons released per atom of each element when an adsorbate is formed
#: from catpath's reservoirs (H2 / H2O / CH4 / N2) under CHE — the slope of
#: ``mu_element`` in U. See the module docstring.
_NU_PER_ELEMENT: dict[str, int] = {"H": -1, "O": 2, "C": 4, "N": 0}

#: Default per-anchor energy bar (eV) when the config states no tolerance —
#: catpath's own ``search.energy_thresh`` default.
DEFAULT_SIGMA_E_EV = 0.05

_FORMULA_TOKEN = re.compile(r"([A-Z][a-z]?)(\d*)")


def element_counts(fragment: str) -> dict[str, int]:
    """Element → count for one adsorbate fragment (``'OH'``, ``'NH2'``).
    Site (``@``) and provenance (``~``) suffixes are stripped like
    ``autocatpath.electrochem.element_counts`` does; ``'*'`` is empty."""
    formula = fragment.split("@", 1)[0].split("~", 1)[0]
    counts: dict[str, int] = {}
    for sym, digits in _FORMULA_TOKEN.findall(formula):
        if sym:
            counts[sym] = counts.get(sym, 0) + (int(digits) if digits else 1)
    return counts


def electrons_released(adsorbate: str) -> int:
    """``nu_A``: proton-electron pairs released forming one ``A`` from the
    reservoirs (``H*`` → -1, ``OH*`` → 1, ``O*`` → 2, ``N*`` → 0).
    Raises ``ValueError`` for an element without a reservoir convention."""
    nu = 0
    for el, k in element_counts(adsorbate).items():
        if el not in _NU_PER_ELEMENT:
            raise ValueError(
                f"no CHE reservoir convention for element {el!r} in {adsorbate!r} "
                "(H, C, N, O only)"
            )
        nu += k * _NU_PER_ELEMENT[el]
    return nu


def u_she(u_rhe: float, ph: float) -> float:
    """V vs SHE for a potential on the RHE scale."""
    return float(u_rhe) - PREFAC_V * float(ph)


def u_rhe(u_she_v: float, ph: float) -> float:
    """V vs RHE for a potential on the SHE scale."""
    return float(u_she_v) + PREFAC_V * float(ph)


@dataclass(frozen=True)
class Anchor:
    """One computed termination: adsorbate ``A`` at count ``n`` (coverage
    ``theta`` ML) on the scanned facet, with its surface free energy at
    U = 0 V_RHE and the bar propagated onto it."""

    adsorbate: str
    n: int
    theta: float
    energy: float
    gamma0: float
    area: float
    site: str | None
    converged: bool
    detached: bool
    sigma_gamma: float
    model: str

    @property
    def nu(self) -> int:
        return electrons_released(self.adsorbate)

    @property
    def slope(self) -> float:
        """``d gamma / dU`` (eV/Å² per V)."""
        return -self.n * self.nu / self.area

    def gamma(self, u: float) -> float:
        return self.gamma0 + self.slope * float(u)

    @property
    def key(self) -> tuple[str, int]:
        return (self.adsorbate, self.n)

    @property
    def g_ads_per_adsorbate(self) -> float:
        """Formation free energy per adsorbate at U = 0 (eV) — the quantity a
        pathway's single-adsorbate node measures at theta → 0."""
        return self.gamma0 * self.area / self.n

    def as_dict(self) -> dict[str, Any]:
        return {
            "adsorbate": self.adsorbate,
            "n": self.n,
            "theta": self.theta,
            "energy": self.energy,
            "gamma0": self.gamma0,
            "slope": self.slope,
            "nu": self.nu,
            "area": self.area,
            "site": self.site,
            "converged": self.converged,
            "detached": self.detached,
            "sigma_gamma": self.sigma_gamma,
            "model": self.model,
        }


def anchors_from_coverage(
    payload: dict[str, Any],
    *,
    sigma_e_ev: float = DEFAULT_SIGMA_E_EV,
    sigma_mu_ev: dict[str, float] | None = None,
    facet_index: int = 0,
) -> list[Anchor]:
    """Parse one ``coverage.json`` payload (``autocatpath.coverage.scan``'s
    return) into anchors.

    ``sigma_e_ev`` is the per-relax energy bar applied to the covered slab
    and the clean slab alike; ``sigma_mu_ev`` an optional stated bar on each
    adsorbate's reservoir pricing (the correction-set uncertainty once the
    engine records one). Both propagate as ``sigma_gamma = sqrt(2 sigma_e^2
    + (n sigma_mu)^2) / area``. A fragment the CHE cannot price (an element
    outside H/C/N/O) is skipped rather than guessed."""
    facets = payload.get("facets") or []
    if not facets:
        return []
    facet = facets[facet_index]
    area = float(facet["area"])
    model = str(payload.get("model") or "")
    sigma_mu = sigma_mu_ev or {}
    out: list[Anchor] = []
    for frag, rec in (facet.get("adsorbates") or {}).items():
        try:
            electrons_released(frag)
        except ValueError:
            continue
        s_mu = float(sigma_mu.get(frag, 0.0))
        for p in rec.get("points") or []:
            n = int(p["n"])
            sg = math.sqrt(2.0 * sigma_e_ev**2 + (n * s_mu) ** 2) / area
            out.append(
                Anchor(
                    adsorbate=str(frag),
                    n=n,
                    theta=float(p["theta"]),
                    energy=float(p["energy"]),
                    gamma0=float(p["gamma"]),
                    area=area,
                    site=rec.get("site"),
                    converged=bool(p.get("converged", True)),
                    detached=bool(p.get("detached", False)),
                    sigma_gamma=sg,
                    model=model,
                )
            )
    return out


def usable(anchors: list[Anchor]) -> list[Anchor]:
    """Anchors that name a real termination: a configuration whose adsorbates
    detached is not a surface phase (the scan flags it, never trusts it).
    Unconverged points stay — they are a numerical caveat, not a different
    phase — and carry their flag through to the report."""
    return [a for a in anchors if not a.detached]


def _winner(anchors: list[Anchor], u: float) -> tuple[Anchor | None, float]:
    """The lowest-gamma anchor at ``u`` against the clean surface (``None``,
    gamma 0)."""
    best: Anchor | None = None
    best_g = 0.0
    for a in anchors:
        g = a.gamma(u)
        if g < best_g:
            best, best_g = a, g
    return best, best_g


def envelope(anchors: list[Anchor], u_grid: list[float]) -> list[dict[str, Any]]:
    """The resting termination along ``u_grid`` (V vs RHE): one row per
    point with the winner (``None`` = clean) and its gamma."""
    rows: list[dict[str, Any]] = []
    for u in u_grid:
        w, g = _winner(anchors, u)
        rows.append(
            {
                "U_RHE": float(u),
                "winner": None
                if w is None
                else {"adsorbate": w.adsorbate, "n": w.n, "theta": w.theta},
                "gamma": g,
            }
        )
    return rows


def _crossing(a: Anchor | None, b: Anchor | None) -> tuple[float, float] | None:
    """``(U*, |s_a - s_b|)`` where the two lines meet (the clean surface is
    the flat line gamma = 0), or ``None`` when parallel."""
    ga, sa = (0.0, 0.0) if a is None else (a.gamma0, a.slope)
    gb, sb = (0.0, 0.0) if b is None else (b.gamma0, b.slope)
    ds = sa - sb
    if abs(ds) < 1e-12:
        return None
    return (gb - ga) / ds, abs(ds)


@dataclass(frozen=True)
class Boundary:
    """Where the resting termination changes along U, with its two bands."""

    u_star: float
    below: tuple[str, int] | None
    above: tuple[str, int] | None
    band_propagated: float
    band_model_form: float | None
    n_models: int

    def as_dict(self) -> dict[str, Any]:
        def _k(k: tuple[str, int] | None) -> dict[str, Any] | None:
            return None if k is None else {"adsorbate": k[0], "n": k[1]}

        return {
            "U_RHE": self.u_star,
            "below": _k(self.below),
            "above": _k(self.above),
            "band_propagated": self.band_propagated,
            "band_model_form": self.band_model_form,
            "n_models": self.n_models,
        }


def _boundary_positions(
    anchors: list[Anchor], u_lo: float, u_hi: float, grid: int
) -> list[tuple[float, Anchor | None, Anchor | None]]:
    """Scan the grid for winner changes and refine each to the exact crossing
    of the two lines involved. Returns ``(U*, below, above)`` triples inside
    the window, ascending in U."""
    step = (u_hi - u_lo) / max(grid - 1, 1)
    out: list[tuple[float, Anchor | None, Anchor | None]] = []
    prev, _ = _winner(anchors, u_lo)
    for i in range(1, grid):
        u = u_lo + i * step
        cur, _ = _winner(anchors, u)
        if cur is prev:
            continue
        hit = _crossing(prev, cur)
        u_star = u - step / 2 if hit is None else hit[0]
        out.append((min(max(u_star, u - step), u), prev, cur))
        prev = cur
    return out


def boundaries(
    anchors_by_model: dict[str, list[Anchor]],
    pooled: list[Anchor],
    *,
    u_window: tuple[float, float],
    grid: int,
) -> list[Boundary]:
    """Boundaries of the pooled envelope with both bands.

    *propagated*: ``sqrt(sigma_below^2 + sigma_above^2) / |s_below - s_above|``
    from the pooled anchors' own bars (the clean surface has no bar and no
    slope).

    *model-form*: the same boundary located in each model's own anchor set
    (the crossing of the same two terminations) and the sample standard
    deviation of those positions; ``None`` below two models."""
    u_lo, u_hi = u_window
    out: list[Boundary] = []
    for u_star, below, above in _boundary_positions(pooled, u_lo, u_hi, grid):
        hit = _crossing(below, above)
        ds = hit[1] if hit else 0.0
        s_b = 0.0 if below is None else below.sigma_gamma
        s_a = 0.0 if above is None else above.sigma_gamma
        prop = math.sqrt(s_b**2 + s_a**2) / ds if ds > 0 else math.inf
        positions: list[float] = []
        for anchors in anchors_by_model.values():
            by_key = {a.key: a for a in anchors}
            mb = None if below is None else by_key.get(below.key)
            ma = None if above is None else by_key.get(above.key)
            if (below is not None and mb is None) or (above is not None and ma is None):
                continue
            h = _crossing(mb, ma)
            if h is not None:
                positions.append(h[0])
        mf = _std(positions) if len(positions) >= 2 else None
        out.append(
            Boundary(
                u_star=u_star,
                below=None if below is None else below.key,
                above=None if above is None else above.key,
                band_propagated=prop,
                band_model_form=mf,
                n_models=len(positions),
            )
        )
    return out


def _std(values: list[float]) -> float:
    m = sum(values) / len(values)
    return math.sqrt(sum((v - m) ** 2 for v in values) / (len(values) - 1))


def pool_anchors(anchors_by_model: dict[str, list[Anchor]]) -> list[Anchor]:
    """One anchor per ``(adsorbate, n)`` across models: ``gamma0`` is the
    mean, ``sigma_gamma`` the mean of the per-model propagated bars (the
    model spread is NOT folded in — it is the model-form band, reported on
    the boundaries). ``model`` names the contributing models."""
    groups: dict[tuple[str, int], list[Anchor]] = {}
    for anchors in anchors_by_model.values():
        for a in usable(anchors):
            groups.setdefault(a.key, []).append(a)
    pooled: list[Anchor] = []
    for key, members in sorted(groups.items()):
        first = members[0]
        pooled.append(
            Anchor(
                adsorbate=key[0],
                n=key[1],
                theta=first.theta,
                energy=sum(m.energy for m in members) / len(members),
                gamma0=sum(m.gamma0 for m in members) / len(members),
                area=first.area,
                site=first.site,
                converged=all(m.converged for m in members),
                detached=False,
                sigma_gamma=sum(m.sigma_gamma for m in members) / len(members),
                model="+".join(sorted({m.model for m in members})),
            )
        )
    return pooled


def anchor_spread(
    anchors_by_model: dict[str, list[Anchor]],
) -> dict[str, dict[str, Any]]:
    """Per ``(adsorbate, n)``: the model-form spread of ``gamma0`` across the
    models that computed it (``None`` below two)."""
    groups: dict[tuple[str, int], list[float]] = {}
    for anchors in anchors_by_model.values():
        for a in usable(anchors):
            groups.setdefault(a.key, []).append(a.gamma0)
    return {
        f"{k[0]}:{k[1]}": {
            "adsorbate": k[0],
            "n": k[1],
            "n_models": len(v),
            "gamma0_model_form": _std(v) if len(v) >= 2 else None,
        }
        for k, v in sorted(groups.items())
    }


def compare_lowest_theta(
    anchors: list[Anchor], reference: dict[str, float]
) -> list[dict[str, Any]]:
    """Slice-1 acceptance check: at the lowest computed coverage each
    adsorbate's per-adsorbate formation energy should reproduce the
    single-adsorbate energy a pathway run measured for the same species
    (``reference`` maps adsorbate → eV). Returns one row per shared species
    with the delta; nothing is judged here — the caller decides what delta
    is acceptable."""
    lowest: dict[str, Anchor] = {}
    for a in usable(anchors):
        cur = lowest.get(a.adsorbate)
        if cur is None or a.n < cur.n:
            lowest[a.adsorbate] = a
    rows: list[dict[str, Any]] = []
    for frag, ref in sorted(reference.items()):
        anc = lowest.get(frag)
        if anc is None:
            continue
        rows.append(
            {
                "adsorbate": frag,
                "theta": anc.theta,
                "g_ads_anchor": anc.g_ads_per_adsorbate,
                "reference": float(ref),
                "delta": anc.g_ads_per_adsorbate - float(ref),
            }
        )
    return rows


def sweep(
    payloads_by_model: dict[str, dict[str, Any]],
    *,
    u_window: tuple[float, float] = (-1.0, 0.5),
    grid: int = 151,
    ph: float = 7.0,
    point_u_rhe: float | None = None,
    sigma_e_ev: float = DEFAULT_SIGMA_E_EV,
    sigma_mu_ev: dict[str, float] | None = None,
) -> dict[str, Any]:
    """The inner CHE sweep: pooled anchors, the resting envelope on a U grid,
    every boundary with its two bands, and the boundaries ranked by the
    propagated band (where the next anchor is worth most).

    ``payloads_by_model`` maps a model tag to that model's ``coverage.json``
    payload (one scan job each). ``ph`` only labels the SHE view (the RHE
    sweep is pH-flat). ``point_u_rhe`` adds the winner at the operating
    point. JSON-safe return."""
    if grid < 2:
        raise ValueError("grid must be >= 2")
    u_lo, u_hi = float(u_window[0]), float(u_window[1])
    if not u_lo < u_hi:
        raise ValueError("u_window must be [lo, hi] with lo < hi")
    by_model = {
        tag: anchors_from_coverage(p, sigma_e_ev=sigma_e_ev, sigma_mu_ev=sigma_mu_ev)
        for tag, p in payloads_by_model.items()
    }
    pooled = pool_anchors(by_model)
    step = (u_hi - u_lo) / (grid - 1)
    u_grid = [u_lo + i * step for i in range(grid)]
    env = envelope(pooled, u_grid)
    bnds = boundaries(by_model, pooled, u_window=(u_lo, u_hi), grid=grid)
    ranked = sorted(bnds, key=lambda b: -b.band_propagated)
    out: dict[str, Any] = {
        "scale": "RHE",
        "pH": float(ph),
        "prefac_V_per_pH": PREFAC_V,
        "U_SHE_window": [u_she(u_lo, ph), u_she(u_hi, ph)],
        "U_RHE_window": [u_lo, u_hi],
        "grid": grid,
        "sigma_e_eV": sigma_e_ev,
        "models": sorted(by_model),
        "n_models": len(by_model),
        "anchors": [a.as_dict() for a in pooled],
        "anchor_model_form": anchor_spread(by_model),
        "skipped_detached": [
            {"model": tag, "adsorbate": a.adsorbate, "n": a.n}
            for tag, anchors in by_model.items()
            for a in anchors
            if a.detached
        ],
        "envelope": env,
        "boundaries": [b.as_dict() for b in bnds],
        "needs_anchor": [b.as_dict() for b in ranked],
        "notes": [
            "gamma(U) = gamma(0) - n*nu*U/area under CHE on the RHE scale; "
            "pH enters only through U_RHE = U_SHE + 0.059*pH.",
            "band_propagated: per-anchor energy bars pushed through the CHE "
            "arithmetic (uncertainty source 2; remedy: add an anchor). "
            "band_model_form: spread of the same boundary across MLIP models "
            "(source 3; remedy: a better potential or an experiment). Never "
            "summed.",
            "The clean surface is the gamma = 0 reference; a detached "
            "configuration is not a termination and is skipped.",
        ],
    }
    if point_u_rhe is not None:
        w, g = _winner(pooled, float(point_u_rhe))
        out["point"] = {
            "U_RHE": float(point_u_rhe),
            "U_SHE": u_she(point_u_rhe, ph),
            "winner": None
            if w is None
            else {"adsorbate": w.adsorbate, "n": w.n, "theta": w.theta},
            "gamma": g,
        }
    return out


def render_text(sweep_out: dict[str, Any]) -> str:
    """One-paragraph summary for a ``job_summary`` chunk."""
    lines: list[str] = []
    models = ", ".join(sweep_out.get("models") or []) or "?"
    lo, hi = sweep_out["U_RHE_window"]
    lines.append(
        f"surface-Pourbaix sweep (RHE, pH {sweep_out['pH']:g} for the SHE view) "
        f"over {lo:+.2f}…{hi:+.2f} V on {models}: "
        f"{len(sweep_out['anchors'])} anchor(s), {len(sweep_out['boundaries'])} boundary(ies)."
    )
    pt = sweep_out.get("point")
    if pt:
        w = pt["winner"]
        term = (
            "clean surface"
            if w is None
            else f"{w['adsorbate']}* at {w['theta']:.2f} ML"
        )
        lines.append(
            f"At U = {pt['U_RHE']:+.2f} V_RHE the resting termination is the {term}."
        )
    for b in sweep_out["boundaries"]:

        def _name(k: dict[str, Any] | None) -> str:
            return "clean" if k is None else f"{k['adsorbate']}*({k['n']})"

        mf = b["band_model_form"]
        mf_txt = "n/a (<2 models)" if mf is None else f"±{mf:.3f} V"
        lines.append(
            f"  {_name(b['below'])} → {_name(b['above'])} at {b['U_RHE']:+.3f} V: "
            f"propagated ±{b['band_propagated']:.3f} V, model-form {mf_txt}."
        )
    top = (sweep_out.get("needs_anchor") or [None])[0]
    if top:
        lines.append(
            f"Widest propagated strip: {top['U_RHE']:+.3f} V (±{top['band_propagated']:.3f} V) "
            "— the next anchor is worth most there."
        )
    return "\n".join(lines)


__all__ = [
    "DEFAULT_SIGMA_E_EV",
    "PREFAC_V",
    "Anchor",
    "Boundary",
    "anchor_spread",
    "anchors_from_coverage",
    "boundaries",
    "compare_lowest_theta",
    "electrons_released",
    "element_counts",
    "envelope",
    "pool_anchors",
    "render_text",
    "sweep",
    "u_rhe",
    "u_she",
    "usable",
]
