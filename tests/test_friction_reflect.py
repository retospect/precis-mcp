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


# ── gr516603: the read-only (write:none / agent_ro) variant ────────


def test_ro_run_eligible_without_flag(monkeypatch):
    """Reto 2026-10-25: the nudge is ON for every read-only run from the
    first release — no flag, no staged rollout. A write-capable run at
    the same settings stays off."""
    assert fr.friction_eligible(has_mcp=True, max_turns=20) is False
    assert fr.friction_eligible(has_mcp=True, max_turns=20, read_only=True) is True


def test_ro_run_still_needs_mcp_and_turns(monkeypatch):
    """Always-on doesn't lift the structural guards: no MCP → nowhere to
    ``put``; under the turn floor → the task keeps its budget."""
    assert fr.friction_eligible(has_mcp=False, max_turns=20, read_only=True) is False
    assert (
        fr.friction_eligible(
            has_mcp=True, max_turns=fr.FRICTION_MIN_TURNS - 1, read_only=True
        )
        is False
    )


def test_ro_footer_is_create_only_with_friction_fallback(monkeypatch):
    out = fr.append_friction_footer("SOUL", has_mcp=True, max_turns=20, read_only=True)
    assert out is not None
    assert out.startswith("SOUL")
    assert fr.FRICTION_REFLECTION_RO in out
    assert "friction: none" in out  # same honored default
    # Create-only: the definer stamps the tags, so the footer must not
    # teach a tags= filing — and it names the stamp so the agent
    # doesn't try.
    assert "tags=[" not in fr.FRICTION_REFLECTION_RO
    assert "friction-model" not in fr.FRICTION_REFLECTION_RO
    assert "source:agent_ro" in out
    assert "put(kind='gripe'" in out
    # The FRICTION-block fallback for when put itself is refused
    # (tier-1 tool deny or a DB cap) — mechanism (b) of the spec.
    assert "FRICTION" in out
    assert "title:" in out
    assert "END" in out


def test_ro_footer_is_whole_prompt_when_no_prior(monkeypatch):
    out = fr.append_friction_footer(None, has_mcp=True, max_turns=20, read_only=True)
    assert out == fr.FRICTION_REFLECTION_RO


def test_rw_footer_still_flag_gated_and_tag_teaching(monkeypatch):
    """The rw variant is unchanged: flag-gated, and still teaches the
    friction + friction-model tags an rw filer must pass itself."""
    _on(monkeypatch)
    out = fr.append_friction_footer(None, has_mcp=True, max_turns=20)
    assert out == fr.FRICTION_REFLECTION
    assert "friction-model" in out
