"""gr345859 — docs/backlog/pcb-missing-constraint-classes.md §F (ruled
2026-10-12): ``ewod_pad_array`` declares its five fabric invariants as
``pcb_measures`` rows at ``strength='invariant'``; ``view='measures'``
evaluates them, and a violated one refuses put/route through the
always-valid gate while ``hard`` keeps steering."""

from __future__ import annotations

import json
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.pcb import PcbHandler
from precis.pcb import eyes as pcb_eyes
from precis.pcb import generators
from precis.pcb.optimize import resolve_measures
from precis.store import Store
from precis.workers.job_types import pcb_route

_6X6 = {"grid": [6, 6]}


@pytest.fixture
def pcb(store: Store) -> PcbHandler:
    return PcbHandler(hub=Hub(store=store))


def _seed(pcb: PcbHandler, store: Store, slug: str = "inv") -> tuple[Any, int]:
    pcb.put(
        id=slug,
        args={
            "generators": [
                {
                    "name": "ARR1",
                    "generator": "ewod_pad_array",
                    "params": dict(_6X6),
                }
            ]
        },
    )
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    board_id = int(store.pcb_graph(ref.id)["board"]["board_id"])
    return ref, board_id


def _evaluated(pcb: PcbHandler, store: Store, ref_id: int) -> dict[str, dict[str, Any]]:
    """view='measures''s own evaluation path -> rows keyed by metric."""
    graph = store.pcb_graph(ref_id)
    measures = store.pcb_measures_list(ref_id)
    results = pcb_eyes.evaluate_measures(
        graph,
        measures,
        store.pcb_features_list(ref_id),
        fabric=pcb._measures_fabric(ref_id, measures),
    )
    return {r["metric"]: r for r in results}


def _fixed_vias(store: Store, board_id: int) -> list[dict[str, Any]]:
    return [r for r in store.pcb_fixed_copper_list(board_id) if r["ctype"] == "via"]


def _via_geom(via: dict[str, Any]) -> dict[str, Any]:
    skip = ("ctype", "layer", "net", "fixed", "generator_name", "envelope", "fixed_id")
    return {k: v for k, v in via.items() if k not in skip}


def _move_via(store: Store, via: dict[str, Any], x: float, y: float) -> None:
    """A hand-edit: rewrite one fixed via's position out of band (what a
    manual copper fix does), bypassing the gate on purpose — the NEXT put
    is what must refuse."""
    geom = {**_via_geom(via), "x": x, "y": y}
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_fixed_copper SET geom = %s WHERE fixed_id = %s",
            (json.dumps(geom), via["fixed_id"]),
        )
        conn.commit()


def _retire_via(store: Store, via: dict[str, Any]) -> None:
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_fixed_copper SET retired_at = now() WHERE fixed_id = %s",
            (via["fixed_id"],),
        )
        conn.commit()


def _electrode_pad(pcb: PcbHandler, ref_id: int, *, not_net: str) -> dict[str, Any]:
    """A real ARR1 pad whose pin's net is not ``not_net`` (so the via put
    on it is foreign-net and the same-net authored-drop exemption can't
    excuse it)."""
    board = pcb.store.pcb_load(ref_id)["board"]
    layers = [str(layer["name"]) for layer in board["stackup"]]
    pads = [
        p
        for p in pcb._drc_pads(ref_id, layers)
        if str(p.get("refdes")) == "ARR1" and p.get("net") != not_net
    ]
    assert pads, "fixture must carry electrode pads"
    return pads[0]


def test_generator_declares_all_five_invariants(pcb: PcbHandler, store: Store) -> None:
    ref, _ = _seed(pcb, store)
    by_metric = {m["metric"]: m for m in store.pcb_measures_list(ref.id)}
    assert set(generators.INVARIANT_METRICS) <= set(by_metric)
    for metric in generators.INVARIANT_METRICS:
        row = by_metric[metric]
        assert row["strength"] == "invariant"
        assert (row.get("meta") or {}).get("generator") == "ARR1"
        assert {"generator": "ARR1"} in (row.get("operands") or [])


def test_measures_view_lists_five_invariants_ok_with_values(
    pcb: PcbHandler, store: Store
) -> None:
    """The §F acceptance, half one: ewod-dogfood-6's view='measures' lists
    all five with an actual value and a verdict."""
    ref, _ = _seed(pcb, store)
    body = pcb.get(id="inv", view="measures").body
    for metric in generators.INVARIANT_METRICS:
        assert metric in body
    rows = _evaluated(pcb, store, ref.id)
    for metric in generators.INVARIANT_METRICS:
        assert rows[metric]["verdict"] == "ok", rows[metric]
        assert rows[metric]["value"] is not None
    params = store.pcb_generators_for(ref.id)["ARR1"]["params"]
    assert rows["hv_pitch_floor"]["value"] == pytest.approx(float(params["pitch"]))
    assert rows["via_per_electrode"]["value"] == 0


