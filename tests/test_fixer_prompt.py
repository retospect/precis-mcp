"""Unit tests for the fixer build prompt (ADR 0048).

The build prompt carries the CLAUDE.md "same-commit map freshness" norm
into every fixer build — the in-build (soft, contextual) layer of the
layered doc-freshness defense. The ship-gate judge and periodic sweep
remain separately deferred.
"""

from __future__ import annotations

from precis.fixer.intake import WorkItem
from precis.fixer.tick import _compose_prompt


def _item() -> WorkItem:
    return WorkItem(
        kind="proposal",
        slug="do-the-thing",
        title="Do the thing",
        branch="fix/do-the-thing",
        spec_text="## In scope\n- something\n",
    )


def test_compose_prompt_carries_spec() -> None:
    prompt = _compose_prompt(_item())
    assert "Do the thing" in prompt
    assert "## In scope" in prompt


def test_compose_prompt_enforces_same_commit_map_freshness() -> None:
    prompt = _compose_prompt(_item())
    # Names the norm and at least the two required maps.
    assert "same commit" in prompt.lower()
    assert "CLAUDE.md" in prompt
    assert "docs/decisions/README.md" in prompt


def test_compose_prompt_names_the_do_not_force_update_set() -> None:
    prompt = _compose_prompt(_item())
    # Archival prose / schema SVG / sealed ADRs are NOT force-updated.
    assert "docs/design/" in prompt
    assert "SVG" in prompt
    assert "append-only" in prompt


def test_compose_prompt_instructs_delegation_to_named_subagents() -> None:
    prompt = _compose_prompt(_item())
    # Mechanical substeps go to named subagents via the Agent tool, not
    # done inline in the (possibly top-tier) builder context.
    assert "test-runner" in prompt
    assert "tidy" in prompt
    assert "documenter" in prompt
    assert "navigator" in prompt
    assert "Agent tool" in prompt
