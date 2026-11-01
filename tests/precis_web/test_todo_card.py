"""gr548796 / gr548797 — the todo handle page and the two-line card.

gr548796: ``/c/td<id>`` (→ ``/todo?focus=<id>``) links to the node's
fisheye render at ``/eye/td<id>``; a markdown list in a todo's details
body renders as a real list; done items read muted instead of
struck-through. gr548797: the row is a two-line card — line 1 the chips
(short tag labels, relative times), line 2 an expand button + title,
everything else under the expand control.
"""

from __future__ import annotations

from typing import Any

import pytest
from fastapi.testclient import TestClient

from precis_web.routes import todo as todo_routes


def _row(**kw: Any) -> dict[str, Any]:
    """A todo row in the shape ``_build_rows`` emits."""
    row: dict[str, Any] = {
        "id": 1,
        "kind": "todo",
        "parent_id": None,
        "title": "Build the thing",
        "body": "",
        "gist": "",
        "status": "open",
        "level": "subtask",
        "depth": 0,
        "created_at": None,
        "status_since": None,
        "started_at": None,
        "timing": "",
        "stuck": False,
        "rollup": {"active": 0, "waiting": 0, "done": 0, "total": 0},
        "is_leaf": True,
        "locked": False,
        "lease_until": None,
        "lease_active": False,
        "halted": False,
        "tags": [],
        "child_failures": [],
        "attention_icons": [],
        "note": "",
    }
    row.update(kw)
    return row


def _one_row_app(monkeypatch: pytest.MonkeyPatch, row: dict[str, Any]) -> None:
    monkeypatch.setattr(todo_routes, "_build_rows", lambda *a, **k: [row])


def test_focus_page_links_the_fisheye_render(client: TestClient) -> None:
    """The td handle page carries its ``/eye/td<id>`` link (gr548796)."""
    resp = client.get("/todo?focus=1")
    assert resp.status_code == 200
    assert 'href="/eye/td1"' in resp.text


def test_details_ordered_list_renders_as_ol(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A markdown ordered list in the details body becomes ``<ol>``."""
    _one_row_app(
        monkeypatch,
        _row(body="Steps:\n1. read-only work tree\n2. loop chunks\n\nDone."),
    )
    resp = client.get("/todo?focus=1")
    assert resp.status_code == 200
    assert "<ol>" in resp.text
    assert "<li>read-only work tree" in resp.text


def _render_row(row: dict[str, Any], **ctx: Any) -> str:
    """Render the card partial standalone (macros stubbed)."""
    from precis_web.deps import templates

    tpl = templates.env.get_template("todo/_row.html.j2")
    base: dict[str, Any] = {
        "focus_id": None,
        "status_choices": todo_routes.STATUS_CHOICES,
        "require_tags": [],
        "exclude_tags": [],
        "show_jobs": "active",
        "serves_id": None,
        "tree_depth": None,
        "filter_inputs": lambda: "",
        "state_qs": lambda *a, **k: "",
        "planner_models": lambda: [],
    }
    base.update(ctx)
    return tpl.render(r=row, **base)


def test_card_partial_two_lines_with_expand() -> None:
    """The todo card renders a meta line, a title line with the expand
    control, and the expanded region (gr548797)."""
    html = _render_row(_row(tags=["thread:factory"]))
    assert html.count('data-row-line="') == 2
    assert 'data-row-line="meta"' in html
    assert 'data-row-line="title"' in html
    assert "data-card-expand" in html
    assert "data-card-detail" in html
    # The expand button precedes the title text on line 2.
    assert html.index("data-card-expand") < html.index("Build the thing")
    # Short tag label on line 1, full tag on the chip's hover.
    assert 'title="thread:factory"' in html
    assert ">factory<" in html
    assert ">thread:factory<" not in html


def test_card_done_reads_muted_not_struck_through() -> None:
    """A done todo is grey with a ✓ chip — no line-through (gr548796)."""
    html = _render_row(_row(status="done"))
    assert "line-through" not in html
    assert ">✓<" in html
