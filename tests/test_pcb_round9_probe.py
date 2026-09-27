"""DIAGNOSTIC PROBE (throwaway) — round 9's kill-early experiment.

Re-measures escape yield on the RING fixture (the grid stand-in that
manufactured its own wall is gone, see ``_qfp_ring_footprint``), across
the levers round 9 is choosing between:

* ``baseline``          — ring fixture, sink under the array, as shipped.
* ``sink_outside``      — sink moved clear of the electrode field. THE
  kill-early arm: if moving the driver off the array does not clear most
  of the ``no_path`` class on its own, round 9's premise is wrong and
  Phase 1 shrinks to closing gr451052.
* ``fabric_noop``       — the generator's escape fabric not claimed as an
  obstacle. The rival explanation (``_claim_fixed_copper`` gives each row
  its own net as owner, so every escape's stub blocks the other 53).
* ``sink_outside_fabric_noop`` — both, to see whether the gains are the
  same gain counted twice.
* ``clearance_093``     — re-judges the ``max()`` clearance collapse on
  the ring, where 0.15mm is what closes the QFP's own pin gaps.

Always passes; it is a measurement, not a contract. Results land in
``.pcb-round9-probe.jsonl`` at the worktree root (NOT /tmp — scripts/test
runs in a container and only the worktree is mounted).
"""

from __future__ import annotations

import collections
import dataclasses
import json
import pathlib
from typing import Any

import pytest

from precis.pcb import generators as pcb_generators
from precis.pcb import realize as pcb_realize
from tests.test_pcb_ewod_dogfood import _drain_one_job, _seed
from tests.test_pcb_ewod_dogfood import pcb  # noqa: F401  (fixture)

OUT = pathlib.Path(__file__).resolve().parents[1] / ".pcb-round9-probe.jsonl"

#: IPC-2221B B4 at 250V, the same figure the design derives.
HV_SEPARATION_MM = 0.4
#: Half-extent of _qfp_ring_footprint's default 17x23mm ring.
SINK_X_HALF = 8.5


def _move_sink_outside(monkeypatch: pytest.MonkeyPatch) -> None:
    """Post-process the expansion so the sink sits clear of the field.

    Deliberately NOT a generator feature: round 9's whole point is that
    the placer should reach this position from a keep-out constraint. This
    just asks whether the position is worth reaching at all.
    """
    orig = pcb_generators._REGISTRY["ewod_pad_array"]

    def patched(name: str, params: dict[str, Any]) -> Any:
        exp = orig(name, params)
        pad_xs = [
            float(p["x"])
            for fp in exp.footprints
            for p in fp.get("pads", [])
            if "x" in p
        ]
        array_x_max = max(pad_xs) if pad_xs else 0.0
        for comp in exp.components:
            if "ewod_sink" in (comp.get("roles") or []):
                # Clear of the field by the HV creepage figure, centred on
                # the array's own vertical span.
                comp["x"] = array_x_max + HV_SEPARATION_MM + SINK_X_HALF
        return exp

    monkeypatch.setitem(pcb_generators._REGISTRY, "ewod_pad_array", patched)


def _noop_fixed_copper(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        pcb_realize, "_claim_fixed_copper", lambda *a, **kw: None
    )


def _force_clearance(monkeypatch: pytest.MonkeyPatch, value: float) -> None:
    orig_rules = pcb_realize._resolve_track_rules
    orig_cfg = pcb_realize.RealizeConfig

    def patched_rules(*a: Any, **kw: Any) -> Any:
        r = orig_rules(*a, **kw)
        return dataclasses.replace(r, clearance_mm=min(r.clearance_mm, value))

    def patched_cfg(*a: Any, **kw: Any) -> Any:
        kw.setdefault("clearance_mm", value)
        return orig_cfg(*a, **kw)

    monkeypatch.setattr(pcb_realize, "_resolve_track_rules", patched_rules)
    monkeypatch.setattr(pcb_realize, "RealizeConfig", patched_cfg)


_ARMS = [
    "baseline",
    "sink_outside",
    "fabric_noop",
    "sink_outside_fabric_noop",
    "clearance_093",
]


@pytest.mark.slow
@pytest.mark.parametrize("arm", _ARMS)
def test_probe_round9_levers(pcb, store, monkeypatch, arm) -> None:  # noqa: F811
    if "sink_outside" in arm:
        _move_sink_outside(monkeypatch)
    if "fabric_noop" in arm:
        _noop_fixed_copper(monkeypatch)
    if arm == "clearance_093":
        _force_clearance(monkeypatch, 0.093)

    slug = _seed(pcb)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    pcb.put(id=slug, args={"op": "route", "seed": 1})
    _drain_one_job(store, ref.id)

    rows = store.pcb_route_status(ref.id)
    status_by_net = {str(r["name"]): str(r["status"]) for r in rows}
    note_by_net = {str(r["name"]): str(r.get("note") or "") for r in rows}
    escapes = {n: s for n, s in status_by_net.items() if n.startswith("ARR1_R")}

    design = store.pcb_load(ref.id)
    copper = store.pcb_copper_list(int(design["board"]["board_id"]))
    routed = [c for c in copper if not c.get("fixed")]

    row = {
        "arm": arm,
        "escapes_total": len(escapes),
        "escapes_realized": sum(1 for s in escapes.values() if s == "realized"),
        "all_status": dict(collections.Counter(status_by_net.values())),
        "fail_reasons": dict(
            collections.Counter(
                note_by_net[n] for n, s in escapes.items() if s != "realized"
            )
        ),
        "routed_track": sum(1 for c in routed if c.get("ctype") == "track"),
        "routed_via": sum(1 for c in routed if c.get("ctype") == "via"),
    }
    with OUT.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")
