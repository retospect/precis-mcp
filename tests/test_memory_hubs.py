"""Memory hubs: the uncapped member listing on a ``section:index`` node, the
write-path ``Next:`` link hints, and the hub columns of the export manifest."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from precis.cli.memory import SPACE_TAG, _created_id, export_memory_nodes
from precis.dispatch import Hub
from precis.handlers.memory import MemoryHandler
from precis.store import Store
from precis.utils import handle_registry
from precis.utils.eye_render import (
    _NEIGHBOR_GROUP_CAP,
    _VERBATIM_CAP,
    first_hop_shape,
    render_eye,
)
from precis.utils.memory_hubs import link_hints

HUB = "section:index"


def _h(ref_id: int) -> str:
    h = handle_registry.try_format("memory", ref_id)
    assert h is not None
    return h


def _note(
    handler: MemoryHandler,
    title: str,
    body: str = "body",
    *tags: str,
    hook: str | None = None,
) -> int:
    return _created_id(
        handler.put(
            text=body,
            title=title,
            tags=[SPACE_TAG, *tags],
            meta={"hook": hook} if hook else None,
        )
    )


def _part_of(store: Store, member: int, hub: int) -> None:
    store.add_link(src_ref_id=member, dst_ref_id=hub, relation="part-of")


def test_hub_lists_every_member_one_line_with_hook(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root hub", "x", HUB)
    n = _NEIGHBOR_GROUP_CAP + 4
    hooked = _note(handler, "Hooked", "body one", hook="the hook text")
    bodied = _note(handler, "Bodied", "first line of body\nsecond")
    members = [hooked, bodied] + [_note(handler, f"M{i}") for i in range(n)]
    for m in members:
        _part_of(store, m, root)
    other = _note(handler, "Rel target")
    # a non-member group on the hub keeps the cap
    extra = [_note(handler, f"Rel {i}") for i in range(_NEIGHBOR_GROUP_CAP + 2)]
    for e in extra:
        store.add_link(src_ref_id=root, dst_ref_id=e, relation="related-to")
    del other

    out = render_eye(store, _h(root), "fisheye+1hop")

    assert out.count("contains: me") == len(members)
    assert "… +" not in out.split("related-to")[0]
    assert f"contains: {_h(hooked)} — Hooked — the hook text" in out
    assert f"contains: {_h(bodied)} — Bodied — first line of body" in out
    assert out.count("related-to: me") == _NEIGHBOR_GROUP_CAP
    assert "… +2 more" in out


def test_hub_member_hook_is_cut_to_about_100_chars(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root", "x", HUB)
    m = _note(handler, "Long", "y" * 300)
    _part_of(store, m, root)
    out = render_eye(store, _h(root), "fisheye+1hop")
    line = next(ln for ln in out.splitlines() if _h(m) in ln)
    assert line.endswith("…") and len(line.split(" — ")[-1]) <= 100


def test_non_hub_keeps_the_cap_on_contains(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Not a hub", "x")
    for i in range(_NEIGHBOR_GROUP_CAP + 3):
        _part_of(store, _note(handler, f"M{i}"), root)
    out = render_eye(store, _h(root), "fisheye+1hop")
    assert out.count("contains: me") == _NEIGHBOR_GROUP_CAP
    assert "… +3 more" in out


def test_first_hop_shape_does_not_count_hub_members_as_hidden(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root", "x", HUB)
    plain = _note(handler, "Plain", "x")
    for i in range(_NEIGHBOR_GROUP_CAP + 3):
        _part_of(store, _note(handler, f"H{i}"), root)
        _part_of(store, _note(handler, f"P{i}"), plain)
    shape = first_hop_shape(store, [root, plain])
    assert shape[root] == (_NEIGHBOR_GROUP_CAP + 3, 0, 0)
    assert shape[plain] == (_NEIGHBOR_GROUP_CAP + 3, _NEIGHBOR_GROUP_CAP + 3, 0)


# ── write-path hints ────────────────────────────────────────────────


def _next_calls(body: str) -> list[str]:
    return [ln for ln in body.splitlines() if "link(kind=" in ln or "tag(kind=" in ln]


def test_put_hints_nearest_hubs_when_no_part_of(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    h1 = _note(handler, "Pump cavitation hub", "pumps cavitation impeller", HUB)
    _note(handler, "Billing hub", "invoices taxes", HUB)
    resp = handler.put(
        text="cavitation damages the impeller",
        title="Cavitation note",
        tags=[SPACE_TAG, "section:gotchas"],
    )
    out, new = resp.body, _created_id(resp)
    assert (
        f"link(kind='memory', id='{_h(new)}', target='{_h(h1)}', rel='part-of')" in out
    )
    assert out.count("rel='part-of')") <= 2
    # once linked, the hint goes quiet (no part-of / type / length hints)
    _part_of(store, new, h1)
    assert link_hints(handler, new) == []


def test_edit_hints_and_quiet_cases(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    # no hub in the space -> nothing
    lone = _note(handler, "Lone", "body", "section:threads")
    assert link_hints(handler, lone) == []
    root = _note(handler, "Root", "x", HUB)
    # the hub itself is never nudged
    assert link_hints(handler, root) == []
    node = _note(handler, "Untyped", "body")
    _part_of(store, node, root)
    calls = [c for c, _d in link_hints(handler, node)]
    assert calls == [f"tag(kind='memory', id='{_h(node)}', add=['section:threads'])"]
    out = handler.edit(id=node, mode="replace", text="z" * (_VERBATIM_CAP + 10)).body
    assert f"fisheye shows {_VERBATIM_CAP} of {_VERBATIM_CAP + 10} chars" in out
    assert "summary node + part-of detail nodes" in out


def test_thread_hints_unlinked_gotchas_of_its_hub(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root", "x", HUB)
    thread = _note(handler, "Thread", "t", "section:threads")
    _part_of(store, thread, root)
    gotchas = [_note(handler, f"G{i}", "g", "section:gotchas") for i in range(7)]
    runbook = _note(handler, "RB", "r", "section:runbooks")
    for g in [*gotchas, runbook]:
        _part_of(store, g, root)
    store.add_link(src_ref_id=gotchas[0], dst_ref_id=thread, relation="qualifies")

    calls = [c for c, _d in link_hints(handler, thread)]

    assert len(calls) == 5
    want = {
        f"link(kind='memory', id='{_h(g)}', target='{_h(thread)}', rel='qualifies')"
        for g in gotchas[1:]
    }
    assert set(calls) <= want
    assert not any(_h(gotchas[0]) in c or _h(runbook) in c for c in calls)


def test_hint_failure_never_breaks_a_write(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    handler = MemoryHandler(hub=hub)

    def boom(*_a: object, **_k: object) -> list[str]:
        raise RuntimeError("boom")

    monkeypatch.setattr("precis.utils.memory_hubs._link_hints", boom)
    assert link_hints(handler, 1) == []
    out = handler.put(text="still written", title="T", tags=[SPACE_TAG]).body
    assert out.startswith("created memory")


# ── manifest columns ────────────────────────────────────────────────


def test_manifest_carries_hub_and_qualified_columns(hub: Hub, tmp_path: Path) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root", "x", HUB)
    thread = _note(handler, "Thread", "t", "section:threads")
    gotcha = _note(handler, "Gotcha", "g", "section:gotchas")
    stray = _note(handler, "Stray", "s")
    _part_of(store, thread, root)
    _part_of(store, gotcha, root)
    store.add_link(src_ref_id=gotcha, dst_ref_id=thread, relation="qualifies")

    export_memory_nodes(store, tmp_path / "n")

    rows = {
        r[0]: r
        for r in (
            ln.split("\t")
            for ln in (tmp_path / "n" / "_sections.tsv")
            .read_text(encoding="utf-8")
            .splitlines()
        )
    }
    assert all(len(r) == 14 for r in rows.values())
    assert rows[_h(root)][8:10] == ["-", "0"]
    assert rows[_h(thread)][8:10] == [_h(root), "1"]
    assert rows[_h(gotcha)][8:10] == [_h(root), "0"]
    assert rows[_h(stray)][8:] == ["", "0", "", "", "", "0"]
    # an agent read of a node's own view stamps last_recalled_at -> accessed
    store.touch_recalled(stray)
    export_memory_nodes(store, tmp_path / "n2")
    row = next(
        ln.split("\t")
        for ln in (tmp_path / "n2" / "_sections.tsv")
        .read_text(encoding="utf-8")
        .splitlines()
        if ln.startswith(_h(stray) + "\t")
    )
    assert row[10] == datetime.now(UTC).strftime("%Y-%m-%d")


# ── recency: ordering and the recall stamp ──────────────────────────


def test_hub_members_newest_first_by_greatest_of_view_recall_update(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root", "x", HUB)
    old, mid, new = (_note(handler, t) for t in ("Old", "Mid", "New"))
    for m in (old, mid, new):
        _part_of(store, m, root)
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '30 days' "
            "WHERE ref_id = ANY(%s)",
            ([old, mid, new],),
        )
        conn.execute(
            "UPDATE refs SET updated_at = now() - interval '10 days' WHERE ref_id = %s",
            (new,),
        )
        # an agent recall makes the oldest-updated node the freshest
        conn.execute(
            "UPDATE refs SET last_recalled_at = now() - interval '1 day' "
            "WHERE ref_id = %s",
            (old,),
        )
        conn.execute(
            "UPDATE refs SET last_viewed_at = now() - interval '5 days' "
            "WHERE ref_id = %s",
            (mid,),
        )

    out = render_eye(store, _h(root), "fisheye+1hop")

    order = [h for h in (_h(old), _h(mid), _h(new)) if h in out]
    assert sorted(order, key=out.index) == [_h(old), _h(mid), _h(new)]


def test_hub_listing_is_capped_with_a_more_line_and_reads_hooks_in_one_query(
    hub: Hub, monkeypatch: pytest.MonkeyPatch
) -> None:
    from precis.utils import eye_render

    monkeypatch.setattr(eye_render, "_HUB_LIST_CAP", 5)
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    root = _note(handler, "Root", "x", HUB)
    for i in range(9):
        _part_of(store, _note(handler, f"M{i}", f"first line {i}\nsecond"), root)

    seen: list[str] = []
    orig = store.chunks.list_chunks_for_ref

    def spy(*a: Any, **k: Any) -> Any:
        seen.append("x")
        return orig(*a, **k)

    monkeypatch.setattr(store.chunks, "list_chunks_for_ref", spy)
    out = render_eye(store, _h(root), "fisheye+1hop")

    assert out.count("contains: me") == 5
    assert f"… +4 more · get(kind='memory', id='{_h(root)}'" in out
    assert "q='contains')" in out
    assert out.splitlines()[-1].startswith("search within:")
    assert "first line" in out and seen == []  # no per-member chunk reads


def test_first_hop_shape_ignores_inbound_edges_from_retired_sources(hub: Hub) -> None:
    store, handler = hub.live_store, MemoryHandler(hub=hub)
    target = _note(handler, "Target", "x")
    gone_src = _note(handler, "Gone src", "x")
    gone_dst = _note(handler, "Gone dst", "x")
    store.add_link(src_ref_id=gone_src, dst_ref_id=target, relation="related-to")
    store.add_link(src_ref_id=target, dst_ref_id=gone_dst, relation="related-to")
    with store.pool.connection() as conn:
        conn.execute(
            "UPDATE refs SET retired_at = now() WHERE ref_id = ANY(%s)",
            ([gone_src, gone_dst],),
        )
    # inbound from retired source: unremovable, not counted; outbound: counted
    assert first_hop_shape(store, [target])[target] == (0, 0, 1)
