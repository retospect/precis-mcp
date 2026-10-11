"""Clickable gripe counts must lead to a real filtered list, not only an href."""

from typing import Any

import pytest

from precis.store import Tag
from precis_web.routes.gripes import _rows


@pytest.mark.parametrize(
    "status", ["open", "triaged", "ready_for_fix", "in_review", "done", "wontfix"]
)
def test_status_count_navigation(
    store: Any, client: Any, monkeypatch: Any, status: str
) -> None:
    from precis_web.routes import gripes

    for state in ["open", "triaged", "ready_for_fix", "in_review", "done", "wontfix"]:
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="gripe", slug=None, title=f"fixture-count-{state}", conn=conn
            )
            store.add_tag(ref.id, Tag.closed("STATUS", state), conn=conn)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)
    live = client.get("/gripes")
    assert live.status_code == 200
    assert 'href="/gripes?status=live"' in live.text
    for state in ["open", "triaged", "ready_for_fix", "in_review"]:
        assert f'href="/gripes?status={state}"' in live.text
    filtered = client.get(f"/gripes?status={status}")
    assert filtered.status_code == 200
    assert f"fixture-count-{status}" in filtered.text
    for state in ["open", "triaged", "ready_for_fix", "in_review", "done", "wontfix"]:
        if state != status:
            assert f"fixture-count-{state}" not in filtered.text
    assert len(_rows(store, status_filter="live")) == 4
    invalid = client.get("/gripes?status=not-a-status")
    assert "fixture-count-open" in invalid.text
    assert "fixture-count-done" not in invalid.text


def test_empty_exact_status_names_selected_status(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    from precis_web.routes import gripes

    with store.tx() as conn:
        ref = store.insert_ref(
            kind="gripe", slug=None, title="fixture-populated-triaged", conn=conn
        )
        store.add_tag(ref.id, Tag.closed("STATUS", "triaged"), conn=conn)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    response = client.get("/gripes?status=open")

    assert response.status_code == 200
    assert "No gripes with status open." in response.text
    assert "No live gripes." not in response.text
    assert "fixture-populated-triaged" not in response.text


def test_live_view_renders_each_status_as_a_collapsible_panel(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """Every live status is its own <details> panel, collapsed in the live
    view so all headers and counts show at the top; a single-status view
    opens its one panel."""
    from precis_web.routes import gripes

    for state in ["open", "triaged", "in_review"]:
        with store.tx() as conn:
            ref = store.insert_ref(
                kind="gripe", slug=None, title=f"fixture-panel-{state}", conn=conn
            )
            store.add_tag(ref.id, Tag.closed("STATUS", state), conn=conn)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    live = client.get("/gripes").text
    for state in ["open", "triaged", "in_review"]:
        assert f'<details class="mb-3 group" data-status="{state}">' in live
        assert f"fixture-panel-{state}" in live

    triaged = client.get("/gripes?status=triaged").text
    assert '<details class="mb-3 group" data-status="triaged" open>' in triaged


def test_list_preview_survives_sql_title_truncation(store: Any) -> None:
    """gr477858: the list query cuts the title in SQL (the page shows one
    line, the whole filed text was ~1 MB over the wire); leading blank
    lines must not eat the preview, and a long first line still truncates."""
    from precis_web.routes.gripes import _PREVIEW_FETCH_CHARS

    assert _PREVIEW_FETCH_CHARS >= 100
    titles = {
        "blank": "\n  \nfirst line preview\n" + "x" * 8000,
        "long": "y" * 8000,
    }
    ids = {}
    for name, title in titles.items():
        with store.tx() as conn:
            ref = store.insert_ref(kind="gripe", slug=None, title=title, conn=conn)
            store.add_tag(ref.id, Tag.closed("STATUS", "open"), conn=conn)
        ids[name] = ref.id
    by_id = {r["ref_id"]: r for r in _rows(store, status_filter="live")}
    assert by_id[ids["blank"]]["title_preview"] == "first line preview"
    assert by_id[ids["long"]]["title_preview"] == "y" * 99 + "…"
