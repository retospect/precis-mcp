"""Pure-function tests for scripts/fleet-remote (no network)."""

from __future__ import annotations

import importlib.machinery
import importlib.util
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parent.parent / "scripts" / "fleet-remote"


def _load():
    loader = importlib.machinery.SourceFileLoader("fleet_remote", str(_PATH))
    spec = importlib.util.spec_from_loader("fleet_remote", loader)
    assert spec is not None
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


fr = _load()


def test_window_target_index_and_name():
    assert fr.window_target("0", "6") == "0:6"
    assert fr.window_target("0", "pcb") == "0:=pcb"
    with pytest.raises(ValueError):
        fr.window_target("0", " ")


def test_is_busy():
    assert fr.is_busy("working\n  esc to interrupt\n")
    assert not fr.is_busy("Worked for 3m 54s • 11:11\n› ")


def test_strip_blank_and_tail():
    lines = fr.strip_blank("a  \n\n \nb\n\nc\n")
    assert lines == ["a", "b", "c"]
    assert fr.tail(lines, 2) == ["b", "c"]
    assert fr.tail(lines, 0) == lines


def test_after_message_slices_and_drops_prompt():
    lines = ["old", "› hello there", "answer 1", "answer 2", "› Ask anything"]
    assert fr.after_message(lines, "hello there") == ["answer 1", "answer 2"]


def test_after_message_uses_last_echo_and_fallback():
    lines = ["› q", "x", "› q", "y"]
    assert fr.after_message(lines, "q") == ["y"]
    assert fr.after_message(["a", "b"], "zzz") == ["a", "b"]


def test_parse_ls_keeps_pipes_in_path():
    rows = fr.parse_ls("1|idle|node|communicator|/a|b\nbad\n")
    assert rows == [("1", "idle", "node", "communicator", "/a|b")]


def test_landed():
    assert fr.landed('x\n› it\'s `$HOME` "ok"', 'it\'s `$HOME` "ok"')
    assert fr.landed("esc to interrupt", "anything")
    assert not fr.landed("nothing", "something else")


def test_buffer_names_unique():
    assert fr.buffer_name() != fr.buffer_name()
    assert fr.buffer_name().startswith("frmsg-")


def test_send_script_guards_before_paste():
    s = fr.script_send("0:1", False, "frmsg-1-ab", "codex,uv,node")
    assert s.index("pane_current_command") < s.index("paste-buffer")
    assert s.index("list-panes") < s.index("paste-buffer")
    assert "load-buffer -b frmsg-1-ab" in s
    assert "paste-buffer -d -p -b frmsg-1-ab" in s
    assert '",codex,uv,node,"' in s
    assert "frmsg " not in s


def test_any_command_skips_process_check_not_pane_count():
    s = fr.script_send("0:1", False, "b", None)
    assert "pane_current_command" not in s
    assert "list-panes" in s


def test_leading_dash_rejected():
    with pytest.raises(ValueError):
        fr.window_target("0", "-t")
    with pytest.raises(ValueError):
        fr.window_target("-x", "1")
    with pytest.raises(ValueError):
        fr.check_ssh_dest("-oProxyCommand=x")
    assert fr.check_ssh_dest("u@h") == "u@h"
    with pytest.raises(ValueError):
        fr.check_pane_commands("codex;rm")


def test_busy_function_separates_failure_from_idle():
    assert "return 2" in fr._BUSY_FN
    assert f"exit {fr.EXIT_MISSING}" in fr.script_wait("0:1", 5)
    assert f"exit {fr.EXIT_MISSING}" in fr.script_ls("0")
    assert f"exit {fr.EXIT_MISSING}" in fr.script_read("0:1", 10)


def test_message_preserved():
    assert fr._message("a  \n\n") == "a  \n"
    assert fr._message("a\n") == "a"
    with pytest.raises(SystemExit):
        fr._message("  \n")
