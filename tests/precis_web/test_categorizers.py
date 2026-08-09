"""Tests for the ``/categorizers`` dashboard + its live enable/disable toggle.

Two layers, mirroring ``test_env_context.py`` / ``test_status_sql.py``:
the fast FakeStore ``client`` fixture for page-shell/route-shape checks
(FakeStore's pool always returns empty rows, so every coverage count
degrades to 0/0 — no schema surprise possible there), plus a real-DB
layer (the shared test Postgres, via the root ``store`` fixture) proving
the actual coverage SQL, the ``service_config``-backed effective-state
read, and the toggle endpoint's writes against seeded chunks/refs/tags.
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store import Store
from precis.store.types import Tag
from precis.workers.service_config import (
    ALL_HOSTS,
    clear_service_config,
    list_service_config,
    set_service_concurrency,
    set_service_prio,
)
from precis_web.app import create_app
from precis_web.routes import categorizers as cz

# ── fast route-shell layer (FakeStore) ──────────────────────────────


def test_categorizers_page_lists_axes_and_topics(client: TestClient) -> None:
    resp = client.get("/categorizers")
    assert resp.status_code == 200
    body = resp.text
    assert "Categorizers" in body

    # A wired chunk-level axis (role3), a dormant ref-level axis (domain),
    # and a topic — all show up in the unified table.
    assert "role3" in body
    assert "domain" in body
    assert "mof" in body  # src/precis/data/topics/mof.yaml

    # Granularity buckets per the level:chunk / level:ref+topics split.
    assert "chunk" in body
    assert "paper+patent" in body

    # Prereq display: dim.yaml declares prereq: [domain].
    assert "domain" in body  # (also as a prereq value on the dim row)

    # Active-state badges: every governing service is default-OFF in the
    # test env (no PRECIS_*_ENABLED / PRECIS_AXES_ENABLED, no
    # service_config row) -> every row reads "off".
    assert "off" in body

    # Every row carries a toggle posting to the live service_config flip.
    assert 'action="/categorizers/toggle"' in body
    assert 'name="service"' in body

    # Every row also carries the live concurrency knob (thread-pool width).
    assert 'action="/categorizers/concurrency"' in body
    assert 'name="concurrency"' in body

    # The heavy coverage panel is deferred to the htmx fragment (OOB swap
    # into per-row placeholders), not computed inline nor rendered as a
    # separate section on the shell page.
    assert 'hx-get="/categorizers/progress"' in body
    assert 'id="lastproc-role3"' in body
    assert 'id="cov-role3"' in body
    assert "<h2" not in body
    assert "↻ refresh coverage" in body


def test_categorizers_page_has_kill_switch_control(client: TestClient) -> None:
    """The global ``classify_topics`` kill-switch is a live On/Off/Default
    control on the page, not just the read-only amber banner."""
    resp = client.get("/categorizers")
    assert resp.status_code == 200
    body = resp.text
    assert "global kill-switch" in body.lower()
    assert 'value="classify_topics"' in body
    assert ">On<" in body
    assert ">Off<" in body
    assert ">Default<" in body


def test_categorizers_page_kill_switch_renders_when_forced_off(
    real_client: TestClient, store: Store
) -> None:
    """An explicit prio-0 ``classify_topics`` row still leaves the On/Off/
    Default control on the page (so an operator can turn it back on),
    alongside the existing amber warning banner."""
    try:
        set_service_prio(store, ALL_HOSTS, "classify_topics", 0, actor="test")
        resp = real_client.get("/categorizers")
        assert resp.status_code == 200
        body = resp.text
        assert "force-disabled" in body  # the existing amber banner
        assert 'value="classify_topics"' in body
        assert ">On<" in body
        assert ">Off<" in body
        assert ">Default<" in body
    finally:
        clear_service_config(store, ALL_HOSTS, "classify_topics")


def test_axis_row_service_mapping() -> None:
    """Each non-cascade axis governs its own ``axis:<id>`` service;
    role3/junk both map to the shared ``classify`` cascade service."""
    effective: dict[str, dict[str, object]] = {}
    rows = {str(a["id"]): cz._axis_row(a, effective) for a in cz._load_axes()}
    assert rows["domain"]["service"] == "axis:domain"
    assert rows["material"]["service"] == "axis:material"
    assert rows["role3"]["service"] == "classify"
    assert rows["junk"]["service"] == "classify"
    # Every row defaults to "off" state + non-overridden with no
    # service_config rows supplied.
    assert rows["domain"]["status"] == "off"
    assert rows["domain"]["overridden"] is False


def test_topic_row_service_mapping() -> None:
    """Every topic governs its own ``topic:<slug>`` service — no
    longer the shared ``classify_topics`` service — and carries no
    shared-toggle note."""
    effective: dict[str, dict[str, object]] = {}
    rows = [cz._topic_row(t, effective, None) for t in cz._load_topics()]
    assert rows
    assert all(r["service"] == f"topic:{r['name']}" for r in rows)
    assert all(r["shared_note"] is None for r in rows)


def test_axis_row_includes_prompt_preview() -> None:
    """#5 — each axis row carries its actual (system, user) LLM prompt for
    the /categorizers hover popover, built in-process from the real
    ``workers/axis_pass.prompt_preview``."""
    effective: dict[str, dict[str, object]] = {}
    rows = {str(a["id"]): cz._axis_row(a, effective) for a in cz._load_axes()}
    preview = rows["domain"]["prompt_preview"]
    assert preview is not None
    assert preview["system"]
    assert preview["user"]


def test_topic_row_includes_passed_through_prompt_preview() -> None:
    """The topics preview is computed once by the caller (shared across topic
    rows, since the pass sends one multi-label prompt over the whole
    taxonomy) and threaded through unchanged."""
    effective: dict[str, dict[str, object]] = {}
    shared_preview = {"system": "sys", "user": "usr"}
    rows = [cz._topic_row(t, effective, shared_preview) for t in cz._load_topics()]
    assert rows
    assert all(r["prompt_preview"] == shared_preview for r in rows)


def test_allowed_services_includes_per_topic_and_kill_switch() -> None:
    """Every topic gets its own ``topic:<slug>`` service, and the
    shared ``classify_topics`` service is retained as the global
    kill-switch target."""
    allowed = cz._allowed_services()
    assert "topic:nh3-synthesis" in allowed
    assert "classify_topics" in allowed


def test_categorizers_page_topic_row_has_drive_chip(client: TestClient) -> None:
    """A topic row renders a click-through chip deep-linking to /drive
    filtered by its ``topic:<slug>`` tag (src/precis/data/topics/llm.yaml)."""
    resp = client.get("/categorizers")
    assert resp.status_code == 200
    body = resp.text
    assert (
        'href="/drive?submitted=1&amp;k=paper&amp;k=patent&amp;tag=topic%3Allm&amp;sort=recency"'
        in body
    )


def test_categorizers_page_ref_axis_row_has_drive_chips_per_value(
    client: TestClient,
) -> None:
    """A ref-level axis (domain) renders one chip per YAML ``values:`` entry,
    tagged in its uppercased namespace."""
    resp = client.get("/categorizers")
    assert resp.status_code == 200
    body = resp.text
    assert (
        'href="/drive?submitted=1&amp;k=paper&amp;k=patent&amp;tag=DOMAIN%3Achemistry&amp;sort=recency"'
        in body
    )


def test_categorizers_page_chunk_axis_row_has_no_drive_chips(
    client: TestClient,
) -> None:
    """A chunk-level axis (role3) carries no chips — its tags live on
    chunks, not papers, so there's nothing for /drive's ref-level facet to
    filter on."""
    resp = client.get("/categorizers")
    assert resp.status_code == 200
    assert "tag=ROLE3%3A" not in resp.text


def test_categorizers_nav_entry_present(client: TestClient) -> None:
    resp = client.get("/status")
    assert resp.status_code == 200
    assert 'href="/categorizers"' in resp.text


def test_categorizers_progress_fragment_renders(client: TestClient) -> None:
    resp = client.get("/categorizers/progress")
    assert resp.status_code == 200
    body = resp.text
    # FakeStore's pool always returns empty rows -> every categorizer
    # renders a real (degraded-to-zero) row, not the query-failed state.
    assert "query failed" not in body
    # Each row is an OOB swap patching the matching main-table placeholder,
    # not a standalone section.
    assert 'hx-swap-oob="true"' in body
    assert 'id="lastproc-role3"' in body
    assert 'id="cov-role3"' in body
    assert "hits" in body  # topic hit-count column


# ── real-DB coverage-math layer ──────────────────────────────────────


@pytest.fixture
def real_client(runtime_with_store) -> TestClient:  # type: ignore[no-untyped-def]
    return TestClient(create_app(runtime=runtime_with_store))


def test_chunk_axis_progress_counts_tagged_over_eligible(store: Store) -> None:
    ref = store.insert_ref(
        kind="paper", slug="cz-test-role3-paper", title="a paper", meta={}
    )
    long_text = "a real scientific sentence about catalysts " * 4  # > 120 chars
    with store.pool.connection() as conn:
        row = conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', %s) RETURNING chunk_id",
            (ref.id, long_text),
        ).fetchone()
        assert row is not None
        chunk_id = int(row[0])
        # A second eligible chunk that never gets tagged.
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) VALUES (%s, 1, 'paragraph', %s)",
            (ref.id, long_text),
        )
        conn.commit()

    store.add_tag(ref.id, Tag.closed("ROLE3", "own"), pos=0, set_by="agent")

    chunk_total = cz._chunk_eligible_total(store)
    assert chunk_total >= 2
    done, total, last_ts = cz._chunk_axis_progress(store, "ROLE3", chunk_total)
    assert done >= 1
    assert total == chunk_total
    assert done <= total
    assert last_ts is not None
    # sanity: the chunk we tagged is really the one carrying ROLE3.
    with store.pool.connection() as conn:
        tagged = conn.execute(
            "SELECT ct.chunk_id FROM chunk_tags ct JOIN tags t ON t.tag_id = ct.tag_id "
            "WHERE t.namespace = 'ROLE3'",
        ).fetchall()
    assert any(r[0] == chunk_id for r in tagged)


def test_ref_axis_progress_dormant_reads_zero(store: Store) -> None:
    """No axis writes a ref-level closed tag yet (the non-role3/junk axes
    are default-OFF and nothing has run them) — the query must still run
    cleanly and report 0."""
    total = cz._paper_patent_total(store)
    done, reported_total, last_ts = cz._ref_axis_progress(store, "DOMAIN", total)
    assert done == 0
    assert reported_total == total
    assert last_ts is None


def test_topic_hit_count_and_marker_done(store: Store) -> None:
    ref = store.insert_ref(
        kind="paper", slug="cz-test-mof-paper", title="a mof paper", meta={}
    )
    store.add_tag(ref.id, Tag.open("topic:mof"), set_by="agent")
    marker_value = cz._current_marker_value(["mof"])
    assert marker_value is not None
    store.add_tag(
        ref.id, Tag.closed(cz._TOPIC_MARKER_NAMESPACE, marker_value), set_by="agent"
    )

    hit_count, last_ts = cz._topic_hit_count(store, "mof")
    assert hit_count >= 1
    assert last_ts is not None
    assert cz._topics_marker_done(store, marker_value) >= 1
    # A slug nothing was tagged with reads 0, not an error.
    no_hit_count, no_last_ts = cz._topic_hit_count(store, "no-such-topic-slug")
    assert no_hit_count == 0
    assert no_last_ts is None


def test_categorizers_progress_fragment_renders_against_real_db(
    real_client: TestClient,
) -> None:
    resp = real_client.get("/categorizers/progress")
    assert resp.status_code == 200
    assert "query failed" not in resp.text


def test_progress_fragment_shows_last_minted_timestamp(
    real_client: TestClient, store: Store
) -> None:
    """The coverage fragment folds each row's most-recent-tag timestamp into
    the same aggregate scan (no extra query) — a categorizer with a tag
    reads a real ``ago`` value, one with none reads "never". The fragment
    emits this as an OOB ``lastproc-<name>`` span per categorizer, so slice
    out each row's own span to avoid a sibling row's "never" text bleeding
    into the assertion."""
    ref = store.insert_ref(
        kind="paper", slug="cz-test-last-minted-paper", title="a paper", meta={}
    )
    long_text = "a real scientific sentence about catalysts " * 4  # > 120 chars
    with store.pool.connection() as conn:
        conn.execute(
            "INSERT INTO chunks (ref_id, ord, chunk_kind, text) "
            "VALUES (%s, 0, 'paragraph', %s)",
            (ref.id, long_text),
        )
        conn.commit()
    store.add_tag(ref.id, Tag.closed("ROLE3", "own"), pos=0, set_by="agent")

    progress = cz._progress_rows(store)
    assert progress["role3"]["last_minted"] is not None
    # An axis nothing has ever tagged has no minted timestamp.
    assert progress["domain"]["last_minted"] is None

    resp = real_client.get("/categorizers/progress")
    assert resp.status_code == 200
    body = resp.text

    def _lastproc_span(name: str) -> str:
        """Slice out this row's own ``lastproc-<name>`` OOB span (up to the
        start of its paired ``cov-<name>`` div, which the fragment always
        emits right after) so the assertion doesn't get fooled by a sibling
        row's "never" text."""
        start_marker = f'<span id="lastproc-{name}"'
        end_marker = f'<div id="cov-{name}"'
        start = body.index(start_marker)
        end = body.index(end_marker, start)
        return body[start:end]

    assert "never" not in _lastproc_span("role3")
    assert "never" in _lastproc_span("domain")


