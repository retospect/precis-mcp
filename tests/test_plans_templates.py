"""Dark-factory plan templates (td542095): ``precis.plans.templates`` minting
todo subtrees, ``put(kind='todo', mode='plan')``, and the parented-put
``llm_tier`` default skipped for non-task plan nodes."""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import TodoHandler
from precis.plans import lanes
from precis.plans.nodes import node_kind, validate_plan_meta
from precis.plans.templates import (
    BRIEF_TASK_CLASS,
    PCB_TASK_CLASS,
    TEMPLATES,
    VALIDATE_TASK_CLASS,
    PlanMint,
)
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


def _body(store: Store, todo_id: int) -> str:
    return "\n".join(
        str(c.text)
        for c in store.chunks.list_chunks_for_ref(todo_id)
        if c.chunk_kind == "todo_body"
    )


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


def test_registry_has_the_seven_templates() -> None:
    assert sorted(TEMPLATES) == [
        "gripe-fix",
        "gripe-fix-direct",
        "pcb-pipeline",
        "pr-intake",
        "release-pass",
        "route-loop",
        "slice-tree",
    ]
    assert TEMPLATES["gripe-fix"].required == ("gripe_id",)
    assert TEMPLATES["release-pass"].required == ()


def test_gripe_fix_direct_shape(handler: TodoHandler, store: Store) -> None:
    """td564019 (Reto 2026-11-04: the codex chain runs independent of
    Claude): no triage, no brief; the build is the first node and carries
    the gripe's text, so a lane without the precis MCP can read it; review,
    gate and validate follow the land as in the staged form."""
    gripe = _gripe(handler)
    mint = TEMPLATES["gripe-fix-direct"].mint(store, None, gripe_id=gripe)
    assert set(mint.ids) == {"root", "build", "review", "gate", "validate"}
    assert _kinds(store, mint) == {
        "root": "phase",
        "build": "task",
        "review": "generator",
        "gate": "gate",
        "validate": "task",
    }
    assert _blocked_by(store, mint) == {
        ("review", "build"),
        ("gate", "build"),
        ("validate", "gate"),
    }
    build = _meta(store, mint.ids["build"])
    assert build["op"] == "run_slice" and "task_class" not in build
    body = _body(store, mint.ids["build"])
    assert f"gr{gripe}" in body and "the widget leaks" in body
    assert "no triage and no brief" in body
    assert _meta(store, mint.ids["validate"])["task_class"] == VALIDATE_TASK_CLASS
    assert _meta(store, mint.ids["review"])["task_class"] == "review"
    # td564019 cut 1: the validate may run on an MCP-less lane too, so its
    # body carries the gripe report as the build's does.
    validate = _body(store, mint.ids["validate"])
    assert f"gr{gripe}" in validate and "the widget leaks" in validate
    assert "validated: <why>" in validate
    # The staged form is untouched by the split.
    staged = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=gripe)
    assert {"triage", "brief"} <= set(staged.ids)


def test_gripe_fix_lane_pins_the_build(handler: TodoHandler, store: Store) -> None:
    """``lane`` rides into the build's ``meta.op.params.lane`` (``run_slice``
    honours the pin before the class pick) in both forms; the handler passes
    it through as an optional template arg."""
    gripe = _gripe(handler)
    mint = TEMPLATES["gripe-fix-direct"].mint(store, None, gripe_id=gripe, lane="codex")
    assert _meta(store, mint.ids["build"])["op"] == {
        "type": "run_slice",
        "params": {"lane": "codex"},
    }
    staged = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=gripe, lane="codex")
    assert _meta(store, staged.ids["build"])["op"]["params"] == {"lane": "codex"}
    assert TEMPLATES["gripe-fix-direct"].optional == ("lane",)
    assert TEMPLATES["gripe-fix"].optional == ("lane",)


