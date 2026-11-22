"""Planner prose starts in the mesh, including on draft-bound file hosts."""

import pytest

from precis.utils.prompt import AssemblyContext
from precis.workers.planner_prompt import _m_contract


@pytest.mark.parametrize("root", ["", "/exports"])
def test_planner_uses_graph_even_on_hosts_with_exports(
    monkeypatch: pytest.MonkeyPatch, root: str
) -> None:
    monkeypatch.setenv("PRECIS_ROOT", root)
    text = _m_contract(AssemblyContext(store=None, ref_id=1, model="test"))
    assert "search(kind='memory', tags=['project:" in text
    assert "If an older brief asks for filesystem access" in text
    assert "rel='derived-into'" in text
    assert "put(kind='tex'" not in text
    assert "get(kind='tex'" not in text