# ── real-DB effective-state + toggle-endpoint layer ─────────────────


def test_effective_state_reflects_service_config(store: Store) -> None:
    """A ``service_config`` all-hosts row overrides the env/profile default,
    and a shared-pass override flips every row that pass governs."""
    try:
        # A prio>=1 row forces a default-OFF axis ON, and is flagged overridden.
        set_service_prio(store, ALL_HOSTS, "axis:material", 5, actor="test")
        eff = cz._effective_state(store)
        assert eff["axis:material"]["enabled"] is True
        assert eff["axis:material"]["overridden"] is True

        # A prio 0 row is the live OFF switch.
        set_service_prio(store, ALL_HOSTS, "axis:material", 0, actor="test")
        assert cz._effective_state(store)["axis:material"]["enabled"] is False

        # Overriding the shared `classify` cascade flips BOTH role3 and junk
        # rows (they map to the one service), reflected in the rendered rows.
        set_service_prio(store, ALL_HOSTS, "classify", 5, actor="test")
        eff = cz._effective_state(store)
        rows = {str(a["id"]): cz._axis_row(a, eff) for a in cz._load_axes()}
        assert rows["role3"]["status"] == "active"
        assert rows["junk"]["status"] == "active"
    finally:
        clear_service_config(store, ALL_HOSTS, "axis:material")
        clear_service_config(store, ALL_HOSTS, "classify")


