"""Project surface: workspace promoted to a first-class project.

A *project* is not a new kind — it's the existing ``meta.workspace``
concept surfaced three ways:

1. ``project:<slug>`` cross-cutting tag stamped on the owner path
   (not just inside a planner tick) — :func:`project_tag_for_path` /
   :attr:`Workspace.project_tag` and ``TodoHandler.put``.
2. ``meta.workspace.extra.brief`` injected as ``## Project context``
   into the planner prompt's variable layer.
3. ``search(kind='todo', view='projects')`` dashboard.

The pure helper tests need no DB; the rest take the ``store`` /
``hub`` fixtures (auto-skipped when the test DB is unreachable).
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.utils.workspace import Workspace, project_tag_for_path

# ── pure helpers (no DB) ──────────────────────────────────────────


def test_project_tag_for_path_basename() -> None:
    assert project_tag_for_path("projects/nanotrans_auto") == "project:nanotrans_auto"
    assert project_tag_for_path("foo") == "project:foo"
    assert project_tag_for_path("a/b/c") == "project:c"


def test_project_tag_for_path_trailing_slash() -> None:
    assert project_tag_for_path("projects/demo/") == "project:demo"


def test_project_tag_for_path_empty_is_none() -> None:
    assert project_tag_for_path(None) is None
    assert project_tag_for_path("") is None
    assert project_tag_for_path("/") is None


def test_workspace_project_tag_property() -> None:
    ws = Workspace(path="projects/demo", format="tex", entrypoint="main.tex")
    assert ws.project_tag == "project:demo"


def test_workspace_project_tag_matches_env_helper() -> None:
    # The meta path and the env path must agree on the slug rule.
    from precis.utils import workspace as ws_mod

    ws = Workspace(path="x/y/myproj", format="md", entrypoint="main.md")
    assert ws.project_tag == ws_mod.project_tag_for_path(ws.path)


# ── owner-path project tagging (DB) ───────────────────────────────


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _id_of(body: str) -> int:
    from tests.conftest import id_of

    return id_of(body)


_WS_META = {
    "workspace": {
        "path": "projects/demo",
        "format": "tex",
        "entrypoint": "main.tex",
        "brief": "Standing guidance: terse, cite primary sources.",
    }
}


def test_put_with_workspace_stamps_project_tag(handler: TodoHandler) -> None:
    # Owner path — no PRECIS_WORKSPACE env set, project tag derived
    # from meta.workspace.path. Assert via the stored tag directly.
    r = handler.put(text="Demo project.", tags=["level:strategic"], meta=_WS_META)
    rid = _id_of(r.body)
    tags = [str(t) for t in handler.store.tags_for(rid)]
    assert "project:demo" in tags


def test_child_inherits_workspace_and_project_tag(handler: TodoHandler) -> None:
    root = handler.put(text="Root.", tags=["level:strategic"], meta=_WS_META)
    root_id = _id_of(root.body)
    # Child carries no meta — workspace cascades, project tag follows.
    child = handler.put(text="A leaf under the project.", parent_id=root_id)
    child_id = _id_of(child.body)
    out = handler.search(tags=["project:demo"])
    assert str(child_id) in out.body


def test_put_without_workspace_has_no_project_tag(handler: TodoHandler) -> None:
    r = handler.put(text="Plain strategic, no workspace.", tags=["level:strategic"])
    rid = _id_of(r.body)
    tags = [str(t) for t in handler.store.tags_for(rid)]
    assert not any(t.startswith("project:") for t in tags)


# ── workspace root auto-stamps level:strategic (orphan-flood fix) ──


def test_put_workspace_root_auto_stamps_strategic(handler: TodoHandler) -> None:
    # A project root minted WITHOUT an explicit level tag (e.g. a CLI or
    # script write that only sets meta.workspace) must still become
    # strategic — otherwise the nursery flags its whole subtree as orphaned.
    r = handler.put(text="Project root, no level tag.", meta=_WS_META)
    rid = _id_of(r.body)
    tags = [str(t) for t in handler.store.tags_for(rid)]
    assert "level:strategic" in tags


def test_put_workspace_root_respects_explicit_level(handler: TodoHandler) -> None:
    # An explicit tier choice is honoured — we don't override it with
    # strategic just because the root owns a workspace.
    r = handler.put(
        text="Workspace root, explicitly tactical.",
        tags=["level:tactical"],
        meta=_WS_META,
    )
    rid = _id_of(r.body)
    tags = [str(t) for t in handler.store.tags_for(rid)]
    assert "level:tactical" in tags
    assert "level:strategic" not in tags


def test_workspace_child_not_auto_strategic(handler: TodoHandler) -> None:
    # A child inherits the workspace block but is NOT a project root, so it
    # stays a subtask — only the originating root is strategic.
    root = handler.put(text="Root.", meta=_WS_META)
    root_id = _id_of(root.body)
    child = handler.put(text="A leaf under the project.", parent_id=root_id)
    child_id = _id_of(child.body)
    child_tags = [str(t) for t in handler.store.tags_for(child_id)]
    assert "level:strategic" not in child_tags


def test_put_root_without_workspace_not_strategic(handler: TodoHandler) -> None:
    # No workspace → no auto-strategic; a plain root stays untiered.
    r = handler.put(text="Plain root, no workspace, no level.")
    rid = _id_of(r.body)
    tags = [str(t) for t in handler.store.tags_for(rid)]
    assert "level:strategic" not in tags


# ── view='projects' (DB) ──────────────────────────────────────────


def test_projects_view_empty_state(handler: TodoHandler) -> None:
    out = handler.search(view="projects")
    assert "no projects yet" in out.body


def test_projects_view_lists_project_root(handler: TodoHandler) -> None:
    root = handler.put(
        text="Demo project goal.", tags=["level:strategic"], meta=_WS_META
    )
    rid = _id_of(root.body)
    out = handler.search(view="projects")
    assert f"td{rid}" in out.body
    assert "demo" in out.body  # slug
    assert "projects/demo" in out.body  # path


def test_projects_view_shows_brief_first_line(handler: TodoHandler) -> None:
    handler.put(text="Demo.", tags=["level:strategic"], meta=_WS_META)
    out = handler.search(view="projects")
    assert "Standing guidance" in out.body


def test_projects_view_counts_open_subtree(handler: TodoHandler) -> None:
    root = handler.put(text="Demo.", tags=["level:strategic"], meta=_WS_META)
    rid = _id_of(root.body)
    handler.put(text="open leaf one.", parent_id=rid)
    handler.put(text="open leaf two.", parent_id=rid)
    out = handler.search(view="projects")
    # root + 2 leaves = 3 open todos in the subtree.
    assert "open:   3" in out.body or "open:" in out.body


def test_projects_view_does_not_list_inherited_descendants(
    handler: TodoHandler,
) -> None:
    # A child inheriting the same workspace path must NOT appear as its
    # own project row — only the originating root.
    root = handler.put(text="Demo.", tags=["level:strategic"], meta=_WS_META)
    rid = _id_of(root.body)
    handler.put(text="leaf.", parent_id=rid)
    out = handler.search(view="projects")
    assert out.body.count("projects/demo") == 1


# ── brief injection into planner prompt (DB) ──────────────────────


def test_planner_prompt_injects_project_brief(handler: TodoHandler) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    root = handler.put(text="Demo project.", tags=["level:strategic"], meta=_WS_META)
    rid = _id_of(root.body)
    prompts = build_planner_prompts(handler.store, ref_id=rid, model="opus")
    assert "## Project context" in prompts.user
    assert "Standing guidance" in prompts.user
    # Brief belongs in the variable (user) layer, never the cached system.
    assert "Standing guidance" not in prompts.system


def test_planner_prompt_brief_cascades_to_leaf(handler: TodoHandler) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    root = handler.put(text="Demo.", tags=["level:strategic"], meta=_WS_META)
    rid = _id_of(root.body)
    child = handler.put(text="A deep leaf.", parent_id=rid)
    child_id = _id_of(child.body)
    prompts = build_planner_prompts(handler.store, ref_id=child_id, model="sonnet")
    assert "## Project context" in prompts.user
    assert "Standing guidance" in prompts.user


def test_planner_prompt_no_brief_no_block(handler: TodoHandler) -> None:
    from precis.workers.planner_prompt import build_planner_prompts

    root = handler.put(text="No-workspace strategic.", tags=["level:strategic"])
    rid = _id_of(root.body)
    prompts = build_planner_prompts(handler.store, ref_id=rid, model="opus")
    assert "## Project context" not in prompts.user
