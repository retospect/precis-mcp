#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: decide ``scripts/prod-psql`` calls by what the SQL does.

``prod-psql`` connects as **write-capable `agent_rw`**. Reto's rulings
(2026-10-01 "default is go", 2026-10-11 gr462727): approved prod writes must
not stall on a prompt; only DESTRUCTIVE ones ask. This hook is the single
decision point, keyed off the SQL:

- A **lone** `scripts/prod-psql --ro "<any SQL>"` -> **allow**: agent_ro inside
  BEGIN READ ONLY, the server refuses writes.
- A **lone** `scripts/prod-psql "<sql>"` (one quoted SQL argument, no shell
  chaining, no ``$``/backtick/backslash interpolation, no ``VAR=`` env
  prefix at all) is parsed statement by statement and judged by its MOST DANGEROUS
  statement:
  * any destructive statement -> **ask** (below);
  * otherwise any write (INSERT, UPDATE ... WHERE, CREATE, REFRESH, nextval, ...) -> **allow**, reason naming a non-destructive prod
    write;
  * only reads (SELECT/EXPLAIN/WITH/SHOW/TABLE/VALUES, and the line-start
    meta-commands ``\\d \\dt \\di \\l \\x``) -> **allow**;
  * a verb we do not know -> ask if it is a known write keyword, else no
    opinion.
- ``--ro`` must be its own word (``--ro"..."`` is not it: the script tests
  ``$1 == --ro``). Any other psql backslash meta-command (``\\!``, ``\\i``,
  ``\\o``, ``\\gexec``, ``\\g``, ``\\set``, ...) **asks**, ``--ro`` included:
  they run on the node whatever the role.
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

- ``DELETE`` (always, with or without WHERE; also inside a CTE), ``DROP``
  (so ``ALTER ... DROP`` too), ``TRUNCATE``;
- an ``UPDATE`` with no ``WHERE`` at its own parenthesis depth;
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
every row (``WHERE true``, ``1=1``) or any huge bulk UPDATE; whether a table
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
    "INSERT", "UPDATE", "CREATE", "ALTER", "REFRESH", "COMMENT", "REINDEX",
    "VACUUM", "ANALYZE", "BEGIN", "COMMIT", "ROLLBACK", "END",
}  # fmt: skip
#: Tokens that make a SELECT/WITH/EXPLAIN statement a write.
_WRITE_TOKENS = {
    "INSERT", "UPDATE", "CREATE", "ALTER", "REFRESH", "COMMENT", "REINDEX",
    "VACUUM", "NEXTVAL", "SETVAL", "INTO",
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


def _statement_verdict(toks: list[str]) -> tuple[str, str]:
    """``(kind, why)`` for one statement; kind is ask / write / read / unknown."""
    for t in toks:
        if t in ("DELETE", "DROP", "TRUNCATE"):
            return "ask", f"{t} is destructive"
    first = toks[0] if toks else ""
    if first in _FORCE_ASK_FIRST:
        return "ask", f"{first} can hide or widen a statement"
    for t in toks:
        if t in _FORCE_ASK_ANY:
            return "ask", f"{t} cannot be judged statically"
    # Every ALTER asks (SET SCHEMA, DETACH, DISABLE TRIGGER, TYPE, RENAME, ADD
    # COLUMN, ... are all schema changes thresholds.md says to confirm).
    if "ALTER" in toks:
        return "ask", "ALTER (thresholds.md schema change)"
    for k, t in enumerate(toks):
        if t == "OR" and toks[k + 1 : k + 2] == ["REPLACE"] and "CREATE" in toks:
            return "ask", "CREATE OR REPLACE rewrites an existing object"
        if t == "FOR" and toks[k + 1 : k + 2] and toks[k + 1] in _LOCK_AFTER_FOR:
            return "ask", "FOR UPDATE/SHARE takes row locks"
        if _SIDE_EFFECT_FN.fullmatch(t):
            return "ask", f"{t} has server-side effects"
    if "VACUUM" in toks and "FULL" in toks:
        return "ask", "VACUUM FULL rewrites the table"
    # An UPDATE statement (not FOR UPDATE / ON UPDATE / DO UPDATE) needs a
    # WHERE at its own parenthesis depth, before that depth closes.
    depths: list[int] = []
    d = 0
    for t in toks:
        d += (t == "(") - (t == ")")
        depths.append(d)
    is_update = False
    for k, t in enumerate(toks):
        if t != "UPDATE" or (k and toks[k - 1] in ("FOR", "KEY", "ON", "DO", "OF")):
            continue
        is_update = True
        found = False
        for m in range(k + 1, len(toks)):
            if depths[m] < depths[k]:
                break
            if toks[m] == "WHERE" and depths[m] == depths[k]:
                found = True
                break
        if not found:
            return "ask", "UPDATE without WHERE"
    if first in _READ_VERBS or first == "(":
        if is_update or _WRITE_TOKENS & set(toks):
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
    if len(segs) == 1 and bare:
        ro, args = calls[0][1]
        m = re.fullmatch(r"\s*(\"[^\"$`\\]*\"|'[^']*')\s*", args)
        if m:
            sql = m.group(1)[1:-1]
            if ro:
                # agent_ro inside BEGIN READ ONLY: the server refuses any
                # write. Backslash meta-commands (\!, \i) still run on the
                # node, so those are judged even here.
                if _strip_meta(sql)[1]:
                    return {
                        "decision": "allow",
                        "reason": "prod-psql --ro (agent_ro, read-only transaction) — auto-approved",
                    }
                return _ask("psql backslash meta-command")
            kind, why = _classify(sql)
            if kind == "ask":
                return _ask(why)
            if kind == "read":
                return {
                    "decision": "allow",
                    "reason": "read-only prod-psql probe (SELECT/EXPLAIN/backslash) — auto-approved",
                }
            if kind == "write":
                return {
                    "decision": "allow",
                    "reason": (
                        "non-destructive prod write (no DELETE/DROP/TRUNCATE, every "
                        "UPDATE has a WHERE) — auto-approved per thresholds.md"
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
            if not _strip_meta(fed)[1]:
                return _ask("psql backslash meta-command")
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
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": result["decision"],
                    "permissionDecisionReason": result["reason"],
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
