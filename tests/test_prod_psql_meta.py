"""``scripts/prod-psql`` never lets caller SQL reach psql's meta-command
lexer (gr521223 rework). The text travels as ONE ``-c`` argument — the
remote command carries ``-c "$(cat)"`` and the remote shell fills it from
ssh's stdin — and psql lexes a -c as a meta-command ONLY when its first
byte is a backslash, so caller SQL whose first non-space character is a
backslash is refused (exit 2) before any ssh. Mid-text ``\\c``/``\\!`` are
server-side syntax errors, not commands; ``:``-variables do not
interpolate inside a -c.

``ssh`` is faked on PATH: it records its argv, then runs the remote
command string through ``sh -c`` — what the hop's sshd does — so the
``$(cat)`` substitution is exercised for real. The ``psql`` the remote
command resolves records the argv psql would see (the -c values, fully
expanded) and emulates the agent_ro role and BEGIN READ ONLY. ``ssh_argv
is None`` below proves ssh was never invoked. No network, no prod
coordinates (``PGB_HOST`` is a placeholder).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only: bash")

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "prod-psql"

#: Runs as the remote command's `psql`: records argv as JSON, then walks the
#: -c values — each is a server string (or the caller's, via "$(cat)") —
#: emulating BEGIN/COMMIT state and agent_ro's write refusal.
_FAKE_PSQL = r"""
import json, os, re, sys

argv = sys.argv[1:]
with open(os.environ["FAKE_ARGS"], "w", encoding="utf-8") as fh:
    json.dump(argv, fh)

# A REUSED pooled backend: TEMP objects persist across runs and COMMITs —
# pgbouncer's transaction pooling runs no server reset between clients, so
# a prod_psql_base left behind would collide with the next capped run.
state = os.environ["FAKE_BACKEND"]
tables = set()
if os.path.exists(state):
    with open(state, encoding="utf-8") as fh:
        tables = set(json.load(fh))

agent_ro = "-U" in argv and argv[argv.index("-U") + 1] == "agent_ro"
in_tx = in_ro_tx = base_live = False
for i, a in enumerate(argv):
    if a != "-c":
        continue
    body = argv[i + 1]
    # EMIT(text) inside a -c emulates a result row carrying caller-chosen
    # text — used to check output rows cannot fake the wrapper's verdict.
    for m in re.finditer(r"EMIT\(([^)]*)\)", body):
        print(m.group(1))
    if "prod_psql_verdict" in body:
        # The verdict is built at runtime as 'prod-psql ' || '<nonce>' ||
        # ': <verdict>' — extract the nonce literal; the full grep target
        # never sits contiguously in the query text.
        nonce = re.search(r"'prod-psql ' \|\| '([^']+)'", body).group(1)
        # FAKE_DROP_VERDICT emulates a nominally clean run whose verdict
        # row never printed — the fail-closed exit-6 path.
        if not os.environ.get("FAKE_DROP_VERDICT"):
            if base_live:
                print(
                    f"prod-psql {nonce}: COMMIT, 0 rows, within the cap of 100"
                )
                print("COMMIT")
            else:
                print(
                    f"prod-psql {nonce}: UNCAPPED, the SQL ended the "
                    "transaction itself, its writes are already committed"
                )
        in_tx = in_ro_tx = base_live = False
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
            # A transaction-local setting dies with its transaction.
            in_tx = in_ro_tx = base_live = False
            print(s.split()[0])
        elif "SET_CONFIG('PRECIS.PROD_PSQL_BASE'" in s:
            # set_config(..., is_local => true) survives only inside a tx.
            base_live = in_tx
            print("set_config")
        elif s.startswith("CREATE TEMP TABLE"):
            name = s.split()[3].lower().rstrip("(")
            if name in tables:
                sys.stderr.write(f'ERROR:  relation "{name}" already exists\n')
                sys.exit(1)
            tables.add(name)
            with open(state, "w", encoding="utf-8") as fh:
                json.dump(sorted(tables), fh)
            print("SELECT 1")
        elif re.match(
            r"(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b", s
        ):
            if agent_ro or in_ro_tx:
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

#: Runs as `ssh`: records argv as JSON, then execs the remote command string
#: through a local `sh -c` — the same quoting/`$(cat)` evaluation the hop's
#: sshd does. stdin flows through to the remote command.
_FAKE_SSH = r"""
import json, os, sys

with open(os.environ["FAKE_SSH_ARGS"], "w", encoding="utf-8") as fh:
    json.dump(sys.argv[1:], fh)
os.execvp("sh", ["sh", "-c", sys.argv[-1]])
"""


