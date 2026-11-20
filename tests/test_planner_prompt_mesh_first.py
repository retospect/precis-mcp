"""Planner prose starts in the mesh, including on draft-bound file hosts."""

from unittest.mock import patch

import pytest

from precis.utils.prompt import AssemblyContext
from precis.workers.planner_prompt import _m_contract


@pytest.mark.parametrize(
    ("root", "disabled", "exports"),
    [
        ("", "", False),
        ("/exports", "tex:bound draft", False),
        ("/exports", "markdown, tex:bound draft", False),
        ("/exports", "", True),
    ],
)
def test_brief_names_hub_search_before_file_kinds(
    monkeypatch: pytest.MonkeyPatch, root: str, disabled: str, exports: bool
) -> None:
    monkeypatch.setenv("PRECIS_ROOT", root)
    monkeypatch.setenv("PRECIS_KINDS_DISABLED", disabled)
    text = _m_contract(AssemblyContext(store=None, ref_id=1, model="test"))
    hub = text.index("search(kind='memory', tags=['project:")
    assert ("put(kind='tex'" in text) is exports
    assert ("get(kind='tex'" in text) is exports
    if exports:
        assert hub < text.index("put(kind='tex'")


def test_bound_draft_suppresses_exports_before_subprocess_overlay(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PRECIS_ROOT", "/exports")
    monkeypatch.delenv("PRECIS_KINDS_DISABLED", raising=False)
    with patch(
        "precis.workers.planner_prompt.bound_draft",
        return_value=("draft", "Title", "tex"),
    ):
        # The binding resolver is mocked; no database operation is performed.
        from unittest.mock import MagicMock

        text = _m_contract(AssemblyContext(store=MagicMock(), ref_id=1, model="test"))
    assert "put(kind='tex'" not in text
    assert "get(kind='tex'" not in text
