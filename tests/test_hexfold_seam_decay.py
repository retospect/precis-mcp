"""Seam decay length on the geo rung -- the number behind the hierarchical
resolved block (docs/backlog/diamondoid-pattern-language.md, "Three-zone
resolved block"): relax a part once, freeze its interior, re-relax only a
seam radius around each join.  That scheme holds iff the perturbation a
join makes on a part's geometry is localised to a few shells of atoms
around the fused rim.

Measured here via ``hexfold.catalogue.measure_environment`` (SPEC §26, step
6 slice 1) on hexfold tubes with ``precis.structure.georelax.relax_graph``
as the injected ``geo`` :data:`hexfold.join.Relaxer` (bond springs at the
covalent-radius sum, VSEPR angle term, non-bond repulsion; sp2 targets):
the same instance ``a`` relaxed free and relaxed fused tip-to-tip onto an
identical ``b``, compared shell by shell in graph distance from the fused
rim.  Two rigid-motion-invariant measures -- per-bond length change and
per-atom bond-angle change -- so a global breathing/length mode of the
longer composite does not masquerade as seam strain.

Geo rung only: the spring model has no rim reconstruction and no bond
alternation, so the *amplitude* at the rim (0.013-0.03 A here) is a lower
bound on what an energy rung would show; the *decay length* is set by the
lattice's elastic screening, which the spring model does carry.  The
zigzag decay is a thin-shell edge mode and its length grows with tube
length until the two ends stop interacting: (12,0) measured 5 shells at
len=4 and 8 shells at len=6 (about 4 hexagon rows).  The short (len=3)
tubes used here for runtime are too short to reach that asymptote, so
``measure_environment`` reports ``coverage="lower-bound"`` for both --
never a confident "full" decay length -- and the pinned bounds below are
lower bounds, not the true decay lengths; those live in the diamondoid
backlog item under "Seam decay, measured".

Lives in top-level ``tests/`` because it imports both ``hexfold`` and
``precis.structure`` (``tests/hexfold/`` stays hexfold-only).
"""

from __future__ import annotations

import numpy as np
import pytest

from hexfold.catalogue import EdgeMotif, measure_environment
from hexfold.join import Relaxer
from precis.structure.georelax import relax_graph

pytestmark = pytest.mark.slow

_TOL = 1e-4
_ITERS = 4000
#: bond-length change (A) and bond-angle change (deg) below which a shell
#: counts as unperturbed -- the same fixed pins the pre-refactor
#: ``_assert_localised`` used (not ``edge.thresh``, which is derived from
#: this very run's own noise floor and so would be self-referential).
_DL_A = 0.002
_DTHETA_DEG = 0.15


def _geo_relaxer() -> Relaxer:
    """The ``geo`` rung as a :data:`hexfold.join.Relaxer`: ``relax_graph``
    over the whole sub-graph it is called with, in-place-mutating its own
    ``coords`` copy (`hexfold.join.Relaxer`'s own contract is a pure
    function), with ``pinned_mask`` translated to `relax_graph`'s own
    ``pinned`` ordinal-set convention -- the same one-line adapter
    `hexfold.join._stick_relaxer`'s docstring anticipates."""

    def relax(
        elements: list[str],
        coords: np.ndarray,
        bonds: list[tuple[int, int, int]],
        rings: list[tuple[int, ...]],
        pinned_mask: np.ndarray,
    ) -> np.ndarray:
        del rings  # relax_graph derives its own angle triples from bonds
        c = np.asarray(coords, dtype=np.float64).copy()
        bonds_ij = [(i, j) for i, j, *_ in bonds]
        pinned = {i for i, p in enumerate(pinned_mask) if p}
        trace = relax_graph(
            elements, c, bonds_ij, pinned, hybridizations="sp2", iters=_ITERS, tol=_TOL
        )
        assert trace.converged, ("relaxer did not converge", trace.n_steps, trace.curve[-1])
        return c

    return relax


def _measure(n: int, m: int, length: int) -> EdgeMotif:
    edge, _bulk = measure_environment(
        (n, m), rung="geo", relax=_geo_relaxer(), length=length
    )
    return edge


def _assert_localised(edge: EdgeMotif, seam_shell: int) -> None:
    """The physics claim itself, over ``edge.profile`` (the measured
    per-shell (max disp, max |dl|, max |dtheta|)), independent of whatever
    ``measure_environment`` itself concluded from it: the join is felt at
    the rim, and every shell beyond ``seam_shell`` -- the far free rim
    included -- is unperturbed on both rigid-invariant measures."""
    shells = sorted(edge.profile)
    assert edge.profile[0][2] > 0.1, edge.profile[0]
    beyond = [s for s in shells if s > seam_shell]
    assert beyond, "tube too short to have an interior beyond the seam shell"
    bad = {
        s: edge.profile[s]
        for s in beyond
        if edge.profile[s][1] >= _DL_A or edge.profile[s][2] >= _DTHETA_DEG
    }
    assert not bad, {s: tuple(round(x, 4) for x in v) for s, v in bad.items()}


def test_zigzag_seam_perturbation_lower_bound_is_five_shells() -> None:
    """(8,0) zigzag at len=3: graph shells are half a hexagon row each along
    the axis, so five shells is about 2.5 rows (~5 A).  A lower bound -- the
    (12,0) len=6 measurement decays over 8 shells (module docstring).
    ``seam_radius`` is pinned by exact equality (not "<= 5"): today's
    calibration of `measure_environment`'s trusted-depth gate
    (``2 * (table_radius + 2)`` shells, `catalogue.py`) reports this
    length-3 tube's own measured depth // 2 whenever that gate is not
    met, which it never is at this length -- a later change to that gate
    must show up here by name."""
    edge = _measure(8, 0, 3)
    assert edge.coverage == "lower-bound"
    assert edge.seam_radius == 5
    _assert_localised(edge, seam_shell=5)


def test_armchair_seam_perturbation_lower_bound_is_two_shells() -> None:
    """(5,5) armchair: one graph shell per hexagon row; the perturbation is
    below threshold from the first interior shell on (measured 1; pinned
    at 2 for margin).  ``seam_radius`` pinned by exact equality -- see
    the zigzag test's docstring for why."""
    edge = _measure(5, 5, 3)
    assert edge.coverage == "lower-bound"
    assert edge.seam_radius == 2
    _assert_localised(edge, seam_shell=2)
