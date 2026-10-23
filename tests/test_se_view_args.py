"""gr503026: ``args={'block': ...}`` is the canonical block selector on
``view='block'`` and ``view='surface_deviation'`` (the name every sibling
view uses); ``args={'name': ...}`` stays accepted as a deprecated alias —
same render plus a hint naming the new key.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, cast

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.store import Store
from precis_se import persist
from precis_se.handler import SeHandler
from precis_se.ops import SeBlock, SeTree


class ReadOnlyStore:
    def get_ref(self, *, kind: str, id: str) -> Any:
        if kind == "se":
            return SimpleNamespace(id=1, slug="design", meta={})
        return None


@pytest.fixture
def handler(monkeypatch: pytest.MonkeyPatch) -> SeHandler:
    tree = SeTree(
        blocks={
            "plain": SeBlock(name="plain"),
            "bound": SeBlock(name="bound", bound_kind="structure", bound="gone"),
        }
    )
    monkeypatch.setattr(persist, "load_tree", lambda *_args: copy.deepcopy(tree))
    return SeHandler(hub=Hub(store=cast(Store, ReadOnlyStore())))


def test_block_selector_is_canonical(handler: SeHandler) -> None:
    body = handler.get(id="design", view="block", args={"block": "plain"}).body
    assert body.startswith("# block 'plain'")
    assert "deprecated alias" not in body


def test_name_alias_renders_with_hint(handler: SeHandler) -> None:
    body = handler.get(id="design", view="block", args={"name": "plain"}).body
    assert body.startswith("# block 'plain'")
    assert "deprecated alias" in body and "args={'block'" in body


def test_agreeing_alias_and_selector_still_hint(handler: SeHandler) -> None:
    body = handler.get(
        id="design", view="block", args={"block": "plain", "name": "plain"}
    ).body
    assert body.startswith("# block 'plain'")
    assert "deprecated alias" in body


def test_disagreeing_alias_and_selector_refused(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="disagree"):
        handler.get(id="design", view="block", args={"block": "plain", "name": "bound"})


def test_missing_selector_names_block(handler: SeHandler) -> None:
    with pytest.raises(BadInput, match="args={'block'"):
        handler.get(id="design", view="block")
    with pytest.raises(BadInput, match="args={'block'"):
        handler.get(id="design", view="block", args={"name": " "})


def test_surface_deviation_takes_block_and_hints_on_name(
    handler: SeHandler,
) -> None:
    # 'bound' has no resolvable structure — the view reaches its own
    # "unknown" render, which is enough to see the selector path.
    canonical = handler.get(
        id="design", view="surface_deviation", args={"block": "bound"}
    ).body
    assert "bound" in canonical
    assert "deprecated alias" not in canonical
    aliased = handler.get(
        id="design", view="surface_deviation", args={"name": "bound"}
    ).body
    assert "bound" in aliased
    assert "deprecated alias" in aliased and "args={'block'" in aliased
