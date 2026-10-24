"""gr514236/514235 (and the one-line half of 514234): se read views'
recoverability.

- An unknown ``view=`` refuses with a family-grouped roster plus the
  closest real view name — not the old ~50-view single-line wall
  (gr514236). ``connects``/``joints`` get a pointed answer naming where
  connects actually render, since no whole-design table exists yet.
- ``view='ops'`` takes ``args={'offset', 'limit'}`` slices that
  concatenate back into :func:`design_ops` — the fold's elision cannot
  strand the middle of a replayable export (gr514235).
- The bare-design eye names ``view='tree'`` so a caller landing there
  can find the blocks (the discoverability sliver of gr514234).
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


def test_connects_points_at_ports_and_block_under_600_chars(
    handler: SeHandler,
) -> None:
    """The gripe: ``view='connects'`` used to answer with a ~1.5k-token
    wall of every view, and the two that show connects were unfindable.
    The reply is now a pointed sentence under the gripe's own bar."""
    handler.put(id="c1", text="{}")
    with pytest.raises(BadInput) as exc:
        handler.get(id="c1", view="connects")
    message = f"{exc.value} {exc.value.next or ''}"
    assert len(message) < 600
    assert "view='ports'" in message
    assert "view='block'" in message


def test_joints_gets_the_same_pointed_answer(handler: SeHandler) -> None:
    handler.put(id="c2", text="{}")
    with pytest.raises(BadInput) as exc:
        handler.get(id="c2", view="joints")
    assert "view='ports'" in str(exc.value.next)


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
    silently part ways again."""
    dispatched: set[str] = set()
    src = textwrap.dedent(inspect.getsource(SeHandler._get_impl))
    for node in ast.walk(ast.parse(src)):
        if (
            not isinstance(node, ast.Compare)
            or not isinstance(node.left, ast.Name)
            or node.left.id != "v"
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
