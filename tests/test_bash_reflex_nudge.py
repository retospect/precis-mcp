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
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests._timing import budget

#: Rule C decides "is this cd redundant?" with ``os.path.isabs`` on the host's
#: own semantics. The fixtures below are POSIX worktree paths (what the hook
#: actually sees — it only ever runs on this repo's macOS/Linux dev boxes), and
#: py3.13's ntpath no longer calls a lone leading slash absolute, so on Windows
#: the positive cases can't fire. The negative cases stay meaningful there.
_posix_paths_only = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX worktree paths (ntpath.isabs('/x') is False)"
)

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
_rule_c = _mod._rule_c
main = _mod.main

# Loading the hook ran its ``sys.path.insert`` for the sibling helpers, so
# ``_pykind_hits`` is importable the same way the hook imports it.
_pykind = importlib.import_module("_pykind_hits")

_WT = "/repo/.claude/worktrees/wt1"

_coderef_mod = _load("coderef_nudge", "coderef-nudge.py")


def _run_main(monkeypatch, payload, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    rc = main()
    out = capsys.readouterr().out
    return rc, (json.loads(out) if out.strip() else None)


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


# ── Rule A target check: clearly non-Python search targets stay silent ──────


def test_non_python_path_args_do_not_fire(tmp_path) -> None:
    """gr514328: grepping a symbol through .sql files or a SQL-only dir is a
    text search coderef can't answer — the nudge must not fire."""
    assert _rule_a("grep -rn TIER migrations/0007_x.sql", str(tmp_path)) is None
    assert _rule_a("rg Foo notes.md", str(tmp_path)) is None
    sql_dir = tmp_path / "migrations"
    sql_dir.mkdir()
    (sql_dir / "0001_a.sql").write_text("select 1;\n", encoding="utf-8")
    (sql_dir / "0002_b.sql").write_text("select 2;\n", encoding="utf-8")
    assert _rule_a("grep -rn TIER migrations", str(tmp_path)) is None


def test_python_and_unknown_targets_still_fire(tmp_path) -> None:
    py_dir = tmp_path / "pkg"
    py_dir.mkdir()
    (py_dir / "mod.py").write_text("x = 1\n", encoding="utf-8")
    (py_dir / "mod.sql").write_text("select 1;\n", encoding="utf-8")
    assert _rule_a("grep Foo pkg", str(tmp_path)) is not None
    # a missing/dir-with-subdirs arg can't be classified -> code-ish default
    assert _rule_a("grep Foo gone/", str(tmp_path)) is not None
    assert _rule_a("grep Foo src/", str(tmp_path)) is not None


def test_coderef_nudge_silent_on_sql_path(monkeypatch, capsys, tmp_path) -> None:
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_input": {"pattern": "TIER", "path": str(tmp_path / "x.sql")},
                    "cwd": str(tmp_path),
                }
            )
        ),
    )
    assert _coderef_mod.main() == 0
    assert capsys.readouterr().out == ""


# ── Rule A outside-the-tree silence (gr520146) ──────────────────────────────


def test_absolute_outside_target_stays_silent(tmp_path) -> None:
    """A symbol grep of a path outside the session tree is grep's domain —
    the python kind can't see it, so the nudge must not fire (it did on a
    lock_timeout grep of the CLI's own venv)."""
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "mod.py").write_text("x = 1\n", encoding="utf-8")
    assert _rule_a(f"grep lock_timeout {outside}", str(repo)) is None


def test_venv_and_site_packages_targets_stay_silent(tmp_path) -> None:
    """site-packages/venv under the tree is still grep's domain — the
    index deliberately skips those dirs."""
    repo = tmp_path / "repo"
    sp = repo / "lib" / "site-packages"
    sp.mkdir(parents=True)
    (sp / "mod.py").write_text("x = 1\n", encoding="utf-8")
    assert _rule_a("grep lock_timeout lib/site-packages", str(repo)) is None
    assert _rule_a("rg lock_timeout /opt/mcps/venv", str(repo)) is None


def test_coderef_nudge_silent_on_outside_path(monkeypatch, capsys, tmp_path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_input": {"pattern": "lock_timeout", "path": str(outside)},
                    "cwd": str(repo),
                }
            )
        ),
    )
    assert _coderef_mod.main() == 0
    assert capsys.readouterr().out == ""


# ── Rule F: recursive grep -c -> -l ─────────────────────────────────────────

_rule_f = _mod._rule_f


def test_recursive_grep_count_nudges_to_l(tmp_path) -> None:
    note = _rule_f("grep -rc keepout src/", str(tmp_path))
    assert note is not None
    assert "-l" in note


