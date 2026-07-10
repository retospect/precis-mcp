"""The deduper — compose a working set into one context (ADR 0051 §6). Multiple
eyes on one document render its overlap ONCE (multi-focus fisheye); rings merge
by ref; small gaps bridge, large gaps collapse to a marker; the cursor's
document leads."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.utils.working_set_render import render_working_set
from precis.workers.working_set import WorkingSet


def _pe(body: str) -> str:
    return re.search(r"pe\d+", body).group(0)


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _flat_doc(
    hub: Hub, plan: PlanHandler, slug: str, names: list[str]
) -> dict[str, str]:
    proj = hub.store.insert_ref(kind="todo", slug=None, title=f"{slug} proj").id
    plan.put(id=slug, title=f"Doc {slug}", project=proj)
    h = {}
    for name in names:
        h[name] = _pe(
            plan.put(id=slug, text=f"{name} body unique", at={"last": True}).body
        )
    return h


def test_overlapping_eyes_render_shared_body_once(hub: Hub, plan: PlanHandler) -> None:
    # two fisheye eyes on the same small doc both cover 'beta'; it must appear
    # ONCE, not once per eye (the whole point of the deduper).
    h = _flat_doc(hub, plan, "d", ["alpha", "beta", "gamma", "delta"])
    ws = WorkingSet()
    ws.focus(h["alpha"], "fisheye")
    ws.focus(h["gamma"], "fisheye")
    out = render_working_set(hub.store, ws)
    assert out.count("beta body unique") == 1
    assert out.count("# Doc d") == 1  # one document block, not two


def test_rings_merge_across_eyes_by_ref(hub: Hub, plan: PlanHandler) -> None:
    store = hub.store
    shared = store.insert_ref(kind="paper", slug="shared", title="Shared Paper")
    only_a = store.insert_ref(kind="paper", slug="onlya", title="Only A Paper")
    proj = store.insert_ref(kind="todo", slug=None, title="proj").id
    plan.put(id="p", title="Doc", project=proj)
    sec_a = _pe(plan.put(id="p", text="Section A", at={"last": True}).body)
    plan.put(
        id="p", text=f"cites paper:{shared.id} paper:{only_a.id}", at={"into": sec_a}
    )
    sec_c = _pe(plan.put(id="p", text="Section C", at={"last": True}).body)
    plan.put(id="p", text=f"cites paper:{shared.id}", at={"into": sec_c})

    ws = WorkingSet()
    ws.focus(sec_a, "fisheye+1hop")
    ws.focus(sec_c, "fisheye+1hop")
    out = render_working_set(store, ws)
    # one merged ring, shared paper listed once
    assert "merged across eyes" in out
    assert out.count("paper:shared — Shared Paper") == 1
    assert "paper:onlya — Only A Paper" in out


def test_small_gap_is_bridged(hub: Hub, plan: PlanHandler) -> None:
    # verbatim eyes on 'a' and 'c' leave a 1-chunk hole at 'b' → bridged.
    h = _flat_doc(hub, plan, "g", ["a", "b", "c"])
    ws = WorkingSet()
    ws.focus(h["a"], "verbatim")
    ws.focus(h["c"], "verbatim")
    out = render_working_set(hub.store, ws)
    assert "b body unique" in out  # the hole filled in
    assert "⋯" not in out  # no collapse marker for a bridged gap


def test_large_gap_collapses_to_marker(hub: Hub, plan: PlanHandler) -> None:
    h = _flat_doc(hub, plan, "big", ["n0", "n1", "n2", "n3", "n4", "n5"])
    ws = WorkingSet()
    ws.focus(h["n0"], "verbatim")
    ws.focus(h["n5"], "verbatim")
    out = render_working_set(hub.store, ws)
    assert "⋯ 4 more ⋯" in out  # n1..n4 collapsed, not silently dropped
    assert "n2 body unique" not in out  # genuinely omitted, not shown


def test_cursor_document_leads(hub: Hub, plan: PlanHandler) -> None:
    ha = _flat_doc(hub, plan, "aaa", ["x"])
    hb = _flat_doc(hub, plan, "bbb", ["y"])
    ws = WorkingSet()
    ws.focus(ha["x"], "verbatim")
    ws.focus(hb["y"], "verbatim")
    ws.set_cursor(hb["y"])  # cursor in doc bbb
    out = render_working_set(hub.store, ws)
    assert out.index("# Doc bbb") < out.index("# Doc aaa")


def test_empty_working_set(hub: Hub, plan: PlanHandler) -> None:
    assert render_working_set(hub.store, WorkingSet()) == "— empty working set —"
