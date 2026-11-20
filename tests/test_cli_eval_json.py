"""``precis eval --json``: one JSON object on stdout, ``created`` for a put.

The create receipts differ by kind (``created todo td735113 (STATUS:open)``,
``created gripe id=734976 (STATUS:open).``, ``created <kind> <handle>.``), so a
script that parsed one shape failed on another (gr735223). ``--json`` derives
the new ref's handle once, in ``eval_cmd``. The tool functions are faked with
the receipt shapes the handlers emit: this pins the CLI contract, not the
handlers' prose.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

import pytest

from precis.cli import eval_cmd
from precis.cli._common import REFUSAL_EXIT


def _run_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    func: Any,
    *,
    verb: str = "put",
) -> tuple[dict[str, Any], str, Any]:
    """Run ``eval --json`` over a fake tool; return (stdout object, stderr, exit)."""
    monkeypatch.setattr(eval_cmd, "parse_command", lambda _c, text=None: (verb, {}))
    monkeypatch.setattr(eval_cmd, "TOOL_REGISTRY", {verb: {"func": func}})
    args = argparse.Namespace(command="x()", text=None, text_file=None, json=True)
    code: Any = None
    try:
        eval_cmd.run(args)
    except SystemExit as e:
        code = e.code
    cap = capsys.readouterr()
    return json.loads(cap.out), cap.err, code


@pytest.mark.parametrize(
    "receipt,handle",
    [
        ("created todo td735113 (STATUS:open).\nNext: x", "td735113"),
        ("created gripe id=734976 (STATUS:open).\n\nNext: y", "gr734976"),
        ("created memory me158.\nNext: z", "me158"),
        ("created finding id=5.\nNext: z", "fi5"),
    ],
)
def test_put_receipts_yield_the_canonical_handle(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    receipt: str,
    handle: str,
) -> None:
    out, _err, code = _run_json(monkeypatch, capsys, lambda **_k: receipt)
    assert code is None
    assert out == {"ok": True, "text": receipt, "created": handle, "error": None}


def test_get_has_no_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    body = "# td735113 a todo\nthe body mentions created todo td1 later"
    out, _err, code = _run_json(monkeypatch, capsys, lambda **_k: body, verb="get")
    assert code is None
    assert out["ok"] is True and out["created"] is None and out["error"] is None
    assert out["text"] == body


@pytest.mark.parametrize(
    "receipt",
    ["created plan 'a-slug' (root dc1)", "created widget id=7."],
)
def test_receipt_without_a_handle_is_null(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    receipt: str,
) -> None:
    out, _err, _code = _run_json(monkeypatch, capsys, lambda **_k: receipt)
    assert out["ok"] is True and out["created"] is None


def test_rendered_refusal_is_ok_false_with_the_class(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    msg = "[error:NotFound] todo td1 not found"
    out, err, code = _run_json(monkeypatch, capsys, lambda **_k: msg, verb="get")
    assert code == REFUSAL_EXIT
    assert out == {"ok": False, "text": msg, "created": None, "error": "NotFound"}
    assert err == ""


def test_call_tool_result_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from mcp.types import CallToolResult, TextContent

    msg = "[error:BadInput] kind required"
    result = CallToolResult(content=[TextContent(type="text", text=msg)], isError=True)
    out, _err, code = _run_json(monkeypatch, capsys, lambda **_k: result)
    assert code == REFUSAL_EXIT
    assert out["ok"] is False and out["error"] == "BadInput" and out["text"] == msg


def test_bad_kwargs_and_crashes_are_json_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def bad(**_k: object) -> str:
        raise TypeError("unexpected keyword 'zz'")

    out, _err, code = _run_json(monkeypatch, capsys, bad)
    assert code == 1
    assert out["ok"] is False and out["error"] == "BadInput" and out["created"] is None

    def boom(**_k: object) -> str:
        raise RuntimeError("kaboom")

    out, _err, code = _run_json(monkeypatch, capsys, boom)
    assert code == 1
    assert out["ok"] is False and out["error"] == "RuntimeError"
    assert "kaboom" in out["text"]


def test_parse_error_is_a_json_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def parse(_c: str, text: str | None = None) -> tuple[str, dict[str, Any]]:
        raise eval_cmd.CommandParseError("not a call")

    monkeypatch.setattr(eval_cmd, "parse_command", parse)
    args = argparse.Namespace(command="x", text=None, text_file=None, json=True)
    with pytest.raises(SystemExit) as exc:
        eval_cmd.run(args)
    assert exc.value.code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False and out["error"] == "CommandParseError"


def test_stray_prints_and_logs_stay_off_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    # A stderr handler, as `precis` configures on the root logger.
    log = logging.getLogger("precis.test_eval_json")
    log.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    log.addHandler(handler)

    def noisy(**_k: object) -> str:
        print("stray handler print")
        log.warning("a log line")
        return "created todo td9 (STATUS:open)."

    try:
        out, err, _code = _run_json(monkeypatch, capsys, noisy)
    finally:
        log.removeHandler(handler)
    assert out["created"] == "td9"
    assert "stray handler print" in err and "a log line" in err


def test_without_the_flag_output_is_unchanged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    receipt = "created todo td735113 (STATUS:open)."
    monkeypatch.setattr(eval_cmd, "parse_command", lambda _c, text=None: ("put", {}))
    monkeypatch.setattr(
        eval_cmd, "TOOL_REGISTRY", {"put": {"func": lambda **_k: receipt}}
    )
    eval_cmd.run(argparse.Namespace(command="x()", text=None, text_file=None))
    cap = capsys.readouterr()
    assert cap.out == receipt + "\n" and cap.err == ""


def test_flag_is_registered() -> None:
    parser = argparse.ArgumentParser()
    eval_cmd.add_parser(parser.add_subparsers())
    ns = parser.parse_args(["eval", "get(kind='skill', id='toc')", "--json"])
    assert ns.json is True
    assert parser.parse_args(["eval", "x"]).json is False
