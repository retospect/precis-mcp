"""``scripts/prod-psql -f FILE`` (gr517627): a local SQL file runs through the
same stdin path as piped SQL — the file is read HERE, because a local path
means nothing after the ssh hop. ``--ro`` and ``--explain`` must wrap the
file's text exactly as they wrap stdin, and a missing/unreadable file exits 2
before anything connects.

``ssh`` is faked on PATH: it records its argv and execs a tiny psql stand-in
that records the stdin psql would receive and emulates the server's read-only
enforcement — a write fails when the call connected as ``agent_ro`` (the role
has no write grants, so it refuses even after a ``COMMIT`` inside the input
ended the ``BEGIN READ ONLY`` transaction) or inside ``BEGIN READ ONLY``
itself. No network, no prod coordinates (``PGB_HOST`` is a placeholder).
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

# Runs as `ssh -o IdentityAgent=none <host> "<remote psql command>"`: argv is
# logged, stdin is the wrapped SQL psql would see, and a write under a
# read-only session answers the way the server would.
_FAKE_PSQL = r"""
import os, re, sys

with open(os.environ["FAKE_ARGS"], "w", encoding="utf-8") as fh:
    fh.write("\n".join(sys.argv[1:]))
text = sys.stdin.read()
with open(os.environ["FAKE_LOG"], "w", encoding="utf-8") as fh:
    fh.write(text)

agent_ro = " -U agent_ro " in " " + " ".join(sys.argv[1:]) + " "
in_ro_tx = False
for stmt in text.split(";"):
    s = re.sub(r"--[^\n]*|/\*.*?\*/", " ", stmt, flags=re.S).strip().upper()
    if not s:
        continue
    if s.startswith("BEGIN"):
        in_ro_tx = "READ ONLY" in s
        print("BEGIN")
    elif s.startswith(("COMMIT", "ROLLBACK", "END", "ABORT")):
        in_ro_tx = False
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


def _run(
    tmp_path: Path, *argv: str
) -> tuple[subprocess.CompletedProcess[str], str, str]:
    """Run prod-psql with the fake ssh first on PATH. Returns (proc, the text
    the remote psql received on stdin, the argv ssh saw)."""
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "fake_psql.py").write_text(_FAKE_PSQL, encoding="utf-8")
    ssh = fake / "ssh"
    ssh.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{fake / "fake_psql.py"}" "$@"\n',
        encoding="utf-8",
    )
    ssh.chmod(0o755)
    log, args_log = tmp_path / "stdin.sql", tmp_path / "ssh.argv"
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
            "FAKE_LOG": str(log),
            "FAKE_ARGS": str(args_log),
        },
        check=False,
    )
    sent = log.read_text(encoding="utf-8") if log.exists() else ""
    sargs = args_log.read_text(encoding="utf-8") if args_log.exists() else ""
    return proc, sent, sargs


