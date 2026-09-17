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

from precis.tools import TOOL_REGISTRY, get_tool_info
from precis.tools.cli_adapter import (
    build_parser_for_tool,
    convert_args_to_payload,
    convert_value,
    run_tool_from_cli,
)


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


# --- gr344095: `tools get`/`put`/`edit` expose --args for typed extras ---


def test_args_dict_json_object_parses_to_dict() -> None:
    """`--args '{"detail": "full"}'` parses to a plain dict, same as any dict param."""
    param_info = _param_info(
        is_list=False, annotation="dict[str, Any] | None", name="args"
    )

    result = convert_value('{"detail": "full"}', param_info)

    assert result == {"detail": "full"}


def test_args_malformed_json_raises_naming_the_param() -> None:
    """Malformed JSON on args= errors clearly instead of passing the raw string."""
    param_info = _param_info(
        is_list=False, annotation="dict[str, Any] | None", name="args"
    )

    with pytest.raises(argparse.ArgumentTypeError, match="args"):
        convert_value('{"detail": ', param_info)


@pytest.mark.parametrize("non_dict_json", ["[1, 2, 3]", "5", '"hello"', "true"])
def test_args_non_dict_json_rejected_with_expected_shape(non_dict_json: str) -> None:
    """Valid JSON that isn't an object is rejected — args= must be a JSON object."""
    param_info = _param_info(
        is_list=False, annotation="dict[str, Any] | None", name="args"
    )

    with pytest.raises(argparse.ArgumentTypeError, match="JSON object"):
        convert_value(non_dict_json, param_info)


def test_get_args_param_is_registered_and_no_longer_skipped() -> None:
    """`get`'s `args` param round-trips through the registry like any other."""
    info = get_tool_info("get")

    args_param = info["parameters"]["args"]

    assert args_param["is_list"] is False
    assert str(args_param["annotation"]) == "dict[str, Any] | None"


@pytest.mark.parametrize("tool_name", ["get", "put", "edit"])
def test_tools_get_put_edit_parsers_expose_an_args_flag(tool_name: str) -> None:
    """`--args` is a real flag on get/put/edit — it was silently dropped before."""
    top = argparse.ArgumentParser()
    subparsers = top.add_subparsers(dest="tool")

    parser = build_parser_for_tool(tool_name, subparsers)

    dests = {action.dest for action in parser._actions}
    assert "args" in dests


def test_search_has_no_args_param_to_expose() -> None:
    """`search` takes structured extras as dedicated kwargs, not an args= dict —
    nothing for the CLI adapter to unlock there."""
    info = get_tool_info("search")

    assert "args" not in info["parameters"]


def test_cli_args_flag_reaches_the_tool_call_as_a_dict(monkeypatch) -> None:
    """`tools get --args '{...}'` reaches the underlying tool call as a plain
    dict — the same payload shape the MCP verb's `args=` accepts."""
    captured: dict = {}

    def fake_get(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setitem(
        TOOL_REGISTRY, "get", {**TOOL_REGISTRY["get"], "func": fake_get}
    )

    top = argparse.ArgumentParser()
    subparsers = top.add_subparsers(dest="tool")
    build_parser_for_tool("get", subparsers)

    ns = top.parse_args(
        [
            "get",
            "--kind",
            "se",
            "--id",
            "blk1",
            "--view",
            "block",
            "--args",
            '{"detail": true}',
        ]
    )

    result = run_tool_from_cli("get", ns)

    assert result == "ok"
    assert captured["args"] == {"detail": True}


def test_cli_args_flag_omitted_behaves_as_before(monkeypatch) -> None:
    """Omitting --args doesn't forward it at all — same as every other unset
    optional flag (``convert_args_to_payload`` only includes a key when the
    value is non-None or required), so the tool's own ``args=None`` default
    applies, unchanged from before this fix."""
    captured: dict = {}

    def fake_get(**kwargs):
        captured.update(kwargs)
        return "ok"

    monkeypatch.setitem(
        TOOL_REGISTRY, "get", {**TOOL_REGISTRY["get"], "func": fake_get}
    )

    top = argparse.ArgumentParser()
    subparsers = top.add_subparsers(dest="tool")
    build_parser_for_tool("get", subparsers)

    ns = top.parse_args(["get", "--kind", "se", "--id", "blk1"])

    run_tool_from_cli("get", ns)

    assert "args" not in captured


def test_cli_args_flag_invalid_json_errors_at_parse_time() -> None:
    """A malformed --args value is rejected by argparse before the tool ever
    runs — the same clear-error contract as any other JSON-typed CLI flag."""
    top = argparse.ArgumentParser()
    subparsers = top.add_subparsers(dest="tool")
    build_parser_for_tool("get", subparsers)

    with pytest.raises(SystemExit):
        top.parse_args(["get", "--args", "not-json"])


def test_convert_args_to_payload_includes_args_key() -> None:
    """`convert_args_to_payload` no longer drops `args=` on the floor."""
    top = argparse.ArgumentParser()
    subparsers = top.add_subparsers(dest="tool")
    build_parser_for_tool("get", subparsers)

    ns = top.parse_args(["get", "--kind", "se", "--args", '{"a": 1}'])

    payload = convert_args_to_payload("get", ns)

    assert payload["args"] == {"a": 1}