def test_gripe_fix_shape(handler: TodoHandler, store: Store) -> None:
    gripe = _gripe(handler)
    mint = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=gripe)
    assert set(mint.ids) == {
        "root",
        "triage",
        "brief",
        "build",
        "review",
        "gate",
        "validate",
    }
    assert _kinds(store, mint) == {
        "root": "phase",
        "triage": "generator",
        "brief": "task",
        "build": "task",
        "review": "generator",
        "gate": "gate",
        "validate": "task",
    }
    # The review is blocked by the build only, and blocks nothing: the
    # gate and the validate never wait on it.
    assert _blocked_by(store, mint) == {
        ("brief", "triage"),
        ("build", "brief"),
        ("review", "build"),
        ("gate", "build"),
        ("validate", "gate"),
    }
    assert _meta(store, mint.ids["build"])["op"] == "run_slice"
    review = _meta(store, mint.ids["review"])
    assert review["generate"] == {"role": "reviewer", "from": f"td{mint.ids['build']}"}
    assert review["task_class"] == "review"
    # Only the build is plain ``code`` work; the other sessions work through
    # the precis MCP and carry a class the MCP-less codex lane does not fit.
    assert "task_class" not in _meta(store, mint.ids["build"])
    assert _meta(store, mint.ids["triage"])["task_class"] == "triage"
    assert _meta(store, mint.ids["brief"])["task_class"] == BRIEF_TASK_CLASS
    assert _meta(store, mint.ids["validate"])["task_class"] == VALIDATE_TASK_CLASS
    assert _body(store, mint.ids["review"]).count("review: LAND") == 1
    assert "Mint nothing yourself" in _body(store, mint.ids["review"])
    assert f"td{mint.ids['build']}" in _body(store, mint.ids["review"])
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


def test_mcp_bound_nodes_never_fit_the_codex_lane(
    handler: TodoHandler, store: Store
) -> None:
    """td562568: with the codex lane on (no MCP), a node whose session mints
    children or posts through the precis MCP must carry a class codex does
    not fit — unclassed it reads ``code`` and would launch there. Every
    generator is classed by its role, the gripe-fix brief and the pcb-fix
    fix by their step; the builds stay ``code``. td564019 cut 1: a review
    and a validate deliver one verdict in the final message and need no
    MCP, so codex fits those two classes as well."""
    codex = lanes.get_lane("codex", store)
    sub = lanes.get_lane("claude-sub", store)
    assert codex is not None and sub is not None
    gripe = _gripe(handler)
    mints = {
        "gripe-fix": TEMPLATES["gripe-fix"].mint(store, None, gripe_id=gripe),
        "slice-tree": TEMPLATES["slice-tree"].mint(
            store, None, section="main/docs/backlog/x.md~H"
        ),
    }
    code_only = {("gripe-fix", "build")}
    # gr573563 (0284): codex fits only review and validate — a lane that
    # cannot run the test gate takes no ``code``.
    verdict_classes = {"review", VALIDATE_TASK_CLASS}
    seen: set[str] = set()
    for name, mint in mints.items():
        for key, todo_id in mint.ids.items():
            meta = _meta(store, todo_id)
            kind = node_kind(meta)
            if kind not in ("task", "generator"):
                continue
            cls = str(meta.get("task_class") or "code")
            seen.add(cls)
            assert sub.fits(cls), (name, key)
            if (name, key) in code_only:
                assert cls == "code", (name, key)
            assert codex.fits(cls) == (cls in verdict_classes), (name, key, cls)
            if kind == "generator":
                assert cls in (meta["generate"]["role"], "review"), (name, key)
    assert {"triage", "brief", "review", VALIDATE_TASK_CLASS} <= seen
    assert PCB_TASK_CLASS not in codex.task_classes


def test_gripe_fix_triage_pins_the_builder_model_in_meta(
    handler: TodoHandler, store: Store
) -> None:
    """gr549046: the triage body tells the generator to pin the builder
    model on each run_slice child in meta.op — the carrier from the
    triage verdict to dispatch."""
    mint = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=_gripe(handler))
    body = _body(store, mint.ids["triage"])
    assert "meta.op" in body
    assert "'type': 'run_slice'" in body
    assert "'model': '<id>'" in body
    assert "'effort': '<low|medium|high>'" in body


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


