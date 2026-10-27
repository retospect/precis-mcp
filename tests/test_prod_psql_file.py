"""``scripts/prod-psql -f FILE`` (gr517627): a local SQL file runs through the
same ``-c`` path as piped SQL — the file is read HERE, because a local path
means nothing after the ssh hop, and sent to the remote psql as one ``-c``
argument (``-c "$(cat)"`` over ssh's stdin; gr521223). ``--ro`` and
``--explain`` must wrap the file's text exactly as they wrap stdin, and a
missing/unreadable file exits 2 before anything connects.

``ssh`` is faked on PATH: it records its argv, then runs the remote command
string through ``sh -c`` so the ``$(cat)`` substitution happens for real; the
``psql`` it resolves records its own argv (the -c values) and emulates the
server's read-only enforcement — a write fails when the call connected as
``agent_ro`` (the role has no write grants, so it refuses even after a
``COMMIT`` inside the input ended the ``BEGIN READ ONLY`` transaction) or
inside ``BEGIN READ ONLY`` itself. No network, no prod coordinates
(``PGB_HOST`` is a placeholder).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: bash")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prod-psql"

_FAKE_PSQL = r"""
import json, os, re, sys

argv = sys.argv[1:]
with open(os.environ["FAKE_ARGS"], "w", encoding="utf-8") as fh:
    json.dump(argv, fh)

agent_ro = "-U" in argv and argv[argv.index("-U") + 1] == "agent_ro"
in_tx = in_ro_tx = False
for i, a in enumerate(argv):
    if a != "-c":
        continue
    body = argv[i + 1]
    if "prod_psql_verdict" in body:
        # The verdict is built at runtime as 'prod-psql ' || '<nonce>' ||
        # ': <verdict>' — extract the nonce literal; the full grep target
        # never sits contiguously in the query text.
        nonce = re.search(r"'prod-psql ' \|\| '([^']+)'", body).group(1)
        if in_tx:
            print(f"prod-psql {nonce}: COMMIT, 0 rows, within the cap of 100")
            print("COMMIT")
        else:
            print(
                f"prod-psql {nonce}: UNCAPPED, the SQL ended the "
                "transaction itself, its writes are already committed"
            )
        in_tx = in_ro_tx = False
        continue
    for stmt in body.split(";"):
        s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", stmt, flags=re.S).strip().upper()
        if not s:
            continue
        if s.startswith("BEGIN"):
            in_tx = True
            in_ro_tx = "READ ONLY" in s
            print("BEGIN")
        elif s.startswith(("COMMIT", "ROLLBACK", "END", "ABORT")):
            in_tx = in_ro_tx = False
            print(s.split()[0])
        elif m := re.match(
            r"(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b", s
        ):
            if agent_ro or in_ro_tx:
                sys.stderr.write(
                    "ERROR:  cannot execute "
                    + m.group(1)
                    + " in a read-only transaction\n"
                )
                sys.exit(3)
            print(m.group(1) + " 0 1")
        else:
            print("OK")
"""

_FAKE_SSH = r"""
import json, os, sys

