"""Make-tree kind + `made-by` alignment (make-tree-vs-design-tree.md v1).

Two trees over shared leaves: the design tree says what a thing IS
(`contains`), the make tree says the ORDER it comes together — steps are
first-class chunk nodes (`mk<id>`) carrying conditions, and blocks align
to steps via chunk-scoped `made-by` links written from the cad side.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.cad import CadHandler
from precis.handlers.make import MakeHandler


@pytest.fixture
def make(store):
    return MakeHandler(hub=Hub(store=store))


@pytest.fixture
def cad(store):
    return CadHandler(hub=Hub(store=store))


def _step_handles(resp_body: str) -> list[str]:
    import re

    return re.findall(r"\bmk\d+\b", resp_body)


def test_create_add_and_render_steps(make):
    out = make.put(id="crane-assembly", title="crane assembly order")
    assert "created make tree" in out.body
    out = make.put(
        id="crane-assembly",
        text="bolt tower to base",
        meta={"fixture": "torque wrench", "torque": "40 Nm"},
    )
    (mk1,) = _step_handles(out.body)
    out = make.put(id="crane-assembly", text="press slew bearing", at={"last": True})
    (mk2,) = _step_handles(out.body)
    # a sub-step nests under its parent
    out = make.put(
        id="crane-assembly", text="grease race first", at={"into": mk2}, status="wip"
    )
    (mk3,) = _step_handles(out.body)

    tree = make.get(id="crane-assembly").body
    assert "2 step" not in tree  # 3 steps
    assert tree.index(mk1) < tree.index(mk2) < tree.index(mk3)
    assert "⟨fixture=torque wrench  torque=40 Nm⟩" in tree
    assert "[wip]" in tree
    # the nested step renders deeper than its parent
    line2 = next(ln for ln in tree.splitlines() if mk2 in ln)
    line3 = next(ln for ln in tree.splitlines() if mk3 in ln)
    assert len(line3) - len(line3.lstrip()) > len(line2) - len(line2.lstrip())


def test_step_handle_survives_edit_and_move(make):
    make.put(id="mt-edit", title="t")
    (mk1,) = _step_handles(make.put(id="mt-edit", text="step one").body)
    (mk2,) = _step_handles(
        make.put(id="mt-edit", text="step two", at={"last": True}).body
    )
    assert f"edited {mk1}" in make.edit(id=mk1, text="step one, reworded").body
    assert f"moved {mk2}" in make.edit(id=mk2, move={"before": mk1}).body
    tree = make.get(id="mt-edit").body
    assert tree.index(mk2) < tree.index(mk1)
    assert "reworded" in make.get(id=mk1).body


def test_made_by_aligns_blocks_to_tree_and_steps(make, cad, store):
    cad.put(id="gantry", text="component frame\nrail add box:w200d20h20")
    make.put(id="gantry-build", title="gantry build order")
    (mk1,) = _step_handles(make.put(id="gantry-build", text="mount rails").body)

    # ref-level: this design is made by this tree
    out = cad.link(id="gantry", target="make:gantry-build", rel="made-by")
    assert "whole make-tree" in out.body
    # chunk-scoped: this block is made in THIS step
    out = cad.link(id="gantry", target=mk1, rel="made-by")
    assert f"step {mk1}" in out.body

    tree = make.get(id="gantry-build").body
    assert "makes: cad:gantry" in tree  # ref-level, on the header
    step_line = next(ln for ln in tree.splitlines() if mk1 in ln)
    assert "⛓ cad:gantry" in step_line  # chunk-scoped, on the step

    # chunk-scoped remove detaches only that edge
    out = cad.link(id="gantry", target=mk1, rel="made-by", mode="remove")
    assert "detached 1" in out.body
    ref = store.get_ref(kind="cad", id="gantry")
    (left,) = store.links_for(ref.id, direction="out", relation="made-by")
    assert left.dst_chunk_id is None


def test_made_by_target_must_be_a_make_tree(cad):
    cad.put(id="widget", text="plate add box:w10d10h2")
    cad.put(id="widget2", text="plate add box:w10d10h2")
    with pytest.raises(BadInput, match="make tree or step"):
        cad.link(id="widget", target="cad:widget2", rel="made-by")


def test_make_link_refuses_non_parent(make):
    make.put(id="mt-l", title="t")
    with pytest.raises(BadInput, match="design side"):
        make.link(id="mt-l", target="make:mt-l", rel="related-to")


def test_make_coverage_lint_flags_unaligned_sub_designs(make, cad):
    cad.put(id="axis_sub", text="component slide\nblock add box:w30d30h10")
    cad.put(
        id="machine",
        text="component base\nbed add box:w300d100h20\nuse axis_sub as ax @0,0,20",
    )
    make.put(id="machine-build", title="machine build")
    make.put(id="machine-build", text="install the axis")

    # no make-tree declared → no lint
    assert "make-coverage" not in cad.get(id="machine", view="links").body
    # declare the tree; the contained sub-design is not aligned → loud
    cad.link(id="machine", target="make:machine-build", rel="made-by")
    body = cad.get(id="machine", view="links").body
    assert "make-coverage" in body and "axis_sub" in body
    # align the child → lint clears
    cad.link(id="axis_sub", target="make:machine-build", rel="made-by")
    assert "make-coverage" not in cad.get(id="machine", view="links").body


def test_delete_step_and_tree(make):
    make.put(id="mt-del", title="t")
    (mk1,) = _step_handles(make.put(id="mt-del", text="only step").body)
    assert f"retired {mk1}" in make.delete(id=mk1).body
    assert "0 step" in make.get(id="mt-del").body
    assert "retired make tree" in make.delete(id="mt-del").body