def test_brief_and_slicer_prompts_carry_links_and_title_style(
    handler: TodoHandler, store: Store
) -> None:
    """gr548798 / gr548799: the brief-writer and slicer prompts express
    order as blocked-by links — never a prose ordering line — and name
    steps in the summary-title house style."""
    gripe = _gripe(handler)
    mint = TEMPLATES["gripe-fix"].mint(store, None, gripe_id=gripe)
    brief = _body(store, mint.ids["brief"])
    assert "· " in brief
    assert "telegraphic" in brief

    mint = TEMPLATES["slice-tree"].mint(store, None, section="main/docs/x.md~Y")
    slicer = _body(store, mint.ids["slicer"])
    assert "blocked-by links" in slicer
    assert "never in a title or body line" in slicer
    assert "· " in slicer
    assert "telegraphic" in slicer


def test_slice_tree_shape(store: Store) -> None:
    mint = TEMPLATES["slice-tree"].mint(
        store, None, section="main/docs/backlog/x.md~Heading"
    )
    assert _kinds(store, mint) == {
        "root": "phase",
        "spec": "generator",
        "slicer": "generator",
        "review": "generator",
    }
    assert _blocked_by(store, mint) == {("slicer", "spec"), ("review", "slicer")}
    assert _meta(store, mint.ids["slicer"])["generate"] == {
        "role": "slicer",
        "from": "main/docs/backlog/x.md~Heading",
    }
    assert _meta(store, mint.ids["review"])["generate"] == {
        "role": "reviewer",
        "from": f"td{mint.ids['slicer']}",
    }
    body = _body(store, mint.ids["review"])
    assert "child todo" in body and "review: REVERT" in body
    _assert_plan_hygiene(store, mint)


def test_release_pass_shape(store: Store) -> None:
    mint = TEMPLATES["release-pass"].mint(store, None)
    assert _kinds(store, mint) == {
        "root": "phase",
        "cut": "task",
        "poll": "task",
        "gate": "gate",
        "loop": "loop",
        "approval": "gate",
        "deploy": "task",
        "confirm": "task",
    }
    assert _blocked_by(store, mint) == {
        ("poll", "cut"),
        ("gate", "poll"),
        ("loop", "poll"),
        ("approval", "gate"),
        ("deploy", "approval"),
        ("confirm", "deploy"),
    }
    assert _meta(store, mint.ids["cut"])["op"] == "release_cut"
    assert _meta(store, mint.ids["poll"])["op"] == "ci_verdict_poll"
    assert _meta(store, mint.ids["deploy"])["op"] == "release_deploy"
    confirm = _meta(store, mint.ids["confirm"])
    assert confirm["op"] == "release_confirm"
    assert confirm["retries"] == 0
    gate = _meta(store, mint.ids["gate"])["gate"]
    assert gate == {"kind": "tests-green", "args": {"range": "0000000"}}
    loop = _meta(store, mint.ids["loop"])["loop"]
    assert loop["until"] == gate
    assert loop["max"] == 3
    approval = _meta(store, mint.ids["approval"])
    assert approval["gate"] == {"kind": "answer"}
    assert approval["ask"]["kind"] == "approval"
    assert approval["ask"]["options"] == ["deploy r<N>", "hold"]
    assert approval["ask"]["blocks"] == f"td{mint.ids['deploy']}"
    assert "ask-user" in {t.value for t in store.tags_for(mint.ids["approval"])}
    assert "release-pass" in {t.value for t in store.tags_for(mint.root_id)}
    _assert_plan_hygiene(store, mint)


def _pcb(store: Store, slug: str = "amp") -> int:
    ref, _created, _counts = store.pcb_apply(
        slug=slug,
        title=slug,
        components=[{"refdes": "R1", "label": "R", "pins": [{"name": "1"}]}],
        nets=[{"name": "SIG"}],
        connections=[{"net": "SIG", "refdes": "R1", "pin": "1"}],
    )
    return int(ref.id)


