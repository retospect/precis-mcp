"""Dark-factory plan node meta (td542092): ``precis.plans.nodes`` shape
validation, and its wiring into ``TodoHandler.put`` / ``edit(meta=...)``."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler
from precis.plans.nodes import node_kind, validate_plan_meta
from precis.store import Store
from tests.conftest import id_of

SHA = "0123abc456def"
GATE_SHA = {"kind": "sha-in-prod", "args": {"sha": SHA}}
GATE_TESTS = {"kind": "tests-green", "args": {"range": "origin/prod..main"}}
GATE_ANSWER = {"kind": "answer"}
GATE_TS = {"kind": "not-before", "args": {"ts": "2026-10-31T12:00:00Z"}}
LOOP = {"max": 3, "until": GATE_ANSWER, "body": "dogfood-cycle"}
GENERATE = {"role": "slicer", "from": "docs/backlog/x.md#Section"}


# --- pure validation -------------------------------------------------------


@pytest.mark.parametrize(
    ("meta", "kind"),
    [
        ({"op": "plan_tick"}, "task"),
        ({"op": "run_slice"}, "task"),  # pending name, td542094
        ({"op": {"type": "pcb_place", "params": {"board": 1}}}, "task"),
        ({"op": {"type": "plan_tick"}}, "task"),
        ({"auto_close": True}, "phase"),
        ({"gate": GATE_SHA}, "gate"),
        ({"gate": GATE_TESTS}, "gate"),
        ({"gate": GATE_ANSWER}, "gate"),
        ({"gate": {"kind": "answer", "args": {}}}, "gate"),
        ({"gate": GATE_TS}, "gate"),
        ({"loop": LOOP}, "loop"),
        ({"loop": {**LOOP, "count": 0}}, "loop"),  # the tick's iteration count
        ({"loop": {**LOOP, "count": 2}}, "loop"),
        ({"generate": GENERATE}, "generator"),
        ({"generate": {"role": "triage", "from": "gripe"}}, "generator"),
        ({"auto_close": True, "generate": GENERATE}, "generator"),
        ({"auto_close": False}, None),
        ({"llm_tier": "opus", "prio": 3}, None),
        ({}, None),
        (None, None),
    ],
)
def test_accepts_each_kind(meta: dict[str, Any] | None, kind: str | None) -> None:
    assert validate_plan_meta(meta) == []
    assert node_kind(meta) == kind


@pytest.mark.parametrize(
    ("meta", "needle"),
    [
        ({"op": "no_such_job_type"}, "not registered"),
        ({"op": ""}, "not registered"),
        ({"op": 7}, "meta.op must be a string or a dict"),
        ({"op": {"type": "no_such_job_type"}}, "not registered"),
        ({"op": {"params": {}}}, "meta.op.type"),
        ({"op": {"type": "plan_tick", "params": [1]}}, "params must be a dict"),
        ({"op": {"type": "plan_tick", "extra": 1}}, "unknown key"),
        ({"auto_close": "yes"}, "true or false"),
        ({"auto_close": 1}, "true or false"),
        ({"gate": "sha-in-prod"}, "meta.gate must be a dict"),
        ({"gate": {}}, "meta.gate.kind"),
        ({"gate": {"kind": "vibes"}}, "allowed values"),
        ({"gate": {"kind": "sha-in-prod"}}, "takes exactly ['sha']"),
        (
            {"gate": {"kind": "sha-in-prod", "args": {"sha": "nothex!"}}},
            "hex",
        ),
        (
            {"gate": {"kind": "sha-in-prod", "args": {"sha": SHA, "x": 1}}},
            "takes exactly",
        ),
        ({"gate": {"kind": "tests-green", "args": {"range": ""}}}, "non-empty"),
        ({"gate": {"kind": "tests-green", "args": {"sha": SHA}}}, "takes exactly"),
        ({"gate": {"kind": "answer", "args": {"x": "y"}}}, "no keys"),
        ({"gate": {"kind": "not-before", "args": {"ts": "tomorrow"}}}, "ISO-8601"),
        (
            {"gate": {"kind": "not-before", "args": {"ts": "2026-10-31T12:00:00"}}},
            "ISO-8601",
        ),
        ({"gate": {"kind": "answer", "when": 1}}, "unknown key"),
        ({"loop": [1]}, "meta.loop must be a dict"),
        ({"loop": {**LOOP, "max": 0}}, "meta.loop.max"),
        ({"loop": {**LOOP, "max": -2}}, "meta.loop.max"),
        ({"loop": {**LOOP, "max": True}}, "meta.loop.max"),
        ({"loop": {**LOOP, "max": "3"}}, "meta.loop.max"),
        ({"loop": {"max": 3, "body": "t"}}, "meta.loop.until is required"),
        ({"loop": {**LOOP, "until": {"kind": "vibes"}}}, "meta.loop.until.kind"),
        ({"loop": {**LOOP, "until": "answer"}}, "meta.loop.until must be a dict"),
        ({"loop": {**LOOP, "body": ""}}, "meta.loop.body"),
        ({"loop": {k: v for k, v in LOOP.items() if k != "body"}}, "meta.loop.body"),
        ({"loop": {**LOOP, "extra": 1}}, "unknown key"),
        ({"loop": {**LOOP, "count": -1}}, "meta.loop.count"),
        ({"loop": {**LOOP, "count": True}}, "meta.loop.count"),
        ({"loop": {**LOOP, "count": "2"}}, "meta.loop.count"),
        ({"loop": {**LOOP, "count": 1.5}}, "meta.loop.count"),
        ({"generate": "slicer"}, "meta.generate must be a dict"),
        ({"generate": {"role": "poet", "from": "x"}}, "meta.generate.role"),
        ({"generate": {"role": "slicer"}}, "meta.generate.from"),
        ({"generate": {"role": "slicer", "from": ""}}, "meta.generate.from"),
        ({"generate": {**GENERATE, "extra": 1}}, "unknown key"),
    ],
)
def test_refuses_malformed_shapes(meta: dict[str, Any], needle: str) -> None:
    problems = validate_plan_meta(meta)
    assert problems, f"{meta!r} was accepted"
    assert needle in " | ".join(problems)


@pytest.mark.parametrize(
    "meta",
    [
        {"gate": GATE_SHA, "loop": LOOP},
        {"gate": GATE_SHA, "generate": GENERATE},
        {"loop": LOOP, "generate": GENERATE},
        {"gate": GATE_SHA, "loop": LOOP, "generate": GENERATE},
    ],
)
def test_refuses_two_of_gate_loop_generate(meta: dict[str, Any]) -> None:
    problems = validate_plan_meta(meta)
    assert any("at most one of gate / loop / generate" in p for p in problems)


def test_node_kind_precedence() -> None:
    # gate/loop/generate beat auto_close; auto_close beats op.
    assert node_kind({"gate": GATE_SHA, "auto_close": True, "op": "plan_tick"}) == (
        "gate"
    )
    assert node_kind({"auto_close": True, "op": "plan_tick"}) == "phase"


# --- handler wiring --------------------------------------------------------


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _root(handler: TodoHandler) -> int:
    return id_of(handler.put(text="Plan root.", meta={"rotation_root": True}).body)


def _child(handler: TodoHandler, root: int, text: str, **meta: Any) -> int:
    return id_of(
        handler.put(text=text, parent_id=root, meta={"llm_tier": None, **meta}).body
    )


def _meta(store: Store, todo_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=todo_id)
    assert ref is not None, todo_id
    return ref.meta or {}


def test_put_accepts_plan_keys(handler: TodoHandler, store: Store) -> None:
    root = _root(handler)
    task = _child(handler, root, "Build it.", op={"type": "run_slice", "params": {}})
    phase = _child(handler, root, "Phase A.", auto_close=True)
    gate = _child(handler, root, "Wait for prod.", gate=GATE_SHA)
    loop = _child(handler, root, "Cycle.", loop=LOOP)
    gen = _child(handler, root, "Slice it.", generate=GENERATE)
    assert _meta(store, task)["op"] == {"type": "run_slice", "params": {}}
    assert _meta(store, phase)["auto_close"] is True
    assert _meta(store, gate)["gate"] == GATE_SHA
    assert _meta(store, loop)["loop"] == LOOP
    assert _meta(store, gen)["generate"] == GENERATE
    # existing behaviour unchanged: the opt-out llm_tier is not stored
    assert "llm_tier" not in _meta(store, gate)


def test_put_refuses_bad_shape_with_next(handler: TodoHandler) -> None:
    root = _root(handler)
    with pytest.raises(BadInput) as exc:
        handler.put(
            text="Bad gate.",
            parent_id=root,
            meta={"llm_tier": None, "gate": {"kind": "vibes"}},
        )
    assert "meta.gate.kind" in exc.value.cause
    assert isinstance(exc.value.next, str)
    assert "meta.gate:" in exc.value.next
    assert "sha-in-prod" in exc.value.next


def test_put_refuses_unregistered_op(handler: TodoHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(text="Bad op.", meta={"rotation_root": True, "op": "nope"})
    assert "not registered" in exc.value.cause
    assert isinstance(exc.value.next, str)
    assert "meta.op:" in exc.value.next


def test_put_refuses_two_exclusive_keys(handler: TodoHandler) -> None:
    root = _root(handler)
    with pytest.raises(BadInput) as exc:
        handler.put(
            text="Both.",
            parent_id=root,
            meta={"llm_tier": None, "gate": GATE_SHA, "loop": LOOP},
        )
    assert "at most one" in exc.value.cause
    assert isinstance(exc.value.next, str)
    assert "meta.gate:" in exc.value.next
    assert "meta.loop:" in exc.value.next


def test_edit_sets_replaces_and_clears_plan_keys(
    handler: TodoHandler, store: Store
) -> None:
    root = _root(handler)
    todo = _child(handler, root, "Plain task.")
    handler.edit(id=todo, meta={"op": "plan_tick", "auto_close": True})
    meta = _meta(store, todo)
    assert (meta["op"], meta["auto_close"]) == ("plan_tick", True)

    handler.edit(id=todo, meta={"gate": GATE_TS})
    assert _meta(store, todo)["gate"] == GATE_TS
    handler.edit(id=todo, meta={"gate": GATE_ANSWER})
    assert _meta(store, todo)["gate"] == GATE_ANSWER

    # replacing the one exclusive key with another needs the first cleared
    handler.edit(id=todo, meta={"gate": None, "loop": LOOP})
    meta = _meta(store, todo)
    assert "gate" not in meta
    assert meta["loop"] == LOOP
    assert meta["op"] == "plan_tick"  # untouched keys survive


def test_edit_refuses_bad_shape_with_next(handler: TodoHandler, store: Store) -> None:
    root = _root(handler)
    todo = _child(handler, root, "Plain task.")
    with pytest.raises(BadInput) as exc:
        handler.edit(id=todo, meta={"loop": {"max": 0, "until": GATE_ANSWER}})
    assert "meta.loop.max" in exc.value.cause
    assert isinstance(exc.value.next, str)
    assert "meta.loop:" in exc.value.next
    assert "loop" not in _meta(store, todo)


def test_edit_refuses_second_exclusive_key(handler: TodoHandler, store: Store) -> None:
    root = _root(handler)
    todo = _child(handler, root, "A gate.", gate=GATE_SHA)
    with pytest.raises(BadInput) as exc:
        handler.edit(id=todo, meta={"generate": GENERATE})
    assert "at most one" in exc.value.cause
    assert "generate" not in _meta(store, todo)


def test_edit_still_refuses_unknown_key_and_names_plan_keys(
    handler: TodoHandler,
) -> None:
    root = _root(handler)
    todo = _child(handler, root, "Plain task.")
    with pytest.raises(BadInput) as exc:
        handler.edit(id=todo, meta={"bogus": 1})
    assert "'gate'" in exc.value.cause
    assert "auto_close" in exc.value.cause
