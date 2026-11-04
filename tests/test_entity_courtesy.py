"""Entity references expose current state when memory prose is rendered."""

from __future__ import annotations

from collections.abc import Iterable

import psycopg
import pytest

from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store, Tag
from precis.utils import entity_courtesy
from tests.conftest import id_of


@pytest.fixture
def memory(hub: Hub) -> MemoryHandler:
    return MemoryHandler(hub=hub)


def _machine(store: Store) -> int:
    ref = store.insert_ref(
        kind="machine",
        slug="ma_courtesy-fixture",
        title="Courtesy fixture",
        meta={"host": "courtesy-fixture", "role": "builder"},
    )
    return int(ref.id)


def _task(store: Store, kind: str = "gripe", status: str = "done") -> int:
    with store.tx() as conn:
        ref = store.insert_ref(kind=kind, slug=None, title="Courtesy task", conn=conn)
        store.add_tag(
            ref.id,
            Tag.parse_strict(f"STATUS:{status}", kind=kind),
            set_by="agent",
            conn=conn,
        )
    return int(ref.id)


@pytest.mark.parametrize("view", [None, "fisheye+1hop"])
def test_memory_body_courtesy_uses_live_entity_state(
    memory: MemoryHandler, store: Store, view: str | None
) -> None:
    machine = _machine(store)
    store.record_heartbeat("courtesy-fixture")
    gripe = _task(store)
    successor = id_of(memory.put(text="The current explanation").body)
    retired = id_of(memory.put(text="The superseded explanation").body)
    store.stamp_ref_meta(retired, {"superseded_by": successor})
    store.retire_ref(retired)
    focus = id_of(
        memory.put(
            title="Entity references",
            text=f"See mh{machine}, gr{gripe}, and me{retired}. Again mh{machine}.",
        ).body
    )

    out = memory.get(id=focus, view=view).body

    courtesy = out.split("— referenced entities —\n", 1)[1].split("\n\n", 1)[0]
    assert f"mh{machine} — role: builder · status: live" in courtesy
    assert f"gr{gripe} — STATUS:done" in courtesy
    assert f"me{retired} — retired → me{successor}" in courtesy
    assert courtesy.count(f"mh{machine} —") == 1
    # Rendering leaves the source body intact; current state is an overlay.
    source = store.get_ref(kind="memory", id=focus)
    assert source is not None
    assert memory._body_text(source).endswith(f"Again mh{machine}.")


@pytest.mark.parametrize("heartbeat", ["live", "stale", "no heartbeat"])
def test_machine_courtesy_reads_heartbeat_overlay(
    memory: MemoryHandler, store: Store, heartbeat: str
) -> None:
    machine = _machine(store)
    if heartbeat != "no heartbeat":
        store.record_heartbeat("courtesy-fixture")
    if heartbeat == "stale":
        with store.pool.connection() as conn:
            conn.execute(
                "UPDATE host_heartbeat SET ts = now() - interval '1 hour' "
                "WHERE host = %s",
                ("courtesy-fixture",),
            )
    focus = id_of(
        memory.put(title="Heartbeat reference", text=f"See mh{machine}.").body
    )

    assert (
        f"mh{machine} — role: builder · status: {heartbeat}"
        in memory.get(id=focus).body
    )


@pytest.mark.parametrize("view", [None, "fisheye+1hop"])
def test_courtesy_caps_distinct_resolved_handles_at_eight(
    memory: MemoryHandler, store: Store, view: str | None
) -> None:
    handles = [f"gr{_task(store)}" for _ in range(10)]
    # Unresolved references and duplicates must not consume the display cap.
    focus = id_of(
        memory.put(
            title="Many references",
            text=" ".join(["gr999999999", *handles[:2], handles[0], *handles[2:]]),
        ).body
    )

    out = memory.get(id=focus, view=view).body
    courtesy = out.split("— referenced entities —\n", 1)[1].split("\n\n", 1)[0]
    rows = [line.strip() for line in courtesy.splitlines() if " — STATUS:done" in line]

    assert rows == [f"{handle} — STATUS:done" for handle in handles[:8]]
    assert "… +2 more" in courtesy
    assert "gr999999999 —" not in courtesy
    assert f"{handles[8]} —" not in courtesy
    assert f"{handles[9]} —" not in courtesy


