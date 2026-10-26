"""gr514236/514235 (and the one-line half of 514234): se read views'
recoverability.

- An unknown ``view=`` refuses with a family-grouped roster plus the
  closest real view name — not the old ~50-view single-line wall
  (gr514236). ``connect``/``joint``/``joints`` get a pointed answer
  naming ``view='connects'`` — the whole-design table the same gripe
  asked for, one row per connect with joint/kind/objectives.
- ``view='ops'`` takes ``args={'offset', 'limit'}`` slices that
  concatenate back into :func:`design_ops` — the fold's elision cannot
  strand the middle of a replayable export (gr514235).
- The bare-design eye names ``view='tree'`` so a caller landing there
  can find the blocks (the discoverability sliver of gr514234).
- ``view='tree'`` nests children whose ``parent`` names an *instance*
  under that instance — they used to vanish entirely when the walk
  expanded only the template's key (gr514243).
"""

from __future__ import annotations

import ast
import inspect
import json
import textwrap

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se.handler import _VIEWS_ALL, SeHandler


@pytest.fixture
def handler(hub: Hub, store: Store) -> SeHandler:
    return SeHandler(hub=hub)


def test_connects_view_is_the_whole_design_table(handler: SeHandler) -> None:
    """gr514236's missing half: one row per connect, ``a.port`` ↔
    ``b.port`` with its joint/kind/objectives — the columns
    ``view='block'`` shows per block."""
    handler.put(
        id="cn1",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "a"},
                    {"op": "add_block", "name": "b"},
                    {"op": "add_block", "name": "c"},
                    {"op": "add_port", "block": "a", "name": "p"},
                    {
                        "op": "add_port",
                        "block": "a",
                        "name": "r",
                        "roles": ["covalent"],
                    },
                    {"op": "add_port", "block": "b", "name": "q"},
                    {
                        "op": "add_port",
                        "block": "c",
                        "name": "s",
                        "roles": ["covalent"],
                    },
                    {
                        "op": "connect",
                        "a": "a.p",
                        "b": "b.q",
                        "joint": {"class": "rigid"},
                    },
                    {"op": "connect", "a": "a.r", "b": "c.s", "kind": "bond"},
                ]
            }
        ),
    )
    body = handler.get(id="cn1", view="connects").body
    assert "2 connect(s)" in body
    assert "a.p" in body and "b.q" in body and "a.r" in body and "c.s" in body
    assert "rigid" in body  # the joint column
    assert "bond" in body  # the atomic-mode kind column, shown on demand


def test_connects_view_reads_honest_when_empty(handler: SeHandler) -> None:
    handler.put(id="cn0", text="{}")
    body = handler.get(id="cn0", view="connects").body
    assert "(no connects declared yet)" in body


def test_connect_like_spellings_point_at_the_view(handler: SeHandler) -> None:
    """Near-miss spellings refuse short and pointed — the gripe's bar
    was a one-line answer under 600 chars, not the ~50-view wall."""
    handler.put(id="c1", text="{}")
    # 'joints' is a real view (the joint vocabulary), not a near-miss.
    for v in ("connect", "joint"):
        with pytest.raises(BadInput) as exc:
            handler.get(id="c1", view=v)
        next_ = str(exc.value.next)
        assert "view='connects'" in next_
        assert len(next_) < 600


def test_an_unknown_view_suggests_the_nearest_real_view(
    handler: SeHandler,
) -> None:
    handler.put(id="c3", text="{}")
    with pytest.raises(BadInput) as exc:
        handler.get(id="c3", view="nope")
    next_ = str(exc.value.next)
    assert "did you mean view=" in next_
    # …and the roster is still there, grouped one family per line.
    assert "view='ops'" in next_
    assert next_.count("\n") >= 3


def test_the_roster_covers_every_dispatched_view() -> None:
    """Drift guard: a ``v == "<view>"`` branch in ``_get_impl`` whose
    name is missing from the roster fails here — the two can never
    silently part ways again. ``completion``/``completion_digest``
    dispatch through the earlier ``completion_view`` alias (bound before
    ``v``), so the walk accepts both names."""
    dispatched: set[str] = set()
    src = textwrap.dedent(inspect.getsource(SeHandler._get_impl))
    for node in ast.walk(ast.parse(src)):
        if (
            not isinstance(node, ast.Compare)
            or not isinstance(node.left, ast.Name)
            or node.left.id not in ("v", "completion_view")
        ):
            continue
        for op, comp in zip(node.ops, node.comparators, strict=True):
            if (
                isinstance(op, ast.Eq)
                and isinstance(comp, ast.Constant)
                and isinstance(comp.value, str)
            ):
                dispatched.add(comp.value)
            elif isinstance(op, ast.In) and isinstance(comp, ast.Tuple):
                dispatched.update(
                    elt.value
                    for elt in comp.elts
                    if isinstance(elt, ast.Constant) and isinstance(elt.value, str)
                )
    assert "" in dispatched  # the default tree — sanity that the walk ran
    assert dispatched - {""} == set(_VIEWS_ALL)


def test_the_bare_design_eye_names_view_tree(handler: SeHandler) -> None:
    """gr514234's cheap half: the eye shows the ref's card and link
    rings but never the design's blocks — the hint names the view that
    does."""
    handler.put(
        id="eye1",
        text=json.dumps({"ops": [{"op": "add_block", "name": "b"}]}),
    )
    body = handler.eye(id="eye1", extent="fisheye+1hop").body
    assert "view='tree'" in body


def _indent_of(body: str, name: str) -> int:
    """The leading-space depth of the tree line naming block ``name``."""
    for line in body.splitlines():
        stripped = line.strip()
        if stripped.startswith("- ") and stripped[2:].split("  ")[0] == name:
            return len(line) - len(line.lstrip())
    raise AssertionError(f"block {name!r} not found in:\n{body}")


def test_tree_nests_children_parented_to_an_instance(handler: SeHandler) -> None:
    """gr514243: ``parent`` may name an *instance* (unicycle-c1's bolts
    hang off crown/crank instances) — the walk used to expand only the
    template's key, so such children vanished from the tree entirely.
    Both keys now render: the template's subtree and this node's own."""
    handler.put(
        id="uni",
        text=json.dumps(
            {
                "ops": [
                    {"op": "add_block", "name": "t_crank"},
                    {"op": "add_block", "name": "t_axle", "parent": "t_crank"},
                    {
                        "op": "instance_block",
                        "name": "crank_left",
                        "template": "t_crank",
                    },
                    {
                        "op": "add_block",
                        "name": "crank_bolt_left",
                        "parent": "crank_left",
                    },
                ]
            }
        ),
    )
    body = handler.get(id="uni", view="tree").body
    crank_depth = _indent_of(body, "crank_left")
    assert _indent_of(body, "crank_bolt_left") > crank_depth
    # The template's own subtree expands under the instance AND under the
    # template's own row in the "templates" section — t_axle appears
    # twice, both indented. t_crank itself is a template (instance-named,
    # gr514241): it leaves the part walk and is listed under "templates".
    assert body.count("- t_axle") == 2
    assert _indent_of(body, "t_axle") > crank_depth
    assert "templates (library parts" in body
    assert "instanced by: crank_left" in body
