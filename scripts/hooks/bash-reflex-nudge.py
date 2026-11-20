#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook (matcher: Bash) — nudge two reflex habits toward the
purpose-built tool.

``coderef-nudge.py`` only ever sees the native ``Grep`` tool, but a symbol
search run via ``Bash`` (``rg``/``grep``/``egrep``) is invisible to it — and
in practice that's most of the traffic. This hook fills that gap plus a
second, unrelated reflex: cluster ops (``ssh <cluster node>``,
``scripts/prod-psql``) run inline on the main loop instead of delegated to
the cheaper ``cluster-ops``/``cluster-admin`` agents.

The nudge rules are NON-BLOCKING (``additionalContext`` only, exit 0) and
deliberately narrow — a nudge that fires too often gets tuned out, same
rationale as ``coderef-nudge.py``'s own docstring. When extraction is
ambiguous, they stay silent rather than guess. Two shell traps are DENIED
instead (``permissionDecision: deny``, the shape ``read-dedup.py`` and the
``guard-*`` hooks emit), because the command can only fail (gr570091):
Rule E's unquoted glob, and a bare ``timeout`` on macOS.

- **Rule A (python kind).** An ``rg``/``grep``/``egrep`` invocation
  (optionally after a single leading ``cd <path> &&``) whose search pattern
  is a bare Python identifier — same ``is_symbol_candidate`` heuristic as
  ``coderef-nudge.py`` (shared via ``_symbol_heuristic.py``), plus a light
  Python-target check (an explicit non-Python ``--include``/``--type``/``-g``
  glob silences it; unscoped defaults to code-ish, mirroring the Grep hook;
  a target outside the session tree or inside a venv/site-packages silences
  it too — the index can't see those, gr520146). The nudge names the right
  tool per intent: ``search(kind='python', mode='lexical'|'semantic')`` for
  where/concept, ``mode='literal'`` for a string VALUE, ``coderef callers``
  for who-calls. On a src-scoped grep it also runs the bounded lookup
  itself (``_pykind_hits.py`` → ``scripts/coderef find``, hard 1s) and
  appends the top 3 ``module::qualname  path:line`` hits — silent fallback
  to the plain nudge on any failure (gr520146, comment 4).

- **Rule F (grep -c).** A GNU ``grep``/``egrep``/``fgrep`` run with a count
  flag (``-c``/``--count``) across a directory — recursive flag or a
  directory target — prints one line per file touched, zero-counts
  included. ``grep -rl``/``rg -l`` prints only matching files. ``rg`` is
  excluded (``rg -c`` already skips zeros); single-file counts stay silent.

- **Rule B (cluster-ops).** The command starts with (or contains, after a
  leading ``cd``) ``ssh <cluster node>`` (node names come from the
  gitignored cluster overlay at run time; no overlay, no ssh nudge) or
  invokes ``scripts/prod-psql`` — nudges toward the read-only ``cluster-ops`` agent
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
  On top of that note, ``_glob_deny`` DENIES (message carries the quoted
  form) when a tokenizer that understands quotes/escapes proves zsh would
  abort: an unquoted word argument with a ``*``/``?``/``[..]`` that matches
  nothing in the payload cwd (a leading ``-`` flag word needs no cwd). Any
  doubt leaves today's note in place: ``$(...)``/backticks/``${``, ``(``/``)``
  (case patterns, glob qualifiers, subshells), ``[[``, ``case``/``select``,
  ``setopt``/``noglob``, heredocs, comments, ``cd`` before a path glob,
  ``$var``/brace/``**`` words, assignment words (``X=*.py``), redirect
  targets, and ``for``/``test``/``[``/``export``-family commands.

- **Rule T (timeout on macOS).** ``timeout`` is not installed on macOS (26
  ``command not found`` in 14 days, gr570091). A ``timeout <N> cmd`` at
  command position is denied with the alternatives, unless ``timeout`` is on
  PATH (coreutils gnubin) or the platform is not Darwin.

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

import glob
import importlib.util
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _pykind_hits import pykind_hits, src_scoped
from _symbol_heuristic import (
    is_symbol_candidate,
    path_arg_is_pythonish,
    path_leaves_project,
)


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

_BARE_KEY = re.compile(r"^\s*([A-Za-z0-9_.-]+):\s*$")
_ANSIBLE_HOST = re.compile(r"^\s*ansible_host:\s*\S")


def _overlay_path() -> Path | None:
    """The gitignored cluster overlay, resolved like scripts/lib/pgb-host.sh:
    $CLUSTER_INVENTORY, else this checkout's copy, else the main checkout's
    (the overlay exists only there when running from a worktree)."""
    env = os.environ.get("CLUSTER_INVENTORY")
    if env:
        return Path(env)
    repo = Path(__file__).resolve().parents[2]
    own = repo / "deploy" / "inventory" / "hosts.yml"
    if own.is_file():
        return own
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "--git-common-dir"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=2,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    common = proc.stdout.strip()
    if proc.returncode != 0 or not common:
        return None
    common_path = Path(common)
    if not common_path.is_absolute():
        common_path = repo / common_path
    main = (common_path / "..").resolve() / "deploy" / "inventory" / "hosts.yml"
    return main if main.is_file() else None


def _cluster_hosts() -> list[str]:
    """Host names from the overlay: each bare ``<name>:`` key whose block
    carries an ``ansible_host:`` (the shape list_cluster_hosts reads). Empty
    when there is no readable overlay — the ssh rule then stays silent."""
    path = _overlay_path()
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    hosts: list[str] = []
    key = ""
    for line in lines:
        m = _BARE_KEY.match(line)
        if m:
            key = m.group(1)
        elif key and _ANSIBLE_HOST.match(line):
            if key not in hosts:
                hosts.append(key)
            key = ""
    return hosts


def _ssh_match(body: str) -> re.Match[str] | None:
    """An ``ssh [flags] <overlay host>`` invocation, or None (fail open)."""
    if "ssh" not in body:
        return None
    hosts = _cluster_hosts()
    if not hosts:
        return None
    alt = "|".join(re.escape(h) for h in hosts)
    return re.search(rf"\bssh\s+(?:-\S+\s+)*({alt})\b", body)


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
        if path_leaves_project(arg, cwd):
            return None
        if path_arg_is_pythonish(arg, cwd) is False:
            return None
    tok = pattern.strip("\"'")
    if not is_symbol_candidate(tok):
        return None
    note = (
        f"[pykind] grepping (via Bash) for the symbol `{tok}` — the "
        "python kind usually answers in one call: "
        f"search(kind='python', mode='lexical'|'semantic', q='{tok}') "
        f"finds where it lives, mode='literal' finds every use of "
        f"'{tok}' as a string VALUE, and `scripts/coderef callers "
        f"<file.py::{tok}>` is the exact who-calls once it's named. "
        "Grep stays right for text, non-Python files and targets "
        "outside this repo."
    )
    # gr520146 ruling (comment 4): on a src-scoped symbol/concept grep the
    # hook runs the lexical lookup itself and appends the top hits — the
    # agent sees the answer, not just the pointer. Bounded inside
    # pykind_hits; a miss/timeout leaves the plain nudge.
    if src_scoped(positional[1:], cwd):
        try:
            hits = pykind_hits(tok, cwd)
        except Exception:
            hits = []
        if hits:
            note += (
                "\nTop hits (the python kind's lexical answer, already run):\n"
                + "\n".join(f"  {h}" for h in hits)
            )
    return note


def _rule_b(command: str) -> str | None:
    body = _CD_PREFIX.sub("", command, count=1)
    ssh = _ssh_match(body)
    if ssh:
        host = ssh.group(1)
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
    r"(?P<sep>=|\s+)(?P<q>['\"]?)(?P<val>[^\s'\"|&;\\]*[*?\[][^\s'\"|&;]*)"
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


# ── shell tokenizer for the deny rules ──────────────────────────────────────
# Just enough of a shell lexer to tell a quoted glob from an unquoted one and
# to find command position. Anything it cannot place with certainty makes it
# bail (``ok=False``) and the deny rules stay out of the way (gr570091).


class _Word:
    __slots__ = ("dyn", "glob", "raw", "redirect")

    def __init__(self, raw: str, glob: bool, dyn: bool, redirect: bool) -> None:
        self.raw = raw
        self.glob = glob  # unquoted * ? or [..] present
        self.dyn = dyn  # unquoted $var / brace / ~user: expansion unknown
        self.redirect = redirect  # target of < or >


def _lex(command: str) -> tuple[list[list[_Word]], bool]:
    """Split ``command`` into simple commands of words.

    Returns ``(commands, ok)``; ``ok`` is False when the lexer bailed on a
    construct it will not interpret (the commands parsed so far, including
    the words of the one in progress, are still returned).
    """
    cmds: list[list[_Word]] = []
    cur: list[_Word] = []
    n = len(command)
    i = 0
    start = -1
    glob_f = dyn_f = False
    redirect_next = False
    word_redirect = False

    def end_word(at: int) -> None:
        nonlocal start, glob_f, dyn_f, word_redirect
        if start < 0:
            return
        cur.append(_Word(command[start:at], glob_f, dyn_f, word_redirect))
        start = -1
        glob_f = dyn_f = False
        word_redirect = False

    def begin(at: int) -> None:
        nonlocal start, redirect_next, word_redirect
        if start < 0:
            start = at
            word_redirect = redirect_next
            redirect_next = False

    def end_cmd() -> None:
        nonlocal cur
        if cur:
            cmds.append(cur)
        cur = []

    def bail(at: int) -> tuple[list[list[_Word]], bool]:
        end_word(at)
        end_cmd()
        return cmds, False

    while i < n:
        ch = command[i]
        if ch in " \t":
            end_word(i)
            i += 1
        elif ch == "\n":
            end_word(i)
            end_cmd()
            i += 1
        elif ch == "\\":
            if i + 1 >= n:
                return bail(i)
            if command[i + 1] == "\n":
                i += 2
                continue
            begin(i)
            i += 2
        elif ch == "'":
            close = command.find("'", i + 1)
            if close < 0:
                return bail(i)
            begin(i)
            i = close + 1
        elif ch == '"':
            j = i + 1
            while j < n and command[j] != '"':
                if command[j] == "\\":
                    j += 2
                elif command[j] == "`" or command.startswith("$(", j):
                    return bail(i)
                else:
                    j += 1
            if j >= n:
                return bail(i)
            begin(i)
            i = j + 1
        elif ch == "$":
            nxt = command[i + 1 : i + 2]
            if nxt in ("(", "{", "'", "`"):
                return bail(i)
            begin(i)
            dyn_f = True
            i += 2 if nxt and (nxt in "?*@#!$-" or nxt.isdigit()) else 1
        elif ch in "`()" or (ch == "#" and start < 0):
            return bail(i)
        elif ch in "<>":
            if command.startswith("<<", i):
                return bail(i)
            end_word(i)
            redirect_next = True
            i += 1
            while i < n and command[i] in "<>&|":
                i += 1
        elif ch in ";&|":
            end_word(i)
            end_cmd()
            redirect_next = False
            i += 1
            while i < n and command[i] in ";&|":
                i += 1
        elif ch in "*?":
            begin(i)
            glob_f = True
            i += 1
        elif ch == "[":
            nxt = command[i + 1 : i + 2]
            if start < 0 and (nxt == "" or nxt in " \t\n"):
                begin(i)  # the `[` test builtin
                i += 1
                continue
            if not re.match(r"\[[^\s'\"\\\]]+\]", command[i:]):
                return bail(i)  # unclosed/odd bracket: maybe a zsh bad pattern
            begin(i)
            glob_f = True
            i += 1
        elif ch == "{":
            begin(i)
            if i + 1 < n and command[i + 1] not in " \t\n":
                dyn_f = True
            i += 1
        elif ch == "~" and start < 0 and not command.startswith("~/", i):
            begin(i)
            if i + 1 < n and command[i + 1] not in " \t\n;&|":
                dyn_f = True
            i += 1
        else:
            begin(i)
            i += 1
    end_word(n)
    end_cmd()
    return cmds, True


_ASSIGN_WORD = re.compile(r"^[A-Za-z_]\w*\+?=")
# Words that open a command but are not it: ``if x`` / ``do x`` / ``! x``.
_RESERVED_LEAD = {
    "if",
    "then",
    "elif",
    "else",
    "while",
    "until",
    "do",
    "!",
    "time",
    "{",
}
# A command with one of these as head is not checked for glob args.
_SKIP_HEADS = {
    "for",
    "test",
    "[",
    "export",
    "typeset",
    "declare",
    "local",
    "readonly",
    "integer",
    "float",
    "alias",
    "eval",
    "trap",
    "function",
}
# Their presence anywhere means the glob semantics are not the default.
_ABORT_HEADS = {"[[", "case", "select", "setopt", "unsetopt", "noglob", "coproc"}
_CD_HEADS = {"cd", "pushd", "popd", "chdir"}


def _head_index(cmd: list[_Word]) -> int | None:
    """Index of the command word (after assignments / ``if`` / ``do``)."""
    for idx, w in enumerate(cmd):
        if w.redirect or _ASSIGN_WORD.match(w.raw) or w.raw in _RESERVED_LEAD:
            continue
        return idx
    return None


def _quoted_form(raw: str) -> str:
    if raw.startswith("-") and "=" in raw:
        flag, _, val = raw.partition("=")
        return f"{flag}='{val}'"
    return f"'{raw}'"


def _glob_matches_nothing(raw: str, cwd: str) -> bool | None:
    """True/False when decidable, None when not (no usable cwd, ``**``...)."""
    if "**" in raw or any(c in raw for c in "'\"\\"):
        return None
    pattern = os.path.expanduser(raw) if raw.startswith("~/") else raw
    root = cwd if cwd and os.path.isdir(cwd) else None
    if root is None and not os.path.isabs(pattern):
        # No cwd to look in: a flag word (``--include=*.py``) cannot name a
        # file; a path glob could.
        return True if raw.startswith("-") else None
    try:
        return next(glob.iglob(pattern, root_dir=root), None) is None
    except (OSError, ValueError):
        return None


def _glob_deny(cmds: list[list[_Word]], cwd: str) -> str | None:
    """Deny reason when zsh would abort with ``no matches found``, else None."""
    has_cd = False
    candidates: list[_Word] = []
    for cmd in cmds:
        idx = _head_index(cmd)
        if idx is None:
            continue
        head = cmd[idx].raw
        if head in _ABORT_HEADS or any(w.raw == "[[" for w in cmd):
            return None
        if head in _CD_HEADS:
            has_cd = True
        if head in _SKIP_HEADS or head in _CD_HEADS:
            continue
        for w in cmd[idx + 1 :]:
            if w.glob and not (w.dyn or w.redirect or _ASSIGN_WORD.match(w.raw)):
                candidates.append(w)
    for w in candidates:
        if has_cd and not w.raw.startswith("-"):
            continue  # the shell's cwd is not the payload's after a cd
        if _glob_matches_nothing(w.raw, cwd):
            return (
                f"[zsh] `{w.raw}` is an unquoted glob that matches nothing "
                "here — zsh aborts the whole line with `no matches found` "
                "before the command runs, so nothing would execute. If the "
                "tool should receive the pattern (grep --include, find -name, "
                f"git ref patterns), quote it: `{_quoted_form(w.raw)}`. If you "
                "meant a file glob, check the path and cwd."
            )
    return None


def _on_macos() -> bool:
    return sys.platform == "darwin"


def _has_timeout() -> bool:
    return shutil.which("timeout") is not None


_TIMEOUT_WRAPPERS = {"env", "nohup", "command", "builtin"}


def _timeout_deny(cmds: list[list[_Word]]) -> str | None:
    """Deny a ``timeout <N> cmd`` at command position on macOS (no binary)."""
    if not _on_macos() or _has_timeout():
        return None
    for cmd in cmds:
        idx = _head_index(cmd)
        while idx is not None and cmd[idx].raw in _TIMEOUT_WRAPPERS:
            idx = idx + 1 if idx + 1 < len(cmd) else None
        if idx is None or cmd[idx].raw != "timeout" or idx + 1 >= len(cmd):
            continue
        rest = " ".join(w.raw for w in cmd[idx + 1 :])
        alt = (
            f"`gtimeout` (installed): `gtimeout {rest}`"
            if shutil.which("gtimeout")
            else "`gtimeout` from coreutils, if installed"
        )
        return (
            "[macos] `timeout` is not installed on macOS — the command would "
            "die with `command not found: timeout`. Run it with "
            "`run_in_background=true` and read the output when it finishes, "
            f"set the Bash tool's own `timeout` parameter, or use {alt}."
        )
    return None


def _precheck_deny(command: str, cwd: str) -> str | None:
    """Reason to deny the command before it runs, or None."""
    cmds, ok = _lex(command)
    reason = _timeout_deny(cmds)
    if reason is None and ok:
        reason = _glob_deny(cmds, cwd)
    return reason


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


# GNU grep/egrep/fgrep only — `rg -c` already omits zero-count files, only
# the grep family prints every touched path (gr520146).
_GREP_COUNT_CMD = re.compile(
    r"^\s*(?:[A-Za-z_][A-Za-z0-9_]*=\S+\s+)*"
    r"(?:\S*/)?(?:grep|egrep|fgrep)\b(?P<rest>.*)$"
)
_SHORT_FLAG = re.compile(r"^-[a-zA-Z]+$")


def _rule_f(command: str, cwd: str) -> str | None:
    """Nudge a recursive ``grep -c`` toward ``-l``.

    GNU ``grep -rc`` prints one line per file it touched — every
    zero-count included — so a corpus-wide count mostly prints noise
    (a coordinator's ``grep -c`` over the skills dir dumped ~300
    zero-count lines into context, gr520146). ``-l`` prints only the
    files that matched. Single-file counts stay silent; ``rg`` is
    excluded because ``rg -c`` already skips zeros.
    """
    body = _CD_PREFIX.sub("", command, count=1)
    m = _GREP_COUNT_CMD.match(body)
    if not m:
        return None
    try:
        tokens = shlex.split(m.group("rest"))
    except ValueError:
        return None
    short_flags = [t[1:] for t in tokens if _SHORT_FLAG.match(t)]
    has_count = "--count" in tokens or any("c" in f for f in short_flags)
    if not has_count:
        return None
    has_recursive = (
        "--recursive" in tokens
        or any(t.startswith("--directories") for t in tokens)
        or any("r" in f or "R" in f for f in short_flags)
    )
    if not has_recursive:
        positional = [
            t for t in tokens if not t.startswith("-") and t not in _SHELL_BREAK
        ]
        has_recursive = any(
            os.path.isdir(t if os.path.isabs(t) else os.path.join(cwd or ".", t))
            for t in positional[1:]
        )
    if not has_recursive:
        return None
    return (
        "[grep] `grep -c` over a directory prints a line per file it "
        "touched — every zero-count included. `grep -rl` (or `rg -l`) "
        "prints only the files that matched."
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
        try:
            reason = _precheck_deny(command, cwd)
        except Exception:
            reason = None  # fail open: a lexer bug must never block a command
        if reason is not None:
            out = {"permissionDecision": "deny", "permissionDecisionReason": reason}
    if out is None:
        note = (
            _rule_e(command)
            or _rule_f(command, cwd)
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
