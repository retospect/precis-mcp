"""``/gripes`` status chips (gr520298): open rows inline, every other
status a chip whose rows lazy-load through ``GET /gripes/rows``.

Page load is one ``COUNT … GROUP BY`` status plus the open rows — the
closed/merged/duplicate cohort (``done``/``wontfix``/off-vocab values
and the soft-deleted ``retired`` bucket) renders as counts, not tables.
"""

import re
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

    def rows_spy(s: Any, *, status_filter: str, **kw: Any) -> Any:
        rows_calls.append(status_filter)
        return real_rows(s, status_filter=status_filter, **kw)

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
    ["triaged", "retired"],
)
def test_rows_endpoint_serves_each_non_open_status(
    store: Any, client: Any, monkeypatch: Any, status: str
) -> None:
    """Acceptance (b)+(c): ``/gripes/rows`` returns rows for a
    non-open chip — a workflow status (triaged keeps working, on the
    shared path) or the retired bucket."""
    from precis_web.routes import gripes

    insert_gripe(store, "fixture-open", status="open")
    if status == "retired":
        ref = insert_gripe(store, f"fixture-{status}", status="done")
        store.retire_ref(ref.id)
    else:
        insert_gripe(store, f"fixture-{status}", status=status)
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    frag = client.get(f"/gripes/rows?status={status}")
    assert frag.status_code == 200
    assert f"fixture-{status}" in frag.text
    assert "fixture-open" not in frag.text


@pytest.mark.parametrize("status", ["closed", "bogus", "refuted"])
def test_rows_endpoint_rejects_status_outside_the_allowlist(
    store: Any, client: Any, monkeypatch: Any, status: str
) -> None:
    """``?status=`` is allowlisted to the STATUS vocabulary plus
    retired/all/live plus every value the grouped count returns — a
    value in none of those (off-vocabulary drift like a legacy
    ``closed`` when no gripe carries it) is a 400 naming the allowed
    list, not a silent "no gripes" empty state."""
    from precis_web.routes import gripes

    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    resp = client.get(f"/gripes/rows?status={status}")
    assert resp.status_code == 400
    for allowed in ("ready_for_fix", "retired", "all", "live"):
        assert allowed in resp.text


def test_rows_endpoint_serves_a_drift_status_the_count_surfaces(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """GRIPE_FIX ruling: a status the grouped count surfaces stays
    expandable even off-vocabulary — a legacy ``closed`` chip's rows
    fragment serves its gripes rather than 400ing."""
    from precis_web.routes import gripes

    _seed_off_vocab_gripe(store, "fixture-drift-closed", "closed")
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    assert 'data-status="closed"' in client.get("/gripes").text
    frag = client.get("/gripes/rows?status=closed")
    assert frag.status_code == 200
    assert "fixture-drift-closed" in frag.text


def test_rows_fragment_pages_at_one_hundred(
    store: Any, client: Any, monkeypatch: Any
) -> None:
    """The lazy fragment caps at ``_ROWS_PAGE`` rows, newest first,
    with a "show more" row fetching the next page via ``offset=`` —
    a 250-row status serves 100 + the link, then the next 100. The
    fixture's ``updated_at`` is tied into buckets (bulk updates land
    rows on one timestamp): the ``ref_id`` ORDER-BY tiebreaker keeps
    the three pages disjoint — 250 distinct ids, none repeated or
    skipped."""
    from precis_web.routes import gripes
    from precis_web.routes.gripes import _ROWS_PAGE

    refs = [
        insert_gripe(store, f"fixture-page-{i:04}", status="done")
        for i in range(_ROWS_PAGE * 2 + 50)
    ]
    with store.tx() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '1 minute'"
            " * mod(ref_id, 7) WHERE kind = 'gripe'"
        )
    monkeypatch.setattr(gripes, "get_store", lambda request: store)

    first = client.get("/gripes/rows?status=done")
    assert first.status_code == 200
    assert first.text.count("<tr data-row ") == _ROWS_PAGE
    assert f"/gripes/rows?status=done&offset={_ROWS_PAGE}" in first.text

    second = client.get(f"/gripes/rows?status=done&offset={_ROWS_PAGE}")
    assert second.status_code == 200
    assert second.text.count("<tr data-row ") == _ROWS_PAGE
    # A page-2+ fragment is bare <tr>s — no table wrapper: it swaps
    # into the tbody in place of the show-more row that fetched it.
    assert "<table" not in second.text
    assert f"offset={_ROWS_PAGE * 2}" in second.text

    third = client.get(f"/gripes/rows?status=done&offset={_ROWS_PAGE * 2}")
    assert third.status_code == 200
    assert third.text.count("<tr data-row ") == 50
    assert "show more" not in third.text

    ids = [
        int(m)
        for page in (first.text, second.text, third.text)
        for m in re.findall(r'href="/gripes/(\d+)"', page)
    ]
    assert len(ids) == len(set(ids)) == _ROWS_PAGE * 2 + 50
    assert set(ids) == {r.id for r in refs}


def test_retired_gripe_refuses_web_mutations(
    store: Any, runtime_with_store: Any, tmp_path: Any
) -> None:
    """The detail page hides a retired gripe's controls, but the guard
    is server-side: the ``tag``/``put``/``delete`` verbs resolve the
    live ref and answer ``Gone`` on a soft-deleted gripe, which the
    routes surface as a 400 naming the soft-delete — not a silent
    redirect."""
    from fastapi.testclient import TestClient

    from precis_web.app import create_app
    from precis_web.config import WebConfig

    ref = insert_gripe(store, "fixture-retired-writes", status="done")
    store.retire_ref(ref.id)
    real_client = TestClient(
        create_app(
            runtime=runtime_with_store,
            web_config=WebConfig(corpus_dir=tmp_path),
        )
    )
    attempts = [
        real_client.post(f"/gripes/{ref.id}/status", data={"value": "triaged"}),
        real_client.post(
            f"/gripes/{ref.id}/status",
            data={"value": "triaged"},
            headers={"HX-Request": "true"},
        ),
        real_client.post(f"/gripes/{ref.id}/comment", data={"text": "still writable?"}),
        real_client.post(f"/gripes/{ref.id}/retire"),
    ]
    for resp in attempts:
        assert resp.status_code == 400
        assert "soft-deleted" in resp.text


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
    assert bogus.status_code == 400


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
