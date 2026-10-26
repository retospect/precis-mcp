"""``/gripes`` status chips (gr520298): open rows inline, every other
status a chip whose rows lazy-load through ``GET /gripes/rows``.

Page load is one ``COUNT … GROUP BY`` status plus the open rows — the
closed/merged/duplicate cohort (``done``/``wontfix``/off-vocab values
and the soft-deleted ``retired`` bucket) renders as counts, not tables.
"""

from typing import Any

import pytest

from precis.store import Tag
from precis_web.routes.gripes import _rows
from tests._refs import insert_gripe


def _seed_off_vocab_gripe(store: Any, title: str, status: str) -> int:
    """A gripe carrying an off-vocabulary STATUS — the drift the
    migration-0176 trigger now refuses, simulated with triggers bypassed
    (the replica role skips non-ALWAYS triggers; the same trick
    ``test_gripe_status_vocab`` uses to strip a STATUS row)."""
    with store.tx() as conn:
        conn.execute("SET LOCAL session_replication_role = replica")
        ref = store.insert_ref(kind="gripe", slug=None, title=title, meta={}, conn=conn)
        store.add_tag(ref.id, Tag.closed("STATUS", status), set_by="agent", conn=conn)
    return int(ref.id)


def test_initial_load_one_grouped_count_plus_open_rows_only(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """Acceptance (a): ``GET /gripes`` issues exactly one grouped count
    and fetches only the open rows — every other status is a chip whose
    rows stay in the DB until its first expand."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-open", status="open")
    insert_gripe(store, "fixture-triaged", status="triaged")
    insert_gripe(store, "fixture-done", status="done")
    retired = insert_gripe(store, "fixture-retired", status="wontfix")
    store.retire_ref(retired.id)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    counts_calls = 0
    rows_calls: list[str] = []
    real_counts, real_rows = gripes._status_counts, gripes._rows

    def counts_spy(s: Any) -> Any:
        nonlocal counts_calls
        counts_calls += 1
        return real_counts(s)

    def rows_spy(s: Any, *, status_filter: str) -> Any:
        rows_calls.append(status_filter)
        return real_rows(s, status_filter=status_filter)

    monkeypatch.setattr(gripes, "_status_counts", counts_spy)
    monkeypatch.setattr(gripes, "_rows", rows_spy)

    live = client.get("/gripes")
    assert live.status_code == 200
    assert counts_calls == 1
    assert rows_calls == ["open"]
    assert "fixture-open" in live.text
    for hidden in ("fixture-triaged", "fixture-done", "fixture-retired"):
        assert hidden not in live.text
    # …but their chips — retired included — are there on the shared
    # lazy path.
    for state in ("triaged", "done", "retired"):
        assert f'hx-get="/gripes/rows?status={state}"' in live.text


@pytest.mark.parametrize(
    "status",
    ["triaged", "closed", "merged", "duplicate", "retired"],
)
def test_rows_endpoint_serves_each_non_open_status(
    store: Any, client: Any, monkeypatch: Any, status: str
) -> None:
    """Acceptance (b)+(c): ``/gripes/rows`` returns rows for whatever the
    chips reported — a workflow status (triaged keeps working, on the
    shared path), closed/merged/duplicate drift, or the retired
    bucket."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-open", status="open")
    if status == "retired":
        ref = insert_gripe(store, f"fixture-{status}", status="done")
        store.retire_ref(ref.id)
    elif status in ("closed", "merged", "duplicate"):
        _seed_off_vocab_gripe(store, f"fixture-{status}", status)
    else:
        insert_gripe(store, f"fixture-{status}", status=status)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    frag = client.get(f"/gripes/rows?status={status}")
    assert frag.status_code == 200
    assert f"fixture-{status}" in frag.text
    assert "fixture-open" not in frag.text


def test_rows_endpoint_returns_only_that_statuss_rows(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """Each status's fragment names its own rows and no other's — and a
    bogus status binds as a parameter, not an error."""
    from precis_web.routes import gripes

    for state in ("open", "triaged", "done"):
        insert_gripe(store, f"fixture-only-{state}", status=state)
    retired = insert_gripe(store, "fixture-only-retired", status="wontfix")
    store.retire_ref(retired.id)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    for state in ("open", "triaged", "done", "retired"):
        frag = client.get(f"/gripes/rows?status={state}")
        assert frag.status_code == 200, state
        assert f"fixture-only-{state}" in frag.text
        for other in ("open", "triaged", "done", "retired"):
            if other != state:
                assert f"fixture-only-{other}" not in frag.text
    bogus = client.get("/gripes/rows?status=bogus%3Bdrop")
    assert bogus.status_code == 200
    assert "No gripes with status" in bogus.text


def test_every_status_renders_as_a_collapsible_chip(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """Every status present is a ``<details>`` chip in workflow order:
    open expanded with its rows inline; the rest collapsed behind an
    hx-get — none of their rows in the served HTML."""
    from precis_web.routes import gripes

    for state in ("open", "triaged", "done"):
        insert_gripe(store, f"fixture-panel-{state}", status=state)
    retired = insert_gripe(store, "fixture-panel-retired", status="wontfix")
    store.retire_ref(retired.id)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    live = client.get("/gripes").text
    assert '<details class="mb-3 group" data-status="open" open>' in live
    assert "fixture-panel-open" in live
    for state in ("triaged", "done", "retired"):
        assert f'data-status="{state}"' in live
        assert f'hx-get="/gripes/rows?status={state}"' in live
    for hidden in (
        "fixture-panel-triaged",
        "fixture-panel-done",
        "fixture-panel-retired",
    ):
        assert hidden not in live


def test_open_chip_renders_with_zero_rows(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """No open gripes: the open chip still renders (count 0, expanded)
    with the friendly empty state, and a rows hit for an empty status
    gets the same line."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-populated-triaged", status="triaged")
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    live = client.get("/gripes")
    assert live.status_code == 200
    assert 'data-status="open" open' in live.text
    assert "No open gripes" in live.text
    assert "fixture-populated-triaged" not in live.text
    frag = client.get("/gripes/rows?status=verify")
    assert frag.status_code == 200
    assert "No gripes with status verify." in frag.text


def test_retired_gripe_detail_stays_readable(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """A retired chip's rows still land somewhere — the detail renders
    body + comments read-only (controls hidden), not a NotFound."""
    from precis_web.routes import gripes

    ref = insert_gripe(store, "fixture-retired-detail", status="done")
    store.retire_ref(ref.id)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    page = client.get(f"/gripes/{ref.id}")
    assert page.status_code == 200
    assert "fixture-retired-detail" in page.text
    assert "Retired" in page.text
    assert 'name="text"' not in page.text  # comment box hidden


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
        ids[name] = insert_gripe(store, title).id
    by_id = {r["ref_id"]: r for r in _rows(store, status_filter="live")}
    assert by_id[ids["blank"]]["title_preview"] == "first line preview"
    assert by_id[ids["long"]]["title_preview"] == "y" * 99 + "…"
