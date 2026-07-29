"""Unit tests for the bash-reflex-nudge hook (matcher: Bash).

Pure — exercises ``_rule_a``/``_rule_b``/``main`` directly, no real Bash
subprocess. Mirrors ``tests/test_checkout_in_primary_guard.py``'s pattern for
loading a hyphenated-name hook script by path. Also covers the shared
``_symbol_heuristic`` module and confirms ``coderef-nudge.py``'s Grep-path
behavior is unchanged after the refactor to use it.
"""

from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path

_HOOKS_DIR = Path(__file__).resolve().parents[1] / "scripts" / "hooks"


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, _HOOKS_DIR / filename)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_mod = _load("bash_reflex_nudge", "bash-reflex-nudge.py")
_rule_a = _mod._rule_a
_rule_b = _mod._rule_b
main = _mod.main

_coderef_mod = _load("coderef_nudge", "coderef-nudge.py")


# ── Rule A: grep/rg via Bash -> coderef ─────────────────────────────────────


def test_bare_ident_grep_fires_rule_a() -> None:
    note = _rule_a("grep Foo src/")
    assert note is not None
    assert "Foo" in note
    assert "scripts/coderef" in note


def test_quoted_string_pattern_does_not_fire() -> None:
    assert _rule_a('grep "some string" src/') is None


def test_stop_word_pattern_does_not_fire() -> None:
    assert _rule_a("grep test src/") is None


def test_non_python_glob_does_not_fire() -> None:
    assert _rule_a("grep --include=*.txt Foo docs/") is None
    assert _rule_a("rg --type=js MyClass .") is None


def test_python_scoped_glob_fires() -> None:
    note = _rule_a("grep --include=*.py Foo src/")
    assert note is not None
    assert "Foo" in note


def test_rg_bare_ident_fires() -> None:
    note = _rule_a("rg -n MyClass src/precis")
    assert note is not None
    assert "MyClass" in note


def test_egrep_bare_ident_fires() -> None:
    note = _rule_a("egrep MyFunc src/precis")
    assert note is not None
    assert "MyFunc" in note


def test_leading_cd_prefix_is_followed() -> None:
    note = _rule_a("cd /some/path && grep MyFunc src/precis")
    assert note is not None
    assert "MyFunc" in note


def test_short_token_does_not_fire() -> None:
    assert _rule_a("grep ab src/") is None


def test_non_grep_command_does_not_fire_rule_a() -> None:
    assert _rule_a("cat src/precis/server.py") is None


def test_unparseable_quoting_stays_silent() -> None:
    # Unbalanced quote -> shlex.split raises -> conservative no-opinion.
    assert _rule_a('grep "Foo src/') is None


def test_piped_grep_with_no_leading_pattern_stays_silent() -> None:
    # First token is a shell break -> we can't confidently name the pattern.
    assert _rule_a("grep -r | wc -l") is None


# ── Rule B: ssh cluster hosts / prod-psql -> cluster-ops ────────────────────


def test_ssh_melchior_fires_rule_b() -> None:
    note = _rule_b("ssh melchior 'journalctl -u precis-worker --since -1h'")
    assert note is not None
    assert "cluster-ops" in note
    assert "melchior" in note


def test_ssh_caspar_fires_rule_b() -> None:
    note = _rule_b("ssh caspar uptime")
    assert note is not None
    assert "caspar" in note


def test_ssh_balthazar_fires_rule_b() -> None:
    note = _rule_b("ssh balthazar 'tail -n 50 /var/log/precis-worker.log'")
    assert note is not None
    assert "balthazar" in note


def test_ssh_other_host_does_not_fire() -> None:
    assert _rule_b("ssh spark uptime") is None


def test_prod_psql_fires_rule_b() -> None:
    note = _rule_b('scripts/prod-psql "SELECT count(*) FROM todo"')
    assert note is not None
    assert "cluster-ops" in note


def test_unrelated_command_does_not_fire_rule_b() -> None:
    assert _rule_b("git status") is None


# ── main() / stdin-JSON wiring ──────────────────────────────────────────────


def _run_main(monkeypatch, payload, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = main()
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


def test_main_fires_on_bare_ident_bash_grep(monkeypatch, capsys) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "grep Foo src/"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    hso = out["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert "Foo" in hso["additionalContext"]


def test_main_fires_on_ssh_cluster_host(monkeypatch, capsys) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "ssh melchior uptime"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    assert "cluster-ops" in out["hookSpecificOutput"]["additionalContext"]


def test_main_silent_on_plain_ls(monkeypatch, capsys) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "ls -la"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


def test_main_silent_on_plain_git_status(monkeypatch, capsys) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "git status"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


def test_main_ignores_non_bash_tool(monkeypatch, capsys) -> None:
    payload = {"tool_name": "Grep", "tool_input": {"command": "grep Foo src/"}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


def test_main_unparseable_stdin_is_silent(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("not json"))
    rc = main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""


def test_main_non_string_command_is_silent(monkeypatch, capsys) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": None}}
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is None


# ── coderef-nudge.py's Grep path is unaffected by the shared-module refactor ─


def test_coderef_nudge_still_fires_on_bare_ident(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"tool_input": {"pattern": "Foo"}}))
    )
    rc = _coderef_mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    parsed = json.loads(out)
    assert "Foo" in parsed["hookSpecificOutput"]["additionalContext"]


def test_coderef_nudge_still_silent_on_stop_word(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(json.dumps({"tool_input": {"pattern": "test"}}))
    )
    rc = _coderef_mod.main()
    out = capsys.readouterr().out
    assert rc == 0
    assert out == ""
