"""scripts/prod-psql's row cap: a non --ro UPDATE/DELETE runs in BEGIN and
rolls back when the transaction changed more than PRECIS_PROD_WRITE_MAX_ROWS
rows (.claude/fleet/authority.md, 2026-10-15).

The wrapper is fixed ``-c`` arguments around the caller's ``-c`` (gr521223):
``BEGIN; SELECT set_config('precis.prod_psql_base', …, true)`` before (the
baseline is a transaction-LOCAL setting — a TEMP TABLE would outlive COMMIT
on a pgbouncer-pooled backend), then the verdict SELECT and a runtime
division-by-zero guard that skips the ``COMMIT`` in its own -c when the
count is over the cap — the disconnect rolls the transaction back. The old
``\\gset``/``\\if`` shape needed psql's meta lexer, which caller text must
never reach. The verdict text carries a per-run nonce so a returned row
cannot fake it.

``ssh`` is faked on PATH: it records its argv and runs the remote command
string through ``sh -c``; the ``psql`` it resolves walks the -c arguments in
order — tracking the transaction, the transaction-local baseline setting
and the caller's writes (``FAKE_ROWS`` each), then answers the verdict -c
the way the server would. No network, no prod coordinates (``PGB_HOST`` is a
placeholder). The cap maths itself was checked against a real local
Postgres 17 on 2026-10-15 (the ``;;`` case that resets psql's ROW_COUNT to
0 is why the count comes from ``pg_stat_xact_user_tables``).
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

rows = int(os.environ.get("FAKE_ROWS", "0"))
# Counts a pooled server connection has not flushed from earlier clients'
# transactions: pg_stat_xact_user_tables shows them inside ours too.
pending = int(os.environ.get("FAKE_PENDING", "0"))
changed = 0
in_tx = False
base_live = False
baseline = 0
agent_ro = "-U" in argv and argv[argv.index("-U") + 1] == "agent_ro"

for i, a in enumerate(argv):
    if a != "-c":
        continue
    body = argv[i + 1]
    if "prod_psql_verdict" in body:
        # The verdict + guard + COMMIT tail -c. base_live models
        # current_setting('precis.prod_psql_base', true) returning the
        # transaction-local value — gone when the caller's SQL already
        # ended the wrapper's transaction.
        # The verdict is built at runtime as 'prod-psql ' || '<nonce>' ||
        # ': <verdict>' — extract the nonce literal; the full grep target
        # never sits contiguously in the query text.
        nonce = re.search(r"'prod-psql ' \|\| '([^']+)'", body).group(1)
        delta = pending + changed - baseline
        cap = int(re.search(r"rows > (\d+)", body).group(1))
        if not base_live:
            print(
                f"prod-psql {nonce}: UNCAPPED, the SQL ended the "
                "transaction itself, its writes are already committed"
            )
        elif delta > cap:
            print(
                f"prod-psql {nonce}: ROLLBACK, {delta} rows is over the cap "
                f"of {cap} (PRECIS_PROD_WRITE_MAX_ROWS), nothing changed"
            )
            sys.stderr.write("ERROR:  division by zero\n")
            sys.exit(3)
        else:
            print(
                f"prod-psql {nonce}: COMMIT, {delta} rows, "
                f"within the cap of {cap}"
            )
            in_tx = base_live = False
            print("COMMIT")
        continue
    for stmt in body.split(";"):
        s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", stmt, flags=re.S).strip().upper()
        if not s:
            continue
        if s.startswith("BEGIN"):
            in_tx = True
            print("BEGIN")
        elif "SET_CONFIG('PRECIS.PROD_PSQL_BASE'" in s:
            # set_config(..., is_local => true): lives until the tx ends.
            baseline = pending + changed
            base_live = in_tx
            print("set_config")
        elif s.startswith(("COMMIT", "ROLLBACK", "END", "ABORT")):
            in_tx = base_live = False
            print(s.split()[0])
        elif re.match(r"(WITH\b.*\b)?(UPDATE|DELETE)\b", s, re.S):
            changed += rows
            print(f"{s.split()[0]} {rows}")
        elif re.match(r"(INSERT|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b", s):
            if agent_ro:
                sys.stderr.write(
                    "ERROR:  cannot execute "
                    + s.split()[0]
                    + " in a read-only transaction\n"
                )
                sys.exit(3)
            print(s.split()[0] + " 0 1")
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
    tmp_path: Path, sql: str, rows: int, **env: str
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
            "FAKE_SSH_ARGS": str(ssh_args),
            "FAKE_ARGS": str(psql_args),
            **env,
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


def test_update_over_the_cap_rolls_back(tmp_path: Path) -> None:
    proc, _, psql_argv = _run(
        tmp_path, "UPDATE refs SET title = 'x' WHERE id < 500", 101
    )
    assert proc.returncode == 4, proc.stderr
    assert re.search(
        r"prod-psql \S+: ROLLBACK, 101 rows is over the cap of 100", proc.stdout
    )
    assert (
        re.search(r"^ROLLBACK$", proc.stdout, re.M) or "division by zero" in proc.stderr
    )
    assert not re.search(r"^COMMIT$", proc.stdout, re.M)
    assert "rolled back" in proc.stderr
    c_args = _c_values(psql_argv)
    assert c_args[0].startswith("BEGIN;")
    assert "pg_stat_xact_user_tables" in c_args[0]


def test_update_under_the_cap_commits(tmp_path: Path) -> None:
    proc, _, _ = _run(tmp_path, "UPDATE refs SET title = 'x' WHERE id IN (1, 2, 3)", 3)
    assert proc.returncode == 0, proc.stderr
    assert re.search(
        r"prod-psql \S+: COMMIT, 3 rows, within the cap of 100", proc.stdout
    )
    assert re.search(r"^COMMIT$", proc.stdout, re.M)
    assert not re.search(r"^ROLLBACK$", proc.stdout, re.M)


def test_delete_and_the_cap_override(tmp_path: Path) -> None:
    proc, _, _ = _run(
        tmp_path,
        "DELETE FROM refs WHERE id < 200",
        150,
        PRECIS_PROD_WRITE_MAX_ROWS="200",
    )
    assert proc.returncode == 0, proc.stderr
    assert re.search(
        r"prod-psql \S+: COMMIT, 150 rows, within the cap of 200", proc.stdout
    )
    proc, _, _ = _run(tmp_path, "DELETE FROM refs WHERE id < 200", 150)
    assert proc.returncode == 4


def test_empty_trailing_statement_does_not_reset_the_count(tmp_path: Path) -> None:
    """`;;` and `; -- note` make psql send an empty query (ROW_COUNT 0); the
    transaction tally the wrapper reads is unaffected."""
    for sql in (
        "UPDATE refs SET a = 1 WHERE id < 999;;",
        "UPDATE refs SET a = 1 WHERE id < 999; -- note",
    ):
        proc, _, _ = _run(tmp_path, sql, 150)
        assert proc.returncode == 4, sql


def test_unflushed_counts_from_earlier_transactions_do_not_count(
    tmp_path: Path,
) -> None:
    """Measured on prod via pgbouncer 2026-10-15: a 1-row UPDATE read 4 on
    the raw tally. The cap is on the difference from the post-BEGIN
    baseline, so pending counts neither inflate nor hide the write."""
    proc, _, psql_argv = _run(
        tmp_path, "UPDATE t SET a = 1 WHERE id = 1", 1, FAKE_PENDING="500"
    )
    assert proc.returncode == 0, proc.stdout
    assert re.search(
        r"prod-psql \S+: COMMIT, 1 rows, within the cap of 100", proc.stdout
    )
    c_args = _c_values(psql_argv)
    base_at = next(i for i, a in enumerate(c_args) if "prod_psql_base" in a)
    user_at = next(i for i, a in enumerate(c_args) if "UPDATE t SET" in a)
    assert base_at < user_at
    proc, _, _ = _run(
        tmp_path, "UPDATE t SET a = 1 WHERE id < 999", 101, FAKE_PENDING="500"
    )
    assert proc.returncode == 4


def test_reads_and_inserts_are_not_wrapped(tmp_path: Path) -> None:
    for sql in ("SELECT 1", "INSERT INTO t VALUES (1)"):
        proc, _, psql_argv = _run(tmp_path, sql, 0)
        assert proc.returncode == 0, proc.stderr
        c_args = _c_values(psql_argv)
        assert not any(a.startswith("BEGIN") for a in c_args), sql
    _, _, psql_argv = _run(tmp_path, "UPDATE t SET a = 1 WHERE id = 1", 1)
    assert _c_values(psql_argv)[0].startswith("BEGIN;")


def test_ro_is_never_wrapped(tmp_path: Path) -> None:
    _run(tmp_path, "SELECT 1", 0)  # builds the fake
    fake = tmp_path / "bin"
    ssh_args, psql_args = tmp_path / "ssh.argv", tmp_path / "psql.argv"
    subprocess.run(
        # A DELETE, not UPDATE … SET: under --ro a SET keyword anywhere in
        # code is refused at the boundary before ssh (round-4).
        ["bash", str(_SCRIPT), "--ro", "DELETE FROM t WHERE id = 1"],
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
    c_args = _c_values(json.loads(psql_args.read_text(encoding="utf-8")))
    assert c_args[0] == "BEGIN READ ONLY;"


def test_own_transaction_control_is_refused(tmp_path: Path) -> None:
    for sql in (
        "UPDATE t SET a = 1 WHERE id = 1; COMMIT",
        "BEGIN; DELETE FROM t WHERE id = 1",
    ):
        proc, ssh_argv, _ = _run(tmp_path, sql, 1)
        assert proc.returncode == 2, sql
        assert ssh_argv is None, "refused SQL must not reach psql"


def test_commit_after_a_dashdash_literal_is_refused(tmp_path: Path) -> None:
    """A `--` inside a literal used to blind the pre-check's comment strip;
    the one-pass scan strips literals and comments together, so this COMMIT
    is refused before ssh (exit 2) instead of reported uncapped (exit 5)."""
    proc, ssh_argv, _ = _run(tmp_path, "UPDATE t SET a = '--' WHERE id = 1; COMMIT", 1)
    assert proc.returncode == 2, proc.stdout
    assert ssh_argv is None


def test_bad_cap_value_is_refused(tmp_path: Path) -> None:
    proc, ssh_argv, _ = _run(
        tmp_path, "UPDATE t SET a = 1 WHERE id = 1", 1, PRECIS_PROD_WRITE_MAX_ROWS="1e9"
    )
    assert proc.returncode == 2 and ssh_argv is None
