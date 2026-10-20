"""The reduced domain: mask, gap field, Dirichlet ports.

Everything is dimension-free. Lateral coordinates are scaled by the chamber's
lateral extent ``L`` (``x* = x/L``, ``y* = y/L``) and the gap by the reference
gap ``h0`` (``h* = h/h0``), so a ``Domain`` is a cell-centred uniform grid
spanning ``x* in [0, 1]`` and ``y* in [0, ny/nx]`` with cell size
``dx* = 1/nx``. The shape *is* the mask; ``h*`` is the local gap over ``h0``
(1 = undrilled, >1 over a pocket), zero outside.

The tracking plane ``z_plane_star`` is the reduced height ``z~ = z/h0`` a
particle must reach to count as broken through: the flat ceiling sits at
``z~ = 1``, the floor at ``z~ = 1 - h*``. It is a *shape* parameter (offset
``/h0``), so it lives here, not on ``trace``.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Ports:
    """Dirichlet boundary cells: inlet held at ``p* = 1``, outlet at ``0``."""

    inlet: np.ndarray  # (ny, nx) bool
    outlet: np.ndarray  # (ny, nx) bool


@dataclass(frozen=True)
class Domain:
    """A thin chamber in reduced units, ready for :func:`precis_flow.solve`."""

    mask: np.ndarray  # (ny, nx) bool: cell centre inside the chamber footprint
    h_star: np.ndarray  # (ny, nx) float: local gap / h0; 0 outside, >0 inside
    ports: Ports
    z_plane_star: float = 0.99  # breakthrough plane, absolute reduced height

    def __post_init__(self) -> None:
        mask = np.asarray(self.mask, dtype=bool)
        if mask.ndim != 2 or mask.shape[0] < 1 or mask.shape[1] < 3:
            raise ValueError("mask must be a 2-D array with nx >= 3")
        h = np.where(mask, np.asarray(self.h_star, dtype=float), 0.0)
        if h.shape != mask.shape:
            raise ValueError("h_star must have the mask's shape")
        if np.any(h[mask] <= 0.0):
            raise ValueError("h_star must be > 0 on every inside cell")
        inlet = np.asarray(self.ports.inlet, dtype=bool)
        outlet = np.asarray(self.ports.outlet, dtype=bool)
        if inlet.shape != mask.shape or outlet.shape != mask.shape:
            raise ValueError("port masks must have the mask's shape")
        if not inlet.any() or not outlet.any():
            raise ValueError("both ports must cover at least one cell")
        if (inlet & outlet).any():
            raise ValueError("inlet and outlet must be disjoint")
        if (inlet & ~mask).any() or (outlet & ~mask).any():
            raise ValueError("ports must lie inside the mask")
        if not 0.0 < self.z_plane_star <= 1.0:
            raise ValueError("z_plane_star must lie in (0, 1]")
        object.__setattr__(self, "mask", mask)
        object.__setattr__(self, "h_star", h)
        object.__setattr__(self, "ports", Ports(inlet=inlet, outlet=outlet))

    @property
    def ny(self) -> int:
        return int(self.mask.shape[0])

    @property
    def nx(self) -> int:
        return int(self.mask.shape[1])

    @property
    def shape(self) -> tuple[int, int]:
        return (self.ny, self.nx)

    @property
    def dx_star(self) -> float:
        """Reduced cell size: the domain always spans ``x* in [0, 1]``."""
        return 1.0 / self.nx

    @property
    def width_star(self) -> float:
        """Reduced lateral width ``W/L = ny/nx``."""
        return self.ny / self.nx

    @property
    def xc_star(self) -> np.ndarray:
        """(nx,) cell-centre ``x*`` coordinates."""
        return (np.arange(self.nx) + 0.5) * self.dx_star

    @property
    def yc_star(self) -> np.ndarray:
        """(ny,) cell-centre ``y*`` coordinates."""
        return (np.arange(self.ny) + 0.5) * self.dx_star


def rasterize_holes(
    mask: np.ndarray, holes: np.ndarray, radius_star: float
) -> np.ndarray:
    """Rasterise blind holes as a reduced extra-depth field.

    ``holes`` is an ``(n, 3)`` array of ``(x*, y*, depth*)``; the result is
    ``depth*(x*, y*)`` on the mask's grid, suitable for ``h* = 1 + depth*``.
    Overlapping holes take the **deepest** value at each cell (drilled pockets
    remove the union of material). Cells outside ``mask`` are forced to zero.

    Vectorised over holes with a bounding-box write per hole, so cost is
    proportional to covered area, not ``n_holes * n_cells``.
    """
    mask = np.asarray(mask, dtype=bool)
    ny, nx = mask.shape
    dx = 1.0 / nx
    xc = (np.arange(nx) + 0.5) * dx
    yc = (np.arange(ny) + 0.5) * dx

    depth = np.zeros((ny, nx), dtype=float)
    holes = np.asarray(holes, dtype=float).reshape(-1, 3)
    for hx, hy, hd in holes:
        if hd <= 0:
            continue
        i0 = max(int(np.floor((hx - radius_star) / dx)), 0)
        i1 = min(int(np.ceil((hx + radius_star) / dx)), nx - 1)
        j0 = max(int(np.floor((hy - radius_star) / dx)), 0)
        j1 = min(int(np.ceil((hy + radius_star) / dx)), ny - 1)
        if i0 > i1 or j0 > j1:
            continue
        dxx = xc[None, i0 : i1 + 1] - hx
        dyy = yc[j0 : j1 + 1, None] - hy
        sub = depth[j0 : j1 + 1, i0 : i1 + 1]
        np.maximum(
            sub,
            np.where(dxx * dxx + dyy * dyy <= radius_star * radius_star, hd, sub),
            out=sub,
        )
    depth[~mask] = 0.0
    return depth
