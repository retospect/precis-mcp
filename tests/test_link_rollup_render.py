"""source-backfill 8a.3 — the composer overlay (the section link map).

Wires the pure 8a.2 rollup into the ADR-0051 composer as a **post-assembly
overlay**: ``render_link_rollup`` for one doc, and the ``link_map`` gate on
``render_working_set`` (default off → byte-identical). Uses ``plan`` docs (a
DraftMixin tree kind) like the other backfill/composer tests.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.utils.working_set_render import render_link_rollup, render_working_set
from precis.workers.working_set import Extent, WorkingSet


def _pe(body: str) -> str:
    return re.search(r"pe\d+", body).group(0)


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _ords(store: Any, ref_id: int) -> dict[int, int]:
    """chunk_id → ord, so a test can create chunk-level links via ``add_link``
    (which takes ``*_pos`` = ord)."""
    with store.pool.connection() as conn:
        rows = conn.execute(
            "SELECT chunk_id, ord FROM chunks WHERE ref_id = %s", (ref_id,)
        ).fetchall()
    return {int(cid): int(ord_) for cid, ord_ in rows}


def _by_text(store: Any, ref_id: int, text: str) -> Any:
    for c in store.reading_order(ref_id, kind="plan"):
        if (c.text or "").strip() == text:
            return c
    raise AssertionError(f"no chunk with text {text!r}")


def _doc(store: Any, plan: PlanHandler) -> tuple[int, dict[str, Any]]:
    """A plan doc: Root › (Methods › m-para) + (Results › r-para). Returns
    ``(ref_id, {label: DraftChunk})``."""
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p", title="Doc", project=proj)
    root = _pe(
        plan.put(id="p", chunk_kind="heading", text="Root", at={"last": True}).body
    )
    ref_id = store.get_draft_chunk(root, kind="plan").ref_id
    plan.put(id="p", chunk_kind="heading", text="Methods", at={"into": root})
    methods = _by_text(store, ref_id, "Methods").dc
    plan.put(id="p", chunk_kind="paragraph", text="m-para", at={"into": methods})
    plan.put(id="p", chunk_kind="paragraph", text="m-para2", at={"into": methods})
    plan.put(id="p", chunk_kind="heading", text="Results", at={"into": root})
    results = _by_text(store, ref_id, "Results").dc
    plan.put(id="p", chunk_kind="paragraph", text="r-para", at={"into": results})
    chunks = {c.text.strip(): c for c in store.reading_order(ref_id, kind="plan")}
    return ref_id, chunks


def test_render_link_rollup_aggregates_by_visibility(
    hub: Hub, plan: PlanHandler
) -> None:
    store = hub.store
    ref_id, ch = _doc(store, plan)
    paper = store.insert_ref(kind="paper", slug="kumar2021", title="Kumar 2021")
    ords = _ords(store, ref_id)

    # Two distinct paras under Methods each cite the held paper (identical
    # edges would dedup on the links UNIQUE, so the count is over *edges*), and
    # m-para also links → r-para (in-doc).
    for para in ("m-para", "m-para2"):
        store.add_link(
            src_ref_id=ref_id,
            src_pos=ords[ch[para].chunk_id],
            dst_ref_id=paper.id,
            relation="cites",
        )
    store.add_link(
        src_ref_id=ref_id,
        src_pos=ords[ch["m-para"].chunk_id],
        dst_ref_id=ref_id,
        dst_pos=ords[ch["r-para"].chunk_id],
        relation="see-also",
    )

    # Visibility: Root + Methods open; the paras + Results collapsed. So the
    # src m-para rolls up to Methods (its open section); the in-doc target
    # r-para (collapsed under collapsed Results) rolls up to the visible Root.
    demand = {ch["Root"].chunk_id: Extent.FULL, ch["Methods"].chunk_id: Extent.FULL}
    views = store.block_views(ref_id)
    out = render_link_rollup(
        store, ref_id, store.reading_order(ref_id, kind="plan"), demand, views
    )

    assert "section link map" in out
    # source section named by its heading title; the paper named by handle.
    from precis.utils import handle_registry

    pa = handle_registry.try_format("paper", paper.id)
    assert f'{ch["Methods"].dc} "Methods"' in out
    assert f"2× {pa}" in out
    # the in-doc target collapsed up to Root (the visible ancestor).
    assert f'1× {ch["Root"].dc} "Root"' in out


def test_render_link_rollup_empty_without_links(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    ref_id, ch = _doc(store, plan)
    demand = {ch["Root"].chunk_id: Extent.FULL}
    out = render_link_rollup(
        store, ref_id, store.reading_order(ref_id, kind="plan"), demand, {}
    )
    assert out == ""


def test_link_map_gate_is_byte_identical_when_off(hub: Hub, plan: PlanHandler) -> None:
    """The overlay ships dark: the default path (and an explicit
    ``link_map=False``) is byte-for-byte unchanged even with links present."""
    store = hub.store
    ref_id, ch = _doc(store, plan)
    paper = store.insert_ref(kind="paper", slug="p1", title="P1")
    ords = _ords(store, ref_id)
    store.add_link(
        src_ref_id=ref_id,
        src_pos=ords[ch["m-para"].chunk_id],
        dst_ref_id=paper.id,
        relation="cites",
    )
    ws = WorkingSet()
    ws.focus(ch["Methods"].dc, "fisheye")

    default = render_working_set(store, ws)
    explicit_off = render_working_set(store, ws, link_map=False)
    assert default == explicit_off
    assert "section link map" not in default


def test_link_map_gate_appends_block_when_on(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    ref_id, ch = _doc(store, plan)
    paper = store.insert_ref(kind="paper", slug="p2", title="P2")
    ords = _ords(store, ref_id)
    store.add_link(
        src_ref_id=ref_id,
        src_pos=ords[ch["m-para"].chunk_id],
        dst_ref_id=paper.id,
        relation="cites",
    )
    ws = WorkingSet()
    ws.focus(ch["Methods"].dc, "fisheye")

    on = render_working_set(store, ws, link_map=True)
    assert "section link map" in on
    from precis.utils import handle_registry

    assert handle_registry.try_format("paper", paper.id) in on
