#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: decide ``scripts/prod-psql`` calls by what the SQL does.

``prod-psql`` connects as **write-capable `agent_rw`**. Reto's rulings
(2026-10-01 "default is go", 2026-10-11 gr462727) and the standing grant in
``.claude/fleet/authority.md`` (2026-10-15): targeted prod writes must not
stall on a prompt; destructive or untargeted ones ask. This hook is the
single decision point, keyed off the SQL:

- A **lone** `scripts/prod-psql --ro "<any SQL>"` -> **allow**: agent_ro inside
  BEGIN READ ONLY, the server refuses writes.
- A **lone** `scripts/prod-psql "<sql>"` (one quoted SQL argument, no shell
  chaining, no ``$``/backtick/backslash interpolation, no ``VAR=`` env
  prefix at all) is parsed statement by statement and judged by its MOST DANGEROUS
  statement:
  * any destructive statement -> **ask** (below);
  * more than one statement when any of them writes -> **ask**;
  * otherwise one write (INSERT, UPDATE ... WHERE, DELETE ... WHERE, CREATE,
    REFRESH, nextval, ...) -> **allow**. ``scripts/prod-psql`` runs SQL that
    mentions UPDATE/DELETE inside BEGIN and rolls it back when the
    transaction changed more than ``PRECIS_PROD_WRITE_MAX_ROWS`` rows
    (default 100);
  * only reads (SELECT/EXPLAIN/WITH/SHOW/TABLE/VALUES, and the line-start
    meta-commands ``\\d \\dt \\di \\l \\x``) -> **allow**;
  * a verb we do not know -> ask if it is a known write keyword, else no
    opinion.
- ``--ro`` must be its own word (``--ro"..."`` is not it: the script tests
  ``$1 == --ro``). Without ``--ro`` any other psql backslash meta-command
  (``\\!``, ``\\i``, ``\\o``, ``\\gexec``, ``\\g``, ``\\set``, ...) **asks**.
