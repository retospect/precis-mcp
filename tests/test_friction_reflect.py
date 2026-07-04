"""Part A — end-of-run tool-friction reflection eligibility + footer."""

from __future__ import annotations

import pytest

from precis.utils import friction_reflect as fr


@pytest.fixture(autouse=True)
def _clear_flag(monkeypatch):
    monkeypatch.delenv("PRECIS_FRICTION_REFLECT", raising=False)


def _on(monkeypatch):
    monkeypatch.setenv("PRECIS_FRICTION_REFLECT", "1")


def test_default_off(monkeypatch):
    # No flag → not enabled, nothing appended even with MCP + turns.
    assert fr.friction_enabled() is False
    assert fr.friction_eligible(has_mcp=True, max_turns=20) is False
    assert fr.append_friction_footer("sys", has_mcp=True, max_turns=20) == "sys"


def test_eligible_requires_mcp_and_turns(monkeypatch):
    _on(monkeypatch)
    assert fr.friction_enabled() is True
    assert fr.friction_eligible(has_mcp=True, max_turns=fr.FRICTION_MIN_TURNS)
    # No MCP → the agent can't put() a gripe → ineligible.
    assert fr.friction_eligible(has_mcp=False, max_turns=20) is False
    # Below the turn floor → skip so the task keeps its budget.
    assert (
        fr.friction_eligible(has_mcp=True, max_turns=fr.FRICTION_MIN_TURNS - 1) is False
    )


def test_footer_appended_when_eligible(monkeypatch):
    _on(monkeypatch)
    out = fr.append_friction_footer("SOUL", has_mcp=True, max_turns=20)
    assert out is not None
    assert out.startswith("SOUL")
    assert fr.FRICTION_REFLECTION in out
    assert "friction: none" in out  # binary-first honored default
    assert "kind='gripe'" in out


def test_footer_is_whole_prompt_when_no_prior(monkeypatch):
    _on(monkeypatch)
    out = fr.append_friction_footer(None, has_mcp=True, max_turns=20)
    assert out == fr.FRICTION_REFLECTION


def test_ineligible_passthrough_preserves_none(monkeypatch):
    _on(monkeypatch)
    # No MCP → passthrough, and None stays None.
    assert fr.append_friction_footer(None, has_mcp=False, max_turns=20) is None
    assert fr.append_friction_footer("x", has_mcp=False, max_turns=20) == "x"
