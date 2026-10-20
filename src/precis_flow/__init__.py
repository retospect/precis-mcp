"""precis_flow — dimension-free thin-chamber flow kernel
(docs/backlog/se-flow-kernel.md, slice 1).

Vendored from the cfdtest ``breakthrough_annealer`` scratch repo
(``flow.py``/``tracker.py``/``cost.py``/``geometry.py``) and recast so nothing
dimensional ever enters the solve or the tracer. numpy + scipy are the only
dependencies; the package imports nothing first-party — every binding to se
objects, the store, or measures lives outside (the slice-2 adapter).

The scaling
-----------

A thin chamber of lateral extent ``L`` and gap ``h(x, y)`` with reference
``h0`` obeys lubrication (Hele-Shaw) theory: ``U = -(h^2/12 mu) grad p`` with
``div(h^3 grad p) = 0``. Scale ``x* = x/L``, ``h* = h/h0``, ``p* = p/dp``,
``U* = U/u0`` with ``u0 = h0^2 dp / (12 mu L)``, ``t* = t/t0`` with
``t0 = L/u0`` (one transit), and nothing dimensional survives:

    div*(h*^3 grad* p*) = 0,    U* = -h*^2 grad* p*,    dx*/dt* = U*

The field, the streamlines, the resistance ratio ``R*``, the breakthrough
fraction and its evenness depend only on *shape*: the mask, the aspect ratio
``h0/L``, pocket depth ``/h0`` and diameter ``/h0``, port width ``/L``, the
seed band ``/L``, the tracking-plane offset ``/h0``. One solve per shape
serves a 1 mm chamber and a 50 cm chamber alike.

The single modelling parameter with physics in it is the vertical dispersion
over deepened floor, ``D_v* = D_v t0 / h0^2`` — transits per vertical mixing
time. It is a required argument to :func:`trace`; the regime card or the
caller states it, never a hidden default.

The regime card
---------------

Dimensions re-enter only at the boundary, through
:class:`RegimeCard.from_physical`: given a real ``(L, h0, mu, rho, dp or u0,
particle d and rho_p, molecular D, mean free path)`` it computes the six
dimensionless numbers of the spec — thinness ``h0/L``, reduced Reynolds
``Re_h h0/L``, Stokes ``St``, gravity ratio ``G``, Peclet ``Pe``, Knudsen
``Kn`` — each with a verdict naming the model that takes over when it fails
(3-D Stokes/Navier-Stokes, non-tracer particles, settling, diffusion, or the
slice-3 nano propagator). ``card.ok`` is the admission test; ``card.failures``
names the failing lines.

The stored-result contract
--------------------------

A stored flow result is reduced units (``x*``, ``t*``, ``R*``, fractions,
``psi`` gap fractions) plus the **scale triple** ``(L, h0, u0)`` plus the
**regime card**. The triple re-dimensionalises a solve for any size with the
same shape; the card tells whether that size is still inside the model.

API
---

- :class:`Domain` — ``(mask, h_star, ports)``; the discretised shape.
- :func:`solve` — ``Domain -> Field(U*, p*, R*)``.
- :class:`Seed` / :func:`trace` — ``(field, seed, n, d_v_star, seed_rng,
  record_frames=0) -> Paths(breakthrough, residence, ...)``.
- :class:`RegimeCard` — ``from_physical(...)`` -> the six verdict lines.
- :func:`nonuniformity` — evenness score over first-crossing points.
- :func:`rasterize_holes` — blind-pocket topography -> ``depth*`` field.

Modules
-------
"""

from .domain import Domain, Ports, rasterize_holes
from .flow import Field, solve
from .regime import RegimeCard, RegimeLine
from .stats import nonuniformity
from .tracer import Paths, Seed, trace

__all__ = [
    "Domain",
    "Field",
    "Paths",
    "Ports",
    "RegimeCard",
    "RegimeLine",
    "Seed",
    "nonuniformity",
    "rasterize_holes",
    "solve",
    "trace",
]
