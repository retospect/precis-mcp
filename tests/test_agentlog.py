"""agentlog kind — run-attribution + the touch graph (migration 0034).

Covers the write-side module (:mod:`precis.agentlog`), the touch
attribution wired into the draft handler, the read handler through
dispatch, and the sweeper GC (links drop, chunks stay)."""

from __future__ import annotations

import pytest

from precis import agentlog
from precis.dispatch import Hub
from precis.handlers.draft import DraftHandler


@pytest.fixture
def draft(hub: Hub) -> DraftHandler:
    return DraftHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


# ── module: open / finalize / read ──────────────────────────────────


def test_open_log_records_prompt_source_and_tag(hub: Hub) -> None:
    log_id = agentlog.open_log(
        hub.store,
        source="plan_tick",
        title="plan_tick #1 (opus)",
        model="opus",
        prompt="SYSTEM\n\n──── USER ────\n\ndo the thing",
        parent_ref_id=1,
        job_ref_id=2,
    )
    ref = hub.store.get_ref(kind="agentlog", id=log_id)
    assert ref is not None
    assert ref.meta["source"] == "plan_tick"
    assert ref.meta["model"] == "opus"
    assert "do the thing" in ref.meta["prompt"]
    assert ref.meta["parent_ref_id"] == 1 and ref.meta["job_ref_id"] == 2
    tags = {str(t) for t in hub.store.tags_for(log_id)}
    assert "agentlog-source:plan_tick" in tags


def test_finalize_stamps_status_and_ended(hub: Hub) -> None:
    log_id = agentlog.open_log(hub.store, source="plan_tick", title="t")
    agentlog.finalize_log(hub.store, log_id=log_id, status="ok")
    ref = hub.store.get_ref(kind="agentlog", id=log_id)
    assert ref.meta["status"] == "ok"
    assert ref.meta.get("ended_at")


def test_list_recent_counts_touched(draft: DraftHandler, hub: Hub) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    log_id = agentlog.open_log(hub.store, source="plan_tick", title="t")
    title = hub.store.reading_order(ref.id)[0]
    agentlog.attach_touch(hub.store, log_id=log_id, chunk_ids=[title.chunk_id])
    rows = agentlog.list_recent(hub.store)
    row = next(r for r in rows if r["ref_id"] == log_id)
    assert row["touched"] == 1
    assert row["source"] == "plan_tick"


# ── env-driven touch attribution through the draft handler ──────────


