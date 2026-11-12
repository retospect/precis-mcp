"""gr632147: the search tool description and scope= refusal must agree
on which kinds accept the one-ref find-in-ref read.

The slim search schema advertises ``scope=`` on every kind (it is a core
param), but the read only exists on kinds whose search implements it.
Before this slice the description claimed ``scope=`` "searches inside one
ref" unqualified, and a kind without the read refused with a bare
kwargs-gate error that named only the refusing kind's own accepted keys —
the caller could not tell scope= was valid elsewhere. The description now
qualifies the read, and the dispatch kwargs gate names the kinds that
accept it, computed from the live handler registry so the hint cannot
drift from the handlers.
"""

from __future__ import annotations

from precis import server


def test_search_description_qualifies_scope() -> None:
    """The advertised slim description must not present ``scope=`` as
    accepted by every kind."""
    tool = server.mcp._tool_manager.get_tool("search")
    assert tool is not None
    desc = tool.description or ""
    assert "find-in-ref" in desc
    assert "refusal" in desc
