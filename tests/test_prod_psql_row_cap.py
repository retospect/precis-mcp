"""scripts/prod-psql's row cap: a non --ro UPDATE/DELETE runs in BEGIN and
rolls back when the transaction changed more than PRECIS_PROD_WRITE_MAX_ROWS
rows (.claude/fleet/authority.md, 2026-10-15).

``ssh`` is faked on PATH by a tiny psql stand-in: it reads the wrapped SQL
from stdin, gives every UPDATE/DELETE ``FAKE_ROWS`` rows, and runs the
wrapper's ``\\gset`` / ``\\if`` / ``\\echo`` lines. No network, no prod
coordinates (``PGB_HOST`` is a placeholder). The wrapper itself was checked
against a real local Postgres 17 on 2026-10-15, including the ``;;`` case
that resets psql's ROW_COUNT to 0 and is why the count comes from
``pg_stat_xact_user_tables``.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: bash")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prod-psql"

_FAKE_PSQL = r"""
import os, re, sys

text = sys.stdin.read()
open(os.environ["FAKE_LOG"], "w", encoding="utf-8").write(text)
rows = int(os.environ.get("FAKE_ROWS", "0"))
changed, in_tx, marker = 0, False, False
var: dict[str, str] = {}
stack: list[bool] = []
buf = ""


def live() -> bool:
    return all(stack)


def run(stmt: str) -> None:
    global changed, in_tx, marker
    s = stmt.strip().upper()
    if not s:
        return
    if s.startswith("BEGIN"):
        in_tx = True
        print("BEGIN")
    elif s.startswith("SET LOCAL PRECIS.PROD_PSQL_CAP"):
        marker = in_tx
        print("SET")
    elif s.startswith(("COMMIT", "ROLLBACK")):
        in_tx = marker = False
        print(s.split()[0])
    elif re.match(r"(WITH\b.*\b)?(UPDATE|DELETE)\b", s, re.S):
        changed += rows
        print(f"{s.split()[0]} {rows}")


for line in text.split("\n"):
    if line.startswith("\\"):
        cmd, _, arg = line[1:].partition(" ")
        if cmd == "if":
            stack.append(live() and var.get(arg.strip()[1:]) == "t")
        elif cmd == "else":
            stack[-1] = not stack[-1] and all(stack[:-1])
        elif cmd == "endif":
            stack.pop()
        elif cmd == "echo" and live():
            print(re.sub(r":(\w+)", lambda m: var[m.group(1)], arg))
        continue
    if not live():
        continue
    if line.endswith("\\gset"):
        cap = int(re.search(r"n > (\d+)", line).group(1))
        var.update(
            prod_psql_rows=str(changed),
            prod_psql_over="t" if changed > cap else "f",
            prod_psql_in_tx="t" if marker else "f",
        )
        continue
    buf += line + "\n"
    while ";" in buf:
        stmt, buf = buf.split(";", 1)
        run(stmt)