def test_draft_edit_attributes_touch_when_env_set(
    draft: DraftHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    log_id = agentlog.open_log(hub.store, source="plan_tick", title="t")
    monkeypatch.setenv(agentlog.ENV_VAR, str(log_id))

    # Add a paragraph — the handler should attribute it to the run.
    title_h = hub.store.reading_order(ref.id)[0].handle
    draft.put(
        id="nt", chunk_kind="paragraph", text="A new para", at={"after": "¶" + title_h}
    )

    links = hub.store.links_for(log_id, direction="out", relation="touched")
    assert len(links) >= 1
    # The touched chunk surfaces on the draft's Connections graph.
    handles = [c.handle for c in hub.store.reading_order(ref.id)]
    conns = hub.store.chunk_connections(ref.id, handles)
    flat = [c for rows in conns.values() for c in rows]
    assert any(c["kind"] == "agentlog" and c["relation"] == "touched" for c in flat)


def test_draft_edit_no_touch_without_env(
    draft: DraftHandler, hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(agentlog.ENV_VAR, raising=False)
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    title_h = hub.store.reading_order(ref.id)[0].handle
    draft.put(id="nt", chunk_kind="paragraph", text="x", at={"after": "¶" + title_h})
    # No agentlog exists, so no touched link anywhere.
    with hub.store.pool.connection() as conn:
        n = conn.execute(
            "SELECT count(*) FROM links WHERE relation = 'touched'"
        ).fetchone()[0]
    assert n == 0


def test_current_from_env_parsing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(agentlog.ENV_VAR, raising=False)
    assert agentlog.current_from_env() is None
    monkeypatch.setenv(agentlog.ENV_VAR, "not-an-int")
    assert agentlog.current_from_env() is None
    monkeypatch.setenv(agentlog.ENV_VAR, "0")
    assert agentlog.current_from_env() is None
    monkeypatch.setenv(agentlog.ENV_VAR, "42")
    assert agentlog.current_from_env() == 42


# ── GC: links drop, chunks stay ─────────────────────────────────────


def test_gc_drops_links_keeps_chunks_softdeletes_log(
    draft: DraftHandler, hub: Hub
) -> None:
    proj = _proj(hub)
    draft.put(id="nt", title="T", project=proj)
    ref = hub.store.get_ref(kind="draft", id="nt")
    chunk = hub.store.reading_order(ref.id)[0]
    log_id = agentlog.open_log(hub.store, source="plan_tick", title="t")
    agentlog.attach_touch(hub.store, log_id=log_id, chunk_ids=[chunk.chunk_id])

    # Backdate the log past the retention window.
    with hub.store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET created_at = now() - interval '40 days' WHERE ref_id = %s",
            (log_id,),
        )
        conn.commit()

    n = agentlog.gc_stale_logs(hub.store, older_than_days=30)
    assert n == 1

    # Log soft-deleted, its touched links gone…
    assert hub.store.get_ref(kind="agentlog", id=log_id) is None
    with hub.store.pool.connection() as conn:
        links = conn.execute(
            "SELECT count(*) FROM links WHERE src_ref_id = %s", (log_id,)
        ).fetchone()[0]
    assert links == 0
    # …but the chunk it touched is untouched.
    assert hub.store.get_draft_chunk(chunk.handle) is not None


def test_gc_spares_fresh_logs(hub: Hub) -> None:
    log_id = agentlog.open_log(hub.store, source="plan_tick", title="t")
    assert agentlog.gc_stale_logs(hub.store, older_than_days=30) == 0
    assert hub.store.get_ref(kind="agentlog", id=log_id) is not None


# ── read handler ────────────────────────────────────────────────────


def test_handler_recent_view_and_no_put(hub: Hub) -> None:
    from precis.handlers.agentlog import AgentLogHandler

    handler = AgentLogHandler(hub=hub)
    agentlog.open_log(hub.store, source="plan_tick", title="plan_tick #7 (opus)")
    out = handler.get(id="/recent").body
    assert "plan_tick #7" in out
    # Read-only kind: no put / edit in the spec.
    assert handler.spec.supports_put is False
    assert handler.spec.supports_edit is False
    assert handler.spec.supports_delete is True


# ── web routes (real store, injected runtime) ───────────────────────


def test_web_list_and_detail_render(draft: DraftHandler, hub: Hub) -> None:
    import types

    from fastapi.testclient import TestClient

    from precis_web.app import create_app

    proj = _proj(hub)
    draft.put(id="smoke", title="Smoke Doc", project=proj)
    ref = hub.store.get_ref(kind="draft", id="smoke")
    log_id = agentlog.open_log(
        hub.store,
        source="plan_tick",
        title="plan_tick #1 (opus)",
        model="opus",
        prompt="SYS\n\n──── USER ────\n\ndo the thing",
        parent_ref_id=proj,
        job_ref_id=999,
    )
    chunk = hub.store.reading_order(ref.id)[0]
    agentlog.attach_touch(hub.store, log_id=log_id, chunk_ids=[chunk.chunk_id])

    app = create_app(runtime=types.SimpleNamespace(store=hub.store))
    with TestClient(app) as client:
        r1 = client.get("/agentlogs")
        assert r1.status_code == 200
        assert "plan_tick #1" in r1.text

        r2 = client.get(f"/agentlogs/{log_id}")
        assert r2.status_code == 200
        assert "Assembled prompt" in r2.text
        assert "do the thing" in r2.text  # prompt captured
        # the touched chunk links back into the draft reader
        assert f"/drafts/smoke#c-{chunk.handle}" in r2.text
