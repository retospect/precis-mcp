"""gr548800(a): plan events append one-line ``quest_log`` entries on the
quest above the plan root — root opened (template mint) and paused (tag),
node entering rescue at its retry cap (plan_dispatch hygiene),
and an ask ``put`` under a plan node (TodoHandler.put). All land through
:func:`precis.quest.logbook.append_entry`, stamped ``by='system'``."""

from __future__ import annotations

from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.todo import TodoHandler
from precis.plans import templates as T
from precis.store import Store
from precis.store.types import Tag
from precis.workers.job_types import plan_dispatch as pd


@pytest.fixture(autouse=True)
def _factory_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PRECIS_FACTORY", raising=False)


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def _quest_and_root(store: Store) -> tuple[int, int]:
    """A quest plus a strategic root todo ``serves``-linked to it."""
    quest = store.insert_ref(kind="quest", slug=None, title="The quest")
    root = store.insert_ref(kind="todo", slug=None, title="Strategic root")
    store.add_link(src_ref_id=root.id, dst_ref_id=quest.id, relation="serves")
    return int(quest.id), int(root.id)


def _gripe_fix(store: Store, parent_id: int | None) -> tuple[int, Any]:
    with store.tx() as conn:
        gripe = store.insert_ref(
            kind="gripe", slug=None, title="Something is off", conn=conn
        )
        store.add_tag(
            int(gripe.id),
            Tag.closed("STATUS", "triaged"),
            set_by="system",
            replace_prefix=True,
            conn=conn,
        )
    return int(gripe.id), T.mint_gripe_fix(store, parent_id, gripe_id=int(gripe.id))


def _logs(store: Store, quest_id: int) -> list[str]:
    return [
        str(b.text)
        for b in store.chunks.list_chunks_for_ref(quest_id)
        if b.chunk_kind == "quest_log"
    ]


def test_plan_opened_writes_a_logbook_entry(store: Store) -> None:
    quest_id, root_id = _quest_and_root(store)
    gripe_id, mint = _gripe_fix(store, root_id)
    assert _logs(store, quest_id) == [
        f"plan td{mint.root_id}: opened — Fix gr{gripe_id}: Something is off"
    ]


def test_plan_opened_with_no_quest_writes_nothing(store: Store) -> None:
    quest = store.insert_ref(kind="quest", slug=None, title="The quest")
    _gid, _mint = _gripe_fix(store, None)
    assert _logs(store, int(quest.id)) == []


def test_minted_ask_nodes_log_ask_raised(store: Store) -> None:
    """The release-pass approval ask minted inside the plan logs a line."""
    quest_id, root_id = _quest_and_root(store)
    mint = T.mint_release_pass(store, root_id)
    logs = _logs(store, quest_id)
    assert any(t.startswith(f"plan td{mint.root_id}: opened") for t in logs)
    asks = [t for t in logs if "ask" in t and "raised" in t]
    assert asks == [
        f"plan td{mint.root_id}: ask td{mint.ids['approval']} raised — "
        "Deploy the release? (answer 'deploy r<N>' or 'hold')"
    ]


def test_pausing_the_plan_root_logs_it(store: Store, handler: TodoHandler) -> None:
    quest_id, root_id = _quest_and_root(store)
    _gid, mint = _gripe_fix(store, root_id)
    handler.tag(id=mint.root_id, add=["STATUS:paused"])
    assert _logs(store, quest_id)[-1].startswith(
        f"plan td{mint.root_id}: paused — Fix gr"
    )
    # Re-pausing an already-paused root logs no second entry.
    handler.tag(id=mint.root_id, add=["STATUS:paused"])
    assert len(_logs(store, quest_id)) == 2  # opened + paused


def test_retry_cap_enters_rescue_and_logs_it(store: Store) -> None:
    """td584381 S1a: a build node at its retry cap enters rescue (no ask);
    the quest log records the rescue entry, not an ask."""
    quest_id, root_id = _quest_and_root(store)
    _gid, mint = _gripe_fix(store, root_id)
    task = mint.ids["build"]
    store.stamp_ref_meta(task, {"retries": 0})
    job = store.insert_ref(kind="job", slug=None, title="run_slice", parent_id=task)
    store.add_tag(
        int(job.id),
        Tag.closed("STATUS", "failed"),
        set_by="system",
        replace_prefix=True,
    )
    res = pd.run_pass(store)
    assert (res.asks, res.rescued) == (0, 1)
    logs = _logs(store, quest_id)
    assert any(
        t.startswith(
            f"plan td{mint.root_id}: node td{task} entered rescue (builder) "
            "at retry cap ("
        )
        for t in logs
    )
    assert not any(t.startswith(f"plan td{mint.root_id}: ask td") for t in logs)
    # The rescue pass owns the node — the next pass files nothing new.
    res2 = pd.run_pass(store)
    assert (res2.asks, res2.rescued) == (0, 0)
    assert len(_logs(store, quest_id)) == len(logs)


def test_an_ask_put_under_the_plan_logs_it(store: Store, handler: TodoHandler) -> None:
    quest_id, root_id = _quest_and_root(store)
    _gid, mint = _gripe_fix(store, root_id)
    resp = handler.put(
        text="Which lane should the fix take?",
        parent_id=mint.ids["build"],
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["the claude lane", "the codex lane"],
                "blocks": "nothing",
                "context": "triage left the lane open",
            }
        },
        tags=["ask-user"],
    )
    assert resp.ref_id is not None
    assert _logs(store, quest_id)[-1] == (
        f"plan td{mint.root_id}: ask td{resp.ref_id} raised — "
        "Which lane should the fix take?"
    )


def test_an_ask_outside_a_plan_logs_nothing(store: Store, handler: TodoHandler) -> None:
    quest_id, root_id = _quest_and_root(store)
    resp = handler.put(
        text="A question with no plan above it?",
        parent_id=root_id,
        meta={
            "ask": {
                "kind": "ruling",
                "options": ["yes", "no"],
                "blocks": "nothing",
            }
        },
        tags=["ask-user"],
    )
    assert resp.ref_id is not None
    assert _logs(store, quest_id) == []
