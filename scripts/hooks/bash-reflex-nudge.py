#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook (matcher: Bash) — nudge two reflex habits toward the
purpose-built tool.

``coderef-nudge.py`` only ever sees the native ``Grep`` tool, but a symbol
search run via ``Bash`` (``rg``/``grep``/``egrep``) is invisible to it — and
in practice that's most of the traffic. This hook fills that gap plus a
second, unrelated reflex: cluster ops (``ssh melchior|caspar|balthazar``,
``scripts/prod-psql``) run inline on the main loop instead of delegated to
the cheaper ``cluster-ops``/``cluster-admin`` agents.

Both rules are NON-BLOCKING (``additionalContext`` only, exit 0) and
deliberately narrow — a nudge that fires too often gets tuned out, same
rationale as ``coderef-nudge.py``'s own docstring. When extraction is
ambiguous, both rules stay silent rather than guess.

- **Rule A (coderef).** An ``rg``/``grep``/``egrep`` invocation (optionally
  after a single leading ``cd <path> &&``) whose search pattern is a bare
  Python identifier — same ``is_symbol_candidate`` heuristic as
  ``coderef-nudge.py`` (shared via ``_symbol_heuristic.py``), plus a light
  Python-target check (an explicit non-Python ``--include``/``--type``/``-g``
  glob silences it; unscoped defaults to code-ish, mirroring the Grep hook).
- **Rule B (cluster-ops).** The command starts with (or contains, after a
  leading ``cd``) ``ssh melchior|caspar|balthazar`` or invokes
  ``scripts/prod-psql`` — nudges toward the read-only ``cluster-ops`` agent
  (or ``cluster-admin`` for a documented write/runbook step) so raw log/psql
  dumps land in the sub-agent's cheap context, not the caller's.

- **Rule D (sed -n slice).** A plain ``sed -n 'X,Yp' <file>`` (or ``'Np'``,
  ``'X,Y p'``) reading one file — no pipe, ``&&``, ``;`` or redirect — is
  the ``Read`` tool's ``offset``/``limit`` done by hand, outside every
  compression/nudge path. Nudges toward ``Read``. Silent on pipelines (sed
  filtering another command's output) and on stdin reads.

- **Rule E (unquoted glob).** A glob passed to a glob-valued flag
  (``--include=``, ``--exclude=``, ``-g``/``--glob``, ``-name``/``-iname``)
  without quotes is expanded by ZSH against the cwd BEFORE the command
  runs — a non-match aborts the whole line with ``no matches found``, so
  the grep never executed (gr514326). Nudges toward the quoted form.

- **Dedup delegate.** Before its own rules run, this hook hands the
  payload to ``read-dedup.py``'s ``hook_output`` — the same script the
  Read matcher runs. That is how whole-file Bash serves (``cat``,
  ``sed -n`` slices, ``head``/``tail``, ``git show``) are deduped and
  >20 KB serves are redirected to Read *without a second settings entry*:
  matchers can't be extended from the tree. A dedup verdict (a deny, the
  only kind it emits on the Bash side) is printed instead of a nudge; a
  delegation/import failure falls through to the rules unchanged.

Wired in .claude/settings.json (PreToolUse, matcher "Bash").
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import shlex
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _symbol_heuristic import is_symbol_candidate, path_arg_is_pythonish


def _load_read_dedup():
    """Sibling ``read-dedup.py`` as a module; None when missing/broken."""
    try:
        spec = importlib.util.spec_from_file_location(
            "read_dedup_hook",
            Path(__file__).resolve().with_name("read-dedup.py"),
        )
        if spec is None or spec.loader is None:
            return None
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception:
        return None


_READ_DEDUP = _load_read_dedup()

# A single leading `cd <path> && ` prefix (the one case the spec calls out;
# deeper segment-walking is deliberately out of scope — stay silent instead).
_CD_PREFIX = re.compile(r"^\s*cd\s+\S+\s*&&\s*")

# The grep-family invocation itself, optionally through a full path and/or
# preceded by env-var assignments (`FOO=bar rg ...`).
_GREP_CMD = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:\S*/)?(?:rg|grep|egrep)\b(?P<rest>.*)$"
)