def test_same_params_reapply_keeps_exactly_one_set(
    pcb: PcbHandler, store: Store
) -> None:
    ref, _ = _seed(pcb, store)
    pcb.put(
        id="inv",
        args={
            "generators": [
                {"name": "ARR1", "generator": "ewod_pad_array", "params": dict(_6X6)}
            ]
        },
    )
    rows = store.pcb_measures_list(ref.id)
    assert len(rows) == 5  # no-op expansion inserts nothing twice


def test_changed_params_reapply_replaces_the_generator_measures(
    pcb: PcbHandler, store: Store
) -> None:
    ref, _ = _seed(pcb, store)
    pcb.put(
        id="inv",
        args={
            "generators": [
                {
                    "name": "ARR1",
                    "generator": "ewod_pad_array",
                    "params": {"grid": [4, 4]},
                }
            ]
        },
    )
    rows = store.pcb_measures_list(ref.id)
    assert len(rows) == 5
    assert all((m.get("meta") or {}).get("generator") == "ARR1" for m in rows)


def test_via_moved_into_pad_violates_and_the_next_put_refuses(
    pcb: PcbHandler, store: Store
) -> None:
    """The §F acceptance, half two: a via inside a pad reads VIOLATED in
    view='measures' and the next put refuses naming the measure."""
    ref, board_id = _seed(pcb, store)
    via = _fixed_vias(store, board_id)[0]
    pad = _electrode_pad(pcb, ref.id, not_net=str(via["net"]))
    _move_via(store, via, float(pad["x"]), float(pad["y"]))

    rows = _evaluated(pcb, store, ref.id)
    assert rows["no_via_in_pad"]["verdict"] == "VIOLATED"
    assert rows["no_via_in_pad"]["value"] >= 1

    with pytest.raises(BadInput, match="invariant:no_via_in_pad"):
        pcb.put(id="inv", args={"nets": [{"name": "ZZ", "class": "sig"}]})
    assert "ZZ" not in store.pcb_net_ids(ref.id)


def test_missing_via_violates_via_per_electrode_and_refuses(
    pcb: PcbHandler, store: Store
) -> None:
    """A violation no GEOMETRIC rule sees (the free space left where a via
    was retired): proves the measure itself gates, not an aliased DRC
    finding."""
    ref, board_id = _seed(pcb, store)
    _retire_via(store, _fixed_vias(store, board_id)[0])

    rows = _evaluated(pcb, store, ref.id)
    assert rows["via_per_electrode"]["verdict"] == "VIOLATED"
    assert rows["via_per_electrode"]["value"] == 1

    with pytest.raises(BadInput, match="invariant:via_per_electrode"):
        pcb.put(id="inv", args={"nets": [{"name": "ZZ", "class": "sig"}]})
    assert "ZZ" not in store.pcb_net_ids(ref.id)


def test_stub_stretched_past_class_length_refuses(
    pcb: PcbHandler, store: Store
) -> None:
    ref, board_id = _seed(pcb, store)
    stubs = [
        r
        for r in store.pcb_fixed_copper_list(board_id)
        if r["ctype"] == "track" and r.get("generator_name") == "ARR1"
    ]
    assert stubs
    stub = stubs[0]
    seg = dict(stub["segments"][0])
    seg["end"] = [float(seg["end"][0]) + 1.0, float(seg["end"][1])]
    geom = {k: v for k, v in stub.items() if k in ("segments", "width_mm")}
    geom["segments"] = [seg]
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE pcb_fixed_copper SET geom = %s WHERE fixed_id = %s",
            (json.dumps(geom), stub["fixed_id"]),
        )
        conn.commit()

    rows = _evaluated(pcb, store, ref.id)
    assert rows["stub_length_match"]["verdict"] == "VIOLATED"
    assert (
        rows["stub_length_match"]["value"] > 0.1
    )  # nominal is exact; +1mm lands ~0.85
    with pytest.raises(BadInput, match="invariant:stub_length_match"):
        pcb.put(id="inv", args={"nets": [{"name": "ZZ", "class": "sig"}]})


def test_authored_invariant_strength_gates_too(pcb: PcbHandler, store: Store) -> None:
    """The strength is generic: an authored separation measure at
    'invariant' that is already violated refuses the put that adds it
    (the after-state carries the violation — an already-false invariant
    cannot be declared)."""
    ref, _ = _seed(pcb, store)
    pcb.put(
        id="inv",
        args={
            "footprints": [
                {
                    "name": "padp",
                    "pads": [
                        {
                            "pin": "1",
                            "shape": "rect",
                            "x": 0.0,
                            "y": 0.0,
                            "w": 1.0,
                            "h": 1.0,
                        }
                    ],
                }
            ],
            "components": [
                {
                    "refdes": "P1",
                    "label": "p",
                    "footprint": "padp",
                    "x": 300.0,
                    "y": 300.0,
                    "pins": [{"name": "1"}],
                },
                {
                    "refdes": "P2",
                    "label": "p",
                    "footprint": "padp",
                    "x": 310.0,
                    "y": 300.0,
                    "pins": [{"name": "1"}],
                },
            ],
        },
    )
    with pytest.raises(BadInput, match="invariant:separation"):
        pcb.put(
            id="inv",
            args={
                "measures": [
                    {
                        "metric": "separation",
                        "operands": [{"instance": "P1"}, {"instance": "P2"}],
                        "goal": 2000.0,
                        "strength": "invariant",
                        "reason": "P1..P2 are ~10mm apart, far under 2000",
                    }
                ]
            },
        )
    # The refused put rolled back: only the five generator rows remain.
    assert len(store.pcb_measures_list(ref.id)) == 5