def test_pcb_pipeline_shape(store: Store) -> None:
    pcb = _pcb(store)
    mint = TEMPLATES["pcb-pipeline"].mint(store, None, board="pcb:amp")
    assert _kinds(store, mint) == {
        "root": "phase",
        "place": "task",
        "loop": "loop",
        "drc": "task",
        "review": "generator",
    }
    assert "route" not in mint.ids
    assert _blocked_by(store, mint) == {
        ("loop", "place"),
        ("drc", "place"),
        ("review", "drc"),
    }
    # The handle resolves at mint: the job ops carry what their
    # PARAMS_SCHEMA requires, the gate and generator the pb<id> handle.
    # Place and route are one job (td542269): pcb_place with the topo
    # placer and route on.
    assert _meta(store, mint.ids["place"])["op"] == {
        "type": "pcb_place",
        "params": {"pcb_ref_id": pcb, "placer": "topo", "route": True},
    }
    assert _meta(store, mint.ids["drc"])["op"] == {
        "type": "pcb_review",
        "params": {"pcb_ref_id": pcb},
    }
    loop = _meta(store, mint.ids["loop"])["loop"]
    assert loop["until"] == {"kind": "drc-clean", "args": {"board": f"pb{pcb}"}}
    assert loop["body"] == "pcb-fix"
    assert _meta(store, mint.ids["review"])["generate"] == {
        "role": "triage",
        "from": f"pb{pcb}",
    }
    for key in ("drc", "review"):
        assert _parent_of(store, mint.ids[key]) == mint.ids["loop"]
    _assert_plan_hygiene(store, mint)


def test_pcb_pipeline_op_params_pass_the_job_schemas(store: Store) -> None:
    # gr542298: plan_dispatch's submit check refused {board} params.
    from precis.workers.job_types import get_job_type
    from precis.workers.job_types.plan_dispatch import _params_problem

    mint = TEMPLATES["pcb-pipeline"].mint(store, None, board=f"pb{_pcb(store)}")
    for key in ("place", "drc"):
        op = _meta(store, mint.ids[key])["op"]
        spec = get_job_type(op["type"])
        assert spec is not None
        assert _params_problem(spec.params_schema, op["params"]) is None
        assert isinstance(op["params"]["pcb_ref_id"], int)


@pytest.mark.parametrize(
    "spell",
    [
        lambda _i: "amp",
        lambda _i: "pcb:amp",
        lambda i: f"PB{i}",
        lambda i: str(i),
        lambda i: i,
    ],
    ids=["slug", "pcb:slug", "handle", "id-str", "id-int"],
)
def test_pcb_pipeline_accepts_board_spellings(store: Store, spell: Any) -> None:
    pcb = _pcb(store)
    mint = TEMPLATES["pcb-pipeline"].mint(store, None, board=spell(pcb))
    assert _meta(store, mint.ids["place"])["op"]["params"]["pcb_ref_id"] == pcb


@pytest.mark.parametrize("board", ["no-such-board", "", "pb999999999"])
def test_pcb_pipeline_refuses_unknown_board(store: Store, board: str) -> None:
    with pytest.raises(BadInput) as exc:
        TEMPLATES["pcb-pipeline"].mint(store, None, board=board)
    assert "names no pcb design" in exc.value.cause
    assert exc.value.next and "search(kind='pcb'" in exc.value.next


