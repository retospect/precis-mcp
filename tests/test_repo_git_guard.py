"""Repo-`git` test calls must declare it — ``needs_repo_git`` or a fallback.

gr581556: the ship gate's containers cannot run git against the checkout —
the warm precis-gate gets ``git archive HEAD`` (no ``.git`` at all) and the
bind fallback mounts a worktree whose ``.git`` file names a gitdir outside
the mount — so ``git ls-files``/``git rev-parse`` exits 128 there while CI
(a real checkout) stays green. A selected test that shells git at the repo
reddened every ``scripts/ship --checked`` on 2026-11-07 (7 failures across
three files); the gate never saw it. This walk is the guard the checked
selection carries against the next one: marked tests skip where
``rev-parse --git-dir`` fails at the repo root (``tests/_repo_git.py``)
and still run in CI/dev.

A flagged call is a ``subprocess.*`` spawn whose argv[0] is a literal
``"git"`` and either

- names the repo root in ``-C``/``--git-dir``/``--work-tree``/``cwd=``
  (``REPO``/``ROOT``/``_REPO_ROOT``/… or a ``Path(__file__)`` /
  ``Path.cwd()`` expression), or
- has neither and so inherits the process cwd — the repo root in every
  pytest lane — with a repo-needed subcommand in a module that never
  chdirs, or
- hides the argv behind a splat (``["git", *args]``) with no ``cwd=`` —
  the target cannot be proven.

``git init``/``clone`` with an explicit path, a stub ``git`` on PATH, and
calls whose ``-C``/``cwd`` is a ``tmp_path`` variable are all fine — the
container has the git binary, just not this checkout's metadata. Known
blind spots (same as the posix guard's): argv in a variable, a string
``shell=True`` command, and chdir'd modules are not resolved.

Failure means: declare the test — ``from tests._repo_git import
needs_repo_git`` on the test or a module ``pytestmark`` — or give the call
a no-checkout fallback (``tests/test_ruff_scripts_excludes.py`` and
``scripts/ci-shard-plan``'s walks are the shape), or vet + ``_EXEMPT`` it
here with the reason.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from tests._policy_scan import walk_files
from tests._repo_git import needs_repo_git, repo_git_visible

_ROOT = Path(__file__).resolve().parents[1]

#: Modules a human vetted: every git call is either tmp-targeted or carries
#: its own no-checkout path.
_EXEMPT: set[str] = {
    # `_git(*args)` splat helper; every caller passes `-C`/an explicit tmp
    # path, so nothing inherits the checkout cwd.
    "tests/test_autocatpath_wheel.py",
    # `ls-files` under a `.git` existence check, else the .gitignore walk.
    "tests/test_lanes_deny.py",
    # Same probe + walk pair; the "git present but refuses" leg fails loudly
    # on purpose — pinned by its own test.
    "tests/test_no_glossary_file.py",
    # `ls-files --cached --others` falls back to the src/ walk.
    "tests/test_pcb_dead_exports.py",
    # `git ls-files` returns None → the .gitignore-aware filesystem walk.
    "tests/test_ruff_scripts_excludes.py",
}

_SPAWN = {"run", "Popen", "check_output", "check_call", "call"}

#: Names tests bind to the checkout root — ``str()``/``Path()`` wrappers are
#: unwrapped before comparison. Lowercase ``repo``/``root`` are tmp repos.
_REPO_NAMES = {
    "REPO",
    "ROOT",
    "REPO_ROOT",
    "_REPO_ROOT",
    "_ROOT",
    "REPO_DIR",
    "PROJECT_ROOT",
    "GIT_ROOT",
    "CHECKOUT",
    "MAIN_ROOT",
    "REPO_UNDER_TEST",
}

#: Subcommands that read or write the repository they run in — an inherited
#: cwd makes the checkout the target.
_REPO_SUBS = {
    "add",
    "archive",
    "blame",
    "branch",
    "cat-file",
    "check-ignore",
    "checkout",
    "cherry-pick",
    "commit",
    "commit-tree",
    "config",
    "count-objects",
    "describe",
    "diff",
    "diff-index",
    "diff-tree",
    "fetch",
    "for-each-ref",
    "log",
    "ls-files",
    "ls-tree",
    "merge",
    "merge-base",
    "merge-tree",
    "mv",
    "name-rev",
    "pull",
    "push",
    "rebase",
    "reflog",
    "remote",
    "reset",
    "restore",
    "rev-list",
    "rev-parse",
    "rm",
    "shortlog",
    "show",
    "show-ref",
    "stash",
    "status",
    "switch",
    "symbolic-ref",
    "tag",
    "update-ref",
    "worktree",
    "write-tree",
}

#: ``init``/``clone`` are repo-touching only when bare — an explicit path
#: targets the path, not the cwd.
_CREATE_SUBS = {"init", "clone"}

#: Leading options that consume a value (the value may be a path/name, not
#: the subcommand). ``-C``/``--git-dir``/``--work-tree`` also pick the repo.
_VALUE_OPTS = {
    "-C",
    "--git-dir",
    "--work-tree",
    "-c",
    "--config-env",
    "--namespace",
    "--exec-path",
}

#: Value-taking options of init/clone, so ``-b main`` doesn't count as the
#: positional that would prove an explicit target.
_CREATE_VALUE_OPTS = {
    "-b",
    "--branch",
    "--template",
    "--separate-git-dir",
    "-o",
    "--origin",
    "-u",
    "--upload-pack",
    "--depth",
    "--jobs",
    "-j",
    "-c",
    "--config",
    "--reference",
    "--shared",
}


def _argv_info(
    elts: list[ast.expr],
) -> tuple[str | None, list[ast.expr], int, bool]:
    """Parse a literal argv (after the ``"git"`` head).

    Returns ``(subcommand, repo-target exprs, positional count, splat)`` —
    ``splat`` only when a non-literal element hides what the subcommand is;
    a non-literal AFTER the subcommand (``str(tmp_path)``, ``*args``) is a
    positional like any other. Options that take a value consume the next
    element; ``-C``/``--git-dir``/``--work-tree`` values land in the
    repo-target list, ``--git-dir=…`` inline values too.
    """
    targets: list[ast.expr] = []
    positional = 0
    sub: str | None = None
    splat = False
    i = 0
    while i < len(elts):
        e = elts[i]
        if not isinstance(e, ast.Constant) or not isinstance(e.value, str):
            if sub is None:
                splat = True
            else:
                positional += 1
            i += 1
            continue
        v = e.value
        if v in _VALUE_OPTS:
            if v in ("-C", "--git-dir", "--work-tree") and i + 1 < len(elts):
                targets.append(elts[i + 1])
            i += 2
            continue
        if v.startswith("--git-dir=") or v.startswith("--work-tree="):
            targets.append(e)
            i += 1
            continue
        if v in _CREATE_VALUE_OPTS and sub in _CREATE_SUBS:
            i += 2
            continue
        if v.startswith("-"):
            i += 1
            continue
        if sub is None:
            sub = v
        else:
            positional += 1
        i += 1
    return sub, targets, positional, splat


def _is_repo_name(node: ast.expr) -> bool:
    """The expression provably names the checkout root."""
    s = ast.unparse(node)
    if "__file__" in s or "Path.cwd()" in s or "os.getcwd()" in s:
        return True
    return any(
        (isinstance(n, ast.Name) and n.id in _REPO_NAMES)
        or (isinstance(n, ast.Attribute) and n.attr in _REPO_NAMES)
        for n in ast.walk(node)
    )


def _git_spawns(tree: ast.AST, from_subprocess: set[str]) -> list[ast.Call]:
    """``subprocess.*`` calls whose argv[0] is a literal ``"git"``."""
    out = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        is_spawn = (
            isinstance(func, ast.Attribute)
            and func.attr in _SPAWN
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        ) or (
            isinstance(func, ast.Name)
            and func.id in _SPAWN
            and func.id in from_subprocess
        )
        if not is_spawn:
            continue
        argv = node.args[0]
        if (
            isinstance(argv, (ast.List, ast.Tuple))
            and argv.elts
            and isinstance(argv.elts[0], ast.Constant)
            and argv.elts[0].value == "git"
        ):
            out.append(node)
    return out


def _flag_reason(call: ast.Call, chdir_free: bool) -> str | None:
    """Why this literal-argv git spawn is repo-targeting, or ``None``."""
    argv = call.args[0]
    assert isinstance(argv, (ast.List, ast.Tuple))
    sub, targets, positional, splat = _argv_info(list(argv.elts[1:]))
    if any(t is not None and _is_repo_name(t) for t in targets):
        return "names the repo root in -C/--git-dir/--work-tree"
    cwd_kw = next((k for k in call.keywords if k.arg == "cwd"), None)
    if cwd_kw is not None:
        if _is_repo_name(cwd_kw.value):
            return "names the repo root in cwd="
        return None  # cwd targets something else — a tmp repo, presumably
    if targets:
        return None  # -C/--git-dir targets a non-repo expression
    if splat:
        return "argv hides the subcommand behind a splat — target unprovable"
    if sub in _REPO_SUBS and chdir_free:
        return f"inherits the checkout cwd for `git {sub}`"
    if sub in _CREATE_SUBS and positional == 0 and chdir_free:
        return f"bare `git {sub}` would touch the checkout at cwd"
    return None


def _reason(argv_and_kwargs: str, chdir_free: bool = True) -> str | None:
    tree = ast.parse(f"subprocess.run({argv_and_kwargs})")
    call = next(n for n in ast.walk(tree) if isinstance(n, ast.Call))
    return _flag_reason(call, chdir_free)


@pytest.mark.parametrize(
    ("call", "chdir_free", "flagged"),
    [
        # the gr581556 shape: inherits the pytest cwd = the checkout root
        ('["git", "rev-parse", "HEAD"]', True, True),
        ('["git", "ls-files", "-z", "--", "tests"]', True, True),
        ('["git", "status", "--porcelain"]', True, True),
        ('["git", "merge-base", "a", "b"]', True, True),
        # the same call is tmp-targeted in a module that chdirs
        ('["git", "rev-parse", "--verify", "HEAD"]', False, False),
        # explicitly named repo roots, either door
        ('["git", "-C", str(_REPO_ROOT), "ls-files", "-z"]', True, True),
        ('["git", "ls-files"], cwd=REPO', True, True),
        ('["git", "ls-files"], cwd=_ROOT / "x"', True, True),
        ('["git", "status"], cwd=Path(__file__).parents[1]', True, True),
        ('["git", "status"], cwd=Path.cwd()', True, True),
        # tmp-targeted calls are the container-fine kind
        ('["git", "-C", str(repo), "rev-parse", "HEAD"]', True, False),
        ('["git", "-C", str(repo), *args]', True, False),
        ('["git", "rev-parse", "HEAD"], cwd=repo', True, False),
        ('["git", "init", "-q", str(tmp_path)]', True, False),
        ('["git", "clone", "-q", str(origin), str(work)]', True, False),
        ('["git", "init", "-q", "-b", "main", str(repo)]', True, False),
        (
            '["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo',
            True,
            False,
        ),
        # a splat before the subcommand can't prove its target
        ('["git", *args]', True, True),
        # bare init/clone touch the cwd; no-repo subcommands never do
        ('["git", "init"]', True, True),
        ('["git", "init", "-q"]', True, True),
        ('["git", "init", "-q", "-b", "main"]', True, True),
        ('["git", "--version"]', True, False),
        ('["git", "credential", "fill"]', True, False),
    ],
)
def test_flag_reason_classifies_argv_shapes(
    call: str, chdir_free: bool, flagged: bool
) -> None:
    """The classifier's own truth table — a ratchet that silently widens or
    narrows stops teaching."""
    got = _reason(call, chdir_free)
    if flagged:
        assert got is not None
    else:
        assert got is None


def test_repo_git_spawns_are_marked_or_resilient() -> None:
    bad: list[str] = []
    for path in walk_files(_ROOT / "tests", "test_*.py"):
        rel = path.relative_to(_ROOT).as_posix()
        if rel in _EXEMPT:
            continue
        source = path.read_text(encoding="utf-8")
        if "needs_repo_git" in source:
            continue
        tree = ast.parse(source)
        from_subprocess = {
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "subprocess"
            for alias in node.names
        }
        # `monkeypatch.chdir`/`os.chdir` anywhere means an inheriting call may
        # still be tmp-targeted — the module opted into cwd semantics.
        chdir_free = "chdir" not in source
        for call in _git_spawns(tree, from_subprocess):
            why = _flag_reason(call, chdir_free)
            if why:
                bad.append(f"{rel}:{call.lineno}: {why}")
    assert not bad, (
        f"{len(bad)} git spawn(s) target this checkout and nothing declares "
        "it — the gate container has no .git, so the test reddens every "
        "scripts/ship --checked land it is selected for while CI stays green "
        "(gr581556). Mark the test (from tests._repo_git import "
        "needs_repo_git), give the call a no-checkout fallback, or vet it "
        "into _EXEMPT with the reason:\n" + "\n".join(bad)
    )


@needs_repo_git
def test_git_rev_parse_head_runs_where_the_checkout_is_real() -> None:
    """gr581556's acceptance shape: a test that shells ``git rev-parse HEAD``
    must not redden ``scripts/ship --checked`` — the fixture skips it where
    git cannot see the checkout; on a real checkout it runs and answers a
    full sha."""
    out = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert re.fullmatch(r"[0-9a-f]{40}", out)


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_repo_git_visible_tracks_the_gitdir(tmp_path: Path) -> None:
    """The probe is the gitdir's answer, not the binary's: an un-inited dir
    is invisible, ``git init`` makes it visible."""
    assert repo_git_visible(tmp_path) is False
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    assert repo_git_visible(tmp_path) is True
