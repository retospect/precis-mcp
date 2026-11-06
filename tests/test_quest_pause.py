"""Tests for ``edit(kind='quest', mode='pause'|'resume')`` (td573297) —
the one-click MCP twin of the /work web page's Pause/Resume buttons.

Pause stamps ``STATUS:paused`` + ``paused-by:quest`` onto every live
strategic root serving the quest and moves the quest to
``STATUS:dormant``; resume lifts only what it stamped and moves the quest
back to ``STATUS:active``. Both append one logbook line and are
idempotent. ``src/precis/workers/job_types/plan_dispatch.py`` (not touched
by this slice) already walks ``STATUS:paused`` up the tree for its doable
filter, so a paused root's leaves leave ``view='doable'`` for free.
"""

from __future__ import annotations

from typing import Any

from precis.store.types import Tag
from tests.conftest import id_of


def _handlers(store: Any) -> tuple[Any, Any]:
    from precis.dispatch import Hub
    from precis.handlers.quest import QuestHandler
    from precis.handlers.todo import TodoHandler

    hub = Hub(store=store)
    return QuestHandler(hub=hub), TodoHandler(hub=hub)


def _quest_with_root(store: Any, quest_h: Any, todo_h: Any) -> tuple[int, int, int]:
    """One quest, one strategic root serving it (``serves`` + rotation_root),
    one leaf under the root."""
    qid = id_of(quest_h.put(text="A perpetual pausable striving").body)
    root_id = id_of(
        todo_h.put(text="Strategic root", meta={"rotation_root": True}).body
    )
    store.add_link(src_ref_id=root_id, dst_ref_id=qid, relation="serves")
    leaf_id = id_of(todo_h.put(text="Leaf work", parent_id=root_id).body)
    return qid, root_id, leaf_id


def _tags(store: Any, ref_id: int) -> list[str]:
    return [str(t) for t in store.tags_for(ref_id)]


class TestQuestPauseResume:
    def test_pause_then_resume_roundtrips_roots_and_quest(self, store: Any) -> None:
        quest_h, todo_h = _handlers(store)
        qid, root_id, leaf_id = _quest_with_root(store, quest_h, todo_h)

        resp = quest_h.edit(id=qid, mode="pause")
        assert "paused" in resp.body
        assert f"td{root_id}" in resp.body
        root_tags = _tags(store, root_id)
        assert "STATUS:paused" in root_tags
        assert "paused-by:quest" in root_tags
        assert "STATUS:dormant" in _tags(store, qid)

        # the doable view under the root is empty while paused — the
        # existing doable filter walks STATUS:paused up the tree.
        doable_paused = todo_h.search(view="doable", under=root_id).body
        assert "no doable leaves" in doable_paused

        # a queued job under the root keeps its own STATUS untouched —
        # pause/resume never touches a job. Parented on the root, not the
        # leaf: a job child would otherwise disqualify the leaf from
        # view='doable' (it stops being childless) independent of pause.
        job = store.insert_ref(
            kind="job", slug=None, title="run_slice", parent_id=root_id, meta={}
        )
        store.add_tag(
            job.id, Tag.parse_strict("STATUS:queued", kind="job"), set_by="system"
        )
        assert "STATUS:queued" in _tags(store, job.id)

        resp2 = quest_h.edit(id=qid, mode="resume")
        assert "resumed" in resp2.body
        assert f"td{root_id}" in resp2.body
        root_tags2 = _tags(store, root_id)
        assert "STATUS:paused" not in root_tags2
        assert "paused-by:quest" not in root_tags2
        assert "STATUS:active" in _tags(store, qid)

        doable_resumed = todo_h.search(view="doable", under=root_id).body
        assert "no doable leaves" not in doable_resumed
        assert f"td{leaf_id}" in doable_resumed

        # the job's STATUS was never touched by either call.
        assert "STATUS:queued" in _tags(store, job.id)

        logbook = quest_h.get(id=qid, view="logbook").body
        assert "paused by" in logbook
        assert "resumed by" in logbook

    def test_root_paused_by_hand_stays_paused_through_quest_pause_resume(
        self, store: Any
    ) -> None:
        quest_h, todo_h = _handlers(store)
        qid, root_id, _leaf_id = _quest_with_root(store, quest_h, todo_h)
        todo_h.tag(id=root_id, add=["STATUS:paused"])

        quest_h.edit(id=qid, mode="pause")
        root_tags = _tags(store, root_id)
        assert "STATUS:paused" in root_tags
        # never stamped — it wasn't the quest that paused it.
        assert "paused-by:quest" not in root_tags

        quest_h.edit(id=qid, mode="resume")
        root_tags2 = _tags(store, root_id)
        # resume only lifts roots it stamped — this one stays paused.
        assert "STATUS:paused" in root_tags2
        assert "paused-by:quest" not in root_tags2

    def test_pause_twice_is_a_no_op(self, store: Any) -> None:
        quest_h, todo_h = _handlers(store)
        qid, root_id, _leaf_id = _quest_with_root(store, quest_h, todo_h)

        quest_h.edit(id=qid, mode="pause")
        before = quest_h.get(id=qid, view="logbook").body

        resp = quest_h.edit(id=qid, mode="pause")
        assert "no change" in resp.body
        after = quest_h.get(id=qid, view="logbook").body
        assert before == after  # no second logbook line
        assert _tags(store, root_id).count("STATUS:paused") == 1

    def test_resume_never_paused_quest_is_a_no_op(self, store: Any) -> None:
        quest_h, todo_h = _handlers(store)
        qid, _root_id, _leaf_id = _quest_with_root(store, quest_h, todo_h)

        resp = quest_h.edit(id=qid, mode="resume")
        assert "no change" in resp.body
        logbook = quest_h.get(id=qid, view="logbook").body
        assert "resumed by" not in logbook
        assert "STATUS:active" in _tags(store, qid)
