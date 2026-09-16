"""Tests for JSON handling in :func:`precis.tools.cli_adapter.convert_value`.

Any ``list[dict]`` verb parameter (edit's ``ops``, put's ``items``,
``supporters``, ``wants``, ``authors``) is unreachable from the CLI mirror
(``scripts/prod-precis``) unless ``convert_value`` routes a JSON-looking
string to ``json.loads`` instead of the comma-splitter. See
``src/precis/tools/cli_adapter.py``.
"""

from __future__ import annotations

import argparse

import pytest

from precis.tools import get_tool_info
from precis.tools.cli_adapter import convert_value


def _param_info(*, is_list: bool, annotation: str, name: str = "value") -> dict:
    """Build a ``param_info`` dict the way ``get_tool_info`` produces it."""
    return {
        "name": name,
        "required": False,
        "default": None,
        "annotation": annotation,
        "is_list": is_list,
    }


def test_json_list_of_dicts_round_trips_on_list_param() -> None:
    """A JSON list of dicts on a `list[dict]` param parses to Python objects."""
    param_info = _param_info(
        is_list=True, annotation="list[dict[str, Any]] | None", name="ops"
    )
    ops_json = '[{"op":"set_mode","mode":"atomic"},{"op":"envelope_fit"}]'

    result = convert_value(ops_json, param_info)

    assert result == [
        {"op": "set_mode", "mode": "atomic"},
        {"op": "envelope_fit"},
    ]


def test_plain_comma_list_still_comma_splits() -> None:
    """A non-JSON value on a list param still comma-splits as before."""
    param_info = _param_info(is_list=True, annotation="list[str]", name="tags")

    result = convert_value("tag1,tag2,tag3", param_info)

    assert result == ["tag1", "tag2", "tag3"]


def test_malformed_json_list_raises_and_is_not_comma_split() -> None:
    """Malformed JSON starting with `[` errors instead of silently splitting."""
    param_info = _param_info(
        is_list=True, annotation="list[dict[str, Any]] | None", name="ops"
    )

    with pytest.raises(argparse.ArgumentTypeError, match="ops"):
        convert_value('[{"op":"set_mode"', param_info)


def test_single_dict_on_list_param_is_wrapped_in_a_list() -> None:
    """A bare `{...}` value on a list param is wrapped as a one-element list."""
    param_info = _param_info(
        is_list=True, annotation="list[dict[str, Any]] | None", name="ops"
    )

    result = convert_value('{"op":"set_mode","mode":"atomic"}', param_info)

    assert result == [{"op": "set_mode", "mode": "atomic"}]


def test_dict_value_on_dict_annotated_non_list_param_parses() -> None:
    """A `{...}` value on a `dict`-annotated non-list param (e.g. `meta`) parses."""
    param_info = _param_info(
        is_list=False, annotation="dict[str, Any] | None", name="meta"
    )

    result = convert_value('{"key": "value"}', param_info)

    assert result == {"key": "value"}


def test_malformed_json_on_dict_param_raises() -> None:
    """Malformed JSON on a dict-annotated non-list param also errors cleanly."""
    param_info = _param_info(
        is_list=False, annotation="dict[str, Any] | None", name="meta"
    )

    with pytest.raises(argparse.ArgumentTypeError, match="meta"):
        convert_value('{"key": "value"', param_info)


def test_edit_ops_param_is_registered_as_a_list() -> None:
    """The real `edit` tool's `ops` param is `is_list=True`, as this fix assumes."""
    info = get_tool_info("edit")

    ops_param = info["parameters"]["ops"]

    assert ops_param["is_list"] is True