- With ``--ro`` the SQL argument is decoded as bash would (single quotes, or
  double quotes with ``\\$ \\` \\" \\\\`` escapes; an unescaped ``$name``/``$(``
  or backtick is shell interpolation and is never allowed), then lexed like
  psql (quotes, dollar bodies, comments) to find the real meta-commands. Only
  those that act on the node outside the read-only transaction **ask**:
  ``\\!``, ``\\copy`` other than ``TO STDOUT``, ``\\o``/``\\g``/``\\gx``/``\\s``
  with a target, ``\\gexec``, ``\\i``/``\\ir``, ``\\w``, ``\\c`` (a reconnect
  leaves agent_ro), ``\\e``/``\\ef``/``\\ev``, ``\\setenv``, ``\\cd``,
  ``\\lo_*``, ``\\password``, and any backtick on a meta-command line (psql
  runs it in a shell). Everything else passes.
- A ``VAR=`` env prefix (``PRECIS_PROD_PSQL_OPTS`` injects psql flags,
  ``PRECIS_PROD_SSH_HOST`` redirects the host) is never auto-allowed, for any
  kind including ``--ro`` and reads.
- Compound / piped / interpolated commands never auto-allow. They **ask** only
  if the SQL they feed prod-psql is destructive, else no opinion (normal
  prompt). Their text is judged raw (quotes not stripped), so a literal
  cannot hide a keyword.

Destructive = ask, checked statically on the SQL text (comments, string
literals and quoted identifiers are blanked first, so ``'DELETE'``, ``-- drop``
and a column ``"drop"`` do not trip it; ``ON UPDATE`` and ``ON CONFLICT DO
UPDATE`` are not UPDATE statements):

- ``DROP`` (so ``ALTER ... DROP`` too), ``TRUNCATE``;
- an ``UPDATE`` or ``DELETE`` (also inside a CTE) with no ``WHERE`` at its
  own parenthesis depth;
- any INSERT/UPDATE/DELETE naming ``pg_catalog``, ``information_schema`` or a
  ``pg_*`` target; ``row_security``; ``CREATE USER|GROUP`` (roles below);
- every ``ALTER`` (thresholds.md schema changes: TYPE, RENAME, ADD COLUMN,
  SET SCHEMA, DETACH, DISABLE ... -- the hook cannot know if a table is
  populated, so all ask), ``CREATE OR REPLACE``, ``VACUUM FULL``, ``REINDEX``,
  ``CLUSTER``, ``session_replication_role``, ``SET ROLE``/``SESSION
  AUTHORIZATION``;
- ``FOR UPDATE|SHARE|NO KEY UPDATE|KEY SHARE`` and side-effecting built-ins
  (``dblink*``, ``lo_*``, ``set_config``, ``pg_reload_conf``,
  ``pg_rotate_logfile``, ``pg_switch_wal``, ``pg_advisory*``, ``pg_create_*``,
  ``pg_drop_*``, ``pg_replication_*``, ``pg_promote``, ``pg_read_*file``,
  ``pg_ls_*``). A deny-list: a user-defined function with side effects is
  residual risk; plain ``pg_stat*``/``pg_size_*``/``pg_get_*`` reads pass;
- on ambiguity: dollar-quoted bodies, ``E'..'`` strings, unterminated
  quotes/comments, ``DO``/``EXECUTE``/``COPY``/``CALL``/``MERGE``/``GRANT``/
  ``REVOKE``/``LOCK``/``CLUSTER``, ``CREATE FUNCTION|PROCEDURE|TRIGGER|RULE``
  (bodies hide statements), roles, ``pg_terminate_backend``.

NOT statically checkable (never asked about here): a ``WHERE`` that matches
every row (``WHERE true``, ``1=1``) -- the row cap in ``scripts/prod-psql``
bounds that for UPDATE/DELETE, not for INSERT ... SELECT; whether a table
being altered is populated; the embedding dimension of
``chunk_embeddings.vector`` beyond the ALTER rule; wiping the DB or
rewriting user-owned data files by a non-SQL route; force-pushes; anything
that leaves precis; a paid run over $25. The agent remits cover those.

Escape hatch: ``ALLOW_PROD_WRITE=1`` disables the **ask** (mirrors
guard-prod-write.py); it never widens the allow.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``).
"""

from __future__ import annotations

import json
import os
import re
import sys
from typing import Any

#: Read-only openers. A statement must start with one of these to be read-only.
_READ_OPENER = re.compile(
    r"^\s*(SELECT|EXPLAIN|WITH|SHOW|TABLE|VALUES)\b"
    r"|^\s*\\[a-z]",  # psql backslash meta-commands: \d \l \dt \x \timing …
    re.IGNORECASE,
)

#: Write / side-effecting keywords (whole-word). Used only for psql
#: meta-commands and statements whose verb we do not recognise.
_WRITE_KEYWORDS = re.compile(
    r"\b("
    r"INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|COPY|MERGE|"
    r"REINDEX|VACUUM|CLUSTER|LOCK|CALL|REFRESH|COMMENT|SECURITY|"
    r"nextval|setval|pg_terminate_backend|pg_cancel_backend"
    r")\b"
    r"|\bFOR\s+(UPDATE|SHARE|NO\s+KEY\s+UPDATE)\b"
    r"|\bSET\s+ROLE\b",
    re.IGNORECASE,
)


def _is_readonly_sql(sql: str) -> bool:
    """True only if every part of ``sql`` is provably read-only."""
    body = sql.strip().strip("\"'").strip()
    if not body:
        return False
    if _WRITE_KEYWORDS.search(body):
        return False
    return bool(_READ_OPENER.search(body))


class _Ambiguous(Exception):
    """SQL we cannot blank reliably; the caller asks."""


_DOLLAR_TAG = re.compile(r"\$[A-Za-z_]*\$")
_TOKEN = re.compile(r"[A-Za-z_][A-Za-z0-9_]*|\(|\)|;")


def _blank(sql: str) -> str:
    """Replace comments, string literals and quoted identifiers with a
    placeholder so keywords inside them are invisible. Raises ``_Ambiguous``
    on anything we cannot blank safely."""
    out: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
            out.append(" ")
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise _Ambiguous("unterminated comment")
            out.append(" ")
        elif c in "'\"":
            if (
                c == "'"
                and i
                and sql[i - 1] in "Ee"
                and not (i > 1 and (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            ):
                raise _Ambiguous("E'' string with backslash escapes")
            j = i + 1
            while True:
                j = sql.find(c, j)
                if j < 0:
                    raise _Ambiguous("unterminated quote")
                if sql.startswith(c, j + 1):
                    j += 2
                    continue
                break
            ident = sql[i + 1 : j]
            if c == '"' and re.fullmatch(r"pg_\w*|information_schema", ident):
                out.append(f" {ident} ")  # a quoted catalog name still counts
            else:
                out.append(" _q " if c == '"' else " ")
            i = j + 1
        elif (
            c == "$"
            and _DOLLAR_TAG.match(sql, i)
            and not (i and (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            raise _Ambiguous("dollar-quoted body")
        else:
            out.append(c)
            i += 1
    return "".join(out)


_READ_VERBS = {"SELECT", "EXPLAIN", "WITH", "SHOW", "TABLE", "VALUES"}
_WRITE_VERBS = {
    "INSERT", "UPDATE", "DELETE", "CREATE", "ALTER", "REFRESH", "COMMENT", "REINDEX",
    "VACUUM", "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK", "END",
}  # fmt: skip
#: Tokens that make a SELECT/WITH/EXPLAIN statement a write.
_WRITE_TOKENS = {
    "INSERT", "UPDATE", "CREATE", "ALTER", "REFRESH", "COMMENT", "REINDEX",
    "VACUUM", "NEXTVAL", "SETVAL", "INTO", "DELETE",
}  # fmt: skip
_FORCE_ASK_FIRST = {
    "DO", "EXECUTE", "COPY", "CALL", "MERGE", "GRANT", "REVOKE", "LOCK", "CLUSTER",
}  # fmt: skip
_FORCE_ASK_ANY = {
    "FUNCTION", "PROCEDURE", "TRIGGER", "RULE", "ROLE", "AUTHORIZATION",
    "PG_TERMINATE_BACKEND", "PG_CANCEL_BACKEND", "REINDEX", "CLUSTER",
}  # fmt: skip
_LOCK_AFTER_FOR = {"UPDATE", "SHARE", "NO", "KEY"}
#: Side-effecting built-ins reachable from a plain SELECT. A deny-list: a
#: user-defined function with side effects is residual risk we cannot see.
_SIDE_EFFECT_FN = re.compile(
    r"DBLINK\w*|LO_\w*|SET_CONFIG|SESSION_REPLICATION_ROLE|PG_RELOAD_CONF"
    r"|PG_ROTATE_LOGFILE|PG_SWITCH_WAL|PG_ADVISORY\w*|PG_CREATE_\w*|PG_DROP_\w*"
    r"|PG_REPLICATION_\w*|PG_PROMOTE|PG_READ_\w*FILE|PG_LS_\w*"
)


#: Statement verbs a data-modifying CTE or subquery can carry.
_DML = {"INSERT", "UPDATE", "DELETE"}
#: Schemas whose rows only the server may write.
_CATALOG = {"PG_CATALOG", "INFORMATION_SCHEMA"}


def _dml_positions(toks: list[str]) -> list[int]:
    """Indexes of the tokens that start an INSERT/UPDATE/DELETE statement
    (not ``FOR UPDATE``, ``ON UPDATE``, ``DO UPDATE``, ``OF UPDATE``, ...)."""
    return [
        k
        for k, t in enumerate(toks)
        if t in _DML
        and not (
            k and toks[k - 1] in ("FOR", "KEY", "ON", "DO", "OF", "BEFORE", "AFTER")
        )
    ]


def _target(toks: list[str], k: int) -> str:
    """The table token after the DML verb at ``k`` (INTO / FROM / ONLY skipped)."""
    rest = [t for t in toks[k + 1 : k + 4] if t not in ("INTO", "FROM", "ONLY")]
    return rest[0] if rest else ""


def _statement_verdict(toks: list[str]) -> tuple[str, str]:
    """``(kind, why)`` for one statement; kind is ask / write / read / unknown."""
    for t in toks:
        if t in ("DROP", "TRUNCATE"):
            return "ask", f"{t} is destructive"
    first = toks[0] if toks else ""
    if first in _FORCE_ASK_FIRST:
        return "ask", f"{first} can hide or widen a statement"
    for t in toks:
        if t in _FORCE_ASK_ANY:
            return "ask", f"{t} cannot be judged statically"
    if first == "CREATE" and toks[1:2] and toks[1] in ("USER", "GROUP"):
        return "ask", f"CREATE {toks[1]} makes a role"
    # Every ALTER asks (SET SCHEMA, DETACH, DISABLE TRIGGER, TYPE, RENAME, ADD
    # COLUMN, ... are all schema changes thresholds.md says to confirm).
    if "ALTER" in toks:
        return "ask", "ALTER (thresholds.md schema change)"
    if "ROW_SECURITY" in toks:
        return "ask", "row_security switches row-level security"
    for k, t in enumerate(toks):
        if t == "OR" and toks[k + 1 : k + 2] == ["REPLACE"] and "CREATE" in toks:
            return "ask", "CREATE OR REPLACE rewrites an existing object"
        if t == "FOR" and toks[k + 1 : k + 2] and toks[k + 1] in _LOCK_AFTER_FOR:
            return "ask", "FOR UPDATE/SHARE takes row locks"
        if _SIDE_EFFECT_FN.fullmatch(t):
            return "ask", f"{t} has server-side effects"
    if "VACUUM" in toks and "FULL" in toks:
        return "ask", "VACUUM FULL rewrites the table"
    # An UPDATE or DELETE statement (a CTE's too) needs a WHERE at its own
    # parenthesis depth, before that depth closes.
    depths: list[int] = []
    d = 0
    for t in toks:
        d += (t == "(") - (t == ")")
        depths.append(d)
    dml = _dml_positions(toks)
    for k in dml:
        t = toks[k]
        if _target(toks, k).startswith("PG_") or _CATALOG & set(toks):
            return "ask", f"{t} on a system catalog"
        if t == "INSERT":
            continue
        found = False
        for m in range(k + 1, len(toks)):
            if depths[m] < depths[k]:
                break
            if toks[m] == "WHERE" and depths[m] == depths[k]:
                found = True
                break
        if not found:
            return "ask", f"{t} without WHERE"
    if first in _READ_VERBS or first == "(":
        if dml or _WRITE_TOKENS & set(toks):
            return "write", ""
        return "read", ""
    if (
        first == "SET"
        and toks[1:2]
        and toks[1] in ("LOCAL", "TRANSACTION", "CONSTRAINTS")
    ):
        return "write", ""
    if first in _WRITE_VERBS:
        return "write", ""
    return "unknown", first


#: The only backslash lines that pass: harmless catalog listings, whole line.
_META_OK = re.compile(r"\s*\\(d\+?|dt\+?|di\+?|l\+?|x)(\s+[\w.]+)?\s*;?\s*")


def _strip_meta(sql: str) -> tuple[str, bool]:
    """Drop whitelisted meta-command lines; ``ok`` False if any other
    backslash remains (anywhere in the text)."""
    keep: list[str] = []
    for line in sql.split("\n"):
        if "\\" in line:
            if not _META_OK.fullmatch(line):
                return sql, False
        else:
            keep.append(line)
    return "\n".join(keep), True


#: Meta-commands that act on the node outside the read-only transaction:
#: shell, file input/output, a reconnect (leaves agent_ro), editors, client
#: env. ``\o``/``\g``/``\gx``/``\s`` and ``\copy`` are judged by argument.
_RO_META_ASK = {
    "!", "gexec", "i", "include", "ir", "include_relative", "w", "write",
    "c", "connect", "e", "edit", "ef", "ev", "setenv", "cd", "password",
    "lo_export", "lo_import", "lo_unlink",
}  # fmt: skip
_RO_META_TARGET = {"o", "out", "g", "gx", "s"}
_COPY_TO_STDOUT = re.compile(
    r"\s*(?:\([^()]*\)\s*)?TO\s+P?STDOUT\b(?![^;]*\bPROGRAM\b)", re.IGNORECASE
)


def _copy_to_stdout(arg: str) -> bool:
    """True when a ``\\copy`` argument writes only to (p)stdout: ``(query) TO
    STDOUT`` or ``table [(cols)] TO STDOUT``."""
    arg = arg.strip()
    if arg.startswith("("):
        depth, quote = 0, ""
        for k, ch in enumerate(arg):
            if quote:
                quote = "" if ch == quote else quote
                continue
            if ch in "'\"":
                quote = ch
                continue
            depth += (ch == "(") - (ch == ")")
            if depth == 0:
                return bool(_COPY_TO_STDOUT.match(arg, k + 1))
        return False
    table = re.match(r"[\w.\"]+", arg)
    return bool(table and _COPY_TO_STDOUT.match(arg, table.end()))


#: Raw-text fallback for a ``--ro`` call we cannot decode (piped or
#: compound): any dangerous meta-command name after a backslash, anywhere.
_RO_RAW_DANGER = re.compile(
    r"\\(?:!|gexec|include_relative|include|ir|i|write|w|connect|c|edit|e[fv]?"
    r"|setenv|cd|password|lo_\w+|copy|out|o|gx|g|s)(?![A-Za-z_])",
    re.IGNORECASE,
)


#: A leading quoted word followed only by fd redirections (``2>&1``,
#: ``>out``) — the SQL argument of a compound ``--ro`` call.
_LEAD_QUOTED = re.compile(
    r"\s*(\"(?:[^\"\\]|\\.)*\"|'[^']*')((?:\s+\d?>>?&?\s*[\w./-]+)*)\s*$", re.S
)


def _bash_quoted_arg(args: str) -> str | None:
    """The text bash passes for ``args`` when it is ONE quoted word, else None.
    Double quotes keep a backslash only before ``$ ` " \\`` and newline; an
    unescaped expansion (``$name``, ``${``, ``$(``, backtick) returns None."""
    args = args.strip()
    if len(args) >= 2 and args[0] == args[-1] == "'" and "'" not in args[1:-1]:
        return args[1:-1]
    if len(args) < 2 or args[0] != '"' or args[-1] != '"':
        return None
    out: list[str] = []
    body, i = args[1:-1], 0
    while i < len(body):
        c = body[i]
        nxt = body[i + 1] if i + 1 < len(body) else ""
        if c == "\\" and nxt and nxt in '$`"\\\n':
            if nxt != "\n":
                out.append(nxt)
            i += 2
            continue
        if c in '"`':
            return None
        if c == "$" and nxt and (nxt.isalnum() or nxt in "_{(@*#?$!-"):
            return None
        out.append(c)
        i += 1
    return "".join(out)


def _psql_meta_lines(sql: str) -> list[str]:
    """Each meta-command line psql would run: the text from an unquoted
    backslash to end of line. Raises ``_Ambiguous`` on an unterminated
    quote, comment or dollar body."""
    metas: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c == "\\":
            j = sql.find("\n", i)
            j = n if j < 0 else j
            metas.append(sql[i:j])
            i = j
        elif sql.startswith("--", i):
            j = sql.find("\n", i)
            i = n if j < 0 else j
        elif sql.startswith("/*", i):
            depth, i = 1, i + 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth, i = depth + 1, i + 2
                elif sql.startswith("*/", i):
                    depth, i = depth - 1, i + 2
                else:
                    i += 1
            if depth:
                raise _Ambiguous("unterminated comment")
        elif c in "'\"":
            esc = (
                c == "'"
                and i > 0
                and sql[i - 1] in "Ee"
                and not (i > 1 and (sql[i - 2].isalnum() or sql[i - 2] == "_"))
            )
            j = i + 1
            while True:
                if j >= n:
                    raise _Ambiguous("unterminated quote")
                if esc and sql[j] == "\\":
                    j += 2
                    continue
                if sql[j] == c:
                    if sql.startswith(c, j + 1):
                        j += 2
                        continue
                    break
                j += 1
            i = j + 1
        elif (
            c == "$"
            and (tag := _DOLLAR_TAG.match(sql, i))
            and not (i and (sql[i - 1].isalnum() or sql[i - 1] == "_"))
        ):
            j = sql.find(tag.group(0), tag.end())
            if j < 0:
                raise _Ambiguous("unterminated dollar quote")
            i = j + len(tag.group(0))
        else:
            i += 1
    return metas


def _ro_meta_danger(sql: str) -> str | None:
    """Why a ``--ro`` SQL text would act outside the read-only transaction,
    or None when every meta-command in it is harmless."""
    try:
        metas = _psql_meta_lines(sql)
    except _Ambiguous as exc:
        return f"ambiguous SQL ({exc})"
    for line in metas:
        if "`" in line:
            return "backtick in a psql meta-command runs a shell"
        # Arguments end at the next backslash, which starts another
        # meta-command (``\x \! id`` runs both), so judge every one.
        for part in line.split("\\")[1:]:
            name, _, arg = part.strip().partition(" ")
            name, arg = name.lower(), arg.strip()
            if name in _RO_META_ASK:
                return f"\\{name} acts on the node outside the read-only transaction"
            if name in _RO_META_TARGET and arg:
                return f"\\{name} {arg.split()[0]} writes a node file or pipe"
            if name == "copy" and not _copy_to_stdout(arg):
                return "\\copy to or from a node file or program"
    return None


def _classify(sql: str, *, strip: bool = True) -> tuple[str, str]:
    """Judge ``sql`` by its most dangerous statement: ``(kind, why)`` with
    kind ask / unknown / write / read. ``strip=False`` judges raw text (shell
    snippets), where a quoted keyword still counts."""
    if "\\" in sql:  # psql meta-command: \! \i \o \gexec run on the node
        sql, ok = _strip_meta(sql)
        if not ok:
            return "ask", "psql backslash meta-command"
        if not sql.strip():
            return "read", ""
    try:
        text = _blank(sql) if strip else sql
    except _Ambiguous as exc:
        return "ask", f"ambiguous SQL ({exc})"
    verdicts: list[tuple[str, str]] = []
    cur: list[str] = []
    for tok in [*_TOKEN.findall(text.upper()), ";"]:
        if tok == ";":
            if cur:
                verdicts.append(_statement_verdict(cur))
            cur = []
        else:
            cur.append(tok)
    # One write per call (.claude/fleet/authority.md): a second statement is
    # where an unreviewed change rides along with the targeted one.
    if len(verdicts) > 1 and any(kind != "read" for kind, _ in verdicts):
        if not any(kind == "ask" for kind, _ in verdicts):
            return "ask", "more than one statement with a write"
    for want in ("ask", "unknown", "write", "read"):
        for kind, why in verdicts:
            if kind == want:
                return kind, why
    return "unknown", "empty"


#: A heredoc opener (``<<EOF``, ``<<-'EOF'``, ``<<"EOF"``); its body is data.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

#: The prod-psql executable as a command word (bare or by path).
_PROD_PSQL_WORD = re.compile(r"^(?:\S*/)?scripts/prod-psql$")

#: An env assignment prefix (``FOO=bar``) before the command word.
_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=\S*$")


def _strip_heredocs(command: str) -> str:
    """Drop every heredoc BODY (the opener line stays). A heredoc is data —
    a design note appended with ``cat >> note <<'EOF'`` that merely mentions
    ``scripts/prod-psql`` or the word DELETE is not a prod write."""
    lines = command.split("\n")
    out: list[str] = []
    pending: list[str] = []
    for line in lines:
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            continue
        out.append(line)
        pending.extend(m.group(2) for m in _HEREDOC.finditer(line))
    return "\n".join(out)


def _segments(command: str) -> list[tuple[str, str]]:
    """Split on ``&&``, ``||``, ``;``, ``|`` and newlines OUTSIDE quotes.
    Returns ``(text, joiner_before)`` pairs so a pipe into prod-psql is
    recognisable."""
    segs: list[tuple[str, str]] = []
    buf: list[str] = []
    quote = ""
    joiner = ""
    i = 0
    while i < len(command):
        c = command[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = ""
            elif c == "\\" and quote == '"' and i + 1 < len(command):
                buf.append(command[i + 1])
                i += 1
        elif c in "'\"":
            quote = c
            buf.append(c)
        elif command.startswith(("&&", "||"), i):
            segs.append(("".join(buf), joiner))
            buf, joiner = [], command[i : i + 2]
            i += 1
        elif c in ";|\n":
            segs.append(("".join(buf), joiner))
            buf, joiner = [], c
        else:
            buf.append(c)
        i += 1
    segs.append(("".join(buf), joiner))
    return [(t, j) for t, j in segs if t.strip()]


def _invocation(segment: str) -> tuple[bool, str] | None:
    """``(ro, args_text)`` when ``segment`` RUNS prod-psql — its command word,
    after any ``FOO=bar`` prefixes, is scripts/prod-psql. ``None`` when the
    name only appears as an argument (a grep pattern, a commit message)."""
    words = segment.strip().split(None, 1)
    while words and _ENV_ASSIGN.match(words[0]):
        words = words[1].split(None, 1) if len(words) > 1 else []
    if not words or not _PROD_PSQL_WORD.match(words[0]):
        return None
    rest = words[1] if len(words) > 1 else ""
    ro = False
    if re.match(
        r"--ro(?:[ \t\n]|$)", rest
    ):  # exact word, as scripts/prod-psql tests $1
        ro, rest = True, rest[len("--ro") :].lstrip()
    return ro, rest


#: gr489447: a read over refs/ref_tags/chunks with a ref_id or
#: status/meta predicate re-implements an MCP view — 239 of 1331
#: prod-psql calls in 7 days did exactly this. The query still runs;
#: we attach a one-line hint naming the MCP equivalent.
_MCP_TABLE_RE = re.compile(r"\b(REFS|REF_TAGS|CHUNKS)\b")
_MCP_PRED_RE = re.compile(r"\b(REF_ID|STATUS|META)\b")


def _mcp_read_hint(sql: str) -> str | None:
    """The MCP pointer for a ref-meta/status read, or None."""
    try:
        text = _blank(sql)
    except _Ambiguous:
        return None
    if _MCP_TABLE_RE.search(text.upper()) and _MCP_PRED_RE.search(text.upper()):
        return (
            "hint: refs meta/status/tag reads are covered by the MCP — "
            "get(kind, id, view='meta'|'tags'), search(kind=K, view='count'), "
            "get(kind, id, q='...'); prefer those over prod-psql"
        )
    return None


def _ask(why: str) -> dict[str, str]:
    return {
        "decision": "ask",
        "reason": (
            f"`scripts/prod-psql` destructive or unclassifiable statement ({why}) "
            "targets **PRODUCTION** (`precis_prod`, agent_rw). Prefer `--ro` for "
            "reads; proceed only for a deliberate destructive prod write."
        ),
    }


def evaluate(command: str) -> dict[str, str] | None:
    """Return a ``{decision, reason}`` dict, or ``None`` for no opinion.

    Pure & testable — no I/O, no env reads (the ALLOW_PROD_WRITE escape hatch is
    applied by ``main`` so the allow can't be switched off).

    Only a segment that RUNS prod-psql counts, heredoc bodies are ignored, and
    the SQL is judged only from what prod-psql receives (its SQL argument, or
    the pipeline feeding its stdin).
    """
    if "prod-psql" not in command:
        return None
    segs = _segments(_strip_heredocs(command))
    calls = [(n, inv) for n, (t, _j) in enumerate(segs) if (inv := _invocation(t))]
    if not calls:
        return None

    # Bash splits words on space/tab/newline only; Python's \s (and str.split)
    # also split on CR/VT/FF, so such a byte could make us read a different
    # argv than bash runs. Never trust a command carrying one.
    if any(c in command for c in "\r\x0b\x0c"):
        return _ask("CR/VT/FF control character in the command")

    # An env prefix (PRECIS_PROD_PSQL_OPTS, PRECIS_PROD_SSH_HOST) injects
    # psql flags / redirects the host: more than the SQL we judge, so it is
    # never auto-allowed -- not even --ro or a read.
    bare = bool(_PROD_PSQL_WORD.match(segs[0][0].split(None, 1)[0]))
    if len(segs) == 1 and bare and calls[0][1][0]:
        sql_ro = _bash_quoted_arg(calls[0][1][1])
        if sql_ro is not None:
            # agent_ro inside BEGIN READ ONLY: the server refuses any write.
            # Only meta-commands acting on the node itself are judged.
            if danger := _ro_meta_danger(sql_ro):
                return _ask(danger)
            out = {
                "decision": "allow",
                "reason": "prod-psql --ro (agent_ro, read-only transaction) — auto-approved",
            }
            if hint := _mcp_read_hint(sql_ro):
                out["context"] = hint
            return out
    if len(segs) == 1 and bare:
        ro, args = calls[0][1]
        m = re.fullmatch(r"\s*(\"[^\"$`\\]*\"|'[^']*')\s*", args)
        if m and not ro:
            sql = m.group(1)[1:-1]
            kind, why = _classify(sql)
            if kind == "ask":
                return _ask(why)
            if kind == "read":
                out = {
                    "decision": "allow",
                    "reason": "read-only prod-psql probe (SELECT/EXPLAIN/backslash) — auto-approved",
                }
                if hint := _mcp_read_hint(sql):
                    out["context"] = hint
                return out
            if kind == "write":
                return {
                    "decision": "allow",
                    "reason": (
                        "targeted prod write (one statement; UPDATE/DELETE carry a "
                        "WHERE, prod-psql rolls back over the row cap) — "
                        "auto-approved per .claude/fleet/authority.md"
                    ),
                }

    # Not a provable lone call: ask only if what prod-psql actually receives
    # is destructive or an unrecognised write; otherwise stay silent.
    for n, (ro, args) in calls:
        fed = args
        k = n
        while k > 0 and segs[k][1] == "|":  # stdin piped in from the left
            k -= 1
            fed += " " + segs[k][0]
        if ro:
            # The SQL is the leading quoted word; redirections after it
            # (`2>&1`) are the shell's business, judged by the normal prompt.
            lead = _LEAD_QUOTED.match(args)
            decoded = _bash_quoted_arg(lead.group(1)) if lead and k == n else None
            if decoded is not None:
                if danger := _ro_meta_danger(decoded):
                    return _ask(danger)
            elif _RO_RAW_DANGER.search(fed) or ("\\" in fed and "`" in fed):
                return _ask("psql meta-command that acts on the node")
            continue
        kind, why = _classify(fed, strip=False)
        if kind == "ask":
            return _ask(why)
        if (
            kind == "unknown"
            and _WRITE_KEYWORDS.search(fed)
            and not _READ_OPENER.search(fed)
        ):
            return _ask("write statement we cannot classify")
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command", "")
    if not isinstance(command, str):
        return 0
    result = evaluate(command)
    if result is None:
        return 0
    if result["decision"] == "ask" and os.environ.get("ALLOW_PROD_WRITE"):
        return 0  # escape hatch silences the ask, not the allow
    hook_out: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": result["decision"],
        "permissionDecisionReason": result["reason"],
    }
    if result.get("context"):
        hook_out["additionalContext"] = result["context"]
    print(json.dumps({"hookSpecificOutput": hook_out}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
