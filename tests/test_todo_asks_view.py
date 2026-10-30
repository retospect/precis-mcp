"""Dark-factory step 1b (td540847): ``search(kind='todo', view='asks')`` —
the asks list in three classes, grouped by quest then thread — and td541979:
a question for the owner is an ``ask-user`` todo carrying ``meta.ask``."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
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


def _ask(
    handler: TodoHandler,
    text: str = "Should the pass cut on a red gate?",
    *,
    blocks: str = "nothing",
    parent_id: int | None = None,
    **ask: Any,
) -> int:
    meta = {
        "ask": {
            "kind": "ruling",
            "options": ["yes, cut", "no, warn only"],
            "blocks": blocks,
            "context": "gate went red twice",
            **ask,
        }
    }
    return id_of(
        handler.put(
            text=text,
            parent_id=parent_id,
            tags=["ask-user", "thread:factory"],
            meta=meta,
        ).body
    )


def _blocked_by(store: Store, src: int) -> list[int]:
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT dst_ref_id FROM links WHERE src_ref_id = %s "
            "AND relation = 'blocked-by' ORDER BY dst_ref_id",
            (src,),
        ).fetchall()
    return [int(r[0]) for r in rows]


def test_asks_view_is_wired_in_the_vocabulary() -> None:
    assert TodoView("asks") is TodoView.ASKS
    assert TodoView.ASKS in _TREE_SEARCH_VIEWS


def test_each_source_lands_in_its_class_under_its_quest(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    model = asks_view.asks_model(store)

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
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["plain"], add=["waiting-for:reto"])
    model = asks_view.asks_model(store)
    assert _row(model, t["plain"]).klass == asks_view.DECISION


def test_a_closed_row_drops_out(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["asking"], add=["STATUS:done"])
    model = asks_view.asks_model(store)
    assert all(r.ref_id != t["asking"] for r in model.rows)


def test_quest_of_walks_parents_then_serves(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    assert asks_view.quest_of(store, t["asking"]) == t["quest"]
    assert asks_view.quest_of(store, t["root"]) == t["quest"]
    lone = id_of(handler.put(text="Serves nothing.", meta={"rotation_root": True}).body)
    assert asks_view.quest_of(store, lone) is None


def test_render_groups_by_class_then_quest_then_thread(
    handler: TodoHandler, hub: Hub
) -> None:
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


def test_render_when_nothing_waits(handler: TodoHandler) -> None:
    assert handler.search(view="asks").body.startswith("# asks — nothing waits on you")


# ── meta.ask (td541979) ───────────────────────────────────────────


def test_meta_ask_without_blocks_is_a_decision_with_its_options(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, parent_id=t["root"])
    row = _row(asks_view.asks_model(store), ask)

    assert row.klass == asks_view.DECISION
    assert row.reasons == ("ask-user",)
    assert row.ask_kind == "ruling"
    assert row.options == ("yes, cut", "no, warn only")
    assert row.context == "gate went red twice"
    assert row.blocks is None
    assert row.quest_id == t["quest"]
    assert row.thread == "factory"
    out = handler.search(view="asks").body
    assert "1. yes, cut" in out
    assert "ruling: gate went red twice" in out
    # a question for the owner is never auto-run
    assert "llm_tier" not in (store.fetch_refs_by_ids([ask])[ask].meta or {})


def test_blocks_writes_a_blocked_by_link_and_files_a_showstopper_under_the_quest(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    # parentless question: it sits under the quest of the slice it blocks
    ask = _ask(handler, blocks=f"td{t['plain']}")

    assert _blocked_by(store, t["plain"]) == [ask]
    row = _row(asks_view.asks_model(store), ask)
    assert row.klass == asks_view.SHOWSTOPPER
    assert row.reasons == ("ask-user", f"blocks:td{t['plain']}")
    assert row.blocks == t["plain"]
    assert row.quest_id == t["quest"]
    assert row.thread == "factory"
    # the blocked slice is parked until the question closes
    assert f"td{t['plain']}" in handler.search(view="blocked").body


def test_answering_closes_the_question_and_releases_the_slice(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, blocks=f"td{t['plain']}", parent_id=t["root"])

    handler.put(id=ask, text="answer: yes, cut; warn-only hides real reds")
    handler.tag(id=ask, add=["STATUS:done"], remove=["ask-user"])

    assert all(r.ref_id != ask for r in asks_view.asks_model(store).rows)
    assert f"td{t['plain']}" not in handler.search(view="blocked").body
    assert "answer: yes, cut" in handler.get(id=ask, view="comments").body


def test_edit_replaces_meta_ask_and_adds_the_link(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    ask = _ask(handler, parent_id=t["root"])
    assert _blocked_by(store, t["plain"]) == []

    handler.edit(
        id=ask,
        meta={"ask": {"kind": "approval", "blocks": f"td{t['plain']}"}},
    )

    assert _blocked_by(store, t["plain"]) == [ask]
    row = _row(asks_view.asks_model(store), ask)
    assert row.klass == asks_view.SHOWSTOPPER
    assert row.ask_kind == "approval"
    assert row.options == ()  # replaced, not merged


@pytest.mark.parametrize(
    ("ask", "fragment"),
    [
        ({"kind": "chat", "blocks": "nothing"}, "unknown kind"),
        ({"kind": "ruling", "blocks": "nothing", "options": "yes"}, "options"),
        ({"kind": "ruling", "blocks": "nothing", "options": ["ok", ""]}, "options"),
        ({"kind": "ruling", "blocks": "nothing", "context": 5}, "context"),
        ({"kind": "ruling", "blocks": "nothing", "colour": "red"}, "unknown"),
        ({"kind": "ruling", "blocks": "soon"}, "blocks"),
        ({"kind": "ruling", "blocks": "gr123456"}, "blocks"),
        ({"kind": "ruling"}, "blocks"),
        ("just a string", "dict"),
    ],
)
def test_malformed_meta_ask_is_refused_with_a_next_step(
    handler: TodoHandler, ask: Any, fragment: str
) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(text="Q?", tags=["ask-user"], meta={"ask": ask})
    assert fragment in exc.value.cause
    assert exc.value.next


def test_meta_ask_needs_the_ask_user_tag(handler: TodoHandler) -> None:
    with pytest.raises(BadInput) as exc:
        handler.put(text="Q?", meta={"ask": {"kind": "ruling", "blocks": "nothing"}})
    assert "ask-user" in exc.value.cause
    assert exc.value.next


def test_blocks_must_name_a_live_other_todo(handler: TodoHandler) -> None:
    with pytest.raises(NotFound) as exc:
        handler.put(
            text="Q?",
            tags=["ask-user"],
            meta={"ask": {"kind": "ruling", "blocks": "td999999999"}},
        )
    assert exc.value.next
    ask = _ask(handler)
    with pytest.raises(BadInput):
        handler.edit(id=ask, meta={"ask": {"kind": "ruling", "blocks": f"td{ask}"}})


def test_a_refused_put_leaves_nothing_behind(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    before = len(store.list_refs(kind="todo", limit=1000))
    with pytest.raises(BadInput):
        handler.put(text="Q?", tags=["ask-user"], meta={"ask": {"kind": "chat"}})
    assert len(store.list_refs(kind="todo", limit=1000)) == before


def test_default_rows_hide_waiting_for_and_discussion(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _tree(handler, store, hub)
    handler.tag(id=t["plain"], add=["waiting-for:reto"])
    ask = _ask(handler, blocks=f"td{t['shape']}")
    full = asks_view.asks_model(store)
    default = {r.ref_id for r in full.default_rows().rows}

    assert {t["stopped"], t["asking"], t["gripe"], ask} <= default
    assert t["plain"] not in default  # bare waiting-for:reto
    assert t["shape"] not in default  # discussion
    assert {r.ref_id for r in full.rows} - default == {t["plain"], t["shape"]}
