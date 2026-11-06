"""td573923 (gr573557): a blocked-only quest leaves the idle group and
sorts by who can unblock — ``needs-you`` at the top, expanded, when
Reto can (a ``waiting-for:reto`` leaf, an open ``ask-user`` blocker, or
the leaf itself an ask); ``blocked-work`` at the bottom, collapsed,
when only other work can. ``run_state`` itself does not move."""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.store import Store
from precis.todo import views, work_view
from tests.test_work_board_rows import (
    _job,
    _quest,
    _quest_tree,
    _sections,
    _slice,
    client,
    handler,
)

__all__ = ["client", "handler"]


def _blocked_by(store: Store, src: int, dst: int) -> int:
    """An open ``blocked-by`` edge ``src → dst``; returns ``dst``."""
    store.add_link(src_ref_id=src, dst_ref_id=dst, relation="blocked-by")
    return dst


def _ask_todo(handler: TodoHandler, store: Store, title: str) -> int:
    """An open ``ask-user`` todo outside every served tree."""
    todo = store.insert_ref(kind="todo", slug=None, title=title)
    handler.tag(id=todo.id, add=["ask-user"])
    return todo.id


def test_waiting_for_reto_leaf_sorts_needs_you(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Reto-blocked quest")
    handler.tag(id=t["leaf"], add=["waiting-for:reto"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "idle"
    assert q.board == "needs-you"
    assert [(u.handle, u.why) for u in q.unblockers] == [
        (f"td{t['leaf']}", "waiting-for:reto")
    ]


def test_blocked_by_ask_user_is_needs_you(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Ask-blocked quest")
    ask = _ask_todo(handler, store, "Which way?")
    _blocked_by(store, t["leaf"], ask)
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.board == "needs-you"
    assert [(u.handle, u.why) for u in q.unblockers] == [(f"td{ask}", "ask-user")]
    # the ask answered, the leaf unblocks and the quest requeues
    handler.tag(id=ask, add=["STATUS:done"], remove=["ask-user"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.board == ""
    assert q.run_state == "queued"


def test_ask_user_leaf_is_needs_you(handler: TodoHandler, hub: Hub) -> None:
    """The leaf itself is the ask (rule c): the unblocker is the leaf."""
    store = hub.live_store
    t = _quest_tree(handler, store, "Asking quest")
    handler.tag(id=t["leaf"], add=["ask-user"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.board == "needs-you"
    assert [(u.handle, u.why) for u in q.unblockers] == [(f"td{t['leaf']}", "ask-user")]


def test_waiting_for_fleet_leaf_is_blocked_work(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Fleet-blocked quest")
    handler.tag(id=t["leaf"], add=["waiting-for:fleet"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "idle"
    assert q.board == "blocked-work"
    assert q.unblockers == ()


def test_blocked_by_plain_todo_is_blocked_work(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Work-blocked quest")
    blocker = store.insert_ref(kind="todo", slug=None, title="Ordinary blocker")
    _blocked_by(store, t["leaf"], blocker.id)
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.board == "blocked-work"


def test_status_blocked_leaf_is_blocked_work(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Status-blocked quest")
    handler.tag(id=t["leaf"], add=["STATUS:blocked"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "idle"
    assert q.board == "blocked-work"


def test_mixed_reto_and_work_blocked_is_needs_you(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Mixed-blocked quest")
    fleet = _slice(handler, t["root"], "Fleet-blocked slice.")
    handler.tag(id=t["leaf"], add=["waiting-for:reto"])
    handler.tag(id=fleet, add=["waiting-for:fleet"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.board == "needs-you"
    assert [u.handle for u in q.unblockers] == [f"td{t['leaf']}"]


def test_a_doable_leaf_keeps_the_quest_queued(handler: TodoHandler, hub: Hub) -> None:
    """One doable leaf beside a blocked one: not blocked-only."""
    store = hub.live_store
    t = _quest_tree(handler, store, "Half-blocked quest")
    blocker = store.insert_ref(kind="todo", slug=None, title="Ordinary blocker")
    blocked = _slice(handler, t["root"], "Blocked slice.")
    _blocked_by(store, blocked, blocker.id)
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "queued"
    assert q.board == ""


def test_paused_only_quest_has_no_board(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Paused quest")
    handler.tag(id=t["leaf"], add=["STATUS:paused"])
    q = _quest(work_view.work_model(store), t["quest"])
    assert q.run_state == "idle"
    assert q.board == ""


def test_board_order_needs_you_first_blocked_work_last(
    handler: TodoHandler, hub: Hub
) -> None:
    store = hub.live_store
    needs = _quest_tree(handler, store, "Needs-you quest")
    handler.tag(id=needs["leaf"], add=["waiting-for:reto"])
    running = _quest_tree(handler, store, "Running quest")
    _job(store, running["leaf"], "running")
    queued = _quest_tree(handler, store, "Queued quest")
    idle = _quest_tree(handler, store, "Idle quest")
    handler.tag(id=idle["leaf"], add=["STATUS:done"])
    blocked = _quest_tree(handler, store, "Blocked-work quest")
    handler.tag(id=blocked["leaf"], add=["waiting-for:fleet"])
    model = work_view.work_model(store)
    assert [q.ref_id for q in model.quests] == [
        needs["quest"],
        running["quest"],
        queued["quest"],
        idle["quest"],
        blocked["quest"],
    ]
    assert (model.n_needs_you, model.n_blocked_work) == (1, 1)
    # the same order on the text view
    out = handler.search(view="work").body
    assert out.index("needs you (1)") < out.index("running (1)")
    assert out.index("running (1)") < out.index("queued (1)")
    assert out.index("queued (1)") < out.index("idle (1)")
    assert out.index("idle (1)") < out.index("blocked by other work (1)")
    # the header counts need-you quests after the idle count
    assert "3 idle · 1 need you" in out


def test_page_groups_needs_you_first_blocked_work_last(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    needs = _quest_tree(handler, store, "Needs-you quest")
    handler.tag(id=needs["leaf"], add=["waiting-for:reto"])
    running = _quest_tree(handler, store, "Running quest")
    _job(store, running["leaf"], "running")
    _quest_tree(handler, store, "Queued quest")
    blocked = _quest_tree(handler, store, "Blocked-work quest")
    handler.tag(id=blocked["leaf"], add=["waiting-for:fleet"])
    html = client.get("/work").text
    assert html.index("Needs you (1)") < html.index("Running (1)")
    assert html.index("Running (1)") < html.index("Queued (1)")
    assert html.index("Blocked by other work (1)") > html.index("Queued (1)")
    sections = _sections(html)
    assert 'data-board="needs-you"' in sections[f"qu{needs['quest']}"]
    assert 'data-run-state="idle"' in sections[f"qu{needs['quest']}"]
    assert 'data-board="blocked-work"' in sections[f"qu{blocked['quest']}"]


def test_needs_you_quest_expands_with_unblocker_links(
    client: TestClient, handler: TodoHandler, store: Store
) -> None:
    needs = _quest_tree(handler, store, "Needs-you quest")
    handler.tag(id=needs["leaf"], add=["waiting-for:reto"])
    other = _quest_tree(handler, store, "Queued quest")
    html = client.get("/work").text
    start = html.index(f'data-handle="qu{needs["quest"]}"')
    section = html[start : html.index("</section>", start)]
    assert "<details open>" in section
    summary = section[section.index("<summary") : section.index("</summary>")]
    assert f'href="/eye/td{needs["leaf"]}"' in summary
    assert "needs you" in summary
    start = html.index(f'data-handle="qu{other["quest"]}"')
    other_section = html[start : html.index("</section>", start)]
    assert "<details open>" not in other_section
    # the text view expands the needs-you group's rows
    out = handler.search(view="work").body
    block = out.split("needs you (1)", 1)[1].split("\n\n", 1)[0]
    assert f"td{needs['leaf']}" in block
    assert f"needs you: td{needs['leaf']} (waiting-for:reto)" in block


def test_blocked_work_quest_stays_collapsed(handler: TodoHandler, hub: Hub) -> None:
    store = hub.live_store
    t = _quest_tree(handler, store, "Blocked-work quest")
    handler.tag(id=t["leaf"], add=["waiting-for:fleet"])
    out = handler.search(view="work").body
    line = next(ln for ln in out.splitlines() if f"qu{t['quest']}" in ln)
    assert "blocked by other work" in line
    # collapsed: the blocked leaf's row does not render
    block = out.split("blocked by other work (1)", 1)[1].split("\n\n", 1)[0]
    assert f"td{t['leaf']}" not in block


def test_blocked_leaf_past_the_row_cap_still_classifies(
    handler: TodoHandler, hub: Hub
) -> None:
    """Leaf verdicts run over every row, before MAX_ROWS_PER_ROOT."""
    store = hub.live_store
    t = _quest_tree(handler, store, "Deep-blocked quest")
    handler.tag(id=t["leaf"], add=["waiting-for:fleet"])
    for i in range(45):
        child = _slice(handler, t["root"], f"Blocked slice {i}.")
        handler.tag(id=child, add=["waiting-for:fleet"])
    q = _quest(work_view.work_model(store), t["quest"])
    root = q.roots[0]
    assert root.hidden > 0
    assert (root.live_leaves, root.blocked_leaves) == (46, 46)
    assert q.board == "blocked-work"


def test_blocker_tag_reads_stay_constant_for_5_and_50_quests(
    handler: TodoHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ``_all_open_tags`` for the root's rows plus one for the
    blocker targets — per root, whatever the quest count."""
    store = hub.live_store
    calls = {"tags": 0, "blockers": 0}
    real_tags = views._all_open_tags
    real_blockers = work_view._open_blockers

    def spy_tags(s: Store, ids: list[int]) -> dict[int, list[str]]:
        calls["tags"] += 1
        return real_tags(s, ids)

    def spy_blockers(s: Store, ids: list[int]) -> dict[int, tuple[int, ...]]:
        calls["blockers"] += 1
        return real_blockers(s, ids)

    monkeypatch.setattr(views, "_all_open_tags", spy_tags)
    monkeypatch.setattr(work_view, "_open_blockers", spy_blockers)
    made = 0
    for n in (5, 50):
        while made < n:
            t = _quest_tree(handler, store, f"Blocked quest {made}")
            _blocked_by(store, t["leaf"], _ask_todo(handler, store, "Which way?"))
            made += 1
        calls["tags"] = 0
        calls["blockers"] = 0
        work_view.work_model(store)
        # per root: rows + blocker targets; plus the unplaced-gripes read
        assert calls["tags"] == 2 * n + 1
        assert calls["blockers"] == n
