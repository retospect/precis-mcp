"""Species boundary conditions and computers for thin-gap chambers.

The species adapter is kept separate from flow rendering and the shared
evaluate path. Its numerical implementation is unchanged by the registry seam;
the computer wrapper checks the chamber's declared physics before calling it.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np

from precis.errors import BadInput
from precis_flow import Paths, Seed, solve, trace
from precis_geom.flow import PortPatch, domain_from_solid
from precis_se.flow import Chamber, FlowSpec, _chamber_placed, _flow_pocket, patch_port
from precis_se.measures import SCOPE_SEP

# ── species BCs and the slice-1 species computers ─────────────────────
#
# (docs/backlog/se-multiphysics.md slice 1) Boundary conditions are
# patch measures — a measure whose ``datum`` is a ``patch:`` selector on
# the chamber block, scoped to one species by its name
# ``<species>/<prop>`` (the qualified ``<block>.<species>/<prop>`` of the
# spec's Q2); a bare ``<prop>`` input measure applies to every species.
# Input measurands: ``inlet-concentration`` (mol/m³, informational while
# the equations are linear), ``wall-rate-constant`` (m/s, floor/lid
# patches), ``species-diffusivity`` (m²/s — falls back to the fluid
# material's diffusivity), ``inlet-velocity`` (m/s) and ``pressure-drop``
# (Pa, needs fluid μ → u0 = h0²dp/(12μL)). A missing input is a reason
# string, never a silent number.

_SPECIES_INPUT_PROPS = (
    "inlet-concentration",
    "inlet-velocity",
    "pressure-drop",
    "species-diffusivity",
    "wall-rate-constant",
)


def _scoped_prop(name: str, species: tuple[str, ...]) -> tuple[str | None, str]:
    """Split a measure name into ``(species, prop)``: ``co/wall-rate-constant``
    → ``('co', 'wall-rate-constant')``; a bare ``wall-rate-constant`` →
    ``(None, 'wall-rate-constant')``. Only the declared species names
    split — ``bt`` stays a bare name. The separator is
    :data:`~precis_se.measures.SCOPE_SEP` (``/``): ``.`` is the
    relation-source ``'block.measure'`` split and never in a name."""
    for sp in species:
        if name.startswith(f"{sp}{SCOPE_SEP}"):
            return sp, name[len(sp) + len(SCOPE_SEP) :]
    if SCOPE_SEP in name:
        head, _, rest = name.partition(SCOPE_SEP)
        if head and head not in species:
            return head, rest
    return None, name


def _bc_measures(
    tree: Any, block: str, spec: FlowSpec
) -> tuple[dict[tuple[str | None, str], Any], list[tuple[str | None, Any]]]:
    """``(species|None, prop) → measure`` for the chamber block's
    species-input patch measures, plus the ``wall-rate-constant`` list —
    scalar props collapse to one value per species (later rows win), but
    each reactive patch is its own measure, so the wall rates collect
    (per-catalyst-patch selectivity needs every one)."""
    out: dict[tuple[str | None, str], Any] = {}
    walls: list[tuple[str | None, Any]] = []
    for m in tree.measures:
        if m.block != block or m.value is None:
            continue
        sp, prop = _scoped_prop(m.name, spec.species)
        # A wall-rate measure is named '<species>/wall-rate-constant' —
        # or carries the measurand outright when the op layer had to
        # suffix the name for a second reactive patch.
        if prop == "wall-rate-constant" or (
            (m.measurand or m.measurand_live) == "wall-rate-constant"
        ):
            walls.append((sp, m))
        elif prop in _SPECIES_INPUT_PROPS:
            out[(sp, prop)] = m
    return out, walls


def _bc_value(
    bcs: dict[tuple[str | None, str], Any], species: str, prop: str
) -> tuple[float | None, Any | None]:
    """A species-scoped BC value, falling back to the unscoped measure."""
    m = bcs.get((species, prop)) or bcs.get((None, prop))
    return (m.value if m is not None else None), m


def _fluid_props(tree: Any, spec: FlowSpec) -> dict[str, float] | None:
    """The fluid material's property map (slug → ``{prop_id: value}``)
    through the tree's store-bound ``materials`` resolver — ``None`` on a
    bare tree or when the spec names no fluid."""
    if spec.fluid is None or getattr(tree, "materials", None) is None:
        return None
    return tree.materials(spec.fluid)


def _fluid_value(props: dict[str, float] | None, *ids: str) -> float | None:
    if not props:
        return None
    for pid in ids:
        v = props.get(pid)
        if isinstance(v, int | float) and v > 0:
            return float(v)
    return None


def _patch_side(chamber: Chamber, patch: PortPatch) -> str | None:
    """``'floor'``/``'lid'`` for a thin-axis face patch; ``None`` for a
    lateral (side) patch — a ``wall-rate-constant`` on a side patch has
    no wall to bind to."""
    if abs(float(patch.normal[chamber.thin_axis])) < 0.9:
        return None
    return (
        "floor"
        if (patch.point[chamber.thin_axis] > chamber.z_ceiling) == chamber.z_flip
        else "lid"
    )


def _patch_cells(chamber: Chamber, patch: PortPatch) -> np.ndarray:
    """Inside cells whose centre sits inside the patch's lateral
    rectangle — the rasteriser for floor/lid ``wall-rate-constant``
    patches (unlike ``_claim_port`` it does not snap to the boundary)."""
    d = chamber.domain
    ny, nx = d.shape
    xa, ya = chamber.x_axis, chamber.y_axis
    cells = np.zeros((ny, nx), dtype=bool)
    pu, pv = float(patch.point[xa]), float(patch.point[ya])
    for j in range(ny):
        cy = chamber.lo[ya] + (j + 0.5) * chamber.L / nx
        for i in range(nx):
            if not d.mask[j, i]:
                continue
            cx = chamber.lo[xa] + (i + 0.5) * chamber.L / nx
            du = (cx - pu) * patch.u[xa] + (cy - pv) * patch.u[ya]
            dv = (cx - pu) * patch.v[xa] + (cy - pv) * patch.v[ya]
            if abs(du) <= patch.half_u + 1e-12 and abs(dv) <= patch.half_v + 1e-12:
                cells[j, i] = True
    return cells


@dataclass(frozen=True)
class _WallCase:
    """One reactive-wall patch bound for the solve."""

    side: str  # 'floor' | 'lid'
    k_s: float  # m/s
    da_w: float  # k_s h0 / D
    cells: np.ndarray  # (ny, nx) bool
    measure_name: str  # the measure that declared it


@dataclass(frozen=True)
class _SpeciesCase:
    """The physical inputs one species' solve needs, gathered from the
    ``flow:`` spec + patch measures + the fluid material."""

    name: str
    epsilon: float  # h0/L
    pe: float  # u0 h0/D
    d_m2s: float  # the diffusivity used (measure or material)
    u0: float  # m/s
    walls: tuple[_WallCase, ...]
    c_in: float | None  # declared inlet concentration (mol/m³), informational


def _species_case_for(
    tree: Any,
    block: str,
    chamber: Chamber,
    spec: FlowSpec,
    species: str,
) -> tuple[_SpeciesCase | None, str | None]:
    """Gather the species' physical inputs, or a reason string — a
    missing input is a reason, never a silent default."""
    fluid = _fluid_props(tree, spec)
    bcs, wall_measures = _bc_measures(tree, block, spec)

    d_m2s, _dm = _bc_value(bcs, species, "species-diffusivity")
    if d_m2s is None:
        d_m2s = _fluid_value(
            fluid, "diffusivity", "species_diffusivity", "diffusion_coefficient"
        )
    if d_m2s is None or d_m2s <= 0:
        return None, (
            f"no diffusivity for species {species!r} — declare a "
            f"'{species}/species-diffusivity' patch measure (m²/s) or give "
            "the fluid material a 'diffusivity' property"
        )

    u0, _um = _bc_value(bcs, species, "inlet-velocity")
    if u0 is None:
        dp, _pm = _bc_value(bcs, species, "pressure-drop")
        mu = _fluid_value(fluid, "viscosity", "dynamic_viscosity")
        if dp is not None and dp > 0 and mu is not None:
            u0 = chamber.h0 * chamber.h0 * dp / (12.0 * mu * chamber.L)
    if u0 is None or u0 <= 0:
        return None, (
            "no operating point — declare an 'inlet-velocity' (m/s) or "
            "'pressure-drop' (Pa) patch measure on the inlet"
        )

    c_in, _cm = _bc_value(bcs, species, "inlet-concentration")

    epsilon = chamber.h0 / chamber.L
    pe = u0 * chamber.h0 / d_m2s
    walls: list[_WallCase] = []
    reasons: list[str] = []
    for sp, m in wall_measures:
        if sp is not None and sp != species:
            continue
        datum = m.datum or ""
        try:
            patch = patch_port(tree, block, datum)
        except BadInput as exc:
            reasons.append(f"{m.name}: {exc}")
            continue
        side = _patch_side(chamber, patch)
        if side is None:
            reasons.append(
                f"{m.name}: datum {datum!r} is a lateral patch — "
                "wall-rate-constant binds to floor/lid patches only"
            )
            continue
        k_s = float(m.value or 0.0)
        if k_s < 0:
            reasons.append(f"{m.name}: wall-rate-constant must be ≥ 0")
            continue
        walls.append(
            _WallCase(
                side=side,
                k_s=k_s,
                da_w=k_s * chamber.h0 / d_m2s,
                cells=_patch_cells(chamber, patch),
                measure_name=m.name,
            )
        )
    if reasons:
        return None, "; ".join(reasons)
    return _SpeciesCase(
        name=species,
        epsilon=epsilon,
        pe=pe,
        d_m2s=d_m2s,
        u0=u0,
        walls=tuple(walls),
        c_in=c_in,
    ), None


def _species_of(
    tree: Any, measure: Any
) -> tuple[Any | None, FlowSpec | None, str | None, str | None]:
    """``(node, spec, species, None)`` or a reason — the measure's
    ``<species>/<prop>`` scope, defaulting to a single declared species."""
    node = tree.blocks.get(measure.block)
    if node is None:
        return None, None, None, f"measure names missing block {measure.block!r}"
    got = _flow_pocket(node, measure.datum or "")
    if isinstance(got, str):
        return None, None, None, got
    _pocket, spec = got
    sp, _prop = _scoped_prop(measure.name, spec.species)
    if sp is None:
        if len(spec.species) == 1:
            sp = spec.species[0]
        elif not spec.species:
            return (
                None,
                spec,
                None,
                (
                    f"pocket shape declares no species — the {measure.name!r} "
                    "measure has nothing to transport"
                ),
            )
        else:
            return (
                None,
                spec,
                None,
                (
                    f"measure {measure.name!r} names no species — scope it as "
                    f"'{measure.block}.<species>/<prop>' (declared: "
                    f"{', '.join(spec.species)})"
                ),
            )
    elif spec.species and sp not in spec.species:
        return (
            None,
            spec,
            None,
            (
                f"species {sp!r} is not declared in the flow: token "
                f"(species={','.join(spec.species)})"
            ),
        )
    return node, spec, sp, None


def _species_run(
    tree: Any, measure: Any, *, n_particles: int = 4000, seed_rng: int = 0
) -> tuple[Any, Any, str | None]:
    """Chamber + species solve for a species-scoped measure — Eulerian
    when ``Pe·ε ≤ 0.1``, the Lagrangian tracer otherwise (the card's
    taylor route). Returns ``(ctx, result, reason)`` where ``ctx`` is
    ``(chamber, spec, case)`` and result a ``SpeciesResult`` or
    ``Paths``."""
    from precis_flow.species import ReactiveWall, transport

    node, spec, sp, err = _species_of(tree, measure)
    if err is not None or spec is None or sp is None:
        return None, None, err
    placed = _chamber_placed(None, tree, measure.block, (spec.inlet, spec.outlet))
    if placed is None:
        return (
            None,
            None,
            (
                f"block {measure.block!r} has no parseable envelope — the "
                "chamber needs a cad envelope"
            ),
        )
    try:
        chamber = domain_from_solid(
            placed,
            patch_port(tree, measure.block, spec.inlet),
            patch_port(tree, measure.block, spec.outlet),
            z_plane_star=spec.zplane,
        )
    except (BadInput, ValueError) as exc:
        return None, None, str(exc)
    case, err = _species_case_for(tree, measure.block, chamber, spec, sp)
    if err is not None:
        return (chamber, spec, None), None, err
    assert case is not None
    field = solve(chamber.domain)
    if case.pe * case.epsilon <= 0.1:
        walls = [
            ReactiveWall(wall=w.side, da_w=w.da_w, cells=w.cells) for w in case.walls
        ]
        try:
            res = transport(
                field,
                epsilon=case.epsilon,
                pe=case.pe,
                walls=walls,
                limiter=True,
            )
        except ValueError as exc:
            return (chamber, spec, case), None, str(exc)
        return (chamber, spec, case), res, None
    # Lagrangian route: the same walls as per-cell Da_w fields.
    wall_fields: dict[str, np.ndarray] = {}
    for w in case.walls:
        arr = wall_fields.setdefault(
            w.side, np.zeros(chamber.domain.shape, dtype=float)
        )
        arr[w.cells] = w.da_w
    walls_arg: dict[str, float | np.ndarray] = dict(wall_fields)
    paths = trace(
        field,
        Seed.across_inlet(chamber.domain, z_lo=0.0, z_hi=1.0),
        n_particles,
        d_v_star=spec.d_v_star,
        seed_rng=seed_rng,
        profile="parabolic",
        pe=case.pe,
        epsilon=case.epsilon,
        walls=walls_arg,
        max_steps=20000,
    )
    return (chamber, spec, case), paths, None


def _wall_patch_index(
    case: _SpeciesCase, measure: Any, chamber: Chamber, tree: Any, block: str
) -> int | None:
    """Which of the case's walls the measure's datum patch names — by
    cell overlap (the measure's patch need not be the rate measure's
    exact patch)."""
    datum = measure.datum or ""
    try:
        patch = patch_port(tree, block, datum)
    except BadInput:
        return None
    cells = _patch_cells(chamber, patch)
    if not cells.any():
        return None
    side = _patch_side(chamber, patch)
    best: tuple[int, int] | None = None
    for i, w in enumerate(case.walls):
        overlap = int((w.cells & cells).sum())
        if overlap and (side is None or w.side == side):
            if best is None or overlap > best[1]:
                best = (i, overlap)
    return best[0] if best else None


def _flow_outlet_conversion(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    ctx, res, err = _species_run(tree, measure)
    if res is None:
        return None, err
    if isinstance(res, Paths):
        absorbed = res.absorbed
        if absorbed is None:
            return 0.0, None
        return float(absorbed.mean()), None
    return float(res.conversion), None


def _flow_outlet_concentration(
    tree: Any, measure: Any
) -> tuple[float | None, str | None]:
    ctx, res, err = _species_run(tree, measure)
    if res is None:
        return None, err
    if isinstance(res, Paths):
        absorbed = res.absorbed
        return float(1.0 - absorbed.mean()) if absorbed is not None else 1.0, None
    return float(res.outlet_mean_c), None


def _flow_wall_flux_fraction(
    tree: Any, measure: Any
) -> tuple[float | None, str | None]:
    """Fraction of the inlet species flux consumed on the measure's
    patch — per-catalyst-patch selectivity."""
    ctx, res, err = _species_run(tree, measure)
    if ctx is None or res is None:
        return None, err
    chamber, spec, case = ctx
    i = _wall_patch_index(case, measure, chamber, tree, measure.block)
    if i is None:
        return None, (
            f"datum {measure.datum!r} names no reactive patch of species "
            f"{case.name!r} — wall-flux-fraction scores a "
            "wall-rate-constant patch"
        )
    w = case.walls[i]
    if isinstance(res, Paths):
        absorbed = res.absorbed
        if absorbed is None or res.absorb_star is None:
            return 0.0, None
        dx = chamber.domain.dx_star
        ii = np.clip(
            np.nan_to_num(res.absorb_star[:, 0] / dx, nan=0.0).astype(int),
            0,
            chamber.domain.nx - 1,
        )
        jj = np.clip(
            np.nan_to_num(res.absorb_star[:, 1] / dx, nan=0.0).astype(int),
            0,
            chamber.domain.ny - 1,
        )
        in_patch = absorbed & np.isfinite(res.absorb_star[:, 0]) & w.cells[jj, ii]
        value = float(in_patch.sum() / res.n_particles)
    else:
        value = (
            float(res.per_wall_sink[i] / res.inlet_flux) if res.inlet_flux > 0 else 0.0
        )
    return value, None


def _flow_damkohler(tree: Any, measure: Any) -> tuple[float | None, str | None]:
    """``Da_I = Σ_w k_s,w·f_w·L/(h0·u0)`` — the transit Damköhler (gauge);
    a patch covers fraction ``f_w`` of the chamber's cells."""
    node, spec, sp, err = _species_of(tree, measure)
    if err is not None or spec is None or sp is None:
        return None, err
    placed = _chamber_placed(None, tree, measure.block, (spec.inlet, spec.outlet))
    if placed is None:
        return None, (
            f"block {measure.block!r} has no parseable envelope — the "
            "chamber needs a cad envelope"
        )
    try:
        chamber = domain_from_solid(
            placed,
            patch_port(tree, measure.block, spec.inlet),
            patch_port(tree, measure.block, spec.outlet),
            z_plane_star=spec.zplane,
        )
    except (BadInput, ValueError) as exc:
        return None, str(exc)
    case, err = _species_case_for(tree, measure.block, chamber, spec, sp)
    if err is not None:
        return None, err
    assert case is not None
    if not case.walls:
        return None, (
            f"no 'wall-rate-constant' patch measure for species {case.name!r} "
            "— Da_I needs a reactive wall"
        )
    n_cells = float(chamber.domain.mask.sum())
    da_i = sum(
        w.k_s * (float(w.cells.sum()) / n_cells) * chamber.L / (chamber.h0 * case.u0)
        for w in case.walls
        if w.cells.any()
    )
    return float(da_i), None
