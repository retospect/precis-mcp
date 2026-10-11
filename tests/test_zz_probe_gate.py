from precis.pcb import session
from tests.test_pcb_escape_replay import _hydrate


def test_probe(store):
    ir, graph, features, footprints, fixed, config = _hydrate(store)
    f = session.placement_drc_findings(
        ir, capability=config.fab_caps, footprints=footprints,
        fixed_copper=fixed, outline=session.outline_from_features(features),
    )
    n, rep = session.placement_drc_report(f)
    assert False, f"PROBE {n} {rep}"