# Explicit non-Python scoping flags: `--include=GLOB`, `-g GLOB` (rg),
# `--type=TYPE` / `-t TYPE` (rg).
_INCLUDE_RE = re.compile(r"(?:--include=|-g\s+)(\S+)")
_TYPE_RE = re.compile(r"(?:--type=|-t\s+)(\S+)")

_SHELL_BREAK = {"|", "||", "&&", ";"}

_SSH_RE = re.compile(r"\bssh\s+(?:-\S+\s+)*(melchior|caspar|balthazar)\b")

# A leading ``cd <path>`` segment for the redundant-own-tree check. Tolerates a
# trailing redirect (``2>/dev/null``) and either ``;`` or ``&&`` separator — the
# exact shapes the audit found the stricter deny-guard regex missing.
_CD_LEAD = re.compile(
    r"""^\s*cd\s+(['\"]?)(?P<path>[^'\"]+?)\1\s*(?:\d*[<>&]+\s*\S+\s*)*(?:;|&&)"""
)


def _grep_target_ok(rest: str) -> bool:
    """True unless the search is explicitly scoped away from Python."""
    m = _INCLUDE_RE.search(rest)
    if m:
        return "py" in m.group(1).lower()
    m = _TYPE_RE.search(rest)
    if m:
        return m.group(1).lower() in ("py", "python")
    return True  # unscoped -> treat as code-ish, mirrors coderef-nudge.py


def _grep_pattern(rest: str) -> str | None:
    """First non-flag token after the grep/rg verb, or None if unclear."""
    try:
        tokens = shlex.split(rest)
    except ValueError:
        return None
    for tok in tokens:
        if tok in _SHELL_BREAK:
            return None  # pattern would live in a later segment - bail
        if tok.startswith("-"):
            continue  # a flag (e.g. -e, -i, -n, --include=...)
        return tok
    return None


def _rule_a(command: str, cwd: str = "") -> str | None:
    body = _CD_PREFIX.sub("", command, count=1)
    m = _GREP_CMD.match(body)
    if not m:
        return None
    rest = m.group("rest")
    if not _grep_target_ok(rest):
        return None
    pattern = _grep_pattern(rest)
    if pattern is None:
        return None
    try:
        positional = [
            t
            for t in shlex.split(rest)
            if t not in _SHELL_BREAK and not t.startswith("-")
        ]
    except ValueError:
        positional = []
    # Every positional after the pattern is a search TARGET; one that is
    # clearly non-Python (a ``.sql`` file, a migrations dir of pure SQL)
    # silences the nudge — coderef only answers over Python (gr514328).
    for arg in positional[1:]:
        if path_arg_is_pythonish(arg, cwd) is False:
            return None
    tok = pattern.strip("\"'")
    if not is_symbol_candidate(tok):
        return None
    return (
        f"[coderef] grepping (via Bash) for the symbol `{tok}` — for "
        f"who-calls / what-depends-on over Python, `scripts/coderef callers "
        f"<file.py::{tok}>` (or `deps`) is exact: no same-named false "
        "positives, and it returns the connected code, not every text hit. "
        "`search(kind='python', mode='pattern', q='<qualname regex>')` is the "
        "fuzzy complement for where-is/how-does. Grep "
        "stays right for text/strings/non-Python or a symbol you can't yet name."
    )


def _rule_b(command: str) -> str | None:
    body = _CD_PREFIX.sub("", command, count=1)
    if _SSH_RE.search(body):
        host = _SSH_RE.search(body).group(1)  # type: ignore[union-attr]
        return (
            f"[cluster-ops] ssh to `{host}` inline burns main-loop context on "
            "raw log/journal output. For a read-only check, the `cluster-ops` "
            "agent (haiku) SSHes, reads, and returns a short digest instead; "
            "for a documented write/runbook step, `cluster-admin` (sonnet)."
        )
    if "scripts/prod-psql" in body:
        return (
            "[cluster-ops] `scripts/prod-psql` inline pulls raw prod rows into "
            "the main-loop context. The `cluster-ops` agent (haiku) runs the "
            "same read-only query and returns a short digest instead."
        )
    return None


