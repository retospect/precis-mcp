"""The draft reader exposes current tree brakes and safe owner actions."""

from __future__ import annotations

import pytest

from precis.store import Store
from precis.store.types import Tag
from precis_web.deps import templates
from precis_web.routes.smartdraft import _work_states


def _draft_tree(store: Store) -> tuple[int, int]:
    draft = store.insert_ref(kind="draft", slug="halted-draft", title="Draft")
    todo = store.insert_ref(kind="todo", slug=None, title="Write the draft")
    store.add_link(src_ref_id=draft.id, dst_ref_id=todo.id, relation="draft-of")
    return int(draft.id), int(todo.id)


def _html(store: Store, draft_id: int) -> str:
    return templates.env.get_template("smartdraft/_work_state.html.j2").render(
        work_states=_work_states(store, draft_id)
    )


def test_banner_carries_runner_context_and_existing_resume(store: Store) -> None:
    draft_id, todo_id = _draft_tree(store)
    store.update_ref(
        todo_id,
        meta_patch={
            "halt": {
                "class": "misconceived",
                "at": "2026-11-10T09:30:00+01:00",
                "summary_first_line": "The requested evidence does not exist.",
            }
        },
    )
    store.add_tag(todo_id, Tag.open("halt:misconceived"), set_by="system")

    html = _html(store, draft_id)

    assert "Work halted · misconceived" in html
    assert "2026-11-10 08:30 UTC" in html
    assert "The requested evidence does not exist." in html
    assert f'action="/todo/{todo_id}/start"' in html


def test_later_owner_hold_does_not_display_an_old_recovered_halt(store: Store) -> None:
    draft_id, todo_id = _draft_tree(store)
    store.update_ref(
        todo_id,
        meta_patch={
            "halt": {
                "class": "infra",
                "tag": "waiting-for:infra",
                "summary_first_line": "An old outage that has recovered.",
            }
        },
    )
    store.add_tag(todo_id, Tag.open("halt:owner-review"), set_by="user")
    html = _html(store, draft_id)
    assert "halt:owner-review" in html
    assert "An old outage that has recovered." not in html


@pytest.mark.parametrize(
    "park", ["waiting-for:infra", "waiting-for:plan-repair", "waiting-for:outage-123"]
)
def test_parked_descendant_is_visible_without_bypassing_recovery(
    store: Store, park: str
) -> None:
    draft_id, parent_id = _draft_tree(store)
    child = store.insert_ref(
        kind="todo",
        slug=None,
        title="Acquire evidence",
        parent_id=parent_id,
        meta={
            "halt": {
                "class": "misconceived"
                if park == "waiting-for:plan-repair"
                else "infra"
            }
        },
    )
    store.add_tag(child.id, Tag.open(park), set_by="system")

    html = _html(store, draft_id)

    assert "Work parked" in html
    assert "Waiting for a safe recovery attempt." in html
    assert f"/todo?focus={child.id}" in html
    assert "/start" not in html


def test_uncertain_outcome_hold_has_no_resume_bypass(store: Store) -> None:
    draft_id, todo_id = _draft_tree(store)
    store.add_tag(todo_id, Tag.open("halt:misconceived"), set_by="system")
    store.add_tag(todo_id, Tag.open("ask-user:uncertain-halt-outcome"), set_by="system")

    html = _html(store, draft_id)

    assert "uncertain-halt-outcome" in html
    assert "/start" not in html


def test_banner_excludes_terminal_and_unrelated_work_and_escapes_reason(
    store: Store,
) -> None:
    draft_id, todo_id = _draft_tree(store)
    store.add_tag(todo_id, Tag.open("child-failed:123"), set_by="system")
    store.update_ref(
        todo_id,
        meta_patch={"halt": {"summary_first_line": "<script>alert(1)</script>"}},
    )
    other = store.insert_ref(kind="todo", slug=None, title="Unrelated")
    store.add_tag(other.id, Tag.open("halt:unrelated"), set_by="system")

    html = _html(store, draft_id)
    assert "Work child failed" in html
    assert "&lt;script&gt;" in html
    assert "<script>" not in html
    assert "unrelated" not in html

    store.add_tag(todo_id, Tag.closed("STATUS", "done"), set_by="system")
    assert _work_states(store, draft_id) == []
