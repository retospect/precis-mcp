"""gr676953: the ``under= requires view='doable'`` refusal must point at
both reads that answer "everything under this todo" — the subtree get
and the claimable-leaf search — never the deprecated top-level
``under='<todo id>'`` spelling.
"""

from __future__ import annotations

import pytest

from precis.dispatch import Hub
from precis.errors import BadInput
from precis.handlers.todo import TodoHandler


@pytest.fixture
def handler(hub: Hub) -> TodoHandler:
    return TodoHandler(hub=hub)


def test_under_without_doable_points_at_tree(handler: TodoHandler) -> None:
    """Both refusal sites (the ``under=`` kwarg; ``args={'under': N}``)
    carry a next= list whose first entry is the subtree read, with the
    caller's own id interpolated."""
    with pytest.raises(BadInput, match="requires view='doable'") as kwarg_exc:
        handler.search(under=7, view="active")
    with pytest.raises(BadInput, match="requires view='doable'") as args_exc:
        handler.search(view="active", args={"under": 7})
    for exc in (kwarg_exc, args_exc):
        text = str(exc.value)
        assert "view='tree'" in text
        assert "args={'under'" in text
        assert "under='<todo id>'" not in text
        assert exc.value.next == [
            "get(kind='todo', id=7, view='tree')",
            "search(kind='todo', view='doable', args={'under': 7})",
        ]
