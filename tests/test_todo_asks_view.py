"""Dark-factory step 1b (td540847): ``search(kind='todo', view='asks')`` —
the asks list in three classes, grouped by quest then thread."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.gripe import GripeHandler
from precis.handlers.todo import _TREE_SEARCH_VIEWS, TodoHandler, TodoView
from precis.store import Store
from precis.store.types import Tag
from precis.todo import asks_view
from tests.conftest import id_of


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _tree(handler: TodoHandler, store: Store, hub: Hub) -> dict[str, Any]:
    """A quest-served root with four slices and a gripe, one per source."""
    quest = store.insert_ref(kind="quest", slug=None, title="Precis runs its own work")
    root = id_of(
        handler.put(text="Dark factory root.", meta={"rotation_root": True}).body
    )
    store.add_link(src_ref_id=root, dst_ref_id=quest.id, relation="serves")
    store.add_tag(root, Tag.open("thread:factory"))

    def slice_(text: str) -> int:
        return id_of(
            handler.put(text=text, parent_id=root, meta={"llm_tier": None}).body
        )

    stopped = slice_("Claimed slice stopped on Reto.")
    handler.tag(
        id=stopped,
        add=["claimed-by:slice-x", "STATUS:doing", "waiting-for:reto"],
    )
    asking = slice_("Slice with a question.")
    store.add_tag(asking, Tag.open("ask-user:which database?"))
    shape = slice_("Slice with a shape question.")
    store.add_tag(shape, Tag.open("discussion"))
    plain = slice_("Slice nobody waits on.")
    gripe = id_of(
        GripeHandler(hub=hub)
        .put(text="Needs a ruling on the lane rule.", tags=["needs:ruling"])
        .body
    )
    store.add_link(src_ref_id=gripe, dst_ref_id=quest.id, relation="serves")
    return {
        "quest": quest.id,
        "root": root,
        "stopped": stopped,
        "asking": asking,
        "shape": shape,
        "plain": plain,
        "gripe": gripe,
    }


def _row(model: asks_view.AsksModel, ref_id: int) -> asks_view.AskRow:
    return next(r for r in model.rows if r.ref_id == ref_id)


def test_asks_view_is_wired_in_the_vocabulary() -> None:
    assert TodoView("asks") is TodoView.ASKS
    assert TodoView.ASKS in _TREE_SEARCH_VIEWS


def test_each_source_lands_in_its_class_under_its_quest(
    handler: TodoHandler, hub: Hub, tmp_path: Path
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    model = asks_view.asks_model(store, review_dir=tmp_path)

    assert _row(model, t["stopped"]).klass == asks_view.SHOWSTOPPER
    assert _row(model, t["asking"]).klass == asks_view.DECISION
    assert _row(model, t["gripe"]).klass == asks_view.DECISION
    assert _row(model, t["shape"]).klass == asks_view.DISCUSSION
    assert all(r.ref_id != t["plain"] for r in model.rows)
    assert len(model.rows) == 4
    # every row resolves to the quest over the root's / its own serves link
    assert {r.quest_id for r in model.rows} == {t["quest"]}
    assert _row(model, t["stopped"]).quest_title == "Precis runs its own work"
    # thread comes from the nearest ancestor that carries one
    assert _row(model, t["asking"]).thread == "factory"
    assert _row(model, t["gripe"]).thread == ""
    # the inline ask question travels with the row
    assert _row(model, t["asking"]).question == "which database?"
    assert _row(model, t["stopped"]).reasons == (
        "waiting-for:reto",
        "claimed-by:slice-x",
    )


def test_waiting_for_reto_unclaimed_is_a_decision(
    handler: TodoHandler, hub: Hub, tmp_path: Path
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["plain"], add=["waiting-for:reto"])
    model = asks_view.asks_model(store, review_dir=tmp_path)
    assert _row(model, t["plain"]).klass == asks_view.DECISION


def test_a_closed_row_drops_out(handler: TodoHandler, hub: Hub, tmp_path: Path) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["asking"], add=["STATUS:done"])
    model = asks_view.asks_model(store, review_dir=tmp_path)
    assert all(r.ref_id != t["asking"] for r in model.rows)


def test_quest_of_walks_parents_then_serves(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    assert asks_view.quest_of(store, t["asking"]) == t["quest"]
    assert asks_view.quest_of(store, t["root"]) == t["quest"]
    lone = id_of(handler.put(text="Serves nothing.", meta={"rotation_root": True}).body)
    assert asks_view.quest_of(store, lone) is None


def test_render_groups_by_class_then_quest_then_thread(
    handler: TodoHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRECIS_REVIEW_QUEUE_DIR", str(tmp_path))
    store = hub.live_store
    t = _tree(handler, store, hub)
    out = handler.search(view="asks").body
    lines = out.splitlines()

    assert lines[0] == "# asks — 1 showstopper · 2 decision · 1 discussion"
    heads = [ln for ln in lines if ln.split(" (")[0] in asks_view.CLASSES]
    assert [h.split(" (")[0] for h in heads] == [
        "showstopper",
        "decision",
        "discussion",
    ]
    # one quest header per class group it appears in, handle first
    assert sum(f"◆ qu{t['quest']} —" in ln for ln in lines) == 3
    stopped = next(ln for ln in lines if f"td{t['stopped']} " in ln)
    assert "waiting-for:reto claimed-by:slice-x — Claimed slice" in stopped
    assert stopped.startswith("      ")  # under a thread line
    assert "thread:factory" in out
    gripe = next(ln for ln in lines if f"gr{t['gripe']} " in ln)
    assert "needs:ruling" in gripe
    assert any(ln.strip() == "? which database?" for ln in lines)
    # the decision block sits between the showstopper and the discussion
    assert out.index(f"td{t['stopped']} ") < out.index(f"td{t['asking']} ")
    assert out.index(f"td{t['asking']} ") < out.index(f"td{t['shape']} ")


def test_render_when_nothing_waits(
    handler: TodoHandler, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("PRECIS_REVIEW_QUEUE_DIR", str(tmp_path))
    assert handler.search(view="asks").body.startswith("# asks — nothing waits on you")


def _item(
    root: Path, name: str, *, kind: str, blocks: str, answer_via: str = "file"
) -> None:
    (root / f"{name}.md").write_text(
        f"---\narea: platform\nsource: organizer\nkind: {kind}\n"
        f"blocks: {blocks}\nanswer-via: {answer_via}\nfiled: 2026-10-30T00:00:00Z\n"
        "---\n**Question.** Should the pass cut on red?\n\n"
        "**Options.** 1. yes\n",
        encoding="utf-8",
    )


def test_review_queue_files_are_rows_with_a_class(
    handler: TodoHandler, hub: Hub, tmp_path: Path
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    _item(tmp_path, "gate-1", kind="decision", blocks="the release pass")
    _item(tmp_path, "look-1", kind="look-at", blocks="nothing")
    _item(tmp_path, "shape-1", kind="design-verdict", blocks="nothing")
    # answered via a todo that already has a row -> not repeated
    _item(
        tmp_path,
        "dup-1",
        kind="decision",
        blocks="nothing",
        answer_via=f"precis:td{t['asking']}",
    )
    (tmp_path / "notes.md").write_text("no frontmatter here", encoding="utf-8")

    model = asks_view.asks_model(store, review_dir=tmp_path)
    by_handle = {r.handle: r for r in model.rows if r.kind == "review"}

    assert set(by_handle) == {"review:gate-1", "review:look-1", "review:shape-1"}
    assert by_handle["review:gate-1"].klass == asks_view.SHOWSTOPPER
    assert by_handle["review:look-1"].klass == asks_view.DECISION
    assert by_handle["review:shape-1"].klass == asks_view.DISCUSSION
    assert by_handle["review:gate-1"].title == "Should the pass cut on red?"
    assert by_handle["review:gate-1"].thread == "platform"
    assert by_handle["review:gate-1"].quest_id is None
    # quest-less rows render in the "no quest" bucket
    out = asks_view.render_asks(store, review_dir=tmp_path).body
    assert "no quest" in out
    assert "review:gate-1" in out


def test_missing_review_dir_is_just_no_files(
    handler: TodoHandler, hub: Hub, tmp_path: Path
) -> None:
    store = hub.live_store
    _tree(handler, store, hub)
    model = asks_view.asks_model(store, review_dir=tmp_path / "absent")
    assert all(r.kind != "review" for r in model.rows)
    assert len(model.rows) == 4
