"""scripts/hooks/guard-prod-psql.py — the permission hook keyed off the SQL.

`--ro` is the form the agent remits prescribe for prod reads (2026-10-02,
after a session-level SET poisoned pgbouncer); the hook must auto-approve a
lone `--ro` call, or every agent read prompts. Shell chaining still gets no
opinion, so `--ro` cannot smuggle a second command past the prompt.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

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


def test_env_prefixed_call_is_never_allowed() -> None:
    cmd = 'PRECIS_PROD_PSQL_OPTS="-At" scripts/prod-psql "SELECT 1"'
    result = _hook().evaluate(cmd)
    assert result is None or result["decision"] != "allow"


# --- gr462727: only destructive writes ask ---------------------------------


def _decision(sql: str) -> str | None:
    result = _hook().evaluate(f'scripts/prod-psql "{sql}"')
    return None if result is None else result["decision"]


def test_non_destructive_writes_are_allowed() -> None:
    for sql in (
        "INSERT INTO refs (slug) VALUES ('x')",
        "UPDATE refs SET title = 'x' WHERE id = 1",
        "INSERT INTO t SELECT * FROM u ON CONFLICT (id) DO UPDATE SET a = 1",
        "WITH u AS (UPDATE refs SET a = 1 WHERE id = 2 RETURNING id) SELECT * FROM u",
        "UPDATE t SET a = (SELECT max(b) FROM u WHERE u.id = 1) WHERE id = 3",
        "CREATE INDEX CONCURRENTLY i ON t (a)",
        "SELECT nextval('s')",
        "INSERT INTO a VALUES (1); UPDATE b SET x = 1 WHERE y = 2",
    ):
        assert _decision(sql) == "allow", sql
    result = _hook().evaluate('scripts/prod-psql "INSERT INTO t VALUES (1)"')
    assert result is not None and "non-destructive prod write" in result["reason"]


def test_destructive_statements_ask() -> None:
    for sql in (
        "DELETE FROM refs WHERE id = 1",  # DELETE asks even with WHERE
        "DELETE FROM refs",
        "DROP TABLE refs",
        "TRUNCATE refs",
        "ALTER TABLE refs DROP COLUMN x",
        "ALTER TABLE refs ALTER COLUMN x TYPE text",
        "ALTER TABLE refs RENAME TO r2",
        "ALTER TABLE refs ADD COLUMN x int",
        "UPDATE refs SET title = 'x'",
        "UPDATE t SET a = (SELECT max(b) FROM u WHERE u.id = 1)",
        "INSERT INTO a VALUES (1); UPDATE b SET x = 1",  # most dangerous wins
        "SELECT 1; DELETE FROM refs WHERE id = 1",
        "WITH d AS (DELETE FROM refs WHERE id = 1 RETURNING *) SELECT * FROM d",
        "WITH u AS (UPDATE t SET a = 1) SELECT * FROM z WHERE z.id = 1",
        "DO 'BEGIN END'",
        "SELECT 'unterminated",
    ):
        assert _decision(sql) == "ask", sql


def test_keyword_tricks_do_not_misfire() -> None:
    for sql in (
        "SELECT 'DELETE FROM refs' AS note",
        "SELECT 1 -- DELETE FROM refs",
        "SELECT /* DROP TABLE x */ 1",
        "SELECT 'it''s DELETE'",
    ):
        assert _decision(sql) == "allow", sql
    # A comment or literal must not hide a WHERE-less UPDATE either.
    assert _decision("UPDATE t SET a = 1 -- WHERE id = 1") == "ask"
    assert _decision("UPDATE t SET a = 'WHERE x'") == "ask"


def test_dollar_quoted_body_asks_when_single_quoted() -> None:
    # Inside double quotes `$$` is a shell expansion (no lone-call opinion);
    # single-quoted it reaches psql, and its body could hide a DELETE.
    result = _hook().evaluate("scripts/prod-psql 'DO $$ BEGIN DELETE FROM t; END $$'")
    assert result is not None and result["decision"] == "ask"


def test_quoted_identifier_named_drop_is_not_destructive() -> None:
    cmd = 'scripts/prod-psql \'SELECT 1 AS "drop", "delete" FROM t\''
    result = _hook().evaluate(cmd)
    assert result is not None and result["decision"] == "allow"


def test_compound_write_is_never_auto_allowed() -> None:
    evaluate = _hook().evaluate
    for cmd in (
        'cd x && scripts/prod-psql "INSERT INTO t VALUES (1)"',
        'scripts/prod-psql "INSERT INTO t VALUES (1)" && rm -rf x',
        "echo 'INSERT INTO t VALUES (1)' | scripts/prod-psql",
        'scripts/prod-psql "INSERT INTO t VALUES ($X)"',
        'FOO=1 scripts/prod-psql "INSERT INTO t VALUES (1)"',
    ):
        result = evaluate(cmd)
        assert result is None or result["decision"] != "allow", cmd
    # ...but a destructive one in a compound command still asks.
    assert evaluate('cd x && scripts/prod-psql "DELETE FROM t"')["decision"] == "ask"


def test_allow_prod_write_env_silences_ask(monkeypatch, capsys) -> None:
    import io
    import json

    payload = {
        "tool_name": "Bash",
        "tool_input": {"command": 'scripts/prod-psql "DELETE FROM t"'},
    }
    monkeypatch.setenv("ALLOW_PROD_WRITE", "1")
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    assert _hook().main() == 0
    assert capsys.readouterr().out == ""


def _wrap_ro(sql: str) -> str:
    """Run scripts/prod-psql's wrap_ro() in bash — the script's only seam."""
    import re
    import subprocess

    script = Path(__file__).resolve().parents[1] / "scripts" / "prod-psql"
    fn = re.search(
        r"^wrap_ro\(\) \{\n.*?^\}\n", script.read_text(encoding="utf-8"), re.S | re.M
    )
    assert fn is not None
    out = subprocess.run(
        ["bash", "-c", f'RO=1\n{fn.group(0)}\nwrap_ro "$1"', "bash", sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    return out.stdout


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: runs bash")
def test_ro_wrapper_terminates_the_user_statement() -> None:
    assert _wrap_ro("SELECT 1") == "BEGIN READ ONLY;\nSELECT 1\n;\nCOMMIT;\n"
    # already terminated, trailing whitespace included: left alone
    assert _wrap_ro("SELECT 1;") == "BEGIN READ ONLY;\nSELECT 1;\nCOMMIT;\n"
    assert _wrap_ro("SELECT 1;  ") == "BEGIN READ ONLY;\nSELECT 1;  \nCOMMIT;\n"
    # a trailing line comment must not swallow the terminator
    assert _wrap_ro("SELECT 1 -- c") == "BEGIN READ ONLY;\nSELECT 1 -- c\n;\nCOMMIT;\n"


# --- gr462727 security review: bypass regressions ---------------------------


def _verdict(cmd: str) -> str | None:
    result = _hook().evaluate(cmd)
    return None if result is None else result["decision"]


def test_ro_must_be_its_own_word() -> None:
    cmd = 'scripts/prod-psql --ro"\nDELETE FROM refs"'
    assert _verdict(cmd) == "ask"


def test_backslash_meta_commands_ask_even_with_ro() -> None:
    for sql in (
        "SELECT 1\n\\! rm -rf ~",
        "SELECT 1 \\gexec",
        "\\gexec",
        "\\i /tmp/x.sql",
        "\\o /tmp/x",
        "\\set x 1",
        "SELECT 1 \\g",
    ):
        assert _verdict(f"scripts/prod-psql '{sql}'") == "ask", sql
        assert _verdict(f"scripts/prod-psql --ro '{sql}'") == "ask", sql
    for sql in ("\\d refs", "\\dt+", "\\l", "\\x"):
        assert _verdict(f"scripts/prod-psql '{sql}'") == "allow", sql
        assert _verdict(f"scripts/prod-psql --ro '{sql}'") == "allow", sql


def test_env_prefix_blocks_every_allow() -> None:
    for cmd in (
        'PRECIS_PROD_PSQL_OPTS=-cDELETE/**/FROM/**/t scripts/prod-psql "SELECT 1"',
        'PRECIS_PROD_SSH_HOST=evil scripts/prod-psql "SELECT 1"',
        'PRECIS_PROD_SSH_HOST=evil scripts/prod-psql --ro "SELECT 1"',
        'FOO=1 scripts/prod-psql "INSERT INTO t VALUES (1)"',
    ):
        assert _verdict(cmd) != "allow", cmd


def test_side_effect_functions_and_row_locks_ask() -> None:
    for sql in (
        "SELECT dblink_exec('x', 'y')",
        "SELECT lo_unlink(1)",
        "SELECT set_config('a', 'b', false)",
        "SELECT pg_reload_conf()",
        "SELECT pg_rotate_logfile()",
        "SELECT pg_switch_wal()",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_create_restore_point('x')",
        "SELECT pg_drop_replication_slot('x')",
        "SELECT pg_replication_origin_create('x')",
        "SELECT pg_promote()",
        "SELECT pg_read_file('/etc/passwd')",
        "SELECT pg_read_binary_file('/etc/passwd')",
        "SELECT pg_ls_dir('/')",
        "SELECT 1 FROM t FOR UPDATE",
        "SELECT 1 FROM t FOR SHARE",
        "SELECT 1 FROM t FOR NO KEY UPDATE",
        "SELECT 1 FROM t FOR KEY SHARE",
    ):
        assert _decision(sql) == "ask", sql
    for sql in (
        "SELECT pg_stat_get_backend_pid(1)",
        "SELECT pg_size_pretty(pg_total_relation_size('refs'))",
        "SELECT pg_get_viewdef('v')",
    ):
        assert _decision(sql) == "allow", sql


def test_non_additive_ddl_asks() -> None:
    for sql in (
        "ALTER SYSTEM SET work_mem = 1",
        "ALTER TABLE t DETACH PARTITION p",
        "ALTER TABLE t SET SCHEMA s",
        "ALTER TABLE t DISABLE TRIGGER ALL",
        "ALTER TABLE t ADD CONSTRAINT c CHECK (a > 0)",
        "CREATE OR REPLACE VIEW v AS SELECT 1",
        "VACUUM FULL t",
        "VACUUM (FULL) t",
        "REINDEX TABLE t",
        "CLUSTER t USING i",
        "SET session_replication_role = replica",
        "SET LOCAL session_replication_role = replica",
        "SET ROLE other",
        "SET SESSION AUTHORIZATION other",
    ):
        assert _decision(sql) == "ask", sql
    assert _decision("VACUUM t") == "allow"


def test_cr_vt_ff_in_the_command_asks() -> None:
    for cmd in (
        'scripts/prod-psql --ro\r"DELETE FROM refs"',
        'scripts/prod-psql --ro\x0b"\nDELETE FROM refs"',
        'scripts/prod-psql --ro\x0c"\rDROP TABLE refs"',
    ):
        assert _verdict(cmd) == "ask", repr(cmd)
