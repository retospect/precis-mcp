"""Tests for the ``/env`` Assembled-context panel (Part 3B).

Two layers, mirroring the existing ``/env`` test style in
``test_routes.py`` (FakeStore for fast route checks) plus a real-DB layer
(the shared test Postgres, via the root ``store``/``runtime_with_store``
fixtures) proving the actual capture-lookup SQL + dry-run reuse against
seeded refs — the thing a FakeStore can't exercise (its fake pool always
returns empty rows).
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis.store import Store
from precis.store.types import Tag
from precis.utils.prompt import Block, Layer, persist_assembled_context
from precis.workers.structural import STRUCTURAL
from precis_web import env_context
from precis_web.app import create_app

# ── real-DB route layer ──────────────────────────────────────────────


@pytest.fixture
def real_client(runtime_with_store) -> TestClient:  # type: ignore[no-untyped-def]
    """A real FastAPI ``TestClient`` backed by the shared test DB (not the
    FakeStore ``client`` fixture) — needed to seed a real
    ``meta.assembled_context`` and prove the panel's SQL finds it."""
    return TestClient(create_app(runtime=runtime_with_store))


def test_env_route_renders_last_real_capture_for_planner_job(
    real_client: TestClient, store: Store
) -> None:
    """A seeded ``plan_tick`` job ref carrying ``meta.assembled_context``
    renders under 'Last real' as a collapsible block, with its job handle."""
    todo = store.insert_ref(kind="todo", slug=None, title="do a thing")
    job = store.insert_ref(
        kind="job",
        slug=None,
        title="plan_tick job",
        parent_id=todo.id,
        meta={"job_type": "plan_tick"},
    )
    blocks = [
        Block(id="contract", layer=Layer.CACHED, text="cached persona text"),
        Block(id="body", layer=Layer.VARIABLE, text="per-tick body text"),
    ]
    persist_assembled_context(store, job.id, blocks)

    resp = real_client.get("/env?agent=job_claude_inproc")
    assert resp.status_code == 200
    body = resp.text
    assert "<details" in body
    assert f"job:{job.id}" in body
    assert "contract" in body
    assert "cached persona text" in body


def test_env_route_renders_last_real_capture_for_structural_digest(
    real_client: TestClient, store: Store
) -> None:
    """A seeded structural digest memory carrying the capture renders under
    'Last real'; the dry-run sub-section assembles fresh alongside it."""
    digest = store.insert_ref(kind="memory", slug=None, title="structural digest")
    store.add_tag(digest.id, Tag.open(STRUCTURAL.tier_tag), set_by="system")
    blocks = [
        Block(
            id="structural.body", layer=Layer.VARIABLE, text="structural findings body"
        ),
    ]
    persist_assembled_context(store, digest.id, blocks)

    resp = real_client.get("/env?agent=structural")
    assert resp.status_code == 200
    body = resp.text
    assert f"memory:{digest.id}" in body
    assert "structural.body" in body
    assert "structural findings body" in body
    # The dry-run sub-section assembled fresh against the (empty) tree —
    # its shared trailing modules always land regardless of tree content.
    assert "reviewer.footer" in body


def test_env_route_degrades_to_no_capture_note_when_nothing_captured_yet(
    real_client: TestClient,
) -> None:
    """No captured context yet for job_claude_inproc → the clear note, not a 500."""
    resp = real_client.get("/env?agent=job_claude_inproc")
    assert resp.status_code == 200
    assert "no captured context yet" in resp.text


def test_env_route_dream_degrades_to_hand_rolled_note(real_client: TestClient) -> None:
    """Dream has no assembler on its path — both sub-sections degrade to
    the hand-rolled note, never a 500."""
    resp = real_client.get("/env?agent=dream_agent")
    assert resp.status_code == 200
    body = resp.text
    assert "hand-rolled prompt" in body


def test_env_route_dry_run_planner_assembles_against_representative_todo(
    real_client: TestClient, store: Store
) -> None:
    """With no ``target_ref_id``, the planner dry-run picks a recent
    ``LLM:``-tagged todo and assembles a real (zero-LLM-call) prompt."""
    todo = store.insert_ref(kind="todo", slug=None, title="write the report")
    store.add_tag(todo.id, Tag.closed("LLM", "opus"), set_by="system")

    resp = real_client.get("/env?agent=job_claude_inproc")
    assert resp.status_code == 200
    body = resp.text
    assert "Dry-run (now)" in body
    assert f"todo:{todo.id}" in body
    assert "dry-run unavailable" not in body


# ── unit layer (env_context functions directly against the real DB) ──


def test_load_last_real_none_when_nothing_captured(store: Store) -> None:
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["job_claude_inproc"]
    assert env_context.load_last_real(store, spec) is None


def test_load_last_real_none_for_dream(store: Store) -> None:
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["dream_agent"]
    assert env_context.load_last_real(store, spec) is None


def test_build_dry_run_none_for_dream(store: Store) -> None:
    from precis.workers.registry import SERVICES_BY_NAME

    spec = SERVICES_BY_NAME["dream_agent"]
    assert env_context.build_dry_run(store, spec) is None


def test_build_dry_run_planner_honors_explicit_target_ref_id(store: Store) -> None:
    """``target_ref_id`` (the draft-reader link's scoping) wins over the
    auto-picked representative todo."""
    from precis.workers.registry import SERVICES_BY_NAME

    todo = store.insert_ref(kind="todo", slug=None, title="scoped target")
    spec = SERVICES_BY_NAME["job_claude_inproc"]

    dry_run = env_context.build_dry_run(store, spec, target_ref_id=todo.id)

    assert dry_run is not None
    assert dry_run.target_label == f"todo:{todo.id}"
    assert dry_run.blocks


def test_build_panel_never_raises_when_last_real_lookup_errors(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lookup failure degrades to a note instead of propagating — the
    contract every route call relies on to never 500 the page."""
    from precis.workers.registry import SERVICES_BY_NAME

    def _boom(_store: object, _spec: object) -> None:
        raise RuntimeError("simulated DB hiccup")

    monkeypatch.setattr(env_context, "load_last_real", _boom)
    spec = SERVICES_BY_NAME["structural"]

    panel = env_context.build_panel(store, spec)

    assert panel.last_real is None
    assert panel.last_real_note is not None