def test_grep_count_on_dir_target_fires(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    note = _rule_f("grep -c keepout src", str(tmp_path))
    assert note is not None


def test_grep_count_single_file_stays_silent(tmp_path) -> None:
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    assert _rule_f("grep -c keepout a.py", str(tmp_path)) is None


def test_rg_count_stays_silent(tmp_path) -> None:
    """`rg -c` already omits zero-count files — no nudge."""
    (tmp_path / "src").mkdir()
    assert _rule_f("rg -c keepout src", str(tmp_path)) is None


def test_grep_without_count_stays_silent(tmp_path) -> None:
    (tmp_path / "src").mkdir()
    assert _rule_f("grep -rn keepout src/", str(tmp_path)) is None


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


# ── Rule C: redundant `cd <own-worktree>; …` prefix -> run bare ─────────────


@_posix_paths_only
def test_redundant_cd_into_own_worktree_fires_rule_c() -> None:
    note = _rule_c(f"cd {_WT} && git status", _WT)
    assert note is not None
    assert "[cwd]" in note


@_posix_paths_only
def test_redundant_cd_subdir_with_semicolon_and_redirect_fires() -> None:
    note = _rule_c(f"cd {_WT}/src 2>/dev/null; ls", _WT)
    assert note is not None


def test_cross_tree_cd_does_not_fire_rule_c() -> None:
    # Cross-tree cd is the deny-guard's job, not this nudge's.
    assert _rule_c("cd /repo && ls", _WT) is None


def test_git_dash_c_does_not_fire_rule_c() -> None:
    assert _rule_c(f"git -C {_WT} status", _WT) is None


def test_bare_command_does_not_fire_rule_c() -> None:
    assert _rule_c("git status", _WT) is None


def test_rule_c_silent_without_cwd() -> None:
    assert _rule_c(f"cd {_WT} && ls", "") is None


@_posix_paths_only
def test_main_fires_rule_c_on_redundant_cd(monkeypatch, capsys) -> None:
    payload = {
        "tool_name": "Bash",
        "cwd": _WT,
        "tool_input": {"command": f"cd {_WT} && ls"},
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    assert "[cwd]" in out["hookSpecificOutput"]["additionalContext"]


# ── main() / stdin-JSON wiring ──────────────────────────────────────────────


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


# ── Rule E: unquoted glob flag-arg -> zsh no-matches trap ───────────────────

_rule_e = _mod._rule_e


@pytest.mark.parametrize(
    "cmd",
    [
        "grep -rn --include=*.sql TIER src/",
        "grep -rn --include *.sql TIER src/",
        "rg -g *.py Foo src/",
        "rg --glob=*.sql TIER .",
        "find . -name *.tex",
        "find . -iname *.PDF",
        "tar --exclude=*.log -cf a.tar d/",
    ],
)
def test_unquoted_glob_arg_fires_rule_e(cmd: str) -> None:
    note = _rule_e(cmd)
    assert note is not None
    assert "no matches found" in note
    assert "'" in note  # names the quoted fix


@pytest.mark.parametrize(
    "cmd",
    [
        "grep -rn --include='*.sql' TIER src/",
        'grep -rn --include="*.sql" TIER src/',
        "rg -g '*.py' Foo src/",
        "find . -name '*.tex'",
        "grep -rn TIER src/",  # no glob anywhere
        "grep -rn --include=x.sql TIER src/",  # flag arg with no glob chars
        "ls *.md",  # a bare file glob is expansion on purpose
        "git status",
    ],
)
def test_rule_e_silent_shapes(cmd: str) -> None:
    assert _rule_e(cmd) is None


def test_main_emits_rule_e(monkeypatch, capsys) -> None:
    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": "grep -rn --include=*.sql TIER src/"},
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    assert out is not None
    assert "[zsh]" in out["hookSpecificOutput"]["additionalContext"]


# ── Rule D: sed -n slice -> Read ────────────────────────────────────────────

_rule_d = _mod._rule_d


@pytest.mark.parametrize(
    "cmd",
    [
        "sed -n '10,40p' src/precis/paper.py",
        'sed -n "10,40p" src/precis/paper.py',
        "sed -n '10,40 p' src/precis/paper.py",
        "sed -n 10,40p src/precis/paper.py",
        "sed -n '7p' README.md",
    ],
)
def test_sed_slice_fires_rule_d(cmd: str) -> None:
    note = _rule_d(cmd)
    assert note is not None
    assert "Read" in note and "offset=" in note


def test_sed_slice_offset_limit_values() -> None:
    note = _rule_d("sed -n '10,40p' a.py")
    assert note is not None
    assert "offset=10, limit=31" in note


@pytest.mark.parametrize(
    "cmd",
    [
        "git log | sed -n '1,5p'",
        "sed -n '1,5p' a.py | head",
        "sed -n '1,5p' a.py && echo hi",
        "sed -n '1,5p' < a.py",
        "sed -n '1,5p'",
        "sed -n '/foo/p' a.py",
        "sed -n '40,10p' a.py",
        "sed -i 's/a/b/' a.py",
    ],
)
def test_sed_non_slice_silent(cmd: str) -> None:
    assert _rule_d(cmd) is None


def test_main_emits_rule_d(monkeypatch, capsys) -> None:
    payload = {"tool_name": "Bash", "tool_input": {"command": "sed -n '1,9p' x.py"}}
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    assert main() == 0
    out = json.loads(capsys.readouterr().out)
    assert "[read]" in out["hookSpecificOutput"]["additionalContext"]


# ── read-dedup delegation (whole-file Bash serves) ──────────────────────────


def _bash_payload(command: str, session: str = "s-del", tu: str = "tu-1") -> dict:
    return {
        "session_id": session,
        "tool_name": "Bash",
        "tool_use_id": tu,
        "tool_input": {"command": command},
    }


def test_main_delegates_cat_repeat_deny(monkeypatch, capsys, tmp_path) -> None:
    """A repeat unchanged `cat f` is DENIED via read-dedup.hook_output —
    the whole point of the delegation (gr499044's Bash dodge)."""
    assert _mod._READ_DEDUP is not None
    monkeypatch.setenv("PRECIS_READ_DEDUP_DIR", str(tmp_path))
    f = tmp_path / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    rc, out = _run_main(monkeypatch, _bash_payload(f"cat {f}"), capsys)
    assert rc == 0 and out is None  # first serve: allowed and recorded
    rc, out = _run_main(monkeypatch, _bash_payload(f"cat {f}", tu="tu-2"), capsys)
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "unchanged since it was served" in hso["permissionDecisionReason"]


def test_main_big_cat_deny_beats_rules(monkeypatch, capsys, tmp_path) -> None:
    """A first unbounded serve over WARN_BYTES is the redirect deny."""
    monkeypatch.setenv("PRECIS_READ_DEDUP_DIR", str(tmp_path))
    f = tmp_path / "big.md"
    f.write_text("x" * (_mod._READ_DEDUP.WARN_BYTES + 1), encoding="utf-8")
    rc, out = _run_main(monkeypatch, _bash_payload(f"cat {f}", session="s-big"), capsys)
    assert rc == 0
    hso = out["hookSpecificOutput"]
    assert hso["permissionDecision"] == "deny"
    assert "Read" in hso["permissionDecisionReason"]


def test_main_dedup_silent_falls_through_to_rules(
    monkeypatch, capsys, tmp_path
) -> None:
    """A first-time small cat is allowed silently and recorded — then rule
    order still applies (no rule fires here, so the call is silent)."""
    monkeypatch.setenv("PRECIS_READ_DEDUP_DIR", str(tmp_path))
    f = tmp_path / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    rc, out = _run_main(monkeypatch, _bash_payload(f"cat {f}", session="s-ft"), capsys)
    assert rc == 0 and out is None


# ── Rule A hits: the hook runs the bounded lookup itself (gr520146 c4) ────


def _recorder(calls: list):
    """A ``pykind_hits`` double that records invocations and returns no hits."""

    def fake(*a, **kw):
        calls.append(a)
        return []

    return fake


def _fake_popen(stdout: str = "", returncode: int = 0, hang: bool = False, record=None):
    """A ``subprocess.Popen`` double: canned stdout/rc (or a communicate
    that always times out), recording spawn argv/kwargs for assertions."""

    class _P:
        def __init__(self, argv, **kw):
            if record is not None:
                record.append({"argv": argv, "kw": kw})
            # an impossible pid — a getpgid/killpg aimed at it must fail,
            # never land on pytest's own process group.
            self.pid = 0x5EAD000
            self.returncode = returncode
            self.stdout = None
            self.killed = False

        def communicate(self, timeout=None):
            if hang:
                raise subprocess.TimeoutExpired("coderef", timeout)
            return stdout, ""

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.killed = True

    return _P


def test_rule_a_appends_top_hits(monkeypatch, tmp_path) -> None:
    (tmp_path / "src").mkdir()  # a missing target is not src-scoped
    canned = [
        "precis.store::Store.get  src/precis/store.py:12",
        "precis.store::Store.get_many  src/precis/store.py:40",
    ]
    monkeypatch.setattr(_mod, "pykind_hits", lambda tok, cwd, **kw: canned)
    note = _rule_a("grep Store src/", str(tmp_path))
    assert note is not None
    assert "Top hits" in note
    for h in canned:
        assert h in note


def test_main_appends_hits(monkeypatch, capsys, tmp_path) -> None:
    (tmp_path / "src").mkdir()  # a missing target is not src-scoped
    monkeypatch.setattr(
        _mod, "pykind_hits", lambda tok, cwd, **kw: ["m::f  src/m.py:1"]
    )
    payload = {
        "tool_name": "Bash",
        "cwd": str(tmp_path),
        "tool_input": {"command": "grep Foo src/"},
    }
    rc, out = _run_main(monkeypatch, payload, capsys)
    assert rc == 0
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert "Top hits" in ctx
    assert "m::f  src/m.py:1" in ctx


def test_rule_a_no_lookup_on_non_src_target(monkeypatch, tmp_path) -> None:
    """A pythonish target OUTSIDE src/ (``tests/``) still gets the nudge but
    no lookup — the ruling's trigger is a src-scoped grep."""
    calls: list = []
    monkeypatch.setattr(_mod, "pykind_hits", _recorder(calls))
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    note = _rule_a("grep Foo tests", str(tmp_path))
    assert note is not None
    assert "Top hits" not in note
    assert calls == []


def test_rule_a_no_lookup_on_literal_grep(monkeypatch, tmp_path) -> None:
    calls: list = []
    monkeypatch.setattr(_mod, "pykind_hits", _recorder(calls))
    assert _rule_a('grep "some text" src/', str(tmp_path)) is None
    assert calls == []


def test_rule_a_falls_back_to_plain_nudge(monkeypatch, tmp_path) -> None:
    """Empty/timeout/error in the lookup leaves today's nudge text."""
    (tmp_path / "src").mkdir()  # src-scoped: the lookup runs and returns []
    monkeypatch.setattr(_mod, "pykind_hits", lambda tok, cwd, **kw: [])
    note = _rule_a("grep Store src/", str(tmp_path))
    assert note is not None
    assert "search(kind='python'" in note
    assert "Top hits" not in note


def test_pykind_hits_timeout_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(_pykind.subprocess, "Popen", _fake_popen(hang=True))
    monkeypatch.setattr(_pykind, "_coderef", lambda cwd: tmp_path / "coderef")
    assert _pykind.pykind_hits("Foo", str(tmp_path)) == []


def test_pykind_hits_kills_within_budget(monkeypatch, tmp_path) -> None:
    """A subprocess that overruns is killed at the timeout — the hook never
    waits past the budget."""
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    monkeypatch.setattr(_pykind, "_coderef", lambda cwd: sleeper)
    monkeypatch.setattr(_pykind, "TIMEOUT_S", 0.2)
    t0 = time.monotonic()
    assert _pykind.pykind_hits("Foo", str(tmp_path)) == []
    assert time.monotonic() - t0 < budget(5)


def test_pykind_hits_bad_rc_returns_empty(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(
        _pykind.subprocess, "Popen", _fake_popen(stdout="boom\n", returncode=2)
    )
    monkeypatch.setattr(_pykind, "_coderef", lambda cwd: tmp_path / "coderef")
    assert _pykind.pykind_hits("Foo", str(tmp_path)) == []


def test_pykind_hits_filters_and_caps(monkeypatch, tmp_path) -> None:
    fake = _fake_popen(
        stdout=(
            "mod.a::f  src/a.py:1\n"
            "tok: no lexical hits\n"
            "not a hit line\n"
            "mod.b::g  src/b.py:2\n"
        )
    )
    monkeypatch.setattr(_pykind.subprocess, "Popen", fake)
    monkeypatch.setattr(_pykind, "_coderef", lambda cwd: tmp_path / "coderef")
    assert _pykind.pykind_hits("Foo", str(tmp_path)) == [
        "mod.a::f  src/a.py:1",
        "mod.b::g  src/b.py:2",
    ]


def test_pykind_hits_budget_is_bounded() -> None:
    assert _pykind.TIMEOUT_S <= 1.0


def test_pykind_hits_invokes_python_directly(monkeypatch, tmp_path) -> None:
    """The child is ``sys.executable <script>`` — never ``uv run``: the
    script's project-mode shebang could sync the venv (a network reach)
    inside a per-grep hook. coderef is stdlib-only, so this interpreter
    runs it."""
    record: list = []
    monkeypatch.setattr(
        _pykind.subprocess,
        "Popen",
        _fake_popen(stdout="m::f  src/m.py:1\n", record=record),
    )
    monkeypatch.setattr(_pykind, "_coderef", lambda cwd: tmp_path / "coderef")
    assert _pykind.pykind_hits("Foo", str(tmp_path)) == ["m::f  src/m.py:1"]
    assert len(record) == 1
    argv = record[0]["argv"]
    assert argv[0] == sys.executable
    assert "uv" not in Path(argv[0]).name
    assert argv[1:] == [
        str(tmp_path / "coderef"),
        "find",
        "Foo",
        "--limit",
        "3",
    ]
    # its own session -> the timeout path can killpg the git-grep child too
    assert record[0]["kw"]["start_new_session"] == (os.name == "posix")


def test_pykind_hits_kills_the_process_group(monkeypatch, tmp_path) -> None:
    """On timeout the whole process group dies — a plain ``proc.kill()``
    would orphan the ``git grep`` grandchild coderef spawned."""
    if os.name != "posix":
        pytest.skip("process-group kill is POSIX-only")
    killed: list = []
    monkeypatch.setattr(_pykind.os, "getpgid", lambda pid: 4242)
    monkeypatch.setattr(
        _pykind.os, "killpg", lambda pgid, sig: killed.append((pgid, sig))
    )
    monkeypatch.setattr(_pykind.subprocess, "Popen", _fake_popen(hang=True))
    monkeypatch.setattr(_pykind, "_coderef", lambda cwd: tmp_path / "coderef")
    assert _pykind.pykind_hits("Foo", str(tmp_path)) == []
    assert killed == [(4242, signal.SIGKILL)]


def test_src_scoped_missing_target_is_no_lookup(tmp_path) -> None:
    """A missing target can't be classified — NOT src-scoped, no lookup
    (guessing would index the wrong tree)."""
    assert _pykind.src_scoped(["gone/"], str(tmp_path)) is False
    src = tmp_path / "src"
    src.mkdir()
    assert _pykind.src_scoped(["src"], str(tmp_path)) is True
    assert _pykind.src_scoped(["src/notthere.py"], str(tmp_path)) is False
    assert _pykind.src_scoped([], str(tmp_path)) is True


def test_src_scoped_resolves_the_tree_root(tmp_path) -> None:
    """cwd a tree subdir: the src/ check resolves against the tree root,
    so ``grep Foo .`` from inside src/ stays src-scoped."""
    (tmp_path / ".git").mkdir()
    inner = tmp_path / "src" / "precis"
    inner.mkdir(parents=True)
    assert _pykind.src_scoped(["."], str(inner)) is True
    outside = tmp_path / "docs"
    outside.mkdir()
    assert _pykind.src_scoped(["."], str(outside)) is False


def test_coderef_resolves_the_payload_tree(tmp_path, tmp_path_factory) -> None:
    """cwd inside a git tree -> that tree's scripts/coderef; a git tree
    without one -> no lookup; no git tree -> the hook's own copy."""
    (tmp_path / ".git").mkdir()
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    coderef = scripts / "coderef"
    coderef.write_text("# py\n", encoding="utf-8")
    sub = tmp_path / "src" / "pkg"
    sub.mkdir(parents=True)
    assert _pykind._coderef(str(sub)) == coderef
    coderef.unlink()
    assert _pykind._coderef(str(sub)) is None
    plain = tmp_path_factory.mktemp("nogit")
    fallback = _pykind._coderef(str(plain))
    assert _pykind.__file__ is not None
    assert fallback == Path(_pykind.__file__).resolve().parent.parent / "coderef"


# ── coderef-nudge.py attaches the same hits on a src-scoped Grep ──────────


def test_coderef_nudge_appends_hits_on_src_path(monkeypatch, capsys, tmp_path) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "x.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(
        _coderef_mod, "pykind_hits", lambda tok, cwd, **kw: ["m::f  src/m.py:1"]
    )
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_input": {"pattern": "Foo", "path": "src"},
                    "cwd": str(tmp_path),
                }
            )
        ),
    )
    assert _coderef_mod.main() == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "m::f  src/m.py:1" in ctx


def test_coderef_nudge_no_lookup_outside_src(monkeypatch, capsys, tmp_path) -> None:
    tdir = tmp_path / "tests"
    tdir.mkdir()
    (tdir / "test_x.py").write_text("x = 1\n", encoding="utf-8")
    calls: list = []
    monkeypatch.setattr(_coderef_mod, "pykind_hits", _recorder(calls))
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            json.dumps(
                {
                    "tool_input": {"pattern": "Foo", "path": "tests"},
                    "cwd": str(tmp_path),
                }
            )
        ),
    )
    assert _coderef_mod.main() == 0
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    assert "Top hits" not in ctx
    assert calls == []