def _external_blockers(store: Store, todo_id: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE relation = 'blocked-by' "
            "AND src_ref_id = %s ORDER BY dst_ref_id",
            (todo_id,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def _child_count(store: Store, todo_id: int) -> int:
    with store.pool.connection() as conn:
        row = conn.execute(
            "SELECT count(*) FROM refs WHERE kind = 'todo' AND parent_id = %s",
            (todo_id,),
        ).fetchone()
    assert row is not None
    return int(row[0])


def test_route_loop_shape(handler: TodoHandler, store: Store) -> None:
    """A loop whose until is an ``answer`` on the loop todo itself, minted
    childless: the tick mints iteration 1 once ``after`` has closed, so a
    loop minted ahead of its prerequisites never fires early (me570353).
    ``context`` is the loop's body, the slicer brief's design section."""
    before = id_of(
        handler.put(text="Land the search first.", meta={"rotation_root": True}).body
    )
    mint = TEMPLATES["route-loop"].mint(
        store,
        None,
        iterations=3,
        after=f"td{before}",
        context="Spec hub: me1. Pin every code child to lane claude-sub.",
    )
    assert set(mint.ids) == {"root", "loop"}
    assert _kinds(store, mint) == {"root": "phase", "loop": "loop"}
    assert _meta(store, mint.ids["loop"])["loop"] == {
        "max": 3,
        "until": {"kind": "answer"},
        "body": "route-fix",
    }
    assert _body(store, mint.ids["loop"]).startswith("Spec hub: me1.")
    # The only blocked-by edge leaves the plan: the loop waits for `after`.
    assert _external_blockers(store, mint.ids["loop"]) == [before]
    assert _external_blockers(store, mint.ids["root"]) == []
    assert _child_count(store, mint.ids["loop"]) == 0
    _assert_plan_hygiene(store, mint)


def test_route_loop_defaults_and_refusals(store: Store) -> None:
    mint = TEMPLATES["route-loop"].mint(store, None)
    assert _meta(store, mint.ids["loop"])["loop"]["max"] == 6
    assert _body(store, mint.ids["loop"]) == ""
    assert _external_blockers(store, mint.ids["loop"]) == []
    assert TEMPLATES["route-loop"].optional == ("iterations", "after", "context")
    with pytest.raises(BadInput, match="iterations"):
        TEMPLATES["route-loop"].mint(store, None, iterations=0)
    with pytest.raises(BadInput, match="not a todo id"):
        TEMPLATES["route-loop"].mint(store, None, after="soon")
    with pytest.raises(BadInput, match="not found"):
        TEMPLATES["route-loop"].mint(store, None, after="td999999999")


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


def test_put_mode_plan_direct_passes_lane(handler: TodoHandler, store: Store) -> None:
    """The handler passes a template's optional args through (``lane``)."""
    gripe = _gripe(handler)
    resp = handler.put(
        mode="plan", template="gripe-fix-direct", gripe_id=gripe, lane="codex"
    )
    assert "gripe-fix-direct" in resp.body
    assert "Triage" not in resp.body
    root = id_of(resp.body)
    tree = handler.get(id=root, view="tree").body
    match = re.search(r"td(\d+) \S+ Build the fix", tree)
    assert match is not None, tree
    assert _meta(store, int(match.group(1)))["op"]["params"] == {"lane": "codex"}


def test_put_mode_plan_route_loop_passes_its_args(
    handler: TodoHandler, store: Store
) -> None:
    """The handler passes the route-loop's optional args through
    (``iterations``, ``after``, ``context``). Shipped without them in the
    put signatures (handler and verb), the MCP layer refused the keys
    before the handler ran and the template could not be used."""
    before = id_of(handler.put(text="Stage.", meta={"rotation_root": True}).body)
    resp = handler.put(
        mode="plan",
        template="route-loop",
        iterations=2,
        after=f"td{before}",
        context="Route in math space first.",
    )
    assert "route-loop" in resp.body
    tree = handler.get(id=id_of(resp.body), view="tree").body
    match = re.search(r"td(\d+) \S+ Example boards", tree)
    assert match is not None, tree
    loop = int(match.group(1))
    assert _meta(store, loop)["loop"]["max"] == 2
    assert _external_blockers(store, loop) == [before]
    assert "Route in math space first." in handler.get(id=loop).body


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


@pytest.mark.parametrize(
    "op",
    ["run_slice", {"type": "plan_tick"}],
    ids=["op-name", "op-dict"],
)
def test_parented_put_skips_llm_tier_for_task_nodes(
    handler: TodoHandler, store: Store, op: Any
) -> None:
    # gr542301: plan_dispatch alone drives a task node, so no plan_tick
    assert _tier_after_parented_put(handler, store, op=op) is None


def test_parented_put_stores_a_dict_op_on_create(
    handler: TodoHandler, store: Store
) -> None:
    """gr549046: put(kind='todo', meta={'op': {...}}) accepts the pin on
    create — the shape the generator brief now instructs (it already did;
    pinned here so it cannot regress to edit-only)."""
    root = id_of(handler.put(text="Root.", meta={"rotation_root": True}).body)
    op = {
        "type": "run_slice",
        "params": {"model": "claude-sonnet-5", "effort": "low"},
    }
    child = id_of(
        handler.put(
            text="Child.", parent_id=root, meta={"llm_tier": None, "op": op}
        ).body
    )
    meta = _meta(store, child)
    assert meta["op"] == op
    assert "llm_tier" not in meta
    assert node_kind(meta) == "task"


def test_parented_put_keeps_llm_tier_default_for_plain(
    handler: TodoHandler, store: Store
) -> None:
    assert _tier_after_parented_put(handler, store) == "opus"
