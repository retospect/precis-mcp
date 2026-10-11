import pytest

from precis.pcb import realize
from precis.pcb.optimize import OptimizeConfig, optimize
from tests.test_pcb_escape_replay import (
    _apply_pin_swap_warm_start,
    _failed,
    _hydrate,
    _resolve_pin_swap_groups,
)


@pytest.mark.slow
@pytest.mark.parametrize("seed", [0, 1])
def test_rc(store, seed):
    ir, graph, features, footprints, fixed, config = _hydrate(store)
    groups = _resolve_pin_swap_groups(ir, graph, [])
    _apply_pin_swap_warm_start(ir, groups, footprints, fixed, "distance")
    r0 = realize.realize(ir, config=config, footprints=footprints, fixed_copper=fixed)
    print("START routed", 55 - len(_failed(ir, r0)))
    pn = ir.pin_net.copy()
    res = optimize(ir, OptimizeConfig(iters=3000, seed=seed, pin_swap_groups=groups))
    print(
        "anneal best_at",
        res.restored_best_at,
        "pins changed",
        int((ir.pin_net != pn).sum()),
    )
    print("RESFIELDS", [k for k in dir(res) if not k.startswith("_")])
    r1 = realize.realize(ir, config=config, footprints=footprints, fixed_copper=fixed)
    print("AFTER routed", 55 - len(_failed(ir, r1)))