def test_effective_state_includes_independent_per_topic_rows(store: Store) -> None:
    """Each topic's ``topic:<slug>`` state is independent — flipping one
    topic ON does NOT flip a sibling topic (the old shared-toggle behavior
    this step replaces)."""
    try:
        eff = cz._effective_state(store)
        assert "topic:nh3-synthesis" in eff
        assert "topic:mof" in eff

        set_service_prio(store, ALL_HOSTS, "topic:nh3-synthesis", 5, actor="test")
        eff = cz._effective_state(store)
        assert eff["topic:nh3-synthesis"]["enabled"] is True
        assert eff["topic:nh3-synthesis"]["overridden"] is True
        # A sibling topic's state is untouched by the flip above.
        assert eff["topic:mof"]["enabled"] is False
        assert eff["topic:mof"]["overridden"] is False
    finally:
        clear_service_config(store, ALL_HOSTS, "topic:nh3-synthesis")


def test_toggle_endpoint_writes_row_and_rejects_unknown_service(
    real_client: TestClient, store: Store
) -> None:
    """POST /categorizers/toggle upserts/deletes the expected all-hosts
    ``service_config`` row, and refuses a service outside the allow-list."""

    def _rows() -> dict[tuple[str, str], int]:
        return {
            (str(r["host"]), str(r["service"])): int(r["prio"])  # type: ignore[call-overload]
            for r in list_service_config(store)
        }

    try:
        # on -> prio DEFAULT_PRIO
        r = real_client.post(
            "/categorizers/toggle",
            data={"service": "axis:domain", "action": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert _rows()[(ALL_HOSTS, "axis:domain")] == 5

        # off -> prio 0
        real_client.post(
            "/categorizers/toggle",
            data={"service": "axis:domain", "action": "off"},
            follow_redirects=False,
        )
        assert _rows()[(ALL_HOSTS, "axis:domain")] == 0

        # default -> row deleted
        real_client.post(
            "/categorizers/toggle",
            data={"service": "axis:domain", "action": "default"},
            follow_redirects=False,
        )
        assert (ALL_HOSTS, "axis:domain") not in _rows()

        # an unknown service is rejected (303 redirect) with NO row written.
        r = real_client.post(
            "/categorizers/toggle",
            data={"service": "axis:not-a-real-axis", "action": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert (ALL_HOSTS, "axis:not-a-real-axis") not in _rows()
    finally:
        clear_service_config(store, ALL_HOSTS, "axis:domain")


def test_concurrency_endpoint_writes_row_and_rejects_unknown_service(
    real_client: TestClient, store: Store
) -> None:
    """POST /categorizers/concurrency upserts the concurrency column without
    disturbing prio, reverts on an empty value, and refuses an unknown
    service — mirroring the toggle endpoint's allow-list guard."""

    def _rows() -> dict[tuple[str, str], dict[str, object]]:
        return {
            (str(r["host"]), str(r["service"])): r for r in list_service_config(store)
        }

    try:
        set_service_prio(store, ALL_HOSTS, "classify", 5, actor="test")

        r = real_client.post(
            "/categorizers/concurrency",
            data={"service": "classify", "concurrency": "6"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        row = _rows()[(ALL_HOSTS, "classify")]
        assert row["concurrency"] == 6
        assert row["prio"] == 5  # untouched

        # Empty value reverts to the default (NULL -> 1, serial).
        real_client.post(
            "/categorizers/concurrency",
            data={"service": "classify", "concurrency": ""},
            follow_redirects=False,
        )
        assert _rows()[(ALL_HOSTS, "classify")]["concurrency"] is None

        # A non-positive value is rejected outright (no write).
        real_client.post(
            "/categorizers/concurrency",
            data={"service": "classify", "concurrency": "0"},
            follow_redirects=False,
        )
        assert _rows()[(ALL_HOSTS, "classify")]["concurrency"] is None

        # An unknown service is rejected — no row written.
        r = real_client.post(
            "/categorizers/concurrency",
            data={"service": "not-a-real-service", "concurrency": "4"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert (ALL_HOSTS, "not-a-real-service") not in _rows()
    finally:
        clear_service_config(store, ALL_HOSTS, "classify")


def test_axis_row_reports_concurrency(store: Store) -> None:
    try:
        set_service_concurrency(store, ALL_HOSTS, "classify", 4, actor="test")
        eff = cz._effective_state(store)
        rows = {str(a["id"]): cz._axis_row(a, eff) for a in cz._load_axes()}
        assert rows["role3"]["concurrency"] == 4
        assert rows["junk"]["concurrency"] == 4
    finally:
        clear_service_config(store, ALL_HOSTS, "classify")


def test_toggle_endpoint_accepts_per_topic_service(
    real_client: TestClient, store: Store
) -> None:
    """A ``topic:<slug>`` service is a valid, independently
    flippable toggle target."""

    def _rows() -> dict[tuple[str, str], int]:
        return {
            (str(r["host"]), str(r["service"])): int(r["prio"])  # type: ignore[call-overload]
            for r in list_service_config(store)
        }

    try:
        r = real_client.post(
            "/categorizers/toggle",
            data={"service": "topic:nh3-synthesis", "action": "on"},
            follow_redirects=False,
        )
        assert r.status_code == 303
        assert _rows()[(ALL_HOSTS, "topic:nh3-synthesis")] == 5
    finally:
        clear_service_config(store, ALL_HOSTS, "topic:nh3-synthesis")