"""


def _run(
    tmp_path: Path, sql: str, rows: int, **env: str
) -> tuple[subprocess.CompletedProcess[str], str]:
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "fake_psql.py").write_text(_FAKE_PSQL, encoding="utf-8")
    ssh = fake / "ssh"
    ssh.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{fake / "fake_psql.py"}"\n',
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    log = tmp_path / "stdin.sql"
    proc = subprocess.run(
        ["bash", str(_SCRIPT), sql],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        env={
            "PATH": f"{fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PGB_HOST": "fake-host",
            "FAKE_ROWS": str(rows),
            "FAKE_LOG": str(log),
            **env,
        },
        check=False,
    )
    return proc, log.read_text(encoding="utf-8") if log.exists() else ""


def test_update_over_the_cap_rolls_back(tmp_path: Path) -> None:
    proc, sent = _run(tmp_path, "UPDATE refs SET title = 'x' WHERE id < 500", 101)
    assert proc.returncode == 4, proc.stderr
    assert "prod-psql: ROLLBACK, 101 rows is over the cap of 100" in proc.stdout
    assert re.search(r"^ROLLBACK$", proc.stdout, re.M)
    assert not re.search(r"^COMMIT$", proc.stdout, re.M)
    assert "rolled back" in proc.stderr
    assert sent.startswith("BEGIN;\n") and "pg_stat_xact_user_tables" in sent


def test_update_under_the_cap_commits(tmp_path: Path) -> None:
    proc, _ = _run(tmp_path, "UPDATE refs SET title = 'x' WHERE id IN (1, 2, 3)", 3)
    assert proc.returncode == 0, proc.stderr
    assert "prod-psql: COMMIT, 3 rows, within the cap of 100" in proc.stdout
    assert re.search(r"^COMMIT$", proc.stdout, re.M)
    assert not re.search(r"^ROLLBACK$", proc.stdout, re.M)


def test_delete_and_the_cap_override(tmp_path: Path) -> None:
    proc, _ = _run(
        tmp_path,
        "DELETE FROM refs WHERE id < 200",
        150,
        PRECIS_PROD_WRITE_MAX_ROWS="200",
    )
    assert proc.returncode == 0, proc.stderr
    assert "prod-psql: COMMIT, 150 rows, within the cap of 200" in proc.stdout
    proc, _ = _run(tmp_path, "DELETE FROM refs WHERE id < 200", 150)
    assert proc.returncode == 4


def test_empty_trailing_statement_does_not_reset_the_count(tmp_path: Path) -> None:
    """`;;` and `; -- note` make psql send an empty query (ROW_COUNT 0); the
    transaction tally the wrapper reads is unaffected."""
    for sql in (
        "UPDATE refs SET a = 1 WHERE id < 999;;",
        "UPDATE refs SET a = 1 WHERE id < 999; -- note",
    ):
        proc, _ = _run(tmp_path, sql, 150)
        assert proc.returncode == 4, sql


def test_reads_and_inserts_are_not_wrapped(tmp_path: Path) -> None:
    for sql in ("SELECT 1", "INSERT INTO t VALUES (1)"):
        proc, sent = _run(tmp_path, sql, 0)
        assert proc.returncode == 0, proc.stderr
        assert "BEGIN" not in sent, sql
    _, sent = _run(tmp_path, "UPDATE t SET a = 1 WHERE id = 1", 1)
    assert "BEGIN" in sent


def test_ro_is_never_wrapped(tmp_path: Path) -> None:
    fake = tmp_path / "bin"
    _run(tmp_path, "SELECT 1", 0)  # builds the fake
    log = tmp_path / "stdin.sql"
    subprocess.run(
        ["bash", str(_SCRIPT), "--ro", "UPDATE t SET a = 1 WHERE id = 1"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        stdin=subprocess.DEVNULL,
        env={
            "PATH": f"{fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PGB_HOST": "fake-host",
            "FAKE_LOG": str(log),
        },
        check=False,
    )
    assert log.read_text(encoding="utf-8").startswith("BEGIN READ ONLY;")


def test_own_transaction_control_is_refused(tmp_path: Path) -> None:
    for sql in (
        "UPDATE t SET a = 1 WHERE id = 1; COMMIT",
        "BEGIN; DELETE FROM t WHERE id = 1",
    ):
        proc, sent = _run(tmp_path, sql, 1)
        assert proc.returncode == 2, sql
        assert sent == "", "refused SQL must not reach psql"


def test_commit_slipping_past_the_refusal_is_reported(tmp_path: Path) -> None:
    """A `--` inside a literal blinds the pre-check's comment strip; the
    server-side marker then reports the write as uncapped (exit 5)."""
    proc, _ = _run(tmp_path, "UPDATE t SET a = '--' WHERE id = 1; COMMIT", 1)
    assert proc.returncode == 5, proc.stdout
    assert "prod-psql: UNCAPPED" in proc.stdout


def test_bad_cap_value_is_refused(tmp_path: Path) -> None:
    proc, sent = _run(
        tmp_path, "UPDATE t SET a = 1 WHERE id = 1", 1, PRECIS_PROD_WRITE_MAX_ROWS="1e9"
    )
    assert proc.returncode == 2 and sent == ""
