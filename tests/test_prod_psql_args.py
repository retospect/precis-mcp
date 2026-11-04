"""prod-psql's -c alias shares the positional SQL guards (gr560170).

Reuse the file-input harness: ssh and psql are local fakes, with no database
or network access. Unknown options must fail before either fake runs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_prod_psql_file import _c_values, _psql_user, _run


def test_command_alias_preserves_sql(tmp_path: Path) -> None:
    sql = "SELECT 'quoted text', '$HOME', '\\x';\nSELECT 2;"
    positional, _, positional_args = _run(tmp_path, sql)
    command, _, command_args = _run(tmp_path, "-c", sql)
    assert positional.returncode == command.returncode == 0
    assert command_args == positional_args
    assert _c_values(command_args) == [sql]
    assert _psql_user(command_args) == "agent_rw"


def test_sql_opening_with_a_comment_is_not_an_option(tmp_path: Path) -> None:
    sql = "-- how many refs\nSELECT count(*) FROM refs;"
    result, _, args = _run(tmp_path, sql)
    assert result.returncode == 0
    assert _c_values(args) == [sql]


@pytest.mark.parametrize("flag", ["--ro", "--explain"])
@pytest.mark.parametrize("flag_first", [True, False])
def test_command_alias_preserves_read_only_role_and_wrapper(
    tmp_path: Path, flag: str, flag_first: bool
) -> None:
    sql = "SELECT * FROM refs;"
    positional, _, positional_args = _run(tmp_path, flag, sql)
    argv = [flag, "-c", sql] if flag_first else ["-c", sql, flag]
    command, _, command_args = _run(tmp_path, *argv)
    assert positional.returncode == command.returncode == 0
    assert command_args == positional_args
    assert _psql_user(command_args) == "agent_ro"
    assert _c_values(command_args)[0] == "BEGIN READ ONLY;"
    assert _c_values(command_args)[-1] == "COMMIT;"


@pytest.mark.parametrize(
    ("flags", "sql"),
    [
        ([], "SET work_mem = '99MB';"),
        ([], "\\d refs"),
        ([], "COMMIT; DELETE FROM refs WHERE ref_id = 1;"),
        (["--ro"], "COMMIT; SELECT 1;"),
        (["--ro"], "SELECT vault.reveal('x');"),
        (["--explain"], "INSERT INTO refs (ref_id) VALUES (1);"),
    ],
)
def test_command_alias_keeps_sql_refusals(
    tmp_path: Path, flags: list[str], sql: str
) -> None:
    positional, positional_ssh, _ = _run(tmp_path, *flags, sql)
    command, command_ssh, _ = _run(tmp_path, *flags, "-c", sql)
    assert positional.returncode == command.returncode == 2
    assert command.stderr == positional.stderr
    assert positional_ssh is command_ssh is None


def test_command_alias_write_keeps_row_cap(tmp_path: Path) -> None:
    sql = "UPDATE refs SET title = 'x' WHERE ref_id = 1;"
    command, _, command_args = _run(tmp_path, "-c", sql)
    assert command.returncode == 0, command.stderr
    values = _c_values(command_args)
    assert len(values) == 3
    assert values[0].startswith("BEGIN;")
    assert "set_config('precis.prod_psql_base'" in values[0]
    assert values[1] == sql
    assert "prod_psql_verdict" in values[-1]
    assert "rows > 100" in values[-1]
    assert _psql_user(command_args) == "agent_rw"


def test_command_alias_read_only_write_still_fails(tmp_path: Path) -> None:
    command, _, command_args = _run(
        tmp_path, "--ro", "-c", "DELETE FROM refs WHERE ref_id = 1;"
    )
    assert command.returncode == 3
    assert "read-only" in command.stderr
    assert _psql_user(command_args) == "agent_ro"


@pytest.mark.parametrize(
    "argv",
    [
        ["--unknown", "SELECT 1;"],
        ["--ro", "-U", "agent_rw", "SELECT 1;"],
        ["-c", "SELECT 1;", "--unknown"],
        ["-c"],
        ["--ro", "-c"],
        ["-c", ""],
        ["-c", "SELECT 1;", "-c", "SELECT 2;"],
        ["-c", "SELECT 1;", "SELECT 2;"],
        ["-c", "SELECT 1;", "-f", "query.sql"],
        ["-f", "query.sql", "-c", "SELECT 1;"],
    ],
)
def test_bad_arguments_show_usage_before_connecting(
    tmp_path: Path, argv: list[str]
) -> None:
    proc, ssh_args, psql_args = _run(tmp_path, *argv)
    assert proc.returncode == 2
    assert "usage: scripts/prod-psql" in proc.stderr
    assert ssh_args is psql_args is None
