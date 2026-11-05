"""The seam's optional hooks preserve the pre-hook floating-point path.

These compare bytes, including NaN payloads and signed zero, rather than
using a numerical tolerance. The reference expressions below are the
pre-hook pressure/velocity arithmetic, independent of the hook branches.
"""

from __future__ import annotations

from dataclasses import fields

import numpy as np
import pytest

from precis_flow import Domain, Field, Paths, Ports, Seed, fv, solve, tracer
from precis_flow.flow import _masked_gradient, _resistance
from precis_flow.tracer import CellProfile


def _domain(flat: bool) -> Domain:
    mask = np.ones((8, 18), dtype=bool)
    mask[:2, 6:9] = False
    jj, ii = np.indices(mask.shape)
    h = np.ones(mask.shape) if flat else 1.0 + 0.7 * np.sin(ii * 0.3) ** 2
    if not flat:
        h += 0.2 * np.cos(jj * 0.7) ** 2
    inlet = np.zeros_like(mask)
    outlet = np.zeros_like(mask)
    inlet[:, 0] = True
    outlet[:, -1] = True
    return Domain(
        mask=mask,
        h_star=np.where(mask, h, 0.0),
        ports=Ports(inlet=inlet, outlet=outlet),
        z_plane_star=0.95,
    )


def _pre_hook_solve(domain: Domain) -> Field:
    h = domain.h_star
    inside = domain.mask
    dx = domain.dx_star
    t_field = np.where(inside, h**3, 0.0)
    p = fv.elliptic(
        domain, t_field, [(domain.ports.inlet, 1.0), (domain.ports.outlet, 0.0)]
    )
    p_for_grad = np.where(inside, np.nan_to_num(p, nan=0.0), 0.0)
    dpdx = _masked_gradient(p_for_grad, inside, dx, axis=1)
    dpdy = _masked_gradient(p_for_grad, inside, dx, axis=0)
    coef = np.where(inside, h**2, 0.0)
    ux = -coef * dpdx
    uy = -coef * dpdy
    floor = np.where(inside, 1.0 - h, 0.0)
    return Field(
        domain=domain,
        p_star=p,
        ux_star=ux,
        uy_star=uy,
        bx_star=_masked_gradient(floor, inside, dx, axis=1),
        by_star=_masked_gradient(floor, inside, dx, axis=0),
        r_star=_resistance(domain, t_field, p),
    )


def _pre_hook_velocity(
    field: Field,
    pos: np.ndarray,
    profile: str = "plug",
    cell_profile: CellProfile | None = None,
) -> np.ndarray:
    assert cell_profile is None
    ux, uy, h, b, bx, by = tracer._sample(field, pos[:, 0], pos[:, 1])
    u_dot_gradb = ux * bx + uy * by
    psi = np.clip((pos[:, 2] - b) / np.where(h > 1e-9, h, 1.0), 0.0, 1.0)
    w = u_dot_gradb * (1.0 - psi)
    if profile == "parabolic":
        shear = 6.0 * psi * (1.0 - psi)
        ux = ux * shear
        uy = uy * shear
    return np.column_stack([ux, uy, w])


def _assert_identical(actual: Field | Paths, expected: Field | Paths) -> None:
    for item in fields(expected):
        if item.name == "domain":
            continue
        a, b = getattr(actual, item.name), getattr(expected, item.name)
        if isinstance(b, np.ndarray):
            assert a.shape == b.shape, item.name
            assert a.dtype == b.dtype, item.name
            assert a.tobytes() == b.tobytes(), item.name
        elif isinstance(b, float):
            assert a.hex() == b.hex(), item.name
        else:
            assert a == b, item.name


@pytest.mark.parametrize("flat", [False, True])
def test_solver_default_and_identity_factor_match_pre_hook(flat: bool) -> None:
    domain = _domain(flat)
    expected = _pre_hook_solve(domain)
    _assert_identical(solve(domain), expected)
    _assert_identical(solve(domain, t_factor=None), expected)
    _assert_identical(solve(domain, t_factor=np.ones(domain.shape)), expected)


@pytest.mark.parametrize("flat", [False, True])
@pytest.mark.parametrize(
    ("profile", "species"), [("plug", False), ("parabolic", False), ("parabolic", True)]
)
def test_tracer_default_and_identity_profile_match_pre_hook(
    flat: bool, profile: str, species: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    domain = _domain(flat)
    field = _pre_hook_solve(domain)
    seed = Seed.across_inlet(domain)

    def run(cell_profile: CellProfile | None = None) -> Paths:
        return tracer.trace(
            field,
            seed,
            64,
            d_v_star=1.3,
            seed_rng=17,
            record_frames=4,
            profile=profile,
            pe=3.0 if species else None,
            epsilon=0.1 if species else None,
            walls={"floor": 0.2} if species else None,
            max_steps=200,
            cell_profile=cell_profile,
        )

    # Inject only the old velocity expression; all sampling, RK2,
    # randomness, crossings, wall reactions and recording still run.
    with monkeypatch.context() as patch:
        patch.setattr(tracer, "_velocity", _pre_hook_velocity)
        expected = run()
    _assert_identical(run(), expected)

    def identity_profile(
        domain: Domain,
        x_star: np.ndarray,
        y_star: np.ndarray,
        psi: np.ndarray,
    ) -> np.ndarray:
        # A constant per-cell field samples to one exactly by nearest
        # cell, leaving the original shear expression in the same order.
        cells = np.ones(domain.shape)
        ii = np.clip((x_star / domain.dx_star).astype(int), 0, domain.nx - 1)
        jj = np.clip((y_star / domain.dx_star).astype(int), 0, domain.ny - 1)
        factor = cells[jj, ii]
        return factor if profile == "plug" else factor * (6.0 * psi * (1.0 - psi))

    _assert_identical(run(identity_profile), expected)