def test_invariant_strength_never_steers(store: Store) -> None:
    """``hard`` keeps steering (resolve_measures emits it, priced ~40x);
    ``invariant`` resolves to nothing — it gates, it is never an anneal
    cost term."""
    inv = {
        "metric": "separation",
        "strength": "invariant",
        "operands": [{"instance": "U1"}, {"instance": "U2"}],
        "goal": 10.0,
    }
    assert resolve_measures([inv]) == ()
    hard = {**inv, "strength": "hard"}
    specs = resolve_measures([hard])
    assert len(specs) == 1 and specs[0].hard


def test_evaluator_is_pending_without_a_generator_binding() -> None:
    res = generators.evaluate_invariant(
        {"metric": "hv_pitch_floor", "operands": [{"generator": "GONE"}]},
        generators={},
        pads=[],
        copper=[],
        layers=[],
    )
    assert res is None


def test_hv_pitch_floor_evaluator() -> None:
    exp = generators.expand("ewod_pad_array", "G1", dict(_6X6))
    row = {"params": exp.canonical_params, "ledger": exp.ledger}
    res = generators.evaluate_invariant(
        {"metric": "hv_pitch_floor", "meta": {"generator": "G1"}},
        generators={"G1": row},
        pads=[],
        copper=[],
        layers=[],
    )
    assert res is not None and res["ok"]
    bad = generators.evaluate_invariant(
        {"metric": "hv_pitch_floor", "meta": {"generator": "G1"}},
        generators={
            "G1": {
                "params": {
                    **exp.canonical_params,
                    "pitch": float(exp.canonical_params["min_pitch"]) - 0.5,
                },
                "ledger": exp.ledger,
            }
        },
        pads=[],
        copper=[],
        layers=[],
    )
    assert bad is not None and not bad["ok"]


def test_pad_covers_plaza_evaluator() -> None:
    exp = generators.expand("ewod_pad_array", "G1", {"grid": [3, 3]})
    row = {"params": exp.canonical_params, "ledger": exp.ledger}
    # A pad's own real polygons do not cover the (1,1) plaza cell at (0,0).
    pads = [{**p, "refdes": "G1"} for p in exp.footprints[0]["pads"]]
    ok = generators.evaluate_invariant(
        {"metric": "pad_covers_plaza", "meta": {"generator": "G1"}},
        generators={"G1": row},
        pads=pads,
        copper=[],
        layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    )
    assert ok is not None and ok["ok"]
    # A fabricated pad polygon over the plaza's pitch-square violates.
    covering = pads + [
        {
            "refdes": "G1",
            "pin": "merged",
            "shape": "polygon",
            "poly": [[-1.2, -1.2], [1.2, -1.2], [1.2, 1.2], [-1.2, 1.2]],
            "layer": "F.Cu",
        }
    ]
    bad = generators.evaluate_invariant(
        {"metric": "pad_covers_plaza", "meta": {"generator": "G1"}},
        generators={"G1": row},
        pads=covering,
        copper=[],
        layers=["F.Cu", "In1.Cu", "In2.Cu", "B.Cu"],
    )
    assert bad is not None and not bad["ok"] and bad["value"] == 1


def test_route_refuses_on_a_violated_invariant(pcb: PcbHandler, store: Store) -> None:
    """'invariant' gates route too (the route job's pre-route gate): the
    job fails 'input' naming the measure, and nothing is written."""
    ref, board_id = _seed(pcb, store)
    _retire_via(store, _fixed_vias(store, board_id)[0])

    ctx = _RouteCtx(store, params={"pcb_ref_id": ref.id, "iters": 10, "seed": 1})
    pcb_route._dispatch(cast(Any, ctx), pcb_route.SPEC)

    assert ctx.failures, "a violated invariant must refuse the route"
    reason, failure_class = ctx.failures[0]
    assert failure_class == "input"
    assert "refusing to route" in reason
    assert "invariant:via_per_electrode" in reason


class _RouteCtx:
    """The job ctx shape pcb_route._dispatch reads (mirrors
    tests/test_pcb_route_placement_gate_epro.py's own stub)."""

    def __init__(self, store: Store, *, params: dict[str, Any]) -> None:
        self.store = store
        self.ref_id = 0
        self.title = "invariant route test"
        self.meta = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:
        pass

    def set_meta(self, **_kw: Any) -> None:
        pass

    def is_cancel_requested(self) -> bool:
        return False
