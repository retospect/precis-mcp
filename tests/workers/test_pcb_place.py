"""``pcb_place`` job_type (pcb-guided-place-route Slice 10) — the enqueued
half of ``put(kind='pcb', args={'op':'place'})``.

Drives ``pcb_place._dispatch`` directly against a minimal fake
``DispatchContext`` (mirrors ``tests/workers/test_embed_batch.py``'s
pattern) rather than a full ``job_inproc`` claim/run cycle — this pins the
job_type's OWN logic (does it actually move instances, respect ``fixed``,
reduce crossings).
"""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.pcb import PcbHandler
from precis.store import Store
from precis.workers.job_types import pcb_place

pytestmark = pytest.mark.db


class _FakeCtx:
    def __init__(self, store: Store, *, params: dict[str, Any]) -> None:
        self.store = store
        ref = store.insert_ref(
            kind="job",
            slug=None,
            title="pcb_place test",
            meta={"executor": "job_inproc", "job_type": "pcb_place"},
        )
        self.ref_id = int(ref.id)
        self.title = "pcb_place test"
        self.meta: dict[str, Any] = {"params": params}
        self.failures: list[tuple[str, str | None]] = []
        self.summaries: list[tuple[str, str]] = []

    def record_failure(self, reason: str, *, failure_class: str | None = None) -> None:
        self.failures.append((reason, failure_class))

    def append_chunk(self, kind: str, text: str) -> None:
        self.summaries.append((kind, text))

    def set_status(self, value: str) -> None:  # pragma: no cover — unused here
        pass

    def set_meta(self, **_kw: Any) -> None:  # pragma: no cover — unused here
        pass

    def is_cancel_requested(self) -> bool:  # pragma: no cover — unused here
        return False


_CROSSED = {
    "components": [
        {"refdes": "A", "label": "ic", "x": 0.0, "y": 0.0, "pins": [{"name": "1"}]},
        {"refdes": "B", "label": "ic", "x": 2.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "C", "label": "ic", "x": 0.0, "y": 2.0, "pins": [{"name": "1"}]},
        {"refdes": "D", "label": "ic", "x": 2.0, "y": 0.0, "pins": [{"name": "1"}]},
    ],
    "nets": [
        {"name": "N1", "class": "signal"},
        {"name": "N2", "class": "signal"},
    ],
    "connections": [
        {"net": "N1", "refdes": "A", "pin": "1"},
        {"net": "N1", "refdes": "B", "pin": "1"},
        {"net": "N2", "refdes": "C", "pin": "1"},
        {"net": "N2", "refdes": "D", "pin": "1"},
    ],
}


def _seed(store: Store, slug: str, args: dict[str, Any]) -> int:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(id=slug, args=args)
    ref = store.get_ref(kind="pcb", id=slug)
    assert ref is not None
    return int(ref.id)


def test_pcb_place_moves_instances_and_reduces_crossings(store: Store) -> None:
    ref_id = _seed(store, "place-x", _CROSSED)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 2000, "seed": 1})
    pcb_place._dispatch(ctx, pcb_place.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    assert ctx.summaries and ctx.summaries[0][0] == "job_summary"

    graph = store.pcb_graph(ref_id)
    placed = {i["refdes"]: (i["x"], i["y"]) for i in graph["instances"]}
    assert all(x is not None and y is not None for x, y in placed.values())

    from precis.pcb import ratsnest

    wires = ratsnest.build_airwires(placed, graph["nets"])
    assert len(ratsnest.crossings(wires)) == 0


def test_pcb_place_never_moves_a_fixed_instance(store: Store) -> None:
    args = {
        "components": [
            {
                "refdes": "J1",
                "label": "conn",
                "x": 0.0,
                "y": 0.0,
                "fixed": "xy",
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U1",
                "label": "ic",
                "x": 40.0,
                "y": 40.0,
                "pins": [{"name": "1"}],
            },
            {
                "refdes": "U2",
                "label": "ic",
                "x": 41.0,
                "y": 40.0,
                "pins": [{"name": "1"}],
            },
        ],
        "nets": [{"name": "N", "class": "signal"}],
        "connections": [
            {"net": "N", "refdes": "U1", "pin": "1"},
            {"net": "N", "refdes": "U2", "pin": "1"},
        ],
    }
    ref_id = _seed(store, "place-fixed", args)
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref_id, "iters": 300, "seed": 2})
    pcb_place._dispatch(ctx, pcb_place.SPEC)  # type: ignore[arg-type]

    assert not ctx.failures
    graph = store.pcb_graph(ref_id)
    j1 = next(i for i in graph["instances"] if i["refdes"] == "J1")
    assert (j1["x"], j1["y"]) == (0.0, 0.0)


def test_pcb_place_fails_legibly_on_empty_design(store: Store) -> None:
    handler = PcbHandler(hub=Hub(store=store))
    handler.put(id="place-empty", args={})
    ref = store.get_ref(kind="pcb", id="place-empty")
    assert ref is not None
    ctx = _FakeCtx(store, params={"pcb_ref_id": ref.id})
    pcb_place._dispatch(ctx, pcb_place.SPEC)  # type: ignore[arg-type]
    assert ctx.failures
    assert "no instances" in ctx.failures[0][0]
