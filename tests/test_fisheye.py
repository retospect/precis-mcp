"""Fisheye neighborhood render (ADR 0051 §6) — assembly over reading_order +
block_views, exercised against a real ``plan`` chunk tree."""

from __future__ import annotations

import re

import pytest

from precis.dispatch import Hub
from precis.handlers.plan import PlanHandler
from precis.utils.fisheye import render_fisheye
from precis.workers.working_set import Extent


def _handles(body: str) -> list[str]:
    return re.findall(r"pe\d+", body)


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _build_tree(hub: Hub, plan: PlanHandler) -> dict[str, str]:
    """A plan with 4 top-level nodes; the 2nd owns a child. Returns a map of
    label → pe-handle."""
    proj = hub.store.insert_ref(kind="todo", slug=None, title="Proj").id
    plan.put(id="p", title="Root", project=proj)
    labels = {}
    for name in ("survey", "axes", "draft", "review"):
        r = plan.put(id="p", text=f"{name} the thing", at={"last": True})
        labels[name] = _handles(r.body)[0]
    child = plan.put(id="p", text="axis: speed", at={"into": labels["axes"]})
    labels["child"] = _handles(child.body)[0]
    return labels


def test_verbatim_eye_is_the_node_alone(hub: Hub, plan: PlanHandler) -> None:
    # verbatim (FULL) is the target's full text, ALONE — no surroundings (§ C0);
    # the neighborhood is what the fisheye rung is for.
    labels = _build_tree(hub, plan)
    out = render_fisheye(
        hub.store, kind="plan", handle=labels["axes"], extent="verbatim"
    )
    assert "axes the thing" in out
    assert f"▸ {labels['axes']}" in out
    # no sibling skirt at this rung
    assert labels["survey"] not in out
    assert labels["draft"] not in out


def test_fisheye_eye_returns_the_neighborhood(hub: Hub, plan: PlanHandler) -> None:
    # fisheye (FIDELITY) is where the spatial neighborhood appears.
    labels = _build_tree(hub, plan)
    out = render_fisheye(
        hub.store, kind="plan", handle=labels["axes"], extent="fisheye"
    )
    assert "axes the thing" in out
    assert labels["survey"] in out  # a reading-order neighbour
    assert labels["draft"] in out


def test_toc_eye_is_a_one_line_bookmark_with_ancestor_branch(
    hub: Hub, plan: PlanHandler
) -> None:
    labels = _build_tree(hub, plan)
    out = render_fisheye(hub.store, kind="plan", handle=labels["child"], extent="toc")
    # ancestor branch present (child sits under 'axes')
    assert out.startswith("↑ ")
    assert "axis: speed" in out
    # one-line — the child bookmark, no big body
    assert f"▸ {labels['child']}" in out


def test_summary_eye_is_the_node_alone(hub: Hub, plan: PlanHandler) -> None:
    # summary is the target's gloss, alone — no surroundings (§ C0).
    labels = _build_tree(hub, plan)
    out = render_fisheye(
        hub.store, kind="plan", handle=labels["draft"], extent=Extent.SUMMARY
    )
    assert labels["draft"] in out
    assert labels["review"] not in out  # no skirt at this rung


def test_fidelity_eye_spans_reading_order(hub: Hub, plan: PlanHandler) -> None:
    labels = _build_tree(hub, plan)
    out = render_fisheye(
        hub.store, kind="plan", handle=labels["survey"], extent="fidelity"
    )
    # a wide graduated span reaches multiple neighbours across the tree
    seen = set(_handles(out))
    assert labels["survey"] in seen
    assert labels["axes"] in seen or labels["draft"] in seen


def test_gloss_lines_are_capped_not_spilled(hub: Hub, plan: PlanHandler) -> None:
    """A toc eye is a bookmark: even if the node carries a prose paragraph, the
    one-line gloss is whitespace-collapsed + clipped, never a wall of text."""
    proj = hub.store.insert_ref(kind="todo", slug=None, title="Proj").id
    plan.put(id="p", title="Root", project=proj)
    prose = "alpha beta gamma delta " * 40  # ~920 chars, one logical line
    h = _handles(plan.put(id="p", text=prose, at={"last": True}).body)[0]

    out = render_fisheye(hub.store, kind="plan", handle=h, extent="toc")
    bookmark = next(line for line in out.splitlines() if h in line)
    assert "\n" not in bookmark
    assert len(bookmark) <= 130  # ▸ + handle + capped gloss
    assert bookmark.rstrip().endswith("…")


def test_unknown_handle_raises(hub: Hub, plan: PlanHandler) -> None:
    with pytest.raises(ValueError, match="no live plan node"):
        render_fisheye(hub.store, kind="plan", handle="pe999999", extent="full")