with open(os.environ["FAKE_SSH_ARGS"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
os.execvp("sh", ["sh", "-c", sys.argv[-1]])
"""


def _run(
    tmp_path: Path, *argv: str
) -> tuple[subprocess.CompletedProcess[str], list[str] | None, list[str] | None]:
    """Run prod-psql with the fake ssh/psql first on PATH. Returns (proc, the
    argv ssh saw — last element is the remote command string — and the argv
    the remote psql saw)."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "fake_psql.py").write_text(_FAKE_PSQL, encoding="utf-8")
    (fake / "fake_ssh.py").write_text(_FAKE_SSH, encoding="utf-8")
    for name, target in (("ssh", "fake_ssh.py"), ("psql", "fake_psql.py")):
        (fake / name).write_text(
            f'#!/bin/sh\nexec "{sys.executable}" "{fake / target}" "$@"\n',
            encoding="utf-8",
        )
        (fake / name).chmod(0o755)
    ssh_args, psql_args = tmp_path / "ssh.argv", tmp_path / "psql.argv"
    # A run that never reaches ssh must not read a stale marker an earlier
    # _run in the same tmp_path left behind.
    ssh_args.unlink(missing_ok=True)
    psql_args.unlink(missing_ok=True)
    proc = subprocess.run(
        ["bash", str(_SCRIPT), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        env={
            "PATH": f"{fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PGB_HOST": "fake-host",
            "FAKE_SSH_ARGS": str(ssh_args),
            "FAKE_ARGS": str(psql_args),
        },
        check=False,
    )
    ssh_argv = (
        json.loads(ssh_args.read_text(encoding="utf-8")) if ssh_args.exists() else None
    )
    psql_argv = (
        json.loads(psql_args.read_text(encoding="utf-8"))
        if psql_args.exists()
        else None
    )
    return proc, ssh_argv, psql_argv


def _c_values(psql_argv: list[str] | None) -> list[str]:
    """The -c argument values the remote psql received, in order."""
    assert psql_argv is not None, "the remote psql never ran"
    return [psql_argv[i + 1] for i, a in enumerate(psql_argv) if a == "-c"]


def _psql_user(psql_argv: list[str] | None) -> str:
    """The -U role the remote psql was invoked as."""
    assert psql_argv is not None, "the remote psql never ran"
    return psql_argv[psql_argv.index("-U") + 1]


def test_file_is_read_locally_and_sent_as_one_c(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT 41 + 1;\n", encoding="utf-8")
    proc, ssh_argv, psql_argv = _run(tmp_path, "-f", str(q))
    assert proc.returncode == 0, proc.stderr
    assert _c_values(psql_argv) == ["SELECT 41 + 1;"]
    # The remote command never sees the local path — only psql's -c arg does.
    assert ssh_argv is not None
    assert "q.sql" not in ssh_argv[-1]
    assert _psql_user(psql_argv) == "agent_rw"


def test_file_flag_long_forms(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT 7;\n", encoding="utf-8")
    for argv in (["--file", str(q)], [f"--file={q}"]):
        proc, _, psql_argv = _run(tmp_path, *argv)
        assert proc.returncode == 0, proc.stderr
        assert "SELECT 7;" in _c_values(psql_argv)


def test_ro_wraps_the_file_in_begin_read_only(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT * FROM refs;\n", encoding="utf-8")
    for argv in (["--ro", "-f", str(q)], ["-f", str(q), "--ro"]):
        proc, _, psql_argv = _run(tmp_path, *argv)
        assert proc.returncode == 0, proc.stderr
        c_args = _c_values(psql_argv)
        assert c_args[0] == "BEGIN READ ONLY;" and c_args[-1] == "COMMIT;", argv
        assert "SELECT * FROM refs;" in c_args
        assert _psql_user(psql_argv) == "agent_ro"


def test_ro_refuses_a_write_from_a_file(tmp_path: Path) -> None:
    """The --ro guarantee for file input: same -c path, so a write in the
    file hits agent_ro inside BEGIN READ ONLY and fails — as a pipe does.
    A DELETE (not UPDATE … SET): under --ro a SET keyword is refused at the
    boundary, so a write that reaches agent_ro must not contain one."""
    q = tmp_path / "write.sql"
    q.write_text("DELETE FROM refs WHERE ref_id = 1;\n", encoding="utf-8")
    proc, _, psql_argv = _run(tmp_path, "--ro", "-f", str(q))
    assert proc.returncode != 0
    assert "read-only" in proc.stderr
    assert _c_values(psql_argv)[0] == "BEGIN READ ONLY;"
    assert _psql_user(psql_argv) == "agent_ro"


def test_ro_refuses_a_commit_inside_the_file(tmp_path: Path) -> None:
    """A ``COMMIT;`` in the file would end the wrapper's BEGIN READ ONLY
    transaction early, so the write after it ran outside the guard — under
    --ro, transaction-control statements are refused at the boundary (exit
    2) before ssh, on file content exactly as on inline SQL."""
    q = tmp_path / "sneaky.sql"
    q.write_text(
        "SELECT 1;\nCOMMIT;\nUPDATE refs SET title = 'x' WHERE ref_id = 1;\n",
        encoding="utf-8",
    )
    proc, ssh_argv, _ = _run(tmp_path, "--ro", "-f", str(q))
    assert proc.returncode == 2
    assert "refused" in proc.stderr
    assert ssh_argv is None


def test_explain_rewrites_a_files_query(tmp_path: Path) -> None:
    q = tmp_path / "claim.sql"
    q.write_text("SELECT * FROM refs FOR UPDATE SKIP LOCKED;\n", encoding="utf-8")
    proc, _, psql_argv = _run(tmp_path, "--explain", "-f", str(q))
    assert proc.returncode == 0, proc.stderr
    c_args = _c_values(psql_argv)
    assert c_args[0] == "BEGIN READ ONLY;" and c_args[-1] == "COMMIT;"
    explain = next(a for a in c_args if a.startswith("EXPLAIN"))
    assert "EXPLAIN SELECT * FROM refs" in explain
    assert "FOR UPDATE" not in explain and "SKIP LOCKED" not in explain
    assert _psql_user(psql_argv) == "agent_ro"


def test_explain_refuses_a_data_modifying_file(tmp_path: Path) -> None:
    # An INSERT — under --ro (which --explain implies) a SET keyword is
    # refused at the boundary before --explain's own check runs, so the
    # data-modifying refusal needs a write without one.
    q = tmp_path / "write.sql"
    q.write_text("INSERT INTO refs (ref_id) VALUES (1);\n", encoding="utf-8")
    proc, ssh_argv, _ = _run(tmp_path, "--explain", "-f", str(q))
    assert proc.returncode == 2
    assert "cannot EXPLAIN a data-modifying" in proc.stderr
    assert ssh_argv is None, "refused SQL must not reach ssh"


def test_file_write_without_ro_takes_the_row_cap_path(tmp_path: Path) -> None:
    q = tmp_path / "write.sql"
    q.write_text("UPDATE refs SET title = 'x' WHERE ref_id < 10;\n", encoding="utf-8")
    proc, _, psql_argv = _run(tmp_path, "-f", str(q))
    assert proc.returncode == 0, proc.stderr
    c_args = _c_values(psql_argv)
    assert c_args[0].startswith("BEGIN;")
    assert "set_config('precis.prod_psql_base'" in c_args[0]
    assert "pg_stat_xact_user_tables" in c_args[0]
    assert "prod_psql_verdict" in c_args[-1]


def test_missing_file_exits_2_without_connecting(tmp_path: Path) -> None:
    proc, ssh_argv, _ = _run(tmp_path, "-f", str(tmp_path / "nope.sql"))
    assert proc.returncode == 2
    assert "cannot read SQL file" in proc.stderr
    assert ssh_argv is None, "a missing file must not reach ssh"


def test_unreadable_file_exits_2_without_connecting(tmp_path: Path) -> None:
    # A directory is not a readable FILE, and unlike chmod 000 it fails `-f`
    # even when the suite runs as root.
    proc, ssh_argv, _ = _run(tmp_path, "--file", str(tmp_path))
    assert proc.returncode == 2
    assert "cannot read SQL file" in proc.stderr
    assert ssh_argv is None


def test_file_and_inline_sql_is_refused(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT 1;\n", encoding="utf-8")
    proc, ssh_argv, _ = _run(tmp_path, "-f", str(q), "SELECT 2")
    assert proc.returncode == 2
    assert "do not pass inline SQL" in proc.stderr
    assert ssh_argv is None


def test_file_flag_without_a_path_exits_2(tmp_path: Path) -> None:
    for argv in (["-f"], ["--ro", "-f"], ["--file="]):
        proc, ssh_argv, _ = _run(tmp_path, *argv)
        assert proc.returncode == 2, argv
        assert re.search(r"needs a (FILE )?path", proc.stderr), argv
        assert ssh_argv is None


def test_file_with_a_meta_command_first_line_is_refused(tmp_path: Path) -> None:
    """-f goes through the same refusal: a leading backslash exits 2."""
    q = tmp_path / "meta.sql"
    q.write_text("\\d refs\n", encoding="utf-8")
    proc, ssh_argv, _ = _run(tmp_path, "-f", str(q))
    assert proc.returncode == 2
    assert "refused for safety" in proc.stderr
    assert ssh_argv is None
