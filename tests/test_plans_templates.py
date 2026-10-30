"""Dark-factory plan templates (td542095): ``precis.plans.templates`` minting
todo subtrees, ``put(kind='todo', mode='plan')``, and the parented-put
``llm_tier`` default skipped for non-task plan nodes."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import TodoHandler
from precis.plans.nodes import node_kind, validate_plan_meta
from precis.plans.templates import TEMPLATES, PlanMint
from precis.store import Store
from tests.conftest import id_of

GATE_SHA = {"kind": "sha-in-prod", "args": {"sha": "0123abc"}}


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _gripe(handler: TodoHandler) -> int:
    return id_of(GripeHandler(hub=handler.hub).put(text="the widget leaks").body)


def _meta(store: Store, todo_id: int) -> dict[str, Any]:
    ref = store.get_ref(kind="todo", id=todo_id)
    assert ref is not None
    return dict(ref.meta or {})


def _blocked_by(store: Store, mint: PlanMint) -> set[tuple[str, str]]:
    """``(blocked key, blocker key)`` pairs among the minted nodes."""
    by_id = {v: k for k, v in mint.ids.items()}
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT src_ref_id, dst_ref_id FROM links "
            "WHERE relation = 'blocked-by' AND src_ref_id = ANY(%s)",
            (list(mint.ids.values()),),
        ).fetchall()
    return {(by_id[s], by_id[d]) for s, d in rows}


def _kinds(store: Store, mint: PlanMint) -> dict[str, str | None]:
    return {k: node_kind(_meta(store, i)) for k, i in mint.ids.items()}


def _parent_of(store: Store, todo_id: int) -> int | None:
    ref = store.get_ref(kind="todo", id=todo_id)
    assert ref is not None
    return ref.parent_id


def _assert_plan_hygiene(store: Store, mint: PlanMint) -> None:
    """Every node validates, carries no llm_tier, is tagged plan:<root>."""
    assert mint.root_id == mint.ids["root"]
    for key, todo_id in mint.ids.items():
        meta = _meta(store, todo_id)
        assert validate_plan_meta(meta) == [], key
        assert "llm_tier" not in meta, key
        tags = {t.value for t in store.tags_for(todo_id)}
        assert f"plan:{mint.root_id}" in tags, key
        assert "STATUS:open" in {
            f"{t.prefix}:{t.value}" for t in store.tags_for(todo_id)
        }, key


def test_registry_has_the_four_templates() -> None:
    assert sorted(TEMPLATES) == [
        "gripe-fix",
        "pcb-pipeline",
        "release-pass",
        "slice-tree",
    ]
    assert TEMPLATES["gripe-fix"].required == ("gripe_id",)
    assert TEMPLATES["release-pass"].required == ()


def test_gripe_fix_shape(handler: TodoHandler, store: Store) -> None:
    gripe = _gripe(handler)
    mint = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=gripe)
    assert set(mint.ids) == {"root", "triage", "brief", "build", "gate", "validate"}
    assert _kinds(store, mint) == {
        "root": "phase",
        "triage": "generator",
        "brief": "task",
        "build": "task",
        "gate": "gate",
        "validate": "task",
    }
    assert _blocked_by(store, mint) == {
        ("brief", "triage"),
        ("build", "brief"),
        ("gate", "build"),
        ("validate", "gate"),
    }
    assert _meta(store, mint.ids["build"])["op"] == "run_slice"
    assert _meta(store, mint.ids["gate"])["gate"] == {
        "kind": "sha-in-prod",
        "args": {"sha": "0000000"},
    }
    assert all(
        _parent_of(store, i) == mint.root_id for k, i in mint.ids.items() if k != "root"
    )
    _assert_plan_hygiene(store, mint)
    # The root is related-to the gripe.
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s AND dst_ref_id = %s "
            "AND relation = 'related-to'",
            (mint.root_id, gripe),
        ).fetchone()
    assert row is not None and row[0] == 1


def test_gripe_fix_gate_carries_the_placeholder_comment(
    handler: TodoHandler, store: Store
) -> None:
    mint = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=_gripe(handler))
    out = handler.get(id=mint.ids["gate"], view="comments").body
    assert "placeholder" in out
    assert "land" in out


def test_gripe_fix_accepts_handle_spelling_and_refuses_unknown_gripe(
    handler: TodoHandler, store: Store
) -> None:
    gripe = _gripe(handler)
    mint = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=f"gr{gripe}")
    assert mint.root_id
    with pytest.raises(BadInput, match="not found"):
        TEMPLATES["gripe-fix"].mint(store, None, gripe_id=gripe + 999_999)
    with pytest.raises(BadInput, match="not a gripe id"):
        TEMPLATES["gripe-fix"].mint(store, None, gripe_id="soon")


def test_slice_tree_shape(store: Store) -> None:
    mint = TEMPLATES["slice-tree"].mint(
        store, None, section="main/docs/backlog/x.md~Heading"
    )
    assert _kinds(store, mint) == {
        "root": "phase",
        "spec": "generator",
        "slicer": "generator",
        "review": "task",
    }
    assert _blocked_by(store, mint) == {("slicer", "spec"), ("review", "slicer")}
    assert _meta(store, mint.ids["slicer"])["generate"] == {
        "role": "slicer",
        "from": "main/docs/backlog/x.md~Heading",
    }
    _assert_plan_hygiene(store, mint)


def test_release_pass_shape(store: Store) -> None:
    mint = TEMPLATES["release-pass"].mint(store, None)
    assert _kinds(store, mint) == {
        "root": "phase",
        "cut": "task",
        "gate": "gate",
        "loop": "loop",
    }
    assert _blocked_by(store, mint) == {("gate", "cut"), ("loop", "cut")}
    loop = _meta(store, mint.ids["loop"])["loop"]
    assert loop["until"] == _meta(store, mint.ids["gate"])["gate"]
    assert loop["max"] == 3
    _assert_plan_hygiene(store, mint)


def test_pcb_pipeline_shape(store: Store) -> None:
    mint = TEMPLATES["pcb-pipeline"].mint(store, None, board="pcb:amp")
    assert _kinds(store, mint) == {
        "root": "phase",
        "place": "task",
        "loop": "loop",
        "route": "task",
        "drc": "task",
        "review": "generator",
    }
    assert _blocked_by(store, mint) == {
        ("loop", "place"),
        ("drc", "route"),
        ("review", "drc"),
    }
    assert _meta(store, mint.ids["place"])["op"]["type"] == "pcb_place"
    for key in ("route", "drc", "review"):
        assert _parent_of(store, mint.ids[key]) == mint.ids["loop"]
    _assert_plan_hygiene(store, mint)


def test_template_mints_under_a_parent(handler: TodoHandler, store: Store) -> None:
    parent = id_of(handler.put(text="Umbrella.", meta={"rotation_root": True}).body)
    mint = TEMPLATES["release-pass"].mint(store, parent)
    assert _parent_of(store, mint.root_id) == parent
    tags = {t.value for t in store.tags_for(mint.root_id)}
    assert f"plan:{mint.root_id}" in tags
    assert f"plan:{parent}" not in tags


# --- put(kind='todo', mode='plan') ----------------------------------------


def test_put_mode_plan_round_trip(handler: TodoHandler, store: Store) -> None:
    gripe = _gripe(handler)
    resp = handler.put(mode="plan", template="gripe-fix", gripe_id=gripe)
    root = id_of(resp.body)
    assert "gripe-fix" in resp.body
    assert "Triage" in resp.body  # the tree view's rows are in the ack
    assert "Validate" in resp.body
    assert _parent_of(store, root) is None
    tree = handler.get(id=root, view="tree").body
    assert "Build the fix" in tree


def test_put_mode_plan_under_parent(handler: TodoHandler, store: Store) -> None:
    parent = id_of(handler.put(text="Umbrella.", meta={"rotation_root": True}).body)
    resp = handler.put(mode="plan", template="release-pass", parent_id=parent)
    root = id_of(resp.body)
    assert _parent_of(store, root) == parent


def test_put_mode_plan_refuses_unknown_template(handler: TodoHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(mode="plan", template="no-such-template")
    msg = str(exc.value)
    for name in sorted(TEMPLATES):
        assert name in msg


def test_put_mode_plan_refuses_missing_argument(handler: TodoHandler) -> None:
    with pytest.raises(BadInput, match="gripe_id"):
        handler.put(mode="plan", template="gripe-fix")
    with pytest.raises(BadInput, match="board"):
        handler.put(mode="plan", template="pcb-pipeline")


def test_put_mode_plan_refuses_free_text(handler: TodoHandler) -> None:
    with pytest.raises(BadInput, match="text"):
        handler.put(mode="plan", template="release-pass", text="hello")


# --- parented put: llm_tier default ----------------------------------------


def _tier_after_parented_put(handler: TodoHandler, store: Store, **meta: Any) -> Any:
    root = id_of(handler.put(text="Root.", meta={"rotation_root": True}).body)
    child = id_of(handler.put(text="Child.", parent_id=root, meta=meta).body)
    return _meta(store, child).get("llm_tier")


@pytest.mark.parametrize(
    "meta",
    [
        {"gate": GATE_SHA},
        {"loop": {"max": 2, "until": GATE_SHA, "body": "release-fix"}},
        {"generate": {"role": "slicer", "from": "x.md~H"}},
        {"auto_close": True},
    ],
    ids=["gate", "loop", "generator", "phase"],
)
def test_parented_put_skips_llm_tier_for_non_task_plan_nodes(
    handler: TodoHandler, store: Store, meta: dict[str, Any]
) -> None:
    assert _tier_after_parented_put(handler, store, **meta) is None


def test_parented_put_keeps_llm_tier_default_for_task_and_plain(
    handler: TodoHandler, store: Store
) -> None:
    assert _tier_after_parented_put(handler, store, op="run_slice") == "opus"
    assert _tier_after_parented_put(handler, store) == "opus"
