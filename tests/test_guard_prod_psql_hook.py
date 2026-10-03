"""scripts/hooks/guard-prod-psql.py — the permission hook keyed off the SQL.

`--ro` is the form the agent remits prescribe for prod reads (2026-10-02,
after a session-level SET poisoned pgbouncer); the hook must auto-approve a
lone `--ro` call, or every agent read prompts. Shell chaining still gets no
opinion, so `--ro` cannot smuggle a second command past the prompt.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

_HOOK = Path(__file__).resolve().parents[1] / "scripts" / "hooks" / "guard-prod-psql.py"


def _hook() -> ModuleType:
    spec = importlib.util.spec_from_file_location("guard_prod_psql", _HOOK)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_lone_ro_call_is_allowed_whatever_the_sql() -> None:
    evaluate = _hook().evaluate
    assert evaluate('scripts/prod-psql --ro "SELECT 1"')["decision"] == "allow"
    # agent_ro + BEGIN READ ONLY refuses writes server-side, so even a write
    # keyword is safe to let through to that refusal.
    assert evaluate("scripts/prod-psql --ro 'DELETE FROM refs'")["decision"] == "allow"


def test_ro_with_shell_chaining_gets_no_allow() -> None:
    result = _hook().evaluate('scripts/prod-psql --ro "SELECT 1" && rm -rf x')
    assert result is None or result["decision"] != "allow"


def test_plain_select_still_allowed_and_plain_write_still_asks() -> None:
    evaluate = _hook().evaluate
    assert evaluate('scripts/prod-psql "SELECT 1"')["decision"] == "allow"
    assert evaluate('scripts/prod-psql "DELETE FROM refs"')["decision"] == "ask"


def test_heredoc_body_mentioning_prod_psql_and_delete_is_not_judged() -> None:
    """A design note appended via heredoc that quotes the script and a write
    keyword is data, not a prod write (the false positive that held the
    ewod-pcb window for 1.5 h on 2026-10-02)."""
    cmd = (
        "cat >> notes.md <<'EOF'\n"
        'Run scripts/prod-psql "DELETE FROM refs" never.\n'
        "EOF"
    )
    assert _hook().evaluate(cmd) is None


def test_name_as_an_argument_is_not_an_invocation() -> None:
    evaluate = _hook().evaluate
    assert evaluate('grep -n "prod-psql" .claude/agents/coder.md') is None
    assert evaluate('git commit -m "prod-psql refuses DELETE via SET"') is None


def test_write_keyword_outside_the_sql_argument_does_not_ask() -> None:
    """Keywords count only in what prod-psql receives."""
    cmd = 'scripts/prod-psql "SELECT 1" > out.txt; echo DELETE done'
    result = _hook().evaluate(cmd)
    assert result is None or result["decision"] != "ask"


def test_piped_write_into_prod_psql_asks() -> None:
    result = _hook().evaluate("echo 'DELETE FROM refs;' | scripts/prod-psql")
    assert result is not None and result["decision"] == "ask"


def test_write_in_a_compound_invocation_asks() -> None:
    cmd = 'cd x && scripts/prod-psql "UPDATE refs SET title = 1"'
    result = _hook().evaluate(cmd)
    assert result is not None and result["decision"] == "ask"


def test_env_prefixed_select_is_allowed() -> None:
    cmd = 'PRECIS_PROD_PSQL_OPTS="-At" scripts/prod-psql "SELECT 1"'
    assert _hook().evaluate(cmd)["decision"] == "allow"
