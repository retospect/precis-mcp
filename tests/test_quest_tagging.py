"""Tests for :mod:`precis.quest.tagging` — the quest:<id> Drive-scoping tag.

Runs against real PG (the ``store`` fixture) so the ``serves`` walk +
``ref_tags`` upsert are exercised end to end, matching the pattern in
``tests/test_quest_gaps.py``.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.errors import NotFound
from precis.handlers.quest import QuestHandler
from precis.quest.search import run_search_step
from precis.quest.tagging import quest_tag_value, tag_serving_papers
from precis.store.types import Tag
from tests.workers._helpers import seed_ref


def _handler(store: Any) -> QuestHandler:
    return QuestHandler(hub=Hub(store=store))


def _created_id(resp: Any) -> int:
    m = re.search(r"\bqu(\d+)\b", resp.body)
    assert m is not None, f"no quest handle in ack: {resp.body!r}"
    return int(m.group(1))


class TestQuestTagValue:
    def test_value_is_quest_colon_id(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A striving"))
        assert quest_tag_value(qid, store) == f"quest:{qid}"

    def test_raises_not_found_for_missing_quest(self, store: Any) -> None:
        with pytest.raises(NotFound):
            quest_tag_value(999_999_999, store)


class TestTagServingPapers:
    def test_tags_only_serves_linked_papers(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A grounded striving"))

        serving = seed_ref(store, title="serving paper", kind="paper")
        store.add_link(src_ref_id=serving, dst_ref_id=qid, relation="serves")

        # A paper that just happens to exist but does NOT serve this quest —
        # must stay untagged.
        unrelated = seed_ref(store, title="unrelated paper", kind="paper")

        # A non-paper server (todo) — serves the quest but must not be
        # tagged (the tag scopes /drive?k=paper only).
        todo_server = seed_ref(store, title="a todo", kind="todo")
        store.add_link(src_ref_id=todo_server, dst_ref_id=qid, relation="serves")

        n = tag_serving_papers(store, qid)
        assert n == 1

        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(serving)
        assert tag not in store.tags_for(unrelated)
        assert tag not in store.tags_for(todo_server)

    def test_idempotent(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A re-tickable striving"))
        p = seed_ref(store, title="paper", kind="paper")
        store.add_link(src_ref_id=p, dst_ref_id=qid, relation="serves")

        first = tag_serving_papers(store, qid)
        second = tag_serving_papers(store, qid)
        assert first == 1
        assert second == 1  # same count — no double-count/dup on re-run

        tag = Tag.open(f"quest:{qid}")
        assert store.tags_for(p).count(tag) == 1  # exactly one tag row

    def test_no_servers_tags_nothing(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A lonely striving"))
        assert tag_serving_papers(store, qid) == 0


class TestTagOnJoin:
    """A paper the lit-search step freshly ``serves``-links picks up the
    ``quest:<id>`` tag in the same step, before any backfill runs."""

    def test_run_search_step_tags_newly_linked_papers(self, store: Any) -> None:
        h = _handler(store)
        qid = _created_id(h.put(text="A quest that goes looking for papers"))
        paper = seed_ref(store, title="Photocatalytic Nitrate Reduction Study")

        step = run_search_step(store, qid, ["photocatalytic nitrate reduction"])

        assert step.papers_linked == 1
        tag = Tag.open(f"quest:{qid}")
        assert tag in store.tags_for(paper)