def test_skill_and_markdown_handles_show_titles(
    memory: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    import precis.handlers.skill as skill_module

    skill = store.ensure_skill_ref("precis-memory-help")
    slugs: list[str] = []

    def skill_title(slug: str) -> str:
        slugs.append(slug)
        return "Memory operations"

    monkeypatch.setattr(skill_module, "_skill_title", skill_title)
    markdown = store.insert_ref(
        kind="markdown", slug="courtesy-document", title="Courtesy document"
    )
    focus = id_of(
        memory.put(
            title="Documentation references", text=f"Use sk{skill} and md{markdown.id}."
        ).body
    )

    out = memory.get(id=focus).body

    assert f"sk{skill} — Memory operations" in out
    assert f"md{markdown.id} — Courtesy document" in out
    assert "precis-memory-help" in slugs


def test_wrong_kind_handle_does_not_resolve_to_another_entity(store: Store) -> None:
    gripe = _task(store)

    states = entity_courtesy.entity_states(
        store, [f"gr{gripe}", f"td{gripe}", f"mh{gripe}", "gr999999999"]
    )

    assert states == {f"gr{gripe}": "STATUS:done"}


@pytest.mark.parametrize("view", [None, "fisheye+1hop"])
def test_render_batches_body_and_neighbour_entity_lookup(
    memory: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    view: str | None,
) -> None:
    machine = _machine(store)
    gripe = _task(store)
    todo = _task(store, kind="todo", status="open")
    focus = id_of(
        memory.put(
            title="Batched references", text=f"See mh{machine} and gr{gripe}."
        ).body
    )
    store.add_link(src_ref_id=focus, dst_ref_id=todo, relation="related-to")
    original = entity_courtesy.entity_states
    calls: list[list[str]] = []

    def spy(lookup_store: Store, handles: Iterable[str]) -> dict[str, str]:
        batch = list(handles)
        calls.append(batch)
        return original(lookup_store, batch)

    monkeypatch.setattr(entity_courtesy, "entity_states", spy)

    out = memory.get(id=focus, view=view).body

    assert len(calls) == 1
    assert {f"mh{machine}", f"gr{gripe}"} <= set(calls[0])
    if view is not None:
        assert f"td{todo}" in calls[0]
        row = next(line for line in out.splitlines() if f"related-to: td{todo}" in line)
        assert "STATUS:open" in row


def test_fisheye_machine_neighbour_shows_current_role_and_status(
    memory: MemoryHandler, store: Store
) -> None:
    machine = _machine(store)
    store.record_heartbeat("courtesy-fixture")
    focus = id_of(memory.put(text="Machine link without an inline reference").body)
    store.add_link(src_ref_id=focus, dst_ref_id=machine, relation="related-to")

    out = memory.get(id=focus, view="fisheye+1hop").body

    row = next(line for line in out.splitlines() if f"related-to: mh{machine}" in line)
    assert "role: builder" in row
    assert "status: live" in row


def test_expanded_second_hop_batches_machine_with_body_reference(
    memory: MemoryHandler, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    machine = _machine(store)
    store.record_heartbeat("courtesy-fixture")
    gripe = _task(store)
    focus = id_of(memory.put(title="Second hop reference", text=f"See gr{gripe}.").body)
    intermediate = id_of(memory.put(text="Intermediate memory").body)
    store.add_link(src_ref_id=focus, dst_ref_id=intermediate, relation="related-to")
    store.add_link(src_ref_id=intermediate, dst_ref_id=machine, relation="related-to")
    original = entity_courtesy.entity_states
    calls: list[list[str]] = []

    def spy(lookup_store: Store, handles: Iterable[str]) -> dict[str, str]:
        batch = list(handles)
        calls.append(batch)
        return original(lookup_store, batch)

    monkeypatch.setattr(entity_courtesy, "entity_states", spy)

    out = memory.get(id=focus, view="fisheye+2hop", q="machine:related-to").body

    assert len(calls) == 1
    assert {f"gr{gripe}", f"mh{machine}"} <= set(calls[0])
    courtesy = out.split("— referenced entities —\n", 1)[1].split("\n\n", 1)[0]
    assert f"gr{gripe} — STATUS:done" in courtesy
    second_hop = out.split("— second hop", 1)[1]
    row = next(line for line in second_hop.splitlines() if f"mh{machine}" in line)
    assert "role: builder" in row
    assert "status: live" in row


@pytest.mark.parametrize("view", [None, "fisheye", "fisheye+1hop", "fisheye+2hop"])
def test_courtesy_failure_preserves_memory_read(
    memory: MemoryHandler,
    store: Store,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    view: str | None,
) -> None:
    gripe = _task(store)
    source = f"The source body remains readable. See gr{gripe}."
    focus = id_of(memory.put(title="Readable despite lookup failure", text=source).body)
    intermediate = id_of(memory.put(text="Intermediate memory").body)
    machine = _machine(store)
    store.add_link(src_ref_id=focus, dst_ref_id=intermediate, relation="related-to")
    store.add_link(src_ref_id=intermediate, dst_ref_id=machine, relation="related-to")

    def fail(_store: Store, _handles: Iterable[str]) -> dict[str, str]:
        raise psycopg.errors.UndefinedColumn("courtesy lookup failed")

    monkeypatch.setattr(entity_courtesy, "entity_states", fail)
    caplog.set_level("DEBUG", logger="precis.utils.entity_courtesy")

    out = memory.get(
        id=focus,
        view=view,
        q="machine:related-to" if view == "fisheye+2hop" else None,
    ).body

    assert source in out
    assert "— referenced entities —" not in out
    assert "entity courtesy read failed" in caplog.text
    if view == "fisheye+1hop":
        assert f"related-to: me{intermediate}" in out
    elif view == "fisheye+2hop":
        assert "— second hop" in out
        assert f"mh{machine}" in out
