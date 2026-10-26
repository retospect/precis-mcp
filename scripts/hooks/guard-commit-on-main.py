#!/usr/bin/env -S uv run --no-project --python >=3.12 python
"""PreToolUse hook: block ``git commit`` when the target branch is ``main``.

The footgun this closes: a worktree session's cwd drifts into the PRIMARY
checkout (or the command ``cd``s there), a ``git commit`` runs, and the work
lands **directly on ``main``** — bypassing the worktree AND the ``scripts/ship``
integration gate (ruff/mypy/pytest). ``scripts/ship`` refuses to run on
``main``, but only at *ship* time; by then the commits already exist and have
to be surgically moved back onto a feature branch. This denies the commit
up-front instead, mirroring ``guard-worktree-path.py`` (remove the *ability* to
do the wrong thing).

Scope, deliberately narrow to stay false-positive-free:
- Only ``git commit`` (incl. ``--amend``) as a whitespace-bounded token —
  a path that merely contains it (``scratch/commit-msg.txt``, gr515527),
  ``--no-commit`` or ``HEAD^{commit}`` is not the subcommand. NOT
  ``git merge`` (a ``git merge --ff-only origin/main`` to sync the primary
  is legitimate), NOT ``commit-tree`` (``scripts/ship``'s plumbing), NOT
  cherry-pick/revert.
- Only when the resolved branch is ``main``/``master``. Commits on any feature
  / ``worktree-*`` / ``docs-*`` branch pass untouched.
- Only in a repo that actually has the ``scripts/ship`` gate this protects.
  A sibling repo without it (e.g. ``autocatpath``, which legitimately ships
  direct-to-main + ``git push``) has no worktree/ship flow to bypass, so a
  commit on its ``main`` is not this footgun — it passes untouched.
- Follows a leading ``cd <path>`` and ``git -C <path>`` so the branch is read
  at the dir the commit actually targets, not just the session cwd.

Second, narrower job (gr514943): a ``git commit`` wrapped in a heredoc or
``$(…)`` substitution — ``git commit -m "$(cat <<'EOF' …)"``, the shape the
harness's own commit guidance shows — is killed at parse time by the
harness's worktree-isolation guard with a bare syntax error that names no
guard and no fix. This hook fires first (PreToolUse runs ahead of that
check) and denies the same call with a message that names the guard and
the ``-F`` alternative, so the refusal carries the fix instead of a parse
error. The denial text is the only thing the repo can improve — the guard
itself is a harness built-in (docs/conventions/container-ops.md). The
wrapped check is scoped to the commit itself: a plain ``git commit`` in
its own segment after an unrelated heredoc or ``$(…)`` elsewhere in the
call is parseable and passes (gr515648).

Third job (gr515109): a ``git commit`` in the gated repo whose message is
inspectable — ``-m`` strings, or an ``-F`` file that exists — must carry
the ``Co-Authored-By:`` trailer the repo's commit convention requires;
the first commit's missing footer otherwise goes unnoticed until a manual
amend. Messages git composes elsewhere are never blocked: no ``-m``/``-F``
(editor path), ``-F -`` (stdin), ``--reuse-message``/``-C``/``--no-edit``
(the message is reused), or an ``-F`` path that can't be read.

Escape hatch: set ``ALLOW_COMMIT_ON_MAIN=1`` in the environment for the rare
legitimate direct-to-main commit.

Wired in ``.claude/settings.json`` (PreToolUse, matcher ``Bash``). See the
``worktree_edit_path_trap`` memory and CLAUDE.md's ship workflow.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys

PROTECTED = {"main", "master"}

# A ``git … commit`` invocation where ``commit`` is a whitespace-bounded
# token — ``scratch/commit-msg.txt`` in a ``git check-ignore`` path,
# ``--no-commit`` on a merge or ``HEAD^{commit}`` is not the subcommand
# (gr515527).
_COMMIT_RE = re.compile(r"\bgit\b[^\n;&|]*?(?<!\S)commit(?!\S)")
# ``git -C <path>`` inside a single segment.
_GIT_C_RE = re.compile(r"\bgit\b\s+(?:-c\s+\S+\s+)*-C\s+(['\"]?)([^'\"\s]+)\1")
# A segment that is *only* a ``cd <path>``.
_CD_RE = re.compile(r"""^\s*cd\s+(['\"]?)([^'\"]+)\1\s*$""")

#: A command substitution ``$(`` or a heredoc opener (``<<EOF``, ``<<-'X'``)
#: — constructs the worktree guard cannot statically parse, so it fails
#: closed with a bare syntax error (gr456287, gr514943). A left-shift such
#: as ``1<<2`` does not match: the delimiter needs a word char or quote.
_UNPARSEABLE_RE = re.compile(r"\$\(|<<-?\s*['\"]?[A-Za-z_]")
#: A heredoc opener carrying its delimiter tag, for tracking body lines.
_HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


def _wrapped_commit(command: str) -> bool:
    """A ``git commit`` tangled with a heredoc or ``$(…)`` — the shape the
    worktree guard kills at parse time (gr514943).

    Only a commit the guard actually cannot parse counts: a ``git commit``
    inside a heredoc BODY (the guard refuses any heredoc whose text names
    git), or one sharing a segment with a ``$(``/``<<``. A plain commit in
    its own segment after an unrelated heredoc — ``python3 <<'EOF' …``
    then ``git commit -F msg`` — is statically parseable and runs
    (gr515648).
    """
    pending: list[str] = []
    for line in command.split("\n"):
        if pending:
            if line.strip() == pending[0]:
                pending.pop(0)
            elif _COMMIT_RE.search(line):
                return True
            continue
        pending.extend(m.group(2) for m in _HEREDOC.finditer(line))
        for seg in re.split(r"&&|\|\||;", line):
            if _COMMIT_RE.search(seg) and _UNPARSEABLE_RE.search(seg):
                return True
    return False


def _branch(cwd: str) -> str:
    """Current branch at ``cwd``; empty string on any failure / detached."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _repo_root(cwd: str) -> str:
    """Toplevel of the git repo at ``cwd``; empty string on any failure."""
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        )
        return out.stdout.strip()
    except Exception:
        return ""


def _resolve(base: str, path: str) -> str:
    path = os.path.expanduser(
        path.strip()
    )  # strip: a `cd /x && …` split leaves a trailing space
    return path if os.path.isabs(path) else os.path.normpath(os.path.join(base, path))


#: The trailer every commit message in this repo must carry (gr515109).
_FOOTER_RE = re.compile(r"^Co-Authored-By:\s*\S", re.M | re.I)

#: ``git commit`` flags after which the message is composed elsewhere —
#: never inspected, never blocked.
_UNINSPECTABLE = (
    "--no-edit",
    "--allow-empty-message",
    "-C",
    "-c",
    "-t",
    "--reuse-message",
    "--reedit-message",
    "--template",
    "--squash",
    "--fixup",
)
_UNINSPECTABLE_EQ = _UNINSPECTABLE[3:]


def _commit_message(seg: str, cwd: str) -> str | None:
    """The message a ``git commit`` carries, when it is inspectable.

    ``-m`` strings join with blank lines; ``-F <file>`` is read relative to
    the commit's target dir. ``None`` when git would get the message a way
    this hook cannot see — the editor path (no ``-m``/``-F``), ``-F -``
    (stdin), ``-C``/``--reuse-message``/``--no-edit`` (reused), or an
    ``-F`` path that does not resolve to a readable file.
    """
    try:
        argv = shlex.split(seg)
    except ValueError:
        return None
    if "commit" not in argv:
        return None
    rest = argv[argv.index("commit") + 1 :]
    msgs: list[str] = []
    file_arg: str | None = None
    i = 0
    while i < len(rest):
        a = rest[i]
        if a in _UNINSPECTABLE or any(a.startswith(p + "=") for p in _UNINSPECTABLE_EQ):
            return None
        if a in ("-m", "--message", "-F", "--file"):
            i += 1
            if i >= len(rest):
                break
            if a in ("-m", "--message"):
                msgs.append(rest[i])
            else:
                file_arg = rest[i]
        elif a == "--trailer":
            i += 1
            if i < len(rest):
                msgs.append(rest[i])
        elif a.startswith(("--trailer=", "--message=")):
            msgs.append(a.partition("=")[2])
        elif a.startswith("--file="):
            file_arg = a.partition("=")[2]
        elif a.startswith("-") and not a.startswith("--") and len(a) > 1:
            # Bundled short flags: -m/-F take a value (glued or next arg);
            # -am "msg" is -a -m "msg".
            for j, ch in enumerate(a[1:]):
                if ch in "mF":
                    val = a[j + 2 :]
                    if not val:
                        i += 1
                        if i < len(rest):
                            val = rest[i]
                        else:
                            val = ""
                    if ch == "m":
                        msgs.append(val)
                    else:
                        file_arg = val
                    break
        i += 1
    if file_arg is not None:
        if file_arg == "-":
            return None
        path = file_arg if os.path.isabs(file_arg) else os.path.join(cwd, file_arg)
        try:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    if msgs:
        return "\n\n".join(msgs)
    return None


def evaluate(command: str, cwd: str) -> str | None:
    """Return a deny reason, or ``None`` to allow. Pure & testable.

    Walks the command's ``&&`` / ``;`` / newline segments left-to-right,
    tracking the effective directory (updated by ``cd``), and denies the first
    ``git commit`` whose resolved branch is protected.
    """
    if not isinstance(command, str) or "commit" not in command:
        return None

    # A commit invocation tangled with a heredoc/`$(…)` is refused by the
    # worktree guard anyway; deny it here with a reason that names the
    # guard and the fix instead of the guard's bare parse error. Scoped to
    # the commit's own segment/body — an unrelated heredoc or substitution
    # elsewhere in the call does not wrap it (gr515648).
    if _wrapped_commit(command):
        return (
            "Refusing `git commit` wrapped in a heredoc or $(…) substitution "
            "— the worktree guard cannot statically prove this shape keeps "
            "git in the worktree, so it would kill the call at parse time "
            "with a bare syntax error that names no fix. Write the message "
            "to a file and run `git commit -F <file>`; a plain literal "
            '`git commit -m "…"` also passes.'
        )

    cur = cwd
    for seg in re.split(r"&&|\|\||;|\n", command):
        cd = _CD_RE.match(seg)
        if cd:
            cur = _resolve(cur, cd.group(2))
            continue
        if not _COMMIT_RE.search(seg):
            continue
        # This segment commits. Read the branch at its target dir.
        gc = _GIT_C_RE.search(seg)
        target = _resolve(cur, gc.group(2)) if gc else cur
        # Only guard a repo that has the scripts/ship gate this protects;
        # a sibling repo without it (autocatpath) ships direct-to-main by design.
        root = _repo_root(target)
        if not root or not os.path.isfile(os.path.join(root, "scripts", "ship")):
            continue
        branch = _branch(target)
        if branch in PROTECTED:
            return (
                f"Refusing `git commit` on `{branch}` (at {target}). That lands "
                "work directly on the primary branch, bypassing the worktree "
                "and the scripts/ship gate. Commit inside a `claude -w` "
                "worktree on a feature branch and let /land (scripts/ship) "
                "merge it. If this really is intended, set ALLOW_COMMIT_ON_MAIN=1."
            )
        # gr515109: the repo convention puts a Co-Authored-By trailer on
        # every commit; nothing caught its absence until a manual amend.
        # Deny only messages this hook can fully read — git-composed ones
        # (editor, stdin, reused) pass untouched.
        msg = _commit_message(seg, target)
        if msg is not None and not _FOOTER_RE.search(msg):
            return (
                "Refusing `git commit` — the message carries no "
                "`Co-Authored-By:` trailer, which this repo requires on "
                "every commit (CLAUDE.md §Conventions that bite; ship "
                "strips it back into the squash footer). Add it to the -m "
                "text or the -F message file, e.g. "
                "`Co-Authored-By: <agent> <noreply@…>`."
            )
    return None


def main() -> int:
    if os.environ.get("ALLOW_COMMIT_ON_MAIN"):
        return 0
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # unparseable → never block
    tool_input = payload.get("tool_input") or {}
    command = tool_input.get("command", "")
    cwd = payload.get("cwd") or os.getcwd()

    reason = evaluate(command, cwd)
    if reason is None:
        return 0
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
