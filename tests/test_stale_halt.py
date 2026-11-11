"""Legacy halted work stays visible without being retried blindly."""

from __future__ import annotations

import pytest

from precis.alerts import list_open_alerts
from precis.store import Store
from precis.store.types import Tag
from precis.workers import nursery


def _halt(store: Store, *, tag: str = "halt:agent-declared", days: int = 15) -> int:
    ref = store.insert_ref(kind="todo", slug=None, title="Stranded planner")
    store.add_tag(ref.id, Tag.open(tag), set_by="system")
    with store.tx() as conn:
        conn.execute(
            "UPDATE ref_tags rt SET created_at = now() - %s::interval "
            "FROM tags t WHERE rt.tag_id = t.tag_id AND rt.ref_id = %s "
            "AND t.namespace = 'OPEN' AND t.value = %s",
            (f"{days} days", ref.id, tag),
        )
    return int(ref.id)


def test_legacy_halt_with_completed_job_child_is_visible(store: Store) -> None:
    todo_id = _halt(store)
    job = store.insert_ref(
        kind="job", slug=None, title="Producing tick", parent_id=todo_id
    )
    store.add_tag(job.id, Tag.closed("STATUS", "succeeded"), set_by="system")

    findings = nursery._detect_stale_halts(store)

    assert [finding.ref_id for finding in findings] == [todo_id]
    assert "halt:agent-declared" in findings[0].detail
    assert "UTC" in findings[0].detail


@pytest.mark.parametrize("terminal", ["done", "won't-do", "auto-timeout"])
def test_completed_todo_does_not_alert(store: Store, terminal: str) -> None:
    todo_id = _halt(store)
    store.add_tag(todo_id, Tag.closed("STATUS", terminal), set_by="system")

    assert nursery._detect_stale_halts(store) == []


def test_fresh_halt_and_live_todo_parent_do_not_alert(store: Store) -> None:
    _halt(store, days=1)
    parent_id = _halt(store)
    store.insert_ref(kind="todo", slug=None, title="Still working", parent_id=parent_id)

    assert nursery._detect_stale_halts(store) == []


def test_retired_todo_and_wrong_namespace_do_not_alert(store: Store) -> None:
    retired_id = _halt(store)
    other_id = _halt(store)
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE ref_id = %s", (retired_id,)
        )
        conn.execute(
            "INSERT INTO tags (namespace, value) VALUES ('FLAG', 'halt:agent-declared') "
            "ON CONFLICT (namespace, value) DO NOTHING"
        )
        conn.execute(
            "UPDATE ref_tags SET tag_id = (SELECT tag_id FROM tags "
            "WHERE namespace = 'FLAG' AND value = 'halt:agent-declared') "
            "WHERE ref_id = %s AND tag_id = (SELECT tag_id FROM tags "
            "WHERE namespace = 'OPEN' AND value = 'halt:agent-declared')",
            (other_id,),
        )

    assert nursery._detect_stale_halts(store) == []


def test_terminal_halt_needs_missing_context_but_legacy_always_surfaces(
    store: Store,
) -> None:
    contextless = _halt(store, tag="halt:misconceived")
    contextual = _halt(store, tag="halt:cost-cap")
    legacy = _halt(store)
    for todo_id in (contextual, legacy):
        store.update_ref(todo_id, meta_patch={"halt": {"class": "infra"}})

    assert {finding.ref_id for finding in nursery._detect_stale_halts(store)} == {
        contextless,
        legacy,
    }


def test_stale_halt_dedups_and_resolves_when_cleared(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    todo_id = _halt(store)
    monkeypatch.setattr(
        nursery, "_DETECTORS", (("stale-halt", nursery._detect_stale_halts),)
    )

    nursery.run_nursery_pass(store)
    nursery.run_nursery_pass(store)
    alerts = list_open_alerts(store)
    assert len(alerts) == 1
    assert alerts[0]["source"] == "nursery:stale-halt"
    assert alerts[0]["severity"] == "info"
    assert alerts[0]["seen_count"] >= 2

    store.remove_tag(todo_id, Tag.open("halt:agent-declared"))
    result = nursery.run_nursery_pass(store)
    assert result.ok == 1
    assert list_open_alerts(store) == []
