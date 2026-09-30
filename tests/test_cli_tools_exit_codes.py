"""The CLI must not report a verb refusal as success (gr458317).

Covers both shell surfaces that render verb results — ``precis tools`` and
``precis eval``. They shared a defect in different halves: ``tools`` exited 0
with the error on stdout; ``eval`` exited non-zero but also put it on stdout,
and had no check at all for a rendered ``[error:…]`` string, so that case fell
through to exit 0 as well. The contract now lives in
``precis.cli._common.is_refusal`` / ``REFUSAL_EXIT`` so the two agree.

The seven-verb protocol returns an error as a *rendered string*, not a raised
exception — correct for the MCP surface, where an agent reads the text. For a
shell caller it was a silent failure: the payload went to stdout and the process
exited 0, so ``cmd > out.txt && process out.txt`` ran ``process`` on an error
string. ``scripts/prod-precis`` is the documented fallback for when the session
MCP is dead, which is exactly when a caller has no other signal to cross-check
against.

These tests pin the shell contract, not the message text: which stream, and
which exit code.
"""

from __future__ import annotations

import argparse

import pytest

from precis.cli import tools as tools_cli
from precis.cli._common import REFUSAL_EXIT


def _run(monkeypatch: pytest.MonkeyPatch, payload: str) -> None:
    monkeypatch.setattr(tools_cli, "run_tool_from_cli", lambda _t, _a: payload)
    tools_cli.run(argparse.Namespace(tool="get"))


def test_refusal_goes_to_stderr_and_exits_nonzero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    payload = "[error:NotFound] se design 'no-such-design' not found"
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, payload)
    assert exc.value.code == tools_cli.REFUSAL_EXIT
    cap = capsys.readouterr()
    assert payload in cap.err
    assert cap.out == "", "a refusal must never reach stdout, where it reads as data"


def test_refusal_exit_is_distinct_from_the_crash_exit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ "The verb said no" and "the CLI is broken" are different answers, and a
    caller that retries on one should not retry on the other."""

    def boom(_t: object, _a: object) -> str:
        raise RuntimeError("kaboom")

    monkeypatch.setattr(tools_cli, "run_tool_from_cli", boom)
    with pytest.raises(SystemExit) as exc:
        tools_cli.run(argparse.Namespace(tool="get"))
    assert exc.value.code == 1
    assert exc.value.code != tools_cli.REFUSAL_EXIT
    assert "kaboom" in capsys.readouterr().err


def test_success_payload_still_goes_to_stdout_at_exit_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _run(monkeypatch, "# paper 123\nA perfectly ordinary result.")
    cap = capsys.readouterr()
    assert "perfectly ordinary" in cap.out
    assert cap.err == ""


def test_a_payload_that_merely_quotes_an_error_code_is_not_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Skills and help text document `[error:NotFound]` inline. Reading one is
    a success, so the match is anchored at the start of the payload — otherwise
    fetching the docs for an error would exit non-zero."""
    payload = "# precis-se-help\n\nAn unknown id returns [error:NotFound]; pass a slug."
    _run(monkeypatch, payload)
    cap = capsys.readouterr()
    assert "[error:NotFound]" in cap.out
    assert cap.err == ""


def test_leading_whitespace_does_not_hide_a_refusal(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        _run(monkeypatch, "\n  [error:Unsupported] unknown view 'abstract'")
    assert exc.value.code == tools_cli.REFUSAL_EXIT
    assert capsys.readouterr().out == ""


def test_eval_sends_a_rendered_refusal_to_stderr_at_the_same_exit_code(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`precis eval`'s gap was narrower and easier to miss: it handled the
    structured CallToolResult case but not a plain rendered `[error:…]` string,
    which fell through to `print(result)` at exit 0."""
    from precis.cli import eval_cmd

    monkeypatch.setattr(
        eval_cmd,
        "parse_command",
        lambda _c, text=None: ("get", {"kind": "se", "id": "nope"}),
    )
    monkeypatch.setattr(
        eval_cmd,
        "TOOL_REGISTRY",
        {"get": {"func": lambda **_k: "[error:NotFound] se design 'nope' not found"}},
    )

    args = argparse.Namespace(
        command="get(kind='se', id='nope')", text=None, text_file=None
    )
    with pytest.raises(SystemExit) as exc:
        eval_cmd.run(args)
    assert exc.value.code == REFUSAL_EXIT
    cap = capsys.readouterr()
    assert "NotFound" in cap.err
    assert cap.out == ""


def test_both_surfaces_agree_on_the_refusal_exit_code() -> None:
    """One constant, two commands. A script that switches between them (the
    prod-precis fallback does) must not have to special-case which one it ran."""
    from precis.cli import eval_cmd

    assert tools_cli.REFUSAL_EXIT == eval_cmd.REFUSAL_EXIT == REFUSAL_EXIT
