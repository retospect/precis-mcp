"""Smoke tests for the shared tool registry.

The registry in ``precis.tools`` is the single source of truth for the
seven-verb API surface, consumed by both the MCP server and the CLI.
These tests pin the registration shape so a refactor can't silently
drop a verb or break parameter introspection.

Companion design: ``docs/decisions/0003-shared-tool-registry.md``.
"""

from __future__ import annotations

import argparse

import pytest

# The canonical verb list (per `docs/decisions/0001` and the v1 surface).
EXPECTED_VERBS = ["get", "search", "put", "edit", "delete", "tag", "link"]


def test_registry_exposes_seven_verbs() -> None:
    """All seven verbs are registered in `precis.tools.TOOL_REGISTRY`."""
    from precis.tools import TOOL_REGISTRY, get_tool_names

    names = get_tool_names()
    assert sorted(names) == sorted(EXPECTED_VERBS), (
        f"registry should expose exactly {EXPECTED_VERBS}, got {names}"
    )
    assert sorted(TOOL_REGISTRY.keys()) == sorted(EXPECTED_VERBS)


@pytest.mark.parametrize("verb", EXPECTED_VERBS)
def test_each_verb_has_metadata(verb: str) -> None:
    """Every registered verb carries func / doc / signature / parameters."""
    from precis.tools import get_tool_info

    info = get_tool_info(verb)
    assert callable(info["func"])
    assert isinstance(info["doc"], str)
    assert info["signature"] is not None
    assert isinstance(info["parameters"], dict)


def test_unknown_tool_raises() -> None:
    """Looking up a non-existent verb is an explicit error."""
    from precis.tools import get_tool_info

    with pytest.raises(ValueError, match="not found"):
        get_tool_info("nonexistent_verb")


def test_cli_adapter_builds_parser_for_get() -> None:
    """The CLI adapter can synthesize an argparse parser for `get`."""
    from precis.tools.cli_adapter import (
        build_parser_for_tool,
        convert_args_to_payload,
    )

    parent = argparse.ArgumentParser()
    subparsers = parent.add_subparsers(dest="cmd")
    build_parser_for_tool("get", subparsers)

    parsed = parent.parse_args(["get", "--kind", "paper", "--id", "123"])
    payload = convert_args_to_payload("get", parsed)
    assert payload["kind"] == "paper"
    assert payload["id"] == "123"


def test_cli_adapter_covers_all_verbs() -> None:
    """`add_tool_parsers` registers a sub-parser for every verb in the registry."""
    from precis.tools.cli_adapter import add_tool_parsers

    parent = argparse.ArgumentParser()
    subparsers = parent.add_subparsers(dest="cmd")
    add_tool_parsers(subparsers)

    # Inspect registered choices directly to avoid triggering required-arg
    # validation on individual verbs.
    registered = set(subparsers.choices.keys())
    missing = set(EXPECTED_VERBS) - registered
    assert not missing, f"add_tool_parsers missed verb(s): {missing}"
