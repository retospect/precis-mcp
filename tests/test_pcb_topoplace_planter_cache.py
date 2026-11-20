"""Via-site retries must not retain abandoned copper indices (gr687847)."""

from __future__ import annotations

import gc
import weakref

import numpy as np

from precis.pcb.clearance import FAB_QUANTUM_MM
from precis.pcb.topoplace import wires
from precis.pcb.topoplace.copper_index import CopperIndex
from precis.pcb.topoplace.mesh import build_mesh
from tests.fixtures.pcb.topo_example_a import example_a


def _planter() -> tuple[wires._ViaPlanter, int]:
    d = example_a()
    mesh = build_mesh(d, d.pose0)
    ctx = wires._make_ctx(mesh, d, 2)
    planter = wires._ViaPlanter(ctx, d)
    for e in np.flatnonzero(~ctx.board):
        if planter.point(int(e), 0.225, 0.15, 0) is not None:
            return planter, int(e)
    raise AssertionError("fixture has no plantable edge")


def test_retry_cache_releases_abandoned_indices() -> None:
    planter, edge = _planter()
    expected = list(planter.points(edge, 0.225, 0.15, 0))
    refs = []
    for _ in range(50):
        dyn = CopperIndex([])
        refs.append(weakref.ref(dyn))
        assert list(planter.points(edge, 0.225, 0.15, 0, dyn)) == expected
        del dyn
    gc.collect()
    assert all(ref() is None for ref in refs)
    assert not planter._pts


def test_retry_cache_reuses_current_version_and_discards_old_verdicts() -> None:
    planter, edge = _planter()
    dyn = CopperIndex([])
    original = list(planter.points(edge, 0.225, 0.15, 0, dyn))
    state = planter._pts[dyn]
    assert list(planter.points(edge, 0.225, 0.15, 0, dyn)) == original
    assert planter._pts[dyn] is state
    old_points = weakref.ref(next(iter(state[1].values())))
    del state
    x, y, _ = original[0]
    dyn.add_disc(x, y, 0.3)
    changed = list(planter.points(edge, 0.225, 0.15, 0, dyn))
    assert original[0] not in changed
    assert changed == [
        pt
        for pt in original
        if np.hypot(pt[0] - x, pt[1] - y) >= 0.225 + 0.3 + 0.15 + FAB_QUANTUM_MM
    ]
    assert old_points() is None
    assert len(planter._pts[dyn][1]) == 1
    # A separate retry starts from its own copper, never a reused ID's verdict.
    other = CopperIndex([])
    assert list(planter.points(edge, 0.225, 0.15, 0, other)) == original
