from collections import Counter

import pytest

from precis.pcb import realize
from tests.test_pcb_escape_replay import (
    _apply_pin_swap_warm_start,
    _failed,
    _hydrate,
    _resolve_pin_swap_groups,
)


@pytest.mark.slow
@pytest.mark.parametrize("method", ["distance", "radial", None])
def test_m(store, method):
    ir, graph, features, footprints, fixed, config = _hydrate(store)
    if method:
        groups = _resolve_pin_swap_groups(ir, graph, [])
        _apply_pin_swap_warm_start(ir, groups, footprints, fixed, method)
    res = realize.realize(ir, config=config, footprints=footprints, fixed_copper=fixed)
    c = Counter(r.kind for r in res.unrouted_reasons)
    print("\nMEASURE", method, "failed", len(res.unrouted), dict(c))
    print("FAILNETS", sorted(_failed(ir, res)))