# Glob-valued flags whose argument must reach the TOOL, not zsh. An
# unquoted ``*.sql`` here is expanded against the cwd first; a non-match
# aborts the line with ``no matches found`` (gr514326).
_GLOB_FLAG_ARG = re.compile(
    r"(?<![-\w])(?P<flag>--(?:include|exclude|glob|iglob)|-[g]|--?i?name)"
    r"(?P<sep>=|\s+)(?P<q>['\"]?)(?P<val>[^\s'\"|&;]*[*?\[][^\s'\"|&;]*)"
)


def _rule_e(command: str) -> str | None:
    """Nudge an UNQUOTED glob flag-arg toward the quoted form."""
    for m in _GLOB_FLAG_ARG.finditer(command):
        if m.group("q"):
            continue  # quoted — the tool gets the glob as intended
        flag, sep, val = m.group("flag"), m.group("sep"), m.group("val")
        quoted = f"{flag}='{val}'" if sep == "=" else f"{flag} '{val}'"
        return (
            f"[zsh] `{flag}{sep}{val}` is an unquoted glob — zsh expands it "
            "against the cwd BEFORE the command runs, and a non-match aborts "
            "the whole line with `no matches found` (the grep never ran). "
            f"Quote it: `{quoted}`."
        )
    return None


# ``sed -n '12,40p' path`` / ``sed -n "12,40 p" path`` / ``sed -n 12p path``;
# the file operand must be a single plain token (no shell metacharacters).
_SED_SLICE = re.compile(
    r"""^\s*sed\s+-n\s+(?P<q>['"]?)(?P<a>\d+)(?:,(?P<b>\d+))?\s*p(?P=q)\s+"""
    r"""(?P<file>[^\s|&;<>$`()'"*?]+)\s*$"""
)


def _rule_d(command: str) -> str | None:
    m = _SED_SLICE.match(_CD_PREFIX.sub("", command, count=1))
    if not m:
        return None
    a = int(m.group("a"))
    b = int(m.group("b") or a)
    if b < a:
        return None
    return (
        f"[read] `sed -n '{a},{b}p' {m.group('file')}` is a hand-rolled Read "
        f"slice — use the Read tool with `offset={a}, limit={b - a + 1}` "
        "instead: same bytes, but line-numbered, and the harness tracks the "
        "file state so re-slicing the same file is visible and edits stay valid."
    )


def _rule_c(command: str, cwd: str) -> str | None:
    """Nudge a redundant ``cd <own-worktree>; …`` prefix.

    cwd is already this worktree and the harness re-anchors it there after every
    Bash call, so a leading ``cd`` into the current tree is pure token waste on
    every command. Non-blocking (the deny-guard only ever fires on *cross*-tree
    cd; this catches the far-more-common same-tree redundancy the prose misses).
    """
    if not cwd:
        return None
    m = _CD_LEAD.match(command)
    if not m:
        return None
    raw = m.group("path").strip()
    tgt = os.path.normpath(os.path.expanduser(raw))
    if not os.path.isabs(tgt):
        return None
    cwd_n = os.path.normpath(cwd)
    if tgt == cwd_n or tgt.startswith(cwd_n + os.sep):
        return (
            "[cwd] the shell is already in this worktree and the harness "
            "re-anchors cwd here after every Bash call — the leading "
            f"`cd {raw};` is redundant on every command. Run bare; to read "
            "another tree use `git -C <path> …`."
        )
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    command = (payload.get("tool_input") or {}).get("command")
    if not isinstance(command, str):
        return 0
    cwd = payload.get("cwd") or ""

    out = None
    if _READ_DEDUP is not None:
        try:
            out = _READ_DEDUP.hook_output(payload)
        except Exception:
            out = None
    if out is None:
        note = (
            _rule_e(command)
            or _rule_a(command, cwd)
            or _rule_b(command)
            or _rule_c(command, cwd)
            or _rule_d(command)
        )
        if note is not None:
            out = {"additionalContext": note}
    if out is None:
        return 0
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", **out}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