def _run(
    tmp_path: Path, *argv: str, stdin_text: str | None = None, **env: str
) -> tuple[subprocess.CompletedProcess[str], list[str] | None, list[str] | None]:
    """Run prod-psql with the fake ssh/psql first on PATH. Returns (proc, the
    argv ssh saw — last element is the remote command string — and the argv
    the remote psql saw). Both are None when ssh never ran."""
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
    pipe: dict[str, Any] = (
        {"input": stdin_text}
        if stdin_text is not None
        else {"stdin": subprocess.DEVNULL}
    )
    proc = subprocess.run(
        ["bash", str(_SCRIPT), *argv],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={
            "PATH": f"{fake}:{os.environ['PATH']}",
            "HOME": str(tmp_path),
            "PGB_HOST": "fake-host",
            "FAKE_SSH_ARGS": str(ssh_args),
            "FAKE_ARGS": str(psql_args),
            "FAKE_BACKEND": str(tmp_path / "backend.state"),
            **env,
        },
        check=False,
        **pipe,
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


#: Meta-command shapes that must never run: each is refused because the SQL
#: begins with a backslash, so the -c slot can never be one.
_BLOCKED = [
    "\\c - agent_rw",
    "\\connect - agent_rw",
    "\\! ls",
    "\\o /tmp/prod-psql-meta-out",
    "\\set FOO bar",
    "\\i /tmp/x.sql",
    "\\ir x.sql",
    "\\copy refs to program 'cat'",
    "\\gexec",
    "\\password",
    "\\q",
    # Display commands are refused too — the -c slot can never be a
    # meta-command; they belong to the interactive shell.
    "\\d refs",
    "\\d+ refs",
    "\\dt+",
    "\\x on",
    "\\pset format wrapped",
    "\\echo done",
    "\\gset",
    "\\if :'x'",
    "\\elif :'y'",
    "\\else",
    "\\endif",
    "\\timing on",
    "\\conninfo",
    # Review must-fix 1: backticks in an allowlisted command's argument run a
    # shell on the ssh host — unreachable now.
    "\\echo `id`",
    "\\if `uname -a`",
    "\\pset format `id`",
    "\\dt+ `id`",
]


@pytest.mark.parametrize("cmd", _BLOCKED)
def test_leading_meta_command_refused_before_ssh(tmp_path: Path, cmd: str) -> None:
    proc, ssh_argv, psql_argv = _run(tmp_path, cmd)
    assert proc.returncode == 2
    assert "refused for safety" in proc.stderr
    assert cmd.split()[0] in proc.stderr, "the refusal names the command"
    assert ssh_argv is None and psql_argv is None, (
        "a refused command must not reach ssh"
    )


def test_whitespace_then_backslash_is_refused(tmp_path: Path) -> None:
    """First NON-SPACE character is the test — indented and multi-line
    leading whitespace both count."""
    for sql in ("   \\d refs", "\n\t \\! ls", " \\echo `id`"):
        proc, ssh_argv, _ = _run(tmp_path, sql)
        assert proc.returncode == 2, sql
        assert ssh_argv is None


def test_meta_command_refused_under_ro(tmp_path: Path) -> None:
    """--ro is exactly the escape \\c enables — refused before ssh too."""
    proc, ssh_argv, _ = _run(tmp_path, "--ro", "\\c - agent_rw")
    assert proc.returncode == 2
    assert "refused for safety" in proc.stderr
    assert ssh_argv is None


def test_meta_command_on_stdin_is_refused(tmp_path: Path) -> None:
    proc, ssh_argv, _ = _run(tmp_path, stdin_text="\\o /tmp/x\n")
    assert proc.returncode == 2
    assert "refused for safety" in proc.stderr
    assert ssh_argv is None


def test_meta_command_in_a_file_is_refused(tmp_path: Path) -> None:
    """A file whose first non-space byte is a backslash refuses; a \\! mid-
    file is inert inside the -c (covered by the _INERT_IN_C shapes)."""
    q = tmp_path / "evil.sql"
    q.write_text("\\! ls\nSELECT 1;\n", encoding="utf-8")
    proc, ssh_argv, _ = _run(tmp_path, "-f", str(q))
    assert proc.returncode == 2
    assert "refused for safety" in proc.stderr
    assert ssh_argv is None


#: Bypass shapes from the security review: they cannot be refused (the first
#: byte is not a backslash) — they arrive inside the single -c argument,
#: where psql sends them to the server verbatim and a bare backslash is a
#: syntax error, never a command.
_INERT_IN_C = [
    "SELECT 1; \\c - agent_rw",
    "SELECT 1;\\connect - agent_rw",
    "SELECT 1 \\\\ \\c - agent_rw",
    "SELECT 1;\n\\! ls",
    "SELECT 1; \\o /tmp/x",
    "SELECT 1; \\set FOO bar",
    # Review 2a: a plain literal ending in a backslash mis-paired a naive
    # stripper — inside -c there is nothing to strip or hide.
    "SELECT 'x\\\\'; \\c - agent_rw; -- '",
    # Review 2b: a comment opener inside a string literal.
    "SELECT '-- z'; \\o /tmp/x",
    # Review 2c: an apostrophe inside a quoted identifier.
    'SELECT "it\'s"; \\gexec',
    # Review 3: \gset + :variable smuggling — there is no lexer and no
    # variable interpolation on a -c at all.
    "SELECT 1 \\gset\nSELECT :'x'",
]


@pytest.mark.parametrize("sql", _INERT_IN_C)
def test_mid_text_meta_arrives_inside_the_single_c(tmp_path: Path, sql: str) -> None:
    proc, ssh_argv, psql_argv = _run(tmp_path, sql)
    assert ssh_argv is not None and psql_argv is not None
    assert sql in _c_values(psql_argv), "the caller text must be one -c arg"
    remote = ssh_argv[-1]
    assert '-c "$(cat)"' in remote, "caller SQL reaches psql only via -c $(cat)"
    assert sql not in remote, "caller SQL must not be spliced into the remote command"


def test_sql_arrives_byte_identical_in_the_c_argument(tmp_path: Path) -> None:
    """' " $ ` \\ ; and newlines survive the ssh-stdin/$(cat) path — a quoted
    command substitution is not re-expanded."""
    sql = (
        "SELECT 'it''s', \"dq\", $$fn \\d body$$, E'\\\\n', a$b, x`y;\n"
        "SELECT 'second line', z\\tw"
    )
    proc, ssh_argv, psql_argv = _run(tmp_path, sql)
    assert proc.returncode == 0, proc.stderr
    assert _c_values(psql_argv) == [sql]


def test_dollar_quoted_body_with_backslash_passes(tmp_path: Path) -> None:
    """The old stripper falsely refused a backslash inside a dollar-quoted
    body; -c makes it plain server SQL."""
    sql = "CREATE FUNCTION f() RETURNS void AS $$ BEGIN PERFORM '\\d'; END $$ LANGUAGE plpgsql"
    proc, _, psql_argv = _run(tmp_path, sql)
    assert sql in _c_values(psql_argv)


def test_ro_wraps_in_begin_read_only_c_args(tmp_path: Path) -> None:
    proc, ssh_argv, psql_argv = _run(tmp_path, "--ro", "SELECT * FROM refs;")
    assert proc.returncode == 0, proc.stderr
    assert _c_values(psql_argv) == [
        "BEGIN READ ONLY;",
        "SELECT * FROM refs;",
        "COMMIT;",
    ]
    assert _psql_user(psql_argv) == "agent_ro"
    assert ssh_argv is not None
    assert ssh_argv[-1].endswith('-c "BEGIN READ ONLY;" -c "$(cat)" -c "COMMIT;"')


def test_ro_refuses_a_write(tmp_path: Path) -> None:
    # A DELETE (not UPDATE … SET) — under --ro a SET keyword is refused at
    # the boundary now, so a write that reaches agent_ro must not contain
    # one. The role's grants are what refuse it, server-side.
    proc, _, psql_argv = _run(tmp_path, "--ro", "DELETE FROM refs WHERE ref_id = 1;")
    assert proc.returncode != 0
    assert "read-only" in proc.stderr
    assert _psql_user(psql_argv) == "agent_ro"


def test_stdin_sql_goes_through_the_same_c(tmp_path: Path) -> None:
    proc, _, psql_argv = _run(tmp_path, stdin_text="SELECT 3;")
    assert proc.returncode == 0, proc.stderr
    assert _c_values(psql_argv) == ["SELECT 3;"]


#: PRECIS_PROD_PSQL_OPTS is an allowlist: only display-only flags survive,
#: each passed to the remote command as its own double-quoted token.
#: Cosmetic -P pset names only — nothing that reflows a result cell.
_ALLOWED_OPTS = [
    "-At",
    "-q",
    "-X",
    "-AHq",
    "-H",
    "--csv",
    "--html",
    "--no-align",
    "--no-psqlrc",
    "--tuples-only",
    "-Pfooter=off",
    "--pset=footer=off",
    "-Ptuples_only=on",
    "-Ppager=off",
    "-Pborder=2",
    "-Pnull=NULL",
    "--pset=linestyle=unicode",
    "-Precordsep=:",
    "-F,",
    "-R:",
    "--field-separator=,",
    "-Tborder=2",
    "--table-attr=2",
    "-z",
    "-0",
    "--field-separator-zero",
    "--record-separator-zero",
]


@pytest.mark.parametrize("opts", _ALLOWED_OPTS)
def test_opts_allowlisted_display_flags_pass(tmp_path: Path, opts: str) -> None:
    proc, ssh_argv, psql_argv = _run(tmp_path, "SELECT 1;", PRECIS_PROD_PSQL_OPTS=opts)
    assert proc.returncode == 0, proc.stderr
    assert psql_argv is not None and ssh_argv is not None
    for tok in opts.split():
        assert tok in psql_argv
        assert f'"{tok}"' in ssh_argv[-1], (
            "each allowed token must reach the remote command quoted"
        )


#: Tokens that reopen a lexer, touch files, set variables, retarget the
#: connection or are bare positionals (psql reads them as dbname/user) —
#: all refused before any ssh.
_FORBIDDEN_OPTS = [
    "-f /tmp/x.sql",
    "--file=/tmp/x.sql",
    "--fil /tmp/x.sql",
    "-Af /tmp/x.sql",
    "-c '\\d'",
    "--command=\\d",
    "-v A=1",
    "--set=A=1",
    "--variable A=1",
    "--vari A=1",
    "-o /tmp/out",
    "--output=/tmp/out",
    "-L /tmp/log",
    "--log-file=/tmp/log",
    # Connection identity — a -U under --ro turns the run back into
    # agent_rw; -d/--dbname carries conninfo strings (user=agent_rw).
    "-U agent_rw",
    "--username=agent_rw",
    "--username agent_rw",
    "-d precis_prod",
    "--dbname=precis_prod",
    "--dbname=user=agent_rw",
    "-h somewhere",
    "--host=somewhere",
    "-p 6432",
    "--port=6432",
    # Bare positional tokens — psql reads them as dbname/user.
    "agent_rw",
    "precis_prod agent_rw",
    "-At agent_rw",
    # Option arguments as a separate token — they must be bundled.
    "-P footer=off",
    "-F ,",
    # Echo flags: -e/-a echo the cap wrapper's own -c text to stdout, and
    # that text is what run_sql greps for the verdict — an echo flag would
    # fake 'rolled back' on a committed write (round-3 must-fix 1).
    "-e",
    "-a",
    "-b",
    "-E",
    "-Ae",
    "-ta",
    "--echo-all",
    "--echo-errors",
    "--echo-queries",
    "--echo-hidden",
    # Layout settings reflow the result cell and can split the verdict row
    # so neither grep matches (round-3 should-fix 4).
    "-x",
    "--expanded",
    "-Pformat=wrapped",
    "--pset=format=csv",
    "-Pcolumns=20",
    "--pset=columns=80",
    "-Pexpanded=on",
    "-Ptitle=report",
    "-Pxheader_width=40",
    "-Punknown_pset=1",
]


@pytest.mark.parametrize("opts", _FORBIDDEN_OPTS)
def test_opts_non_display_options_refused(tmp_path: Path, opts: str) -> None:
    proc, ssh_argv, _ = _run(tmp_path, "SELECT 1;", PRECIS_PROD_PSQL_OPTS=opts)
    assert proc.returncode == 2, opts
    assert "refused for safety" in proc.stderr
    assert ssh_argv is None


#: Review must-fix 1: OPTS characters outside [A-Za-z0-9 space =_.,:+-] —
#: quotes, substitutions and separators re-parse on the ssh host AFTER the
#: token check. Every one exits 2 before ssh.
_OPTS_SHELL_REPROS = [
    "'-c' '\\! touch PWNED'",
    '"-f" /etc/hostname',
    "-A $(touch PWNED)",
    "-A;cmd",
    "-A|cmd",
    "-A `cmd`",
    "-A &",
    "-A\n-t",
    "-A\t-t",
]


@pytest.mark.parametrize("opts", _OPTS_SHELL_REPROS)
def test_opts_shell_metacharacters_refused(tmp_path: Path, opts: str) -> None:
    proc, ssh_argv, _ = _run(tmp_path, "SELECT 1;", PRECIS_PROD_PSQL_OPTS=opts)
    assert proc.returncode == 2, opts
    assert "refused for safety" in proc.stderr
    assert ssh_argv is None


def test_opts_u_agent_rw_under_ro_is_refused(tmp_path: Path) -> None:
    """Review must-fix 2: the exact escalation — --ro plus a -U option."""
    proc, ssh_argv, _ = _run(
        tmp_path,
        "--ro",
        "COMMIT; UPDATE refs SET title=title; BEGIN",
        PRECIS_PROD_PSQL_OPTS="-U agent_rw",
    )
    assert proc.returncode == 2
    assert "refused for safety" in proc.stderr
    assert ssh_argv is None


def test_write_wrapper_still_runs(tmp_path: Path) -> None:
    """A capped write keeps its fixed -c wrapper: BEGIN + the tx-local
    baseline setting before the caller's -c, the verdict/guard -c after."""
    proc, ssh_argv, psql_argv = _run(
        tmp_path, "UPDATE refs SET title='x' WHERE ref_id < 10;"
    )
    assert proc.returncode == 0, proc.stderr
    c_args = _c_values(psql_argv)
    assert c_args[0].startswith("BEGIN;")
    assert "set_config('precis.prod_psql_base'" in c_args[0]
    assert c_args[1] == "UPDATE refs SET title='x' WHERE ref_id < 10;"
    assert "prod_psql_verdict" in c_args[2] and c_args[2].rstrip().endswith("COMMIT;")
    assert "current_setting('precis.prod_psql_base'" in c_args[2]


def test_capped_wrapper_is_safe_on_a_reused_pooled_backend(tmp_path: Path) -> None:
    """Review must-fix 3: pgbouncer transaction pooling reuses a server
    backend without resetting it — the fake keeps created TEMP tables
    across runs. Two capped runs in a row must both pass, and the wrapper
    -c args must contain no DDL at all."""
    sql = "UPDATE refs SET title='x' WHERE ref_id < 10;"
    psql_argv = None
    for _ in range(2):
        proc, _, psql_argv = _run(tmp_path, sql)
        assert proc.returncode == 0, proc.stderr
    c_args = _c_values(psql_argv)
    wrapper = [a for a in c_args if a != sql]
    assert wrapper, "expected the fixed wrapper -c args around the caller's"
    for arg in wrapper:
        assert not re.search(r"\b(CREATE|TEMP|DROP|ALTER|TRUNCATE)\b", arg), arg


def test_a_result_row_cannot_fake_the_rollback_verdict(tmp_path: Path) -> None:
    """Review should-fix: a returned row containing 'prod-psql: ROLLBACK'
    must not exit 4 — only the nonce-anchored verdict line counts."""
    sql = (
        "UPDATE refs SET title='x' WHERE ref_id=1 "
        "RETURNING 'EMIT(prod-psql: ROLLBACK, 999 rows is over the cap)'"
    )
    proc, _, _ = _run(tmp_path, sql)
    assert proc.returncode == 0, proc.stderr
    assert "prod-psql: ROLLBACK, 999 rows is over the cap" in proc.stdout
    assert re.search(r"prod-psql \S+: COMMIT", proc.stdout)


def test_tx_control_hidden_after_a_dashdash_literal_is_refused(
    tmp_path: Path,
) -> None:
    """Review should-fix: the exact bypass string — a '--' inside a string
    literal must not hide the COMMIT/BEGIN that follows it."""
    sql = (
        "UPDATE refs SET title=title; SELECT '--'; COMMIT; "
        "UPDATE refs SET title=title; BEGIN"
    )
    proc, ssh_argv, _ = _run(tmp_path, sql)
    assert proc.returncode == 2
    assert ssh_argv is None


def test_ro_refuses_transaction_mode_and_identity_changes(
    tmp_path: Path,
) -> None:
    """SET TRANSACTION / SET SESSION CHARACTERISTICS READ WRITE (or an
    identity change) under --ro is refused — round 4 widened it to ANY
    SET, RESET, DISCARD or LOAD anywhere in code position."""
    for sql in (
        "COMMIT; SET TRANSACTION READ WRITE; SELECT 1",
        "SET SESSION CHARACTERISTICS AS TRANSACTION READ WRITE",
        "SET SESSION AUTHORIZATION agent_rw",
        "SET ROLE agent_rw",
        "SET LOCAL TRANSACTION READ WRITE; CREATE TEMP TABLE zz(a int)",
        "SET LOCAL enable_seqscan = off; SELECT 1",
        "RESET enable_seqscan; SELECT 1",
        "DISCARD ALL; SELECT 1",
        "LOAD 'auto_explain'; SELECT 1",
        "START TRANSACTION READ WRITE",
        "SELECT 'x'; SET TRANSACTION READ WRITE -- trailing",
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 2, sql
        assert "plain read queries" in proc.stderr, sql
        assert ssh_argv is None, sql
    # agent_rw callers own their session — only --ro adds this refusal.
    proc, _, _ = _run(tmp_path, "SET TRANSACTION READ WRITE; SELECT 1")
    assert proc.returncode == 0, proc.stderr


def test_ro_refuses_set_config_and_u_ampersand_tokens(tmp_path: Path) -> None:
    """Round-4 DECIDED: set_config( is refused outright — its setting name
    is a string literal the code scan cannot see — and so is any U& token,
    which hides a name behind unicode escapes (the review's bypass spelled
    transaction_read_only as U&"transaction\\005fread_only"). In the blanked
    text the escaped literal is gone and a bare u& token remains."""
    for sql in (
        "SELECT set_config('transaction_read_only', 'off', true)",
        "SELECT set_config('default_transaction_read_only','off',false)",
        "SELECT set_config('enable_seqscan', 'off', true); SELECT 1",
        "SELECT pg_catalog.set_config('x', 'y', false)",
        'SET LOCAL U&"transaction\\005fread_only" = off; CREATE TEMP TABLE zz(a int)',
        "SELECT U&'d\\0065lt\\0065'",
        'SELECT 1 FROM U&"refs"',
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 2, sql
        assert "plain read queries" in proc.stderr, sql
        assert ssh_argv is None, sql


def test_ro_plain_reads_with_the_refused_words_as_data_pass(
    tmp_path: Path,
) -> None:
    """The scan runs on CODE only: SET/U& inside a literal or dollar body
    is data, and a non-ASCII byte ends an identifier before a keyword —
    all plain reads, all pass."""
    for sql in (
        "SELECT 'SET LOCAL transaction_read_only = off; é' AS s",
        "SELECT $tag$body; SET x$tag$, $é$also SET$é$",
        "SELECT 1 AS éSET",
        "SELECT 1 AS u, 2 & 3",
        "SELECT 1 -- transaction_read_only is just a comment",
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 0, (sql, proc.stderr)
        assert ssh_argv is not None, sql


def test_ro_refuses_transaction_control_statements(tmp_path: Path) -> None:
    """Round-3 must-fix 2: the BEGIN READ ONLY wrapper only holds while its
    transaction lives — a caller COMMIT/ROLLBACK/BEGIN/SAVEPOINT ends or
    replaces it and later statements run outside. The review's exact repro
    ran a CREATE with exit 0; the whole class exits 2 before ssh."""
    for sql in (
        "COMMIT; BEGIN READ WRITE; CREATE TEMP TABLE zz(a int)",
        "COMMIT; CREATE TEMP TABLE zz(a int)",
        "ROLLBACK; SELECT 1",
        "END; SELECT 1",
        "ABORT; SELECT 1",
        "SAVEPOINT s; SELECT 1",
        "BEGIN READ WRITE; SELECT 1",
        "SELECT 1; START TRANSACTION READ WRITE",
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 2, sql
        assert "refused" in proc.stderr
        assert ssh_argv is None, sql


def test_ro_refuses_read_only_guc_flips(tmp_path: Path) -> None:
    """Round-3 must-fix 2: SET LOCAL transaction_read_only and
    set_config('default_transaction_read_only', …) flip the mode without a
    transaction-mode keyword. Round 4 refuses the whole class: every SET
    spelling and set_config( itself, so the name no longer matters."""
    for sql in (
        "SET LOCAL transaction_read_only = off; CREATE TEMP TABLE zz(a int)",
        "SET LOCAL default_transaction_read_only = off; SELECT 1",
        "SET transaction_read_only = off; SELECT 1",
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 2, sql
        assert ssh_argv is None, sql


def test_dollar_sign_in_an_identifier_is_not_a_dollar_quote(tmp_path: Path) -> None:
    """Round-3 should-fix 3: a$$ and x$t$ are identifiers ('$' is a valid
    identifier continuation), not open dollar quotes — mis-lexing one
    blanked everything up to the next $$ / $t$ and hid the COMMIT the
    tx-control refusal exists to catch. Both reach exit 2 before ssh."""
    for sql in (
        "SELECT 1 AS a$$; COMMIT; UPDATE refs SET title=title; BEGIN; SELECT 1 AS b$$",
        "UPDATE refs SET title=title WHERE ref_id=1; SELECT 1 AS x$t$; "
        "COMMIT; SELECT 2 AS y$t$",
    ):
        proc, ssh_argv, _ = _run(tmp_path, sql)
        assert proc.returncode == 2, sql
        assert ssh_argv is None, sql


def test_a_real_dollar_quoted_body_still_blanks(tmp_path: Path) -> None:
    """The lookbehind must not break a genuine dollar quote: a COMMIT inside
    $$…$$ is literal text, not transaction control — the capped run goes
    through."""
    sql = "UPDATE refs SET title='x' WHERE ref_id=1; SELECT $$ COMMIT $$"
    proc, _, psql_argv = _run(tmp_path, sql)
    assert proc.returncode == 0, proc.stderr
    assert sql in _c_values(psql_argv)


def test_non_ascii_identifier_and_dollar_tag_bytes(tmp_path: Path) -> None:
    """Round-4 must-fix 3: identifiers and dollar-quote tags also take bytes
    \\x80-\\xff — the ASCII-only classes mis-lexed é. Repro A (capped):
    é$$ is ONE identifier — the $$ opens no quote, so the following COMMIT
    is seen and refused. Repro B (--ro): $é$ IS a dollar-quote tag — its
    body blanks, but the COMMIT after it is seen and refused."""
    sql = "SELECT 1 AS é$$; COMMIT; UPDATE refs SET title=title; BEGIN; SELECT 1 AS é$$"
    proc, ssh_argv, _ = _run(tmp_path, sql)
    assert proc.returncode == 2
    assert ssh_argv is None
    sql = (
        "SELECT $é$ $$ $é$; COMMIT; BEGIN READ WRITE; "
        "CREATE TEMP TABLE zz2(a int); SELECT $$x$$"
    )
    proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
    assert proc.returncode == 2
    assert ssh_argv is None


#: Round-5 review repros (psqlbs5): scanner mis-lexes that blanked code PG
#: still runs — each exits 2 before ssh on the named path now. Items 1-3
#: are lexer fixes; 4-5 are DECIDED refusals on both paths (a DO body's
#: dollar quote and a quoted "set_config" both blank for the code scan and
#: leave session state on a pooled backend).
_ROUND5_REFUSED = [
    # 1: a -- comment ends at \r too, not only \n.
    (
        "--ro",
        "SELECT 1; -- x\rCOMMIT; BEGIN READ WRITE; CREATE TEMP TABLE zz5(a int); SELECT 2",
    ),
    (None, "SELECT 1; -- x\rCOMMIT; UPDATE refs SET title=title; BEGIN; SELECT 2"),
    (None, "SELECT 1; -- x\rSET work_mem = '99MB'; SHOW work_mem"),
    # 2: /* */ comments nest — closing the outer at the first */ left a '
    # that opened a literal over the real code.
    (
        "--ro",
        "/* /* */ ' */ COMMIT; BEGIN READ WRITE; CREATE TEMP TABLE zz6(a int); SELECT '1'",
    ),
    (None, "/* /* */ ' */ COMMIT; UPDATE refs SET title=title; BEGIN; SELECT '1'"),
    (None, "/* /* */ ' */ SET work_mem='98MB'; SHOW work_mem; SELECT '1'"),
    # 3: E'a'<newline>'...' continues the SAME E-string — the continued
    # segment still takes backslash escapes, so \' does not close it.
    (
        "--ro",
        "SELECT E'a'\n'\\' x' ; COMMIT; BEGIN READ WRITE; CREATE TEMP TABLE zz8(a int); SELECT 'y'",
    ),
    (
        None,
        "SELECT E'a'\n'\\' x' ; COMMIT; UPDATE refs SET title=title; BEGIN; SELECT 'y'",
    ),
    # 4: a quoted "set_config" (any case) — the identifier blanking hid it.
    ("--ro", "SELECT \"set_config\"('work_mem','77MB',false); SHOW work_mem"),
    (None, "SELECT \"set_config\"('work_mem','77MB',false); SHOW work_mem"),
    (None, "SELECT \"SET_CONFIG\"('work_mem','77MB',false)"),
    # 5: DO blocks — the dollar-quoted body blanks for the scan.
    ("--ro", "DO $$BEGIN PERFORM set_config('work_mem','66MB',false); END$$"),
    ("--ro", "DO $$BEGIN EXECUTE 'SET search_path = pg_temp'; END$$"),
    (None, "DO $$BEGIN PERFORM 1; END$$"),
    # Round-7: the continuation gap counts -- comments and \v as
    # whitespace before the newline (pg17), so E'a' -- c\n'\' x' is ONE
    # E-literal — without it \' closed the segment and a later ' hid the
    # COMMIT/SET.
    (
        "--ro",
        "SELECT E'a' -- c\n'\\' x' ; COMMIT; BEGIN READ WRITE; "
        "CREATE TEMP TABLE z(a int); SELECT 'y'",
    ),
    (None, "SELECT E'a' -- c\n'\\' x'; SET work_mem='9MB'; SELECT 'y'"),
    (
        "--ro",
        "SELECT E'a'\x0b\n'\\' x' ; COMMIT; BEGIN READ WRITE; "
        "CREATE TEMP TABLE z2(a int); SELECT 'y'",
    ),
]


@pytest.mark.parametrize(("flag", "sql"), _ROUND5_REFUSED)
def test_round5_review_repros_refused_before_ssh(
    tmp_path: Path, flag: str | None, sql: str
) -> None:
    proc, ssh_argv, _ = _run(tmp_path, *([flag] if flag else []), sql)
    assert proc.returncode == 2, (sql, proc.stderr)
    assert "refused" in proc.stderr, sql
    assert ssh_argv is None, sql


def test_round5_legit_reads_still_pass(tmp_path: Path) -> None:
    """The still-pass side of the round-5 review: a quoted identifier
    holding '; COMMIT' stays blanked (only the exact name "set_config" is
    re-emitted), a literal holding '"set_config"(' is data, a newline-
    continued literal is one value, and a -- comment may end at \r."""
    for sql in (
        'SELECT "a""; COMMIT" FROM refs',
        "SELECT '\"set_config\"(' AS s",
        "SELECT 'x'\n'y'",
        "SELECT E'x'\n'y'",
        "SELECT 1 -- trailing\rcomment",
        "SELECT $$ /* not a comment */ $$",
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 0, (sql, proc.stderr)
        assert ssh_argv is not None, sql


def test_ro_refuses_vault_schema_references(tmp_path: Path) -> None:
    """gr523021: agent_ro can EXECUTE vault.reveal/set/delete, so a plain
    SELECT could read or drop a secret — --ro refuses any `vault.`
    reference in code position, bare or "vault"-quoted, in any case.
    'vault.x' inside a literal, -- a comment and a table merely named
    vault (no dot follows) stay data and pass."""
    for sql in (
        "SELECT vault.reveal('x')",
        "select \"vault\".reveal('x')",
        "SELECT * FROM vault.list()",
        "SELECT * FROM VAULT.list()",
        "SELECT x FROM vault . list()",
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 2, (sql, proc.stderr)
        assert "secrets are not readable" in proc.stderr, sql
        assert ssh_argv is None, sql
    for sql in (
        "SELECT 'vault.x' AS s, 'keep vault.reveal here'",
        "SELECT 1 -- vault.reveal('x') is a comment",
        "SELECT * FROM vault",
    ):
        proc, ssh_argv, _ = _run(tmp_path, "--ro", sql)
        assert proc.returncode == 0, (sql, proc.stderr)
        assert ssh_argv is not None, sql


def test_a_capped_run_with_no_verdict_row_exits_6(tmp_path: Path) -> None:
    """Fail closed: a nominally clean capped run whose verdict row never
    printed leaves the write's outcome unknown — its own exit code 6, not
    0 and not UNCAPPED's 5."""
    proc, ssh_argv, _ = _run(
        tmp_path,
        "UPDATE refs SET title='x' WHERE ref_id = 1;",
        FAKE_DROP_VERDICT="1",
    )
    assert proc.returncode == 6
    assert "no verdict row" in proc.stderr
    assert ssh_argv is not None


def test_wrapper_sql_never_carries_the_contiguous_verdict(tmp_path: Path) -> None:
    """Round-3 must-fix 1, second half: even with echo flags refused, the
    grep target must not appear contiguously in any -c text — it is built
    at runtime with || so an echoed query can never print a fakeable line."""
    proc, _, psql_argv = _run(tmp_path, "UPDATE refs SET title='x' WHERE ref_id=1;")
    assert proc.returncode == 0, proc.stderr
    for arg in _c_values(psql_argv):
        assert not re.search(r"prod-psql \S+: (ROLLBACK|UNCAPPED|COMMIT)", arg), arg


def test_the_c_size_cap_is_131071_after_the_explain_prepend(
    tmp_path: Path,
) -> None:
    """Round-3 should-fix 5: MAX_ARG_STRLEN counts the NUL, so 131072 bytes
    fails E2BIG on the hop and 131071 is the last size that execs — and the
    check must run AFTER --explain prepends 'EXPLAIN ' (8 bytes). The big
    texts arrive via stdin — a >131071-byte argv can't exec locally either."""
    big = "SELECT " + "x" * (131072 - len("SELECT "))  # exactly 131072
    proc, ssh_argv, _ = _run(tmp_path, stdin_text=big)
    assert proc.returncode == 2 and ssh_argv is None
    assert "131072" in proc.stderr
    proc, ssh_argv, _ = _run(tmp_path, stdin_text=big[:-1])
    assert proc.returncode == 0, proc.stderr
    assert ssh_argv is not None  # 131071 — the last size that execs
    # 131064 + 'EXPLAIN ' = 131072 → refused; one byte less goes through.
    proc, ssh_argv, _ = _run(tmp_path, "--explain", stdin_text=big[:131064])
    assert proc.returncode == 2 and ssh_argv is None
    proc, ssh_argv, _ = _run(tmp_path, "--explain", stdin_text=big[:131063])
    assert proc.returncode == 0, proc.stderr
    assert ssh_argv is not None


def test_sql_over_the_argv_limit_is_refused_locally(tmp_path: Path) -> None:
    """A single -c is one argv item, capped at 131071 bytes (MAX_ARG_STRLEN
    counts the NUL) — refuse locally instead of failing E2BIG on the hop.
    (A text that big can't be a local argv either — it arrives via stdin.)"""
    proc, ssh_argv, _ = _run(tmp_path, stdin_text="SELECT " + "x" * 200_000)
    assert proc.returncode == 2
    assert "131071" in proc.stderr
    assert ssh_argv is None
    proc, _, _ = _run(tmp_path, stdin_text="SELECT " + "x" * 100_000)
    assert proc.returncode == 0, proc.stderr


def test_session_set_still_refused(tmp_path: Path) -> None:
    """The pgbouncer pooling rule is orthogonal to meta-commands and stays."""
    proc, ssh_argv, _ = _run(
        tmp_path, "SET default_transaction_read_only = on; SELECT 1"
    )
    assert proc.returncode == 2
    assert "session-level SET" in proc.stderr
    assert ssh_argv is None


def test_backslash_inside_a_string_literal_is_data(tmp_path: Path) -> None:
    proc, _, psql_argv = _run(
        tmp_path, "SELECT '\\d', '; \\c', E'\\\\o', \"\\! \" FROM refs"
    )
    assert proc.returncode == 0, proc.stderr
    assert "SELECT '\\d', '; \\c', E'\\\\o', \"\\! \" FROM refs" in _c_values(psql_argv)
