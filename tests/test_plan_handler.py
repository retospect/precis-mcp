"""PlanHandler — the reasoning-outline kind (ADR 0051 §2b).

A ``plan`` mirrors ``draft`` on the same chunk-tree substrate but is a
distinct kind: it renders whole with ``[open]``/``[wip]``/``done:`` markers
+ a ``▸`` cursor and is NEVER exported. These cover the A1 DoD: handle-code
round-trip, create + nodes, the marked whole-tree render + cursor, node
resolution by ``pe<id>``, retire, and the export refusal.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.plan import PlanHandler
from precis.utils import handle_registry as hr


def _pe(body: str) -> str:
    """Extract the first ``pe<id>`` node handle from a plan response."""
    m = re.search(r"pe\d+", body)
    assert m is not None, f"no pe handle in {body!r}"
    return m.group(0)


@pytest.fixture
def plan(hub: Hub) -> PlanHandler:
    return PlanHandler(hub=hub)


def _proj(hub: Hub) -> int:
    return hub.store.insert_ref(kind="todo", slug=None, title="Proj").id


# ── handle codes ─────────────────────────────────────────────────────


def test_handle_codes_round_trip() -> None:
    assert hr.KIND_CODES["plan"] == "po"
    assert hr.CHUNK_CODES["plan"] == "pe"
    # record handle
    assert hr.format_handle("plan", 7) == "po7"
    assert hr.parse("po7") == ("plan", False, 7)
    # chunk handle
    assert hr.format_handle("plan", 42, chunk=True) == "pe42"
    assert hr.parse("pe42") == ("plan", True, 42)


# ── create + nodes ───────────────────────────────────────────────────


def test_create_requires_project(plan: PlanHandler, hub: Hub) -> None:
    with pytest.raises(BadInput, match="project="):
        plan.put(id="p1", title="Plan One")


def test_create_then_add_nodes(plan: PlanHandler, hub: Hub) -> None:
    proj = _proj(hub)
    r = plan.put(id="p1", title="Plan One", project=proj)
    assert "created plan 'p1'" in r.body
    # a second plan on the same project is refused (1:1 plan-of)
    with pytest.raises(ValueError, match="already has a plan"):
        plan.put(id="p1b", title="Dup", project=proj)

    a = plan.put(id="p1", text="draft the intro", at={"last": True}, status="open")
    b = plan.put(id="p1", text="gather citations", at={"last": True}, status="wip")
    assert "added 1 node" in a.body
    assert _pe(a.body) != _pe(b.body)


def test_add_node_requires_text(plan: PlanHandler, hub: Hub) -> None:
    proj = _proj(hub)
    plan.put(id="p1", title="P", project=proj)
    with pytest.raises(BadInput, match="requires text="):
        plan.put(id="p1", chunk_kind="paragraph")


def test_create_with_project_and_text_creates_not_lookup(
    plan: PlanHandler, hub: Hub
) -> None:
    # Regression (the plan-create chicken-and-egg, the top prod confusion
    # signal): a create call that also carries text= must NOT be misrouted into
    # the add-node lookup and hit "plan slug 'p1' not found". project= is the
    # create signal; the text= seeds the first node.
    proj = _proj(hub)
    r = plan.put(id="p1", title="Plan One", project=proj, text="first thought")
    assert "created plan 'p1'" in r.body
    assert "added node pe" in r.body  # the text became the first node


def test_mode_create_routes_to_create(plan: PlanHandler, hub: Hub) -> None:
    # An explicit mode='create' forces the create path even with text= and no
    # project — so the error is the actionable "requires project=", proving it
    # did NOT fall into the add-node lookup ("doesn't exist yet").
    with pytest.raises(BadInput, match="requires project="):
        plan.put(id="ghost2", text="kickoff", mode="create")


def test_add_node_to_missing_plan_gives_create_hint(
    plan: PlanHandler, hub: Hub
) -> None:
    # Adding a node to a plan that doesn't exist yet must surface an actionable
    # "create it first (needs project=)" message, not the misleading raw
    # NotFound "slug not found" that read as a chicken-and-egg.
    with pytest.raises(BadInput, match="doesn't exist yet"):
        plan.put(id="ghost", text="a node", at={"last": True})


# ── whole-tree render: markers + cursor ──────────────────────────────


def test_render_shows_markers_and_cursor(plan: PlanHandler, hub: Hub) -> None:
    proj = _proj(hub)
    plan.put(id="p1", title="Plan One", project=proj)
    n_open = _pe(plan.put(id="p1", text="open task", at={"last": True}).body)
    n_wip = _pe(
        plan.put(id="p1", text="wip task", at={"last": True}, status="wip").body
    )
    n_done = _pe(
        plan.put(id="p1", text="done task", at={"last": True}, status="done").body
    )

    body = plan.get(id="p1").body
    # each status marker present against the right node
    assert f"[wip] {n_wip}" in body
    assert f"done: {n_done}" in body
    # cold-start cursor: first open node marked ▸ (not [open])
    assert f"▸ {n_open}" in body

    # explicit cursor overrides the cold-start default
    plan.edit(id="p1", cursor=n_wip)
    body2 = plan.get(id="p1").body
    assert f"▸ {n_wip}" in body2
    assert f"[open] {n_open}" in body2  # no longer the cursor
    # belief prefix renders
    plan.edit(id=n_open, belief="⚠")
    assert f"⚠[open] {n_open}" in plan.get(id="p1").body


def test_outline_gloss_is_capped_to_one_line(plan: PlanHandler, hub: Hub) -> None:
    """A model *will* write a paragraph into a plan node; the whole-tree render
    is one-line-per-node, so a prose body is whitespace-collapsed + clipped with
    an ellipsis and never spills the line. The full body stays readable via
    get(id='pe<id>')."""
    proj = _proj(hub)
    plan.put(id="p1", title="P", project=proj)
    prose = (
        "Thermodynamic driving force: electrochemical stability windows set the\n"
        "oxidative and reductive limits; wider windows suggest lower decomposition "
        "propensity, but kinetic barriers dominate the actual formation rates."
    )
    h = _pe(plan.put(id="p1", text=prose, at={"last": True}).body)

    outline_line = next(
        line for line in plan.get(id="p1").body.splitlines() if h in line
    )
    assert "\n" not in outline_line
    assert len(outline_line) <= 120  # marker + handle + capped gloss
    assert outline_line.rstrip().endswith("…")  # clipped
    # the verbatim body survives on the node read
    assert "kinetic barriers dominate" in plan.get(id=h).body


def test_cursor_rejects_foreign_node(plan: PlanHandler, hub: Hub) -> None:
    proj = _proj(hub)
    proj2 = _proj(hub)
    plan.put(id="p1", title="P1", project=proj)
    plan.put(id="p2", title="P2", project=proj2)
    other = _pe(plan.put(id="p2", text="x", at={"last": True}).body)
    with pytest.raises(BadInput, match="not a node in plan"):
        plan.edit(id="p1", cursor=other)


# ── node resolution + retire ─────────────────────────────────────────


def test_node_resolves_by_pe_handle(plan: PlanHandler, hub: Hub) -> None:
    proj = _proj(hub)
    plan.put(id="p1", title="P", project=proj)
    h = _pe(plan.put(id="p1", text="a concrete step", at={"last": True}).body)
    r = plan.get(id=h)
    assert "a concrete step" in r.body
    assert h in r.body


def test_retire_node(plan: PlanHandler, hub: Hub) -> None:
    proj = _proj(hub)
    plan.put(id="p1", title="P", project=proj)
    h = _pe(plan.put(id="p1", text="throwaway", at={"last": True}).body)
    r = plan.delete(id=h)
    assert f"retired {h}" in r.body
    assert h not in plan.get(id="p1").body


def test_edit_and_move_node(plan: PlanHandler, hub: Hub) -> None:
    proj = _proj(hub)
    plan.put(id="p1", title="P", project=proj)
    a = _pe(plan.put(id="p1", text="first", at={"last": True}).body)
    b = _pe(plan.put(id="p1", text="second", at={"last": True}).body)
    # edit text
    plan.edit(id=a, text="first (revised)")
    assert "first (revised)" in plan.get(id=a).body
    # move b before a
    plan.edit(id=b, move={"before": a})
    ref = hub.store.get_ref(kind="plan", id="p1")
    order = [c.dc for c in hub.store.reading_order(ref.id, kind="plan")]
    assert order.index(b) < order.index(a)


# ── export refuses a plan ────────────────────────────────────────────


def test_export_refuses_plan(plan: PlanHandler, hub: Hub, tmp_path: Path) -> None:
    from precis.export.latex import export_draft

    proj = _proj(hub)
    plan.put(id="p1", title="P", project=proj)
    ref = hub.store.get_ref(kind="plan", id="p1")
    with pytest.raises(BadInput, match="not an exportable deliverable"):
        export_draft(hub.store, ref, target_dir=tmp_path)
