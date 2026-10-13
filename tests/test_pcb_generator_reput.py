"""gr481705 — a stale-``ewod_pad_array`` re-put through the JUDGED
``PcbHandler.put`` surface must not reset a placed generator-owned
instance to the expansion's emitted seed pose.

The fixture board ``ewod-dogfood-6-escape-regression`` stores ARR1 at an
old ``ewod_pad_array`` version with ``ARR1_SINK_0`` already placed
(moved off-centroid and rotated by the placer). Re-putting the same
generators entry — the refresh the stale-route guard's own ``next``
hints at — used to retire the sink and re-insert it at the emitted
centroid seed, a pose the always-valid gate refuses: the centroid lands
sink pads on the array's own plaza vias (14 ``invariant:no_via_in_pad``
hits). The re-apply now carries each generator-owned instance's stored
pose forward onto the re-emitted component; only the explicit
``regenerate`` escape re-seeds.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import generators as pcb_generators
from precis.pcb.snapshot import load_snapshot, read_snapshot
from precis.store import Store


def _hydrate(store: Store) -> Any:
    fixture = read_snapshot(Path("tests/fixtures/pcb/ewod-dogfood-6-replay-v1.json.gz"))
    with store.pool.connection() as conn:
        load_snapshot(conn, fixture, "ewod-dogfood-6-escape-regression")
    ref = store.get_ref(kind="pcb", id="ewod-dogfood-6-escape-regression")
    assert ref is not None
    return ref


def _pose(store: Store, ref_id: int, refdes: str) -> tuple[Any, ...]:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT x, y, rot, layer FROM pcb_instances "
            "WHERE ref_id = %s AND refdes = %s AND retired_at IS NULL",
            (ref_id, refdes),
        ).fetchone()
    assert row is not None
    return tuple(row)


def _reput(pcb: PcbHandler, store: Store, ref: Any) -> None:
    params = store.pcb_generators_for(ref.id)["ARR1"]["params"]
    pcb.put(
        id=str(ref.slug),
        args={
            "generators": [
                {
                    "name": "ARR1",
                    "generator": "ewod_pad_array",
                    "params": dict(params),
                }
            ]
        },
    )


def test_stale_generators_reput_through_judged_put(store: Store) -> None:
    """The headline: the re-put succeeds (14 no_via_in_pad hits used to
    refuse it) AND the placed sink keeps its stored pose — neither the
    seed's centroid nor its rot is re-asserted, and the stored row lands
    at the current code version."""
    ref = _hydrate(store)
    stored = store.pcb_generators_for(ref.id)["ARR1"]
    current = pcb_generators.VERSIONS["ewod_pad_array"]
    assert int(stored["version"]) < current  # the fixture itself is stale
    pose_before = _pose(store, ref.id, "ARR1_SINK_0")
    seed = pcb_generators.expand("ewod_pad_array", "ARR1", dict(stored["params"]))
    emitted = {c["refdes"]: c for c in seed.components}["ARR1_SINK_0"]
    assert (emitted["x"], emitted["y"], emitted.get("rot", 0.0)) != pose_before[:3]

    pcb = PcbHandler(hub=Hub(store=store))
    _reput(pcb, store, ref)

    rows = store.pcb_generators_for(ref.id)
    assert int(rows["ARR1"]["version"]) == current
    assert _pose(store, ref.id, "ARR1_SINK_0") == pose_before


def test_regenerate_reput_still_reseeds(store: Store) -> None:
    """``regenerate`` is the documented explicit reset — "retires the
    whole expansion, placement and routing with it" — so it must NOT
    carry the stored pose forward: the sink lands back on the emitted
    seed (here, refused by the same invariant — proving the reset
    reached the judge rather than silently keeping stale placement)."""
    ref = _hydrate(store)
    params = dict(store.pcb_generators_for(ref.id)["ARR1"]["params"])
    pcb = PcbHandler(hub=Hub(store=store))
    with pytest.raises(BadInput, match="invariant:no_via_in_pad"):
        pcb.put(
            id=str(ref.slug),
            args={
                "generators": [
                    {
                        "name": "ARR1",
                        "generator": "ewod_pad_array",
                        "params": params,
                        "regenerate": True,
                    }
                ]
            },
        )
    # Rolled back wholesale: the stored row still sits at the old version.
    rows = store.pcb_generators_for(ref.id)
    assert int(rows["ARR1"]["version"]) < pcb_generators.VERSIONS["ewod_pad_array"]


def test_no_via_in_pad_still_refuses_a_genuinely_bad_board(store: Store) -> None:
    """Companion to the fix: the same judged surface must still refuse
    when the violation is real. The re-put lands the v6 expansion's
    invariant measures; a foreign-net via then moved onto an electrode
    pad (out of band — the gate on the NEXT put is what is exercised)
    leaves the board's declared ``no_via_in_pad`` violated."""
    ref = _hydrate(store)
    handler = PcbHandler(hub=Hub(store=store))
    _reput(handler, store, ref)  # now carries the invariant measures
    board_id = int(store.pcb_graph(ref.id)["board"]["board_id"])
    via = next(r for r in store.pcb_fixed_copper_list(board_id) if r["ctype"] == "via")
    layers = [
        str(layer["name"]) for layer in store.pcb_load(ref.id)["board"]["stackup"]
    ]
    pad = next(
        p
        for p in handler._drc_pads(ref.id, layers)
        if p.get("refdes") == "ARR1" and p.get("net") != via["net"]
    )
    geom = {
        k: v
        for k, v in via.items()
        if k
        not in (
            "ctype",
            "layer",
            "net",
            "fixed",
            "generator_name",
            "envelope",
            "fixed_id",
        )
    }
    geom.update({"x": float(pad["x"]), "y": float(pad["y"])})
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_fixed_copper SET geom = %s WHERE fixed_id = %s",
            (json.dumps(geom), via["fixed_id"]),
        )
        conn.commit()
    with pytest.raises(BadInput, match="invariant:no_via_in_pad"):
        handler.put(id=str(ref.slug), args={"nets": [{"name": "ZZ", "class": "sig"}]})
    assert "ZZ" not in store.pcb_net_ids(ref.id)