def test_file_is_read_locally_and_sent_over_stdin(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT 41 + 1;\n", encoding="utf-8")
    proc, sent, sargs = _run(tmp_path, "-f", str(q))
    assert proc.returncode == 0, proc.stderr
    assert sent == "SELECT 41 + 1;\n"
    # The remote command never sees the local path — only psql's stdin does.
    assert "q.sql" not in sargs
    assert "-U agent_rw" in sargs


def test_file_flag_long_forms(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT 7;\n", encoding="utf-8")
    for argv in (["--file", str(q)], [f"--file={q}"]):
        proc, sent, _ = _run(tmp_path, *argv)
        assert proc.returncode == 0, proc.stderr
        assert "SELECT 7;" in sent


def test_ro_wraps_the_file_in_begin_read_only(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT * FROM refs;\n", encoding="utf-8")
    for argv in (["--ro", "-f", str(q)], ["-f", str(q), "--ro"]):
        proc, sent, sargs = _run(tmp_path, *argv)
        assert proc.returncode == 0, proc.stderr
        assert sent.startswith("BEGIN READ ONLY;\n"), argv
        assert "SELECT * FROM refs;" in sent
        assert sent.endswith("COMMIT;\n")
        assert "-U agent_ro" in sargs


def test_ro_refuses_a_write_from_a_file(tmp_path: Path) -> None:
    """The --ro guarantee for file input: same stdin path, so a write in the
    file hits agent_ro inside BEGIN READ ONLY and fails — as a pipe does."""
    q = tmp_path / "write.sql"
    q.write_text("UPDATE refs SET title = 'x' WHERE ref_id = 1;\n", encoding="utf-8")
    proc, sent, sargs = _run(tmp_path, "--ro", "-f", str(q))
    assert proc.returncode != 0
    assert "read-only" in proc.stderr
    assert sent.startswith("BEGIN READ ONLY;")
    assert "-U agent_ro" in sargs


def test_ro_survives_a_commit_inside_the_file(tmp_path: Path) -> None:
    """A ``COMMIT;`` in the file ends the wrapper's transaction early; the
    agent_ro role still refuses the write after it (grants, not the tx)."""
    q = tmp_path / "sneaky.sql"
    q.write_text(
        "SELECT 1;\nCOMMIT;\nUPDATE refs SET title = 'x' WHERE ref_id = 1;\n",
        encoding="utf-8",
    )
    proc, sent, sargs = _run(tmp_path, "--ro", "-f", str(q))
    assert proc.returncode != 0
    assert "read-only" in proc.stderr
    assert "-U agent_ro" in sargs


def test_explain_rewrites_a_files_query(tmp_path: Path) -> None:
    q = tmp_path / "claim.sql"
    q.write_text("SELECT * FROM refs FOR UPDATE SKIP LOCKED;\n", encoding="utf-8")
    proc, sent, sargs = _run(tmp_path, "--explain", "-f", str(q))
    assert proc.returncode == 0, proc.stderr
    assert sent.startswith("BEGIN READ ONLY;\n")
    assert "EXPLAIN SELECT * FROM refs" in sent
    assert "FOR UPDATE" not in sent and "SKIP LOCKED" not in sent
    assert "-U agent_ro" in sargs


def test_explain_refuses_a_data_modifying_file(tmp_path: Path) -> None:
    q = tmp_path / "write.sql"
    q.write_text("UPDATE refs SET title = 'x' WHERE ref_id = 1;\n", encoding="utf-8")
    proc, sent, _ = _run(tmp_path, "--explain", "-f", str(q))
    assert proc.returncode == 2
    assert "cannot EXPLAIN a data-modifying" in proc.stderr
    assert sent == "", "refused SQL must not reach ssh"


def test_file_write_without_ro_takes_the_row_cap_path(tmp_path: Path) -> None:
    q = tmp_path / "write.sql"
    q.write_text("UPDATE refs SET title = 'x' WHERE ref_id < 10;\n", encoding="utf-8")
    proc, sent, _ = _run(tmp_path, "-f", str(q))
    assert proc.returncode == 0, proc.stderr
    assert sent.startswith("BEGIN;\nSET LOCAL precis.prod_psql_cap")
    assert "pg_stat_xact_user_tables" in sent


def test_missing_file_exits_2_without_connecting(tmp_path: Path) -> None:
    proc, sent, _ = _run(tmp_path, "-f", str(tmp_path / "nope.sql"))
    assert proc.returncode == 2
    assert "cannot read SQL file" in proc.stderr
    assert sent == "", "a missing file must not reach ssh"


def test_unreadable_file_exits_2_without_connecting(tmp_path: Path) -> None:
    # A directory is not a readable FILE, and unlike chmod 000 it fails `-f`
    # even when the suite runs as root.
    proc, sent, _ = _run(tmp_path, "--file", str(tmp_path))
    assert proc.returncode == 2
    assert "cannot read SQL file" in proc.stderr
    assert sent == ""


def test_file_and_inline_sql_is_refused(tmp_path: Path) -> None:
    q = tmp_path / "q.sql"
    q.write_text("SELECT 1;\n", encoding="utf-8")
    proc, sent, _ = _run(tmp_path, "-f", str(q), "SELECT 2")
    assert proc.returncode == 2
    assert "do not pass inline SQL" in proc.stderr
    assert sent == ""


def test_file_flag_without_a_path_exits_2(tmp_path: Path) -> None:
    for argv in (["-f"], ["--ro", "-f"], ["--file="]):
        proc, sent, _ = _run(tmp_path, *argv)
        assert proc.returncode == 2, argv
        assert re.search(r"needs a (FILE )?path", proc.stderr), argv
        assert sent == ""
